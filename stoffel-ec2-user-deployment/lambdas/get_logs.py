import json
import os
from datetime import datetime, timezone

import boto3

dynamodb = boto3.resource("dynamodb")
logs_client = boto3.client("logs")

JOBS_TABLE_NAME = os.environ["JOBS_TABLE_NAME"]
LOG_GROUP_NAME = os.environ["LOG_GROUP_NAME"]
jobs_table = dynamodb.Table(JOBS_TABLE_NAME)


def handler(event, context):
    job_id = (event.get("pathParameters") or {}).get("job_id")
    if not job_id:
        return _response(400, {"error": "job_id path parameter is required"})

    item = jobs_table.get_item(Key={"job_id": job_id}).get("Item")
    if not item:
        return _response(404, {"error": "job not found"})

    log_streams = item.get("log_streams") or {}
    if not log_streams:
        return _response(404, {
            "error": "no logs recorded yet for this job - it may still be QUEUED, "
                     "or failed before any node started",
        })

    logs_by_node = {}
    for node, stream_name in sorted(log_streams.items()):
        try:
            resp = logs_client.get_log_events(
                logGroupName=LOG_GROUP_NAME,
                logStreamName=stream_name,
                startFromHead=True,
            )
            logs_by_node[node] = [
                {
                    "timestamp": datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc).isoformat(),
                    "message": e["message"],
                }
                for e in resp.get("events", [])
            ]
        except logs_client.exceptions.ResourceNotFoundException:
            logs_by_node[node] = [{"timestamp": None, "message": "(log stream not found - may have expired or the task never wrote logs)"}]

    return _response(200, {
        "job_id": job_id,
        "status": item.get("status"),
        "logs": logs_by_node,
    })


def _response(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=str),
    }
