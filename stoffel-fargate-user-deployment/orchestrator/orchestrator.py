#!/usr/bin/env python3
"""Runs one MPC job end-to-end as a single Fargate task: launches the
coordinator + party tasks, waits for the run to actually finish, and reports
status to DynamoDB. Invoked once per queued job by Step Functions' ECS
RunTask.sync integration, so this process's own exit code is what that
integration (and therefore the job-lock release) waits on.

Ports today's operator-driven run-nodes script (see ../../stoffel-fargate-deployment/run-nodes)
to a boto3 script instead of a human at a terminal with the AWS CLI.
"""
import os
import socket
import time
import traceback
from datetime import datetime, timezone

import boto3

ecs = boto3.client("ecs")
sd = boto3.client("servicediscovery")
ec2c = boto3.client("ec2")
s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

CLUSTER_NAME = os.environ["CLUSTER_NAME"]
SECURITY_GROUP_ID = os.environ["SECURITY_GROUP_ID"]
SUBNET_IDS = os.environ["SUBNET_IDS"].split(",")
COORD_TASK_DEF_ARN = os.environ["COORD_TASK_DEF_ARN"]
COORD_CLOUDMAP_SERVICE_ID = os.environ["COORD_CLOUDMAP_SERVICE_ID"]
PARTY_TASK_DEF_ARNS = os.environ["PARTY_TASK_DEF_ARNS"].split(",")
PARTY_CLOUDMAP_SERVICE_IDS = os.environ["PARTY_CLOUDMAP_SERVICE_IDS"].split(",")
DEPLOYED_PARTIES = len(PARTY_TASK_DEF_ARNS)
PROGRAM_MOUNT = "/app/programs"
NAMESPACE = "stoffel-coord.local"
JOBS_TABLE_NAME = os.environ["JOBS_TABLE_NAME"]
PROGRAM_S3_BUCKET = os.environ["PROGRAM_S3_BUCKET"]

JOB_ID = os.environ["JOB_ID"]
PROGRAM_S3_KEY = os.environ["PROGRAM_S3_KEY"]
NUM_PARTIES = int(os.environ.get("NUM_PARTIES", "5"))
THRESHOLD = int(os.environ.get("THRESHOLD", "1"))
BACKEND = os.environ.get("BACKEND", "honeybadger")
CURVE = os.environ.get("CURVE", "bls12-381")
N_INPUTS = os.environ.get("N_INPUTS", "")

jobs_table = dynamodb.Table(JOBS_TABLE_NAME)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def update_job(status=None, **fields):
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
        Key={"job_id": JOB_ID},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def fail(message):
    update_job(status="FAILED", error=message, finished_at=now_iso())
    print(f"ERROR: {message}", flush=True)
    raise SystemExit(1)


def cleanup_stale_cloudmap(service_id):
    # Each run registers a new instance ID (derived from the task ARN), so
    # stale registrations from a previous failed run are never automatically
    # removed and can cause DNS to return a dead IP alongside the new one.
    instances = sd.list_instances(ServiceId=service_id).get("Instances", [])
    for inst in instances:
        sd.deregister_instance(ServiceId=service_id, InstanceId=inst["Id"])


def run_task(task_def_arn, overrides):
    resp = ecs.run_task(
        cluster=CLUSTER_NAME,
        launchType="FARGATE",
        taskDefinition=task_def_arn,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": SUBNET_IDS,
                "securityGroups": [SECURITY_GROUP_ID],
                "assignPublicIp": "ENABLED",
            }
        },
        overrides=overrides,
    )
    if not resp.get("tasks"):
        raise RuntimeError(f"failed to launch task {task_def_arn}: {resp.get('failures')}")
    return resp["tasks"][0]["taskArn"]


def stop_task(task_arn, reason="job complete"):
    try:
        ecs.stop_task(cluster=CLUSTER_NAME, task=task_arn, reason=reason)
    except Exception as e:
        print(f"WARNING: failed to stop task {task_arn}: {e}", flush=True)


def wait_running(task_arns, timeout_s=300):
    waiter = ecs.get_waiter("tasks_running")
    waiter.wait(cluster=CLUSTER_NAME, tasks=task_arns, WaiterConfig={"Delay": 5, "MaxAttempts": max(timeout_s // 5, 1)})


def wait_stopped(task_arns, timeout_s=3600):
    waiter = ecs.get_waiter("tasks_stopped")
    waiter.wait(cluster=CLUSTER_NAME, tasks=task_arns, WaiterConfig={"Delay": 10, "MaxAttempts": max(timeout_s // 10, 1)})


def describe(task_arns):
    return ecs.describe_tasks(cluster=CLUSTER_NAME, tasks=task_arns)["tasks"]


def task_ips(task_arn):
    details = describe([task_arn])[0]["attachments"][0]["details"]
    eni_id = next(d["value"] for d in details if d["name"] == "networkInterfaceId")
    private_ip = next(d["value"] for d in details if d["name"] == "privateIPv4Address")
    eni = ec2c.describe_network_interfaces(NetworkInterfaceIds=[eni_id])["NetworkInterfaces"][0]
    public_ip = eni.get("Association", {}).get("PublicIp")
    return private_ip, public_ip


def register_cloudmap(service_id, instance_id, private_ip):
    sd.register_instance(
        ServiceId=service_id,
        InstanceId=instance_id,
        Attributes={"AWS_INSTANCE_IPV4": private_ip},
    )


def instance_id_of(task_arn):
    return task_arn.rsplit("/", 1)[-1]


def wait_for_dns(hostname, timeout_s=30, interval_s=1):
    # Cloud Map register_instance succeeding doesn't mean the DNS record has
    # actually propagated yet - parties 1..N bootstrap via party0's hostname
    # immediately at container startup and fail hard (no retry) if it isn't
    # resolvable yet, so confirm resolution ourselves before launching them.
    deadline = time.time() + timeout_s
    last_error = None
    while time.time() < deadline:
        try:
            socket.getaddrinfo(hostname, None)
            return
        except socket.gaierror as e:
            last_error = e
            time.sleep(interval_s)
    raise RuntimeError(f"DNS never resolved for {hostname} within {timeout_s}s: {last_error}")


def download_program():
    local_path = f"/tmp/{os.path.basename(PROGRAM_S3_KEY)}"
    s3.download_file(PROGRAM_S3_BUCKET, PROGRAM_S3_KEY, local_path)
    dest = os.path.join(PROGRAM_MOUNT, os.path.basename(PROGRAM_S3_KEY))
    with open(local_path, "rb") as src, open(dest, "wb") as out:
        out.write(src.read())
    return dest


def main():
    update_job(status="RUNNING", started_at=now_iso())

    min_parties = 2 * THRESHOLD + 1
    if NUM_PARTIES < min_parties or NUM_PARTIES > DEPLOYED_PARTIES:
        fail(f"invalid num_parties={NUM_PARTIES} for threshold={THRESHOLD} "
             f"(need {min_parties}-{DEPLOYED_PARTIES})")

    program_path = download_program()

    cleanup_stale_cloudmap(COORD_CLOUDMAP_SERVICE_ID)
    for svc_id in PARTY_CLOUDMAP_SERVICE_IDS[:NUM_PARTIES]:
        cleanup_stale_cloudmap(svc_id)

    node_certs = ",".join(f"/app/ids/pub/nodes/node{i}.crt" for i in range(NUM_PARTIES))

    coord_command = [
        "--addr", "0.0.0.0",
        "--hash", "0000000000000000000000000000000000000000000000000000000000000000",
        "--server-cert", "/app/ids/pub/coord.crt",
        "--server-key", "/app/ids/priv/coord.der",
        "--n", str(NUM_PARTIES),
        "--t", str(THRESHOLD),
        "--n-inputs", N_INPUTS or "0",
        "--backend", BACKEND,
        "--output-clients", "/app/ids/pub/clients/client0.crt,/app/ids/pub/clients/client1.crt",
        "--initial-mpc-nodes", node_certs,
    ]
    coord_env = [
        {"name": "STOFFEL_MPC_BACKEND", "value": BACKEND},
        {"name": "STOFFEL_MPC_CURVE", "value": CURVE},
        {"name": "STOFFEL_THRESHOLD", "value": str(THRESHOLD)},
        {"name": "STOFFEL_N_PARTIES", "value": str(NUM_PARTIES)},
    ]

    print("Starting coordinator...", flush=True)
    coord_arn = run_task(
        COORD_TASK_DEF_ARN,
        {"containerOverrides": [{"name": "Container", "command": coord_command, "environment": coord_env}]},
    )
    # The coordinator (unlike party tasks) never exits on its own - it's a
    # long-running process, not a one-shot computation - so the orchestrator
    # owns its lifecycle for this job and must explicitly stop it once the
    # parties are done, in every case (success, failure, or exception here -
    # including failures below this point, like a DNS wait timeout), or it
    # leaks as a permanently-running (and billing) Fargate task.
    try:
        wait_running([coord_arn])
        coord_private_ip, coord_public_ip = task_ips(coord_arn)
        register_cloudmap(COORD_CLOUDMAP_SERVICE_ID, instance_id_of(coord_arn), coord_private_ip)
        print("Waiting for coordinator's Cloud Map DNS record to propagate...", flush=True)
        wait_for_dns(f"coordinator.{NAMESPACE}")

        party_env = [
            {"name": "STOFFEL_PROGRAM", "value": program_path},
            {"name": "STOFFEL_MPC_BACKEND", "value": BACKEND},
            {"name": "STOFFEL_MPC_CURVE", "value": CURVE},
            {"name": "STOFFEL_N_PARTIES", "value": str(NUM_PARTIES)},
            {"name": "STOFFEL_THRESHOLD", "value": str(THRESHOLD)},
        ]
        if N_INPUTS:
            party_env.append({"name": "STOFFEL_CLIENT_INPUT_TOTAL", "value": N_INPUTS})
        party_overrides = {"containerOverrides": [{"name": "Container", "environment": party_env}]}

        print("Starting party0...", flush=True)
        party0_arn = run_task(PARTY_TASK_DEF_ARNS[0], party_overrides)
        wait_running([party0_arn])
        party0_private_ip, party0_public_ip = task_ips(party0_arn)
        register_cloudmap(PARTY_CLOUDMAP_SERVICE_IDS[0], instance_id_of(party0_arn), party0_private_ip)
        print("Waiting for party0's Cloud Map DNS record to propagate...", flush=True)
        wait_for_dns(f"party0.{NAMESPACE}")

        party_arns = [party0_arn]
        endpoints = {"coordinator": f"{coord_public_ip}:31415", "party0": f"{party0_public_ip}:16180"}
        # Deterministic awslogs stream naming (<prefix>/Container/<task-id>)
        # recorded so /jobs/{job_id}/logs can fetch them later without the
        # caller needing AWS access of their own.
        log_streams = {
            "coordinator": f"coordinator/Container/{instance_id_of(coord_arn)}",
            "party0": f"party0/Container/{instance_id_of(party0_arn)}",
        }

        if NUM_PARTIES > 1:
            print(f"Starting parties 1-{NUM_PARTIES - 1} in parallel...", flush=True)
            remaining_arns = [run_task(PARTY_TASK_DEF_ARNS[i], party_overrides) for i in range(1, NUM_PARTIES)]
            wait_running(remaining_arns)
            for i, arn in zip(range(1, NUM_PARTIES), remaining_arns):
                private_ip, public_ip = task_ips(arn)
                register_cloudmap(PARTY_CLOUDMAP_SERVICE_IDS[i], instance_id_of(arn), private_ip)
                endpoints[f"party{i}"] = f"{public_ip}:{16180 + i}"
                log_streams[f"party{i}"] = f"party{i}/Container/{instance_id_of(arn)}"
                party_arns.append(arn)

        update_job(endpoints=endpoints, log_streams=log_streams)

        print("Nodes started, waiting for the parties to finish (not the coordinator, which never stops on its own)...", flush=True)
        wait_stopped(party_arns, timeout_s=3600)

        failures = []
        for t in describe(party_arns):
            for c in t.get("containers", []):
                exit_code = c.get("exitCode")
                if exit_code not in (0, None):
                    failures.append(f"{t['taskArn'].rsplit('/', 1)[-1]}/{c['name']}: exit {exit_code}")

        if failures:
            fail("; ".join(failures))
    finally:
        print("Stopping coordinator...", flush=True)
        stop_task(coord_arn)

    update_job(status="SUCCEEDED", finished_at=now_iso())
    print("Job succeeded.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        update_job(status="FAILED", error=traceback.format_exc()[-2000:], finished_at=now_iso())
        raise SystemExit(1)
