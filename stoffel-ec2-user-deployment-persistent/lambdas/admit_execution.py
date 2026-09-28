"""Admits one execution onto the already-running standing mesh: publishes a
`prepare` control command to every party (see StoffelVM's
standing_control.rs) and waits for each party's synchronous acknowledgement
(StandingControlOutcomeV1::Event or ::Rejected), then records RUNNING (or
FAILED, via fail() below) directly on the execution. This is the entire
orchestration flow - there is no separate status-polling/cleanup step.

Does NOT return coordinator/party endpoint addresses: those are fixed
Elastic IPs, identical for every execution, not anything specific to this
one, so nothing needs to look them up per-execution here. Both real
consumers fetch the same public config.json instead (see app.py's
_add_web_site) - the hosted website via a plain fetch(), and
./run-program (which needs no AWS credentials, only an API key) via a
plain curl, same as it already hardcodes this deployment's API URL.

Invoked once per submitted execution by the state machine's single
LambdaInvoke - see submit_execution.py, which starts a fresh state machine
execution per request instead of funnelling through any shared queue/lock,
since the standing mesh runs many executions concurrently.
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3

from party_control import PartyControlError, send_prepare_command

dynamodb = boto3.resource("dynamodb")

EXECUTIONS_TABLE_NAME = os.environ["EXECUTIONS_TABLE_NAME"]
PARTY_INSTANCE_IDS = os.environ["PARTY_INSTANCE_IDS"].split(",")

executions_table = dynamodb.Table(EXECUTIONS_TABLE_NAME)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def update_execution(execution_id, status=None, **fields):
    expr_names = {}
    expr_values = {":updated_at": now_iso()}
    set_parts = ["updated_at = :updated_at"]
    if status is not None:
        expr_names["#status"] = "status"
        expr_values[":status"] = status
        set_parts.append("#status = :status")
    for k, v in fields.items():
        expr_names[f"#{k}"] = k
        expr_values[f":{k}"] = v
        set_parts.append(f"#{k} = :{k}")
    executions_table.update_item(
        Key={"execution_id": execution_id},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def fail(execution_id, message):
    update_execution(execution_id, status="FAILED", error=message, finished_at=now_iso())
    raise RuntimeError(message)


def _admit_on_party(args):
    party_id, instance_id, payload_json = args
    try:
        send_prepare_command(party_id=party_id, instance_id=instance_id, payload_json=payload_json)
        return party_id, None
    except PartyControlError as e:
        return party_id, str(e)


def handler(event, context):
    execution = event["execution"]
    execution_id = execution["execution_id"]
    program_id = execution["program_id"]
    entry = execution.get("entry", "main")
    clients = execution.get("clients", [])

    payload = {
        "operation": "prepare",
        "admission": {
            "execution_id": execution_id,
            "program_id": program_id,
            "entry": entry,
            "clients": clients,
        },
    }
    payload_json = json.dumps(payload, separators=(",", ":"))

    print(f"[{execution_id}] admitting on {len(PARTY_INSTANCE_IDS)} parties...", flush=True)

    tasks = [(i, instance_id, payload_json) for i, instance_id in enumerate(PARTY_INSTANCE_IDS)]
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        results = list(pool.map(_admit_on_party, tasks))

    errors = [f"party{party_id}: {err}" for party_id, err in results if err]
    if errors:
        fail(execution_id, f"admission failed: {'; '.join(errors)}")

    update_execution(execution_id, status="RUNNING", admitted_at=now_iso())
    print(f"[{execution_id}] admitted on all parties.", flush=True)

    return {"execution_status": "RUNNING"}
