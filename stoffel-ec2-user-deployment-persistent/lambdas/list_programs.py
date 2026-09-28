"""GET /programs - lists the programs the operator has staged and deployed
onto the live standing mesh (./upload-program + ./deploy). Users can only
run programs that show up here; there is no user-facing upload path (see
app.py's docstring)."""
import json
import os

import boto3

dynamodb = boto3.resource("dynamodb")
PROGRAMS_TABLE_NAME = os.environ["PROGRAMS_TABLE_NAME"]
programs_table = dynamodb.Table(PROGRAMS_TABLE_NAME)


def handler(event, context):
    items = []
    resp = programs_table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = programs_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))

    programs = sorted(
        (
            {"name": i["name"], "program_id": i["program_id"], "staged_at": i.get("staged_at")}
            for i in items
        ),
        key=lambda p: p["name"],
    )
    return _response(200, {"programs": programs})


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
