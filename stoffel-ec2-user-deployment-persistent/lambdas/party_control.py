"""Shared helpers for driving StoffelVM's standing-node control plane
(../../StoffelVM/crates/stoffel-vm-runner/src/standing_control.rs) over SSM
Run Command from Lambda.

Every party's command journal must be strictly, contiguously sequenced -
StandingControlPump errors ("non-contiguous command event journal") if a
sequence number is skipped, and that error is fatal to the *entire* party's
control loop, not just the one bad command. AWS SSM Run Command gives no
ordering guarantee between commands sent to the same instance in quick
succession, so under concurrent requests, allocating a sequence number is
not enough by itself - actually *delivering* commands to a given party must
also happen one at a time, in sequence order.

DynamoDB conditional updates on PARTY_CONTROL_TABLE do both jobs with one
call: the same UpdateItem both claims a short lease (so no other concurrent
Lambda invocation can interleave a delivery to this party) and hands out
the next sequence number. The lease is held only for the single SSM round
trip needed to deliver that one command - order of seconds - never for an
execution's lifetime. This is what lets many executions still run
concurrently on the mesh: only the act of *publishing a control command* to
a given party is serialized, not anything about the execution itself.
"""
import json
import os
import time
import uuid

import boto3

ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")

PARTY_CONTROL_TABLE_NAME = os.environ["PARTY_CONTROL_TABLE_NAME"]
party_control_table = dynamodb.Table(PARTY_CONTROL_TABLE_NAME)

STANDING_CONTROL_DIR = "/home/ec2-user/standing/control"
LOCK_LEASE_SECONDS = 20
LOCK_ACQUIRE_TIMEOUT_SECONDS = 25
LOCK_RETRY_INTERVAL_SECONDS = 0.5


class PartyControlError(Exception):
    pass


def _acquire_sequence(party_id: int):
    """Atomically claims a short-lived lease on this party's command journal
    and returns (sequence, lease_holder). Retries with backoff while another
    Lambda invocation holds the lease; raises PartyControlError if it can't
    acquire one within LOCK_ACQUIRE_TIMEOUT_SECONDS."""
    holder = uuid.uuid4().hex
    deadline = time.time() + LOCK_ACQUIRE_TIMEOUT_SECONDS
    key = str(party_id)
    while True:
        now = int(time.time())
        try:
            resp = party_control_table.update_item(
                Key={"party_id": key},
                UpdateExpression=(
                    "SET lock_holder = :holder, lock_expires_at = :expires, "
                    "seq = if_not_exists(seq, :zero) + :one"
                ),
                ConditionExpression="attribute_not_exists(lock_holder) OR lock_expires_at < :now",
                ExpressionAttributeValues={
                    ":holder": holder,
                    ":expires": now + LOCK_LEASE_SECONDS,
                    ":zero": 0,
                    ":one": 1,
                    ":now": now,
                },
                ReturnValues="UPDATED_NEW",
            )
            return int(resp["Attributes"]["seq"]), holder
        except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            if time.time() >= deadline:
                raise PartyControlError(
                    f"could not acquire party{party_id}'s control lock within "
                    f"{LOCK_ACQUIRE_TIMEOUT_SECONDS}s (another admission/cancel in progress)"
                )
            time.sleep(LOCK_RETRY_INTERVAL_SECONDS)


def _release_sequence(party_id: int, holder: str):
    try:
        party_control_table.update_item(
            Key={"party_id": str(party_id)},
            UpdateExpression="REMOVE lock_holder, lock_expires_at",
            ConditionExpression="lock_holder = :holder",
            ExpressionAttributeValues={":holder": holder},
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        # Lease already expired and was re-acquired by someone else - fine,
        # the lease itself is what protects correctness, not this cleanup.
        pass


def _send_ssm(instance_id: str, script: str) -> str:
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script]},
    )
    return resp["Command"]["CommandId"]


def _wait_ssm(instance_id: str, command_id: str, timeout_s: int, interval_s: float = 1.0) -> str:
    deadline = time.time() + timeout_s
    time.sleep(interval_s)
    while True:
        try:
            resp = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            if time.time() >= deadline:
                raise PartyControlError(f"SSM command {command_id} never registered on {instance_id}")
            time.sleep(interval_s)
            continue
        status = resp["Status"]
        if status == "Success":
            return resp["StandardOutputContent"]
        if status in ("Cancelled", "TimedOut", "Failed", "Cancelling"):
            raise PartyControlError(
                f"SSM command failed on {instance_id} ({status}): "
                f"{resp.get('StandardErrorContent', '')[-1000:]}"
            )
        if time.time() >= deadline:
            raise PartyControlError(f"SSM command {command_id} on {instance_id} timed out after {timeout_s}s")
        time.sleep(interval_s)


def _send_gap_filler(party_id: int, instance_id: str, seq: int) -> None:
    """Writes a harmless placeholder command directly into party_id's command
    journal at sequence `seq`, without waiting for its ack. Last-resort
    fallback for when _send_ssm itself fails (e.g. SSM throttling) after
    _acquire_sequence has already reserved that number in DynamoDB: without
    this, `seq` is gone forever - the next admission always asks DynamoDB
    for the *next* number, never revisits a skipped one - so
    StandingControlPump's poll_one (../../StoffelVM's standing_control.rs)
    would wait on that exact sequence's file forever, permanently wedging
    every later command already queued behind it on this party. The
    placeholder is a Cancel for a random, essentially-never-real execution
    id, which the pump rejects harmlessly and moves past."""
    commands_dir = f"{STANDING_CONTROL_DIR}/commands"
    seq_str = f"{seq:020d}"
    placeholder = json.dumps(
        {"operation": "cancel", "execution_id": uuid.uuid4().hex + uuid.uuid4().hex},
        separators=(",", ":"),
    )
    script = f"""set -e
mkdir -p "{commands_dir}"
TMP="{commands_dir}/.tmp.$$"
cat > "$TMP" <<'STOFFEL_PAYLOAD_EOF'
{placeholder}
STOFFEL_PAYLOAD_EOF
mv "$TMP" "{commands_dir}/{seq_str}.json"
"""
    _send_ssm(instance_id, script)


def _publish_and_wait_ack(party_id: int, instance_id: str, payload_json: str, ack_timeout_s: int) -> str:
    seq, holder = _acquire_sequence(party_id)
    try:
        events_dir = f"{STANDING_CONTROL_DIR}/events/party{party_id}"
        commands_dir = f"{STANDING_CONTROL_DIR}/commands"
        seq_str = f"{seq:020d}"
        script = f"""set -e
mkdir -p "{events_dir}" "{commands_dir}"
TMP="{commands_dir}/.tmp.$$"
cat > "$TMP" <<'STOFFEL_PAYLOAD_EOF'
{payload_json}
STOFFEL_PAYLOAD_EOF
mv "$TMP" "{commands_dir}/{seq_str}.json"
ACK="{events_dir}/{seq_str}.json"
DEADLINE=$(($(date +%s) + {ack_timeout_s}))
while [ ! -f "$ACK" ]; do
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "ERROR: no acknowledgement for command {seq_str} within {ack_timeout_s}s" >&2
    exit 3
  fi
  sleep 0.2
done
cat "$ACK"
"""
        try:
            command_id = _send_ssm(instance_id, script)
        except Exception as send_error:
            # The command for `seq` was never delivered at all - fill the
            # slot with a no-op so the pump can move past it instead of
            # wedging forever. This admission attempt still fails (the
            # caller sees send_error either way); the gap-fill only protects
            # every *future* admission on this party from being taken down
            # with it.
            try:
                _send_gap_filler(party_id, instance_id, seq)
            except Exception as gap_error:
                print(
                    f"ERROR: party{party_id} sequence {seq} was never delivered "
                    f"({send_error}) and the gap-filler also failed ({gap_error}) - "
                    f"party{party_id} will now wait on sequence {seq} forever until "
                    "it is filled manually",
                    flush=True,
                )
            raise
        return _wait_ssm(instance_id, command_id, timeout_s=ack_timeout_s + 15)
    finally:
        _release_sequence(party_id, holder)


def send_prepare_command(party_id: int, instance_id: str, payload_json: str, ack_timeout_s: int = 25):
    """Publishes a `prepare` admission command to one party and waits for its
    synchronous ack (StandingControlOutcomeV1::Event or ::Rejected). Raises
    PartyControlError if the party rejects it (e.g. the program isn't in its
    live catalog - see StandingProgramCatalog) or never acks in time. Once
    every party has acked, admit_execution.py itself considers the
    execution RUNNING - there is no further async readiness-tracking step."""
    ack = _publish_and_wait_ack(party_id, instance_id, payload_json, ack_timeout_s)
    if '"outcome":"event"' not in ack:
        raise PartyControlError(f"admission rejected: {ack.strip()[-500:]}")


def send_cancel_command(party_id: int, instance_id: str, execution_id: str, ack_timeout_s: int = 15):
    payload = json.dumps({"operation": "cancel", "execution_id": execution_id}, separators=(",", ":"))
    _publish_and_wait_ack(party_id, instance_id, payload, ack_timeout_s)
