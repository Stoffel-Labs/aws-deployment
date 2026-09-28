"""Fetches each node's current standing-container log stream, filtered to a
time window around one execution's lifetime.

Unlike the one-off deployment, there is no per-job log stream to look up:
the coordinator/party containers run continuously across many executions,
logging to whatever stream name the operator's last ./deploy picked (see
that script). The current stream for each node is found via
DescribeLogStreams' latest-by-last-event-time, then filtered by
FilterLogEvents' startTime/endTime to just this execution's window.

Because every execution running concurrently on a node shares that node's
one ongoing log stream, this is a best-effort time-window filter, not a
clean per-execution log - lines from other overlapping executions on the
same node can appear interleaved. stoffel-run doesn't tag log lines with an
execution_id to filter on more precisely.
"""
import json
import os
from datetime import datetime, timezone

import boto3

dynamodb = boto3.resource("dynamodb")
logs_client = boto3.client("logs")

EXECUTIONS_TABLE_NAME = os.environ["EXECUTIONS_TABLE_NAME"]
LOG_GROUP_NAME = os.environ["LOG_GROUP_NAME"]
PARTY_INSTANCE_IDS = os.environ["PARTY_INSTANCE_IDS"].split(",")
executions_table = dynamodb.Table(EXECUTIONS_TABLE_NAME)

WINDOW_BUFFER_MS = 30_000
MAX_EVENTS_PER_NODE = 2000


def _latest_stream(prefix):
    # CloudWatch Logs rejects orderBy=LastEventTime combined with
    # logStreamNamePrefix (InvalidParameterException) - list every matching
    # stream instead (default order, paginated) and pick the one with the
    # newest creationTime ourselves. Each ./deploy creates a fresh stream
    # per node (named "<node>/<deploy-timestamp>-<pid>"), so there's
    # normally only a handful to page through.
    latest = None
    kwargs = {"logGroupName": LOG_GROUP_NAME, "logStreamNamePrefix": f"{prefix}/"}
    while True:
        resp = logs_client.describe_log_streams(**kwargs)
        for stream in resp.get("logStreams", []):
            if latest is None or stream.get("creationTime", 0) > latest.get("creationTime", 0):
                latest = stream
        next_token = resp.get("nextToken")
        if not next_token:
            break
        kwargs["nextToken"] = next_token
    return latest["logStreamName"] if latest else None


def _iso_to_ms(value):
    if not value:
        return None
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def handler(event, context):
    execution_id = (event.get("pathParameters") or {}).get("execution_id")
    if not execution_id:
        return _response(400, {"error": "execution_id path parameter is required"})

    item = executions_table.get_item(Key={"execution_id": execution_id}).get("Item")
    if not item:
        return _response(404, {"error": "execution not found"})

    start_ms = _iso_to_ms(item.get("created_at"))
    end_ms = _iso_to_ms(item.get("finished_at"))
    if start_ms is not None:
        start_ms -= WINDOW_BUFFER_MS
    if end_ms is not None:
        end_ms += WINDOW_BUFFER_MS

    nodes = ["coordinator"] + [f"party{i}" for i in range(len(PARTY_INSTANCE_IDS))]
    logs_by_node = {}
    for node in nodes:
        stream = _latest_stream(node)
        if not stream:
            logs_by_node[node] = [{"timestamp": None, "message": "(no log stream found yet for this node - has ./deploy run?)"}]
            continue

        kwargs = {"logGroupName": LOG_GROUP_NAME, "logStreamNames": [stream]}
        if start_ms is not None:
            kwargs["startTime"] = start_ms
        if end_ms is not None:
            kwargs["endTime"] = end_ms

        events = []
        next_token = None
        while True:
            if next_token:
                kwargs["nextToken"] = next_token
            resp = logs_client.filter_log_events(**kwargs)
            events.extend(resp.get("events", []))
            next_token = resp.get("nextToken")
            if not next_token or len(events) >= MAX_EVENTS_PER_NODE:
                break

        logs_by_node[node] = [
            {
                "timestamp": datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc).isoformat(),
                "message": e["message"],
            }
            for e in events
        ]

    return _response(200, {
        "execution_id": execution_id,
        "status": item.get("status"),
        "created_at": item.get("created_at"),
        "finished_at": item.get("finished_at"),
        "logs": logs_by_node,
    })


def _response(status, payload):
    return {
        "statusCode": status,
        # Access-Control-Allow-Origin lets a browser page (e.g. this deployment's voting
        # example's start.html) call this API with fetch() directly - API Gateway's CORS
        # preflight (OPTIONS, configured via default_cors_preflight_options in app.py) only
        # covers the preflight request itself; the actual GET/POST response still needs this
        # header from the Lambda for the browser to let JS read the response body at all.
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(payload, default=str),
    }
