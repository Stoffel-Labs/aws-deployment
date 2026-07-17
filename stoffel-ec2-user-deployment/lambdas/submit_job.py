import datetime
import json
import os
import uuid

import boto3

dynamodb = boto3.resource("dynamodb")
sqs = boto3.client("sqs")

JOBS_TABLE_NAME = os.environ["JOBS_TABLE_NAME"]
QUEUE_URL = os.environ["QUEUE_URL"]

jobs_table = dynamodb.Table(JOBS_TABLE_NAME)

# Mirrors the defaults in ../../stoffel-fargate-deployment/run-nodes.
DEFAULTS = {
    "num_parties": "5",
    "threshold": "1",
    "backend": "honeybadger",
    "curve": "bls12-381",
    "n_inputs": "",
}


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid JSON body"})

    program_key = body.get("program_key")
    if not program_key:
        return _response(400, {"error": "program_key is required (obtained from POST /programs/presign)"})

    job = dict(DEFAULTS)
    for field in ("num_parties", "threshold", "backend", "curve", "n_inputs"):
        if field in body:
            job[field] = str(body[field])

    job_id = str(uuid.uuid4())
    job["job_id"] = job_id
    job["program_s3_key"] = program_key

    now = _now()
    jobs_table.put_item(Item={
        "job_id": job_id,
        "status": "QUEUED",
        "program_s3_key": program_key,
        "params": job,
        "created_at": now,
        "updated_at": now,
    })

    # Single message group -> strict FIFO ordering of submissions. Actual
    # one-at-a-time *execution* is enforced by the DynamoDB lock inside the
    # Step Functions state machine, not by this queue (see app.py).
    sqs.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps(job),
        MessageGroupId="jobs",
        MessageDeduplicationId=job_id,
    )

    return _response(202, {"job_id": job_id, "status": "QUEUED"})


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _response(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }
