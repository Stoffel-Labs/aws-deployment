"""Polls the party instances (not the coordinator, which never exits on its
own - same reasoning as the Fargate deployment's orchestrator) to see
whether a job's containers have finished. Invoked repeatedly by the state
machine's Wait/CheckJobStatus/Choice loop until it reports a terminal status.

Each poll is a fresh, non-blocking `docker inspect` sent via SSM - cheap
enough to run every ~10s for the lifetime of a job without materializing any
long-lived compute of its own (the "waiting" happens in Step Functions' Wait
state, not in a running process).
"""
import os
import time

import boto3

ssm = boto3.client("ssm")

CONTAINER_NAME = os.environ["CONTAINER_NAME"]
PARTY_INSTANCE_IDS = os.environ["PARTY_INSTANCE_IDS"].split(",")

INSPECT_CMD = (
    f'docker inspect --format "{{{{.State.Status}}}} {{{{.State.ExitCode}}}}" {CONTAINER_NAME} '
    '2>/dev/null || echo "missing -1"'
)


def send_ssm(instance_id):
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [INSPECT_CMD]},
    )
    return resp["Command"]["CommandId"]


def poll_ssm(instance_id, command_id, timeout_s=20, interval_s=1):
    deadline = time.time() + timeout_s
    time.sleep(interval_s)
    while time.time() < deadline:
        try:
            resp = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(interval_s)
            continue
        status = resp["Status"]
        if status == "Success":
            return resp["StandardOutputContent"].strip()
        if status in ("Cancelled", "TimedOut", "Failed", "Cancelling"):
            # Treat as inconclusive rather than a hard job failure - a
            # single SSM hiccup shouldn't fail an otherwise-healthy job; the
            # next poll cycle will try again.
            return None
        time.sleep(interval_s)
    return None


def handler(event, context):
    job = event["job"]
    num_parties = int(job.get("num_parties", 5))
    instance_ids = PARTY_INSTANCE_IDS[:num_parties]

    command_ids = [send_ssm(iid) for iid in instance_ids]

    still_running = False
    failures = []
    for i, (instance_id, command_id) in enumerate(zip(instance_ids, command_ids)):
        output = poll_ssm(instance_id, command_id)
        if not output:
            still_running = True
            continue
        status, _, exit_code = output.strip().partition(" ")
        if status == "exited":
            if exit_code not in ("0", ""):
                failures.append(f"party{i}: exit {exit_code}")
        else:
            # "running", "missing" (not started/reaped yet), or anything
            # else - keep polling.
            still_running = True

    if still_running:
        return {"job_status": "RUNNING"}
    if failures:
        return {"job_status": "FAILED", "error": "; ".join(failures)}
    return {"job_status": "SUCCEEDED"}
