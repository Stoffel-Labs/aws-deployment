"""Terminal step for a job: best-effort stops the coordinator's container
(it never exits on its own, so something has to explicitly stop it - same
reasoning as the Fargate deployment's orchestrator finally-block) and
records the final status/error in DynamoDB.

Invoked either from the normal path (outcome = CheckJobStatus's terminal
SUCCEEDED/FAILED payload) or from the state machine's shared failure path
(outcome = a synthetic {"status": "FAILED", "error": <Step Functions error
info>} built from a caught StartJob/CheckJobStatus/FinishJob exception).
"""
import os
from datetime import datetime, timezone

import boto3

ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")

JOBS_TABLE_NAME = os.environ["JOBS_TABLE_NAME"]
CONTAINER_NAME = os.environ["CONTAINER_NAME"]
COORD_INSTANCE_ID = os.environ["COORD_INSTANCE_ID"]

jobs_table = dynamodb.Table(JOBS_TABLE_NAME)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def stop_coordinator():
    try:
        ssm.send_command(
            InstanceIds=[COORD_INSTANCE_ID],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [f"docker rm -f {CONTAINER_NAME} >/dev/null 2>&1 || true"]},
        )
    except Exception as e:
        print(f"WARNING: failed to send stop command to coordinator: {e}", flush=True)


def handler(event, context):
    job = event["job"]
    job_id = job["job_id"]
    outcome = event.get("outcome") or {}
    status = outcome.get("job_status") or outcome.get("status") or "FAILED"
    error = outcome.get("error")

    stop_coordinator()

    expr_names = {"#status": "status"}
    expr_values = {":status": status, ":finished_at": now_iso()}
    set_parts = ["#status = :status", "finished_at = :finished_at"]
    if error:
        expr_names["#error"] = "error"
        expr_values[":error"] = str(error)
        set_parts.append("#error = :error")

    jobs_table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )

    print(f"[{job_id}] finished: {status}", flush=True)
    return {"status": status}
