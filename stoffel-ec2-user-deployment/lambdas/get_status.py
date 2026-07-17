import json
import os

import boto3

dynamodb = boto3.resource("dynamodb")
JOBS_TABLE_NAME = os.environ["JOBS_TABLE_NAME"]
jobs_table = dynamodb.Table(JOBS_TABLE_NAME)


def handler(event, context):
    job_id = (event.get("pathParameters") or {}).get("job_id")
    if not job_id:
        return _response(400, {"error": "job_id path parameter is required"})

    item = jobs_table.get_item(Key={"job_id": job_id}).get("Item")
    if not item:
        return _response(404, {"error": "job not found"})

    # Status only - no captured MPC results. Users run their own client
    # (run-client) against `endpoints` once status is RUNNING, same as today.
    result = {
        "job_id": item["job_id"],
        "status": item.get("status"),
        "created_at": item.get("created_at"),
        "started_at": item.get("started_at"),
        "finished_at": item.get("finished_at"),
        "endpoints": item.get("endpoints"),
        "error": item.get("error"),
    }
    return _response(200, result)


def _response(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=str),
    }
