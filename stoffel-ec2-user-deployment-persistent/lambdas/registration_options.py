"""Issues a real, server-generated WebAuthn registration challenge for a one-time
registration token - the first half of the standard two-step WebAuthn registration
ceremony (generate options, then verify the response against them; see
register_client.py, the second half). Without this step, a client-generated challenge
that the server never records would satisfy the WebAuthn API's own requirement that
*some* challenge be present, while providing none of the anti-replay guarantee the
challenge is actually meant for - see the design plan for the full reasoning.

No API key required (see app.py's `_add_api`): gated by the one-time token, same as
POST /client-registrations itself.
"""
import json
import os

import boto3
from webauthn import generate_registration_options

dynamodb = boto3.resource("dynamodb")

CLIENT_REGISTRATIONS_TABLE_NAME = os.environ["CLIENT_REGISTRATIONS_TABLE_NAME"]
WEBAUTHN_RP_ID = os.environ["WEBAUTHN_RP_ID"]
client_registrations_table = dynamodb.Table(CLIENT_REGISTRATIONS_TABLE_NAME)


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "invalid JSON body"})

    token = body.get("token")
    if not token or not isinstance(token, str):
        return _response(400, {"error": "token is required"})

    item = client_registrations_table.get_item(Key={"token": token}).get("Item")
    if not item:
        return _response(404, {"error": "this registration link is invalid or has already been used"})
    if item.get("used_at"):
        return _response(409, {"error": "this registration link is invalid or has already been used"})

    options = generate_registration_options(
        rp_id=WEBAUTHN_RP_ID,
        rp_name="Stoffel private voting",
        user_name=item["client_name"],
    )

    try:
        client_registrations_table.update_item(
            Key={"token": token},
            UpdateExpression="SET challenge = :c",
            ConditionExpression="attribute_exists(#t) AND attribute_not_exists(used_at)",
            ExpressionAttributeNames={"#t": "token"},
            ExpressionAttributeValues={":c": options.challenge},
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return _response(409, {"error": "this registration link is invalid or has already been used"})

    return _response(200, {"challenge": list(options.challenge)})


def _response(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(payload, default=str),
    }
