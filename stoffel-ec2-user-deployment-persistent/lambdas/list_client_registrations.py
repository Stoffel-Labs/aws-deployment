"""Lists registered-and-materialized WebAuthn clients, for start.html's voter picker.

Unlike POST /client-registrations (register_client.py, deliberately no API key - a one-time
token gates that instead), this route requires the operator's API key: it's exposing who has
registered, which is operator-facing information, not something a one-time registration link
recipient needs.

Only returns clients with both `used_at` (registration completed) and `materialized_at` (an
operator has run ./materialize-registered-clients and ./deploy, so the identity is actually
usable in an execution's `clients` list) set - a registered-but-not-yet-materialized client
would fail admission's resolve_admission lookup if picked, so there's no point offering it.
"""
import json
import os

import boto3

dynamodb = boto3.resource("dynamodb")

CLIENT_REGISTRATIONS_TABLE_NAME = os.environ["CLIENT_REGISTRATIONS_TABLE_NAME"]
client_registrations_table = dynamodb.Table(CLIENT_REGISTRATIONS_TABLE_NAME)


def handler(event, context):
    items = []
    resp = client_registrations_table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = client_registrations_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))

    clients = sorted(
        (
            {
                "client_name": item["client_name"],
                "label": item.get("label", ""),
                "created_at": item["created_at"],
            }
            for item in items
            if item.get("used_at") and item.get("materialized_at")
        ),
        key=lambda client: client["created_at"],
    )
    return _response(200, {"clients": clients})


def _response(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(payload, default=str),
    }
