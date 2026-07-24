"""Starts one MPC job on the persistent coordinator/party EC2 instances via
SSM Run Command, then returns once every node's container has actually
started (not finished - see check_job_status for that).

Invoked once per queued job by the state machine's first LambdaInvoke. Every
node's address (instance ID, private IP, public/Elastic IP) is known
statically from CDK outputs baked into this function's environment - unlike
the Fargate deployment's orchestrator, there's no ENI lookup or Cloud Map
registration/DNS-propagation wait here, which is what makes this fast enough
to run as a short Lambda instead of a long-lived task.
"""
import os
import time
from datetime import datetime, timezone

import boto3

ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")

JOBS_TABLE_NAME = os.environ["JOBS_TABLE_NAME"]
PROGRAM_S3_BUCKET = os.environ["PROGRAM_S3_BUCKET"]
LOG_GROUP_NAME = os.environ["LOG_GROUP_NAME"]
CONTAINER_NAME = os.environ["CONTAINER_NAME"]
REGION = os.environ["AWS_REGION"]

COORD_INSTANCE_ID = os.environ["COORD_INSTANCE_ID"]
COORD_PRIVATE_IP = os.environ["COORD_PRIVATE_IP"]
COORD_PUBLIC_IP = os.environ["COORD_PUBLIC_IP"]
COORD_IMAGE_URI = os.environ["COORD_IMAGE_URI"]

PARTY_INSTANCE_IDS = os.environ["PARTY_INSTANCE_IDS"].split(",")
PARTY_PRIVATE_IPS = os.environ["PARTY_PRIVATE_IPS"].split(",")
PARTY_PUBLIC_IPS = os.environ["PARTY_PUBLIC_IPS"].split(",")
PARTY_IMAGE_URI = os.environ["PARTY_IMAGE_URI"]
DEPLOYED_PARTIES = len(PARTY_INSTANCE_IDS)

PROGRAM_MOUNT = "/app/programs"

jobs_table = dynamodb.Table(JOBS_TABLE_NAME)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def update_job(job_id, status=None, **fields):
    expr_names = {}
    expr_values = {":updated_at": now_iso()}
    set_parts = ["updated_at = :updated_at"]
    if status is not None:
        expr_names["#status"] = "status"
        expr_values[":status"] = status
        set_parts.append("#status = :status")
    for k, v in fields.items():
        expr_names[f"#{k}"] = k
        expr_values[f":{k}"] = v
        set_parts.append(f"#{k} = :{k}")
    jobs_table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def fail(job_id, message):
    update_job(job_id, status="FAILED", error=message, finished_at=now_iso())
    raise RuntimeError(message)


def send_ssm(instance_id, script):
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script]},
    )
    return resp["Command"]["CommandId"]


def wait_all_ssm(pending, timeout_s=90, interval_s=1):
    """pending: dict[label] -> (instance_id, command_id). Polls all of them
    concurrently (bounded by the slowest single command, not the sum) until
    every one reaches Success, or raises on the first failure/timeout."""
    deadline = time.time() + timeout_s
    remaining = dict(pending)
    time.sleep(interval_s)  # give SSM a moment to register the commands
    while remaining:
        if time.time() >= deadline:
            raise RuntimeError(f"SSM commands for {sorted(remaining)} did not finish within {timeout_s}s")
        for label, (instance_id, command_id) in list(remaining.items()):
            try:
                resp = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            except ssm.exceptions.InvocationDoesNotExist:
                continue
            status = resp["Status"]
            if status == "Success":
                del remaining[label]
            elif status in ("Cancelled", "TimedOut", "Failed", "Cancelling"):
                raise RuntimeError(
                    f"{label} failed to start ({status}): "
                    f"{resp.get('StandardErrorContent', '')[-1000:]}"
                )
        if remaining:
            time.sleep(interval_s)


def ping_snippet(self_name, addr_book):
    parts = []
    for name, ip in addr_book:
        if name == self_name:
            continue
        parts.append(f'echo "Pinging {name}..."; ping -c 4 {ip} || true;')
    return " ".join(parts)


def build_coord_script(node_certs, num_parties, threshold, n_inputs, backend, stream, pings):
    run_coord_args = (
        "--addr 0.0.0.0 "
        "--hash 0000000000000000000000000000000000000000000000000000000000000000 "
        "--server-cert /app/ids/pub/coord.crt --server-key /app/ids/priv/coord.der "
        f"--n {num_parties} --t {threshold} --n-inputs {n_inputs or 0} --backend {backend} "
        "--output-clients /app/ids/pub/clients/client0.crt,/app/ids/pub/clients/client1.crt "
        f"--initial-mpc-nodes {node_certs}"
    )
    entry = f'/app/run-coord {run_coord_args}; EXIT=$?; {pings} exit $EXIT'
    # No `docker login`/`docker pull` here on purpose: the image was already
    # pulled into every instance's local cache at boot (see
    # _base_user_data in app.py), and `docker run` against an already-cached
    # local tag needs neither - skipping both is what keeps this script's
    # SSM round trip on the order of a second. The operator-facing
    # run-nodes script does re-pull, since picking up a freshly redeployed
    # image matters more there than shaving latency off it.
    return "\n".join([
        "set -e",
        f"docker rm -f {CONTAINER_NAME} >/dev/null 2>&1 || true",
        f"docker run -d --name {CONTAINER_NAME} --network host --log-driver awslogs "
        f"--log-opt awslogs-region={REGION} --log-opt awslogs-group={LOG_GROUP_NAME} --log-opt awslogs-stream={stream} "
        f"--env-file /etc/stoffel-env "
        f"-e STOFFEL_N_PARTIES=\"{num_parties}\" "
        f"--entrypoint /bin/bash {COORD_IMAGE_URI} -c '{entry}'",
    ])


def build_party_script(
    party_id, bind_port, rpc_port, program_s3_key, program_basename, program_path,
    n_inputs, backend, curve, num_parties, threshold, coord_addr, bootstrap_addr,
    stream, pings,
):
    env = {
        "STOFFEL_N_PARTIES": str(num_parties),
        "STOFFEL_THRESHOLD": str(threshold),
        "STOFFEL_ENTRY": "main",
        "RUST_LOG": "info",
        "RUST_BACKTRACE": "1",
        "STOFFEL_SKIP_HOST_WAIT": "true",
        "STOFFEL_ROLE": "leader" if party_id == 0 else "party",
        "STOFFEL_PARTY_ID": str(party_id),
        "STOFFEL_BIND_ADDR": f"0.0.0.0:{bind_port}",
        "STOFFEL_RPC_ADDR": f"0.0.0.0:{rpc_port}",
        "STOFFEL_CERT": f"/app/ids/pub/nodes/node{party_id}.crt",
        "STOFFEL_KEY": f"/app/ids/priv/nodes/node{party_id}.der",
        "STOFFEL_PROGRAM": program_path,
        "STOFFEL_MPC_BACKEND": backend,
        "STOFFEL_MPC_CURVE": curve,
        "STOFFEL_COORD_ADDR": coord_addr,
    }
    if n_inputs:
        env["STOFFEL_CLIENT_INPUT_TOTAL"] = n_inputs
    if bootstrap_addr:
        env["STOFFEL_BOOTSTRAP_ADDR"] = bootstrap_addr
    env_flags = " ".join(f'-e {k}="{v}"' for k, v in env.items())

    entry = (
        f'/app/entrypoint.sh; EXIT=$?; {pings} '
        'MEM=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null'
        ' || cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes 2>/dev/null'
        ' || cat /sys/fs/cgroup/memory.current 2>/dev/null);'
        ' [ -n "$MEM" ] && echo "PEAK_MEM_BYTES: $MEM";'
        ' exit $EXIT'
    )

    return "\n".join([
        "set -e",
        "mkdir -p /home/ec2-user/programs",
        f"aws s3 cp s3://{PROGRAM_S3_BUCKET}/{program_s3_key} /home/ec2-user/programs/{program_basename}",
        # Identity cert/key are no longer baked into the party image (see
        # ../StoffelVM/Dockerfile.benchmark-flexible) - fetch this party's
        # own node cert/key fresh from S3 on every job start, same as the
        # program above, then bind-mount the local ids/ tree into the
        # container at /app/ids to match STOFFEL_CERT/STOFFEL_KEY below.
        # Fetching per job (not once at instance boot) means a job started
        # anytime after the operator's `aws s3 sync ids/ s3://<bucket>/ids/`
        # (see app.py's IdsS3Uri output) always picks up current certs,
        # regardless of when this instance itself booted.
        "mkdir -p /home/ec2-user/ids/pub/nodes /home/ec2-user/ids/priv/nodes",
        f"aws s3 cp s3://{PROGRAM_S3_BUCKET}/ids/pub/nodes/node{party_id}.crt "
        f"/home/ec2-user/ids/pub/nodes/node{party_id}.crt",
        f"aws s3 cp s3://{PROGRAM_S3_BUCKET}/ids/priv/nodes/node{party_id}.der "
        f"/home/ec2-user/ids/priv/nodes/node{party_id}.der",
        f"docker rm -f {CONTAINER_NAME} >/dev/null 2>&1 || true",
        f"docker run -d --name {CONTAINER_NAME} --network host --log-driver awslogs "
        f"--log-opt awslogs-region={REGION} --log-opt awslogs-group={LOG_GROUP_NAME} --log-opt awslogs-stream={stream} "
        f"-v /home/ec2-user/programs:/app/programs:ro -v /home/ec2-user/ids:/app/ids:ro "
        f"--env-file /etc/stoffel-env {env_flags} "
        f"--entrypoint /bin/bash {PARTY_IMAGE_URI} -c '{entry}'",
    ])


def handler(event, context):
    job = event["job"]
    job_id = job["job_id"]
    program_s3_key = job["program_s3_key"]
    num_parties = int(job.get("num_parties", 5))
    threshold = int(job.get("threshold", 1))
    backend = job.get("backend", "honeybadger")
    curve = job.get("curve", "bls12-381")
    n_inputs = job.get("n_inputs") or ""

    min_parties = 2 * threshold + 1
    if num_parties < min_parties or num_parties > DEPLOYED_PARTIES:
        fail(job_id, f"invalid num_parties={num_parties} for threshold={threshold} "
                      f"(need {min_parties}-{DEPLOYED_PARTIES})")

    update_job(job_id, status="RUNNING", started_at=now_iso())

    program_basename = os.path.basename(program_s3_key)
    program_path = f"{PROGRAM_MOUNT}/{program_basename}"
    node_certs = ",".join(f"/app/ids/pub/nodes/node{i}.crt" for i in range(num_parties))
    coord_addr = f"{COORD_PRIVATE_IP}:31415"
    bootstrap_addr = f"{PARTY_PRIVATE_IPS[0]}:9000"
    addr_book = [("coord", COORD_PRIVATE_IP)] + [
        (f"node{i}", PARTY_PRIVATE_IPS[i]) for i in range(num_parties)
    ]

    print(f"[{job_id}] starting coordinator + {num_parties} parties...", flush=True)

    pending = {}

    coord_stream = f"coordinator/{job_id}"
    coord_script = build_coord_script(node_certs, num_parties, threshold, n_inputs, backend,
                                       coord_stream, ping_snippet("coord", addr_book))
    pending["coordinator"] = (COORD_INSTANCE_ID, send_ssm(COORD_INSTANCE_ID, coord_script))

    log_streams = {"coordinator": coord_stream}
    endpoints = {"coordinator": f"{COORD_PUBLIC_IP}:31415"}

    for i in range(num_parties):
        stream = f"party{i}/{job_id}"
        script = build_party_script(
            i, 9000 + i, 16180 + i, program_s3_key, program_basename, program_path,
            n_inputs, backend, curve, num_parties, threshold, coord_addr,
            bootstrap_addr if i != 0 else "", stream, ping_snippet(f"node{i}", addr_book),
        )
        pending[f"party{i}"] = (PARTY_INSTANCE_IDS[i], send_ssm(PARTY_INSTANCE_IDS[i], script))
        log_streams[f"party{i}"] = stream
        endpoints[f"party{i}"] = f"{PARTY_PUBLIC_IPS[i]}:{16180 + i}"

    try:
        wait_all_ssm(pending, timeout_s=90)
    except Exception as e:
        fail(job_id, f"failed to start nodes: {e}")

    update_job(job_id, endpoints=endpoints, log_streams=log_streams)
    print(f"[{job_id}] all nodes started.", flush=True)

    return {"job_status": "RUNNING", "endpoints": endpoints}
