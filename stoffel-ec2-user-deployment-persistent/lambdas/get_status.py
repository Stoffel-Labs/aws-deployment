import json
import os

import boto3

dynamodb = boto3.resource("dynamodb")
EXECUTIONS_TABLE_NAME = os.environ["EXECUTIONS_TABLE_NAME"]
executions_table = dynamodb.Table(EXECUTIONS_TABLE_NAME)


def handler(event, context):
    execution_id = (event.get("pathParameters") or {}).get("execution_id")
    if not execution_id:
        return _response(400, {"error": "execution_id path parameter is required"})

    item = executions_table.get_item(Key={"execution_id": execution_id}).get("Item")
    if not item:
        return _response(404, {"error": "execution not found"})

    # Status only - no captured MPC results. Users run their own client
    # against `endpoints` once status is RUNNING, same as the one-off
    # deployment (see app.py's docstring).
    result = {
        "execution_id": item["execution_id"],
        "status": item.get("status"),
        "program_id": item.get("program_id"),
        "program_name": item.get("program_name"),
        "entry": item.get("entry"),
        "clients": item.get("clients"),
        "created_at": item.get("created_at"),
        "admitted_at": item.get("admitted_at"),
        "finished_at": item.get("finished_at"),
        "endpoints": item.get("endpoints"),
        "error": item.get("error"),
    }
    return _response(200, result)


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
