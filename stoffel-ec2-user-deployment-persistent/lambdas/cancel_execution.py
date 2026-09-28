"""Cancels an execution.

api_handler (POST /executions/{execution_id}/cancel) records
status=CANCELLING and immediately hands off to worker_handler - a separate
Lambda resource built from this same file (see app.py) invoked
asynchronously - to do the actual best-effort `cancel` control command on
every party. Splitting it this way means the API response never risks
running into API Gateway's 29s integration timeout waiting on N parties'
control-lock-and-SSM round trips (see party_control.py).
"""
import json
import os
from datetime import datetime, timezone

import boto3

from party_control import send_cancel_command

dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")

EXECUTIONS_TABLE_NAME = os.environ["EXECUTIONS_TABLE_NAME"]
# Only worker_handler needs this; api_handler's Lambda resource doesn't set
# it (it only hands off to the worker), so default to empty rather than
# fail at import time for that resource.
PARTY_INSTANCE_IDS = os.environ.get("PARTY_INSTANCE_IDS", "").split(",") if os.environ.get("PARTY_INSTANCE_IDS") else []
WORKER_FUNCTION_NAME = os.environ.get("WORKER_FUNCTION_NAME", "")

executions_table = dynamodb.Table(EXECUTIONS_TABLE_NAME)

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def api_handler(event, context):
    execution_id = (event.get("pathParameters") or {}).get("execution_id")
    if not execution_id:
        return _response(400, {"error": "execution_id path parameter is required"})

    item = executions_table.get_item(Key={"execution_id": execution_id}).get("Item")
    if not item:
        return _response(404, {"error": "execution not found"})

    # Only the API key that submitted an execution (see submit_execution.py's api_key_id)
    # may cancel it - without this, every key is equally privileged over every execution_id.
    # An execution with no recorded api_key_id (submitted before this check existed) has no
    # ownership to enforce, so it's left cancellable by anyone, same as before.
    owner_key_id = item.get("api_key_id")
    caller_key_id = ((event.get("requestContext") or {}).get("identity") or {}).get("apiKeyId")
    if owner_key_id and owner_key_id != caller_key_id:
        return _response(403, {"error": "this execution was submitted with a different API key"})

    if item.get("status") in TERMINAL_STATUSES:
        return _response(200, {"execution_id": execution_id, "status": item["status"]})

    executions_table.update_item(
        Key={"execution_id": execution_id},
        UpdateExpression="SET #status = :status, updated_at = :now",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": "CANCELLING", ":now": _now()},
    )

    lambda_client.invoke(
        FunctionName=WORKER_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps({"execution_id": execution_id}).encode(),
    )

    return _response(202, {"execution_id": execution_id, "status": "CANCELLING"})


def worker_handler(event, context):
    execution_id = event["execution_id"]
    print(f"[{execution_id}] cancelling on {len(PARTY_INSTANCE_IDS)} parties...", flush=True)
    for i, instance_id in enumerate(PARTY_INSTANCE_IDS):
        try:
            send_cancel_command(party_id=i, instance_id=instance_id, execution_id=execution_id)
        except Exception as e:
            print(f"WARNING: [{execution_id}] cancel on party{i} failed: {e}", flush=True)

    try:
        # Don't clobber a terminal status admit_execution.py's own fail()
        # already recorded in the meantime (e.g. admission itself failed
        # right as this cancel was requested) - first write wins.
        executions_table.update_item(
            Key={"execution_id": execution_id},
            UpdateExpression="SET #status = :status, finished_at = :now",
            ConditionExpression="attribute_not_exists(finished_at)",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": "CANCELLED", ":now": _now()},
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        pass

    print(f"[{execution_id}] cancel worker done.", flush=True)


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
