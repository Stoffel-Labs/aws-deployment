"""Validates a run request and hands it off to admit_execution.py to admit
onto the standing mesh.

Unlike the one-off/queued deployment's submit_job.py, there is no shared
queue or lock here: every request kicks off its own independent, async
admit_execution_fn invocation (InvocationType="Event" - the same fire-and-
forget pattern cancel_execution.py's api_handler/worker_handler split
already uses; there's no multi-step orchestration left to justify Step
Functions once admission lost its status-polling/cleanup loop), and the
standing mesh itself is what lets many of them run concurrently (see
app.py's docstring) - this Lambda's only job is to validate the request,
resolve the program, generate an execution_id, and kick off that
independent invocation.
"""
import json
import os
import re
from datetime import datetime, timezone

import boto3

dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")

EXECUTIONS_TABLE_NAME = os.environ["EXECUTIONS_TABLE_NAME"]
PROGRAMS_TABLE_NAME = os.environ["PROGRAMS_TABLE_NAME"]
ADMIT_EXECUTION_FUNCTION_NAME = os.environ["ADMIT_EXECUTION_FUNCTION_NAME"]

executions_table = dynamodb.Table(EXECUTIONS_TABLE_NAME)
programs_table = dynamodb.Table(PROGRAMS_TABLE_NAME)

PROGRAM_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def _now():
    return datetime.now(timezone.utc).isoformat()


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid JSON body"})

    program_name = body.get("program_name")
    program_id = body.get("program_id")
    if not program_name and not program_id:
        return _response(400, {"error": "program_name or program_id is required - see GET /programs"})

    if program_id:
        if not PROGRAM_ID_RE.match(program_id):
            return _response(400, {"error": "program_id must be 64 lowercase hex characters"})
    else:
        item = programs_table.get_item(Key={"name": program_name}).get("Item")
        if not item:
            return _response(404, {"error": f"no program named '{program_name}' - see GET /programs"})
        program_id = item["program_id"]

    entry = body.get("entry", "main")

    clients = body.get("clients")
    if clients is None:
        try:
            n_clients = int(body.get("n_clients", 0) or 0)
        except (TypeError, ValueError):
            return _response(400, {"error": "n_clients must be an integer"})
        clients = [{"certificate": f"client{i}.crt", "manifest_slot": i} for i in range(n_clients)]
    elif not isinstance(clients, list) or not all(
        isinstance(c, dict) and "certificate" in c and "manifest_slot" in c for c in clients
    ):
        return _response(400, {"error": "clients must be a list of {certificate, manifest_slot}"})

    execution_id = os.urandom(32).hex()
    now = _now()

    # The API key ID (not the key's secret value) API Gateway resolves once it's validated
    # `x-api-key` against the usage plan - see event["requestContext"]["identity"]["apiKeyId"]
    # in API Gateway's Lambda-proxy integration event. Recorded so cancel_execution.py's
    # api_handler can check that only the same key that submitted an execution can cancel
    # it - every key is otherwise equally privileged over every execution_id today.
    api_key_id = ((event.get("requestContext") or {}).get("identity") or {}).get("apiKeyId")

    executions_table.put_item(Item={
        "execution_id": execution_id,
        "status": "PREPARING",
        "program_id": program_id,
        "program_name": program_name,
        "entry": entry,
        "clients": clients,
        "created_at": now,
        "updated_at": now,
        "api_key_id": api_key_id,
    })

    lambda_client.invoke(
        FunctionName=ADMIT_EXECUTION_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps({
            "execution": {
                "execution_id": execution_id,
                "program_id": program_id,
                "entry": entry,
                "clients": clients,
            }
        }).encode(),
    )

    return _response(202, {"execution_id": execution_id, "status": "PREPARING"})


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
