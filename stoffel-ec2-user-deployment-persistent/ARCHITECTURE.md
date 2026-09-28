# Architecture

## Overview

This deployment gives external users a self-service, API-key-authenticated
way to run StoffelVM MPC programs on a shared EC2 cluster, the same goal as
`../stoffel-ec2-user-deployment` - but built around StoffelVM's
`--standing-node` control plane instead of one-container-per-job. The
coordinator and every party start once (`./deploy`) and stay running;
"running a program" becomes admitting a fresh *execution* onto that
already-running mesh, and many executions run concurrently instead of one
job at a time behind a cluster-wide lock.

```
Operator                          Users (API key only)
   |                                    |
   | cdk deploy                        |
   | ./upload-ids                      |
   | ./upload-program <name>           |
   | ./deploy  ---------> [standing mesh: coordinator + N parties, always up]
   |                                    |
   |                          POST /executions {program_name, ...}
   |                                    |
   |                    [Step Functions: AdmitExecution -> poll -> FinishExecution]
   |                                    |
   |                          GET /executions/{id}      (status + endpoints)
   |                          GET /executions/{id}/logs
   |                          POST /executions/{id}/cancel
   |                                    |
   |                    user's own MPC client <-> party/coordinator endpoints
```

## Requirements this design satisfies

- **Self-service for users, no AWS credentials needed.** Same API-key
  model as the one-off deployment (API Gateway usage plan + per-user keys
  via `./add-api-key`).
- **Many executions run concurrently.** The standing mesh's whole point is
  serving more than one execution at a time - there is no per-job lock or
  FIFO queue anywhere in this stack (contrast the one-off deployment's
  DynamoDB lock + SQS FIFO queue, needed there because the container itself
  *was* the job).
- **Minimal latency from "submit" to "running".** No container launch in
  the hot path at all - admitting an execution is a small JSON control
  command written into an already-running process's control directory (a
  few SSM round trips), not a `docker run`.
- **Program provenance stays operator-controlled.** Because
  `StandingProgramCatalog::load` freezes the program catalog at container
  startup, letting arbitrary users upload-and-run in one step would mean
  any user's new program could force a mesh restart that kills every other
  user's in-flight execution. So staging is operator-only (`./upload-program`
  + `./deploy`); users can only run what's already staged.

## Compute layer

Same shape as the one-off deployment (see `app.py`): one VPC, public
subnets only (no NAT Gateway - every node needs a public IP for external MPC
clients anyway), one coordinator + N party `t4g.small` (Graviton/arm64)
instances, each with
an Elastic IP allocated independently of the instance so every address is a
CloudFormation output the moment `cdk deploy` finishes.

The difference is what runs on them and when:

- **User data** (`cdk deploy` time) only installs Docker, logs into ECR,
  and pre-pulls the coordinator/party images. It does **not** start any
  container - there's nothing to start yet (ids/programs aren't staged, and
  a fresh stack has no reason to guess a topology/threshold).
- **`./deploy`** (operator, over SSM, any time after) is the only thing
  that ever runs `docker run`/`docker rm -f` against these instances. It
  syncs `ids/` and `standing-programs/` from the assets bucket onto each
  instance's local disk, then starts the coordinator (`run-coord`, no
  `--one-off`, stays up) and every party (`stoffel-run --standing-node
  --control-dir ... --program-dir ...`, `--restart unless-stopped`) fresh.
  Mirrors `../stoffel-ec2-cross-region-deployment/standing-deploy`,
  collapsed to a single region.
- Every container carries `--restart unless-stopped`, so `./stop-nodes` +
  `./start-nodes` (pausing compute billing) don't need a `./deploy` in
  between - Docker brings the same containers back once its daemon is up.

Topology (`num_parties`/`threshold`) is fixed at `cdk deploy` time via CDK
context, same bounds as the one-off deployment (`2*threshold+1 <=
num_parties <= 10`). Unlike
`../stoffel-ec2-cross-region-deployment/standing-deploy`, `./deploy` here
always runs the mesh with every deployed party - there's no `--num-parties`
subset option, which removes a whole axis of "what's the live topology
right now" bookkeeping the orchestration Lambdas would otherwise need.
`--threshold` can still be chosen per `./deploy` call (bounded by what's
valid for the deployed party count), since it's purely a `--standing-node`
process flag, not infrastructure.

## User-facing layer

### 1. Program catalog: assets bucket + ProgramsTable

`./upload-program` content-addresses a compiled `.stflb` (the same digest
`StandingProgramCatalog::load` derives, and `stoffel-run
--print-program-id` prints), uploads it to
`s3://<assets-bucket>/standing-programs/<program_id>.stflb`, and registers
`{name, program_id, staged_at}` in `ProgramsTable`. Neither step touches
the running mesh - `./deploy` is what syncs the S3 prefix onto every
party's `--program-dir` and restarts the containers so the new entry in the
catalog is actually loaded.

`GET /programs` (`lambdas/list_programs.py`) just scans `ProgramsTable` -
the source of truth for what's *staged*, which may be ahead of what's
*live* on the mesh if `./deploy` hasn't run since the last upload.

### 2. Submit: one Step Functions execution per request

`POST /executions` (`lambdas/submit_execution.py`) resolves `program_name`
→ `program_id` (or accepts a bare `program_id`), builds the client roster,
generates a random `execution_id`, writes a `PREPARING` row to
`ExecutionsTable`, and calls `states:StartExecution` directly - no
queue, no EventBridge Pipe. Every request gets its own independent Standard
Workflow execution; nothing here waits for any other request. Compare the
one-off deployment, where `submit_job.py` enqueues onto a FIFO SQS queue
specifically so the state machine's DynamoDB lock can serialize dequeuing.

### 3. Admit: AdmitExecution Lambda + party control locks

`lambdas/admit_execution.py` publishes a `prepare` control command (`{
"operation": "prepare", "admission": {execution_id, program_id, entry,
clients} }`) to every party and waits for each one's synchronous
acknowledgement (`StandingControlOutcomeV1::Event` or `::Rejected` - see
`../StoffelVM/crates/stoffel-vm-runner/src/standing_control.rs`). A
`Rejected` ack (e.g. the program isn't in that party's live catalog) fails
the execution with a clear error.

This is the one place genuine serialization still exists, and it's worth
being precise about why: StoffelVM's standing control journal requires a
**strictly contiguous** per-party command sequence (`StandingControlPump`
errors, fatally to that party's whole control loop, on any gap), and SSM
Run Command gives no ordering guarantee between commands sent to the same
instance in quick succession. `lambdas/party_control.py` uses a DynamoDB
conditional update on `PartyControlTable` to atomically claim a short lease
*and* hand out the next sequence number in one call, holds that lease only
for the single SSM round trip needed to deliver the one command (a few
seconds), and releases it immediately after. **This serializes command
*delivery* to a given party, not execution *runtime*** - many admitted
executions still run concurrently; only the act of publishing the next
control command to a party is one-at-a-time.

### 4. Track: CheckExecutionStatus poll loop

Same Wait(10s) → LambdaInvoke → Choice shape as the one-off deployment's
`check_job_status.py`, but polling for a different signal:
`lambdas/check_execution_status.py` reads each party's async event files
(`async-<execution_id>-{completed,failed,cancelled}.json`, written by
`standing_control.rs::write_async_event`) instead of a container's exit
code, since the container never exits. `RUNNING` until every party reports
`completed`, `FAILED` if any reports `failed`, `CANCELLED` if any reports
`cancelled`.

### 5. Finish: FinishExecution / HandleFailure

`lambdas/finish_execution.py` is invoked from two places - the normal
terminal branch of the poll loop, and the shared `HandleFailure` catch path
for any uncaught error in AdmitExecution/CheckExecutionStatus - both
ultimately calling the same Lambda with an `outcome`. Whenever that outcome
isn't `SUCCEEDED`, it best-effort sends a `cancel` control command to every
party (cheap and safe even for a party where the execution never got past
admission) before recording the final status, so a partially-admitted or
abandoned execution doesn't linger in any party's live-execution table.
Unlike the one-off deployment's `finish_job.py`, there's no container to
stop and no cluster lock to release - this execution finishing never blocks
any other execution.

### 6. Cancel: split API/worker Lambdas

`POST /executions/{id}/cancel` needs to reach every party (same
lease-and-SSM round trip as admission) before it's actually done, which can
take longer than API Gateway's 29s integration timeout under lock
contention. `lambdas/cancel_execution.py` splits into `api_handler`
(records `CANCELLING`, asynchronously invokes the worker, returns 202 -
all under a second) and `worker_handler` (the actual per-party cancel
commands + final `CANCELLED` write) as two separate Lambda resources
sharing one file.

### 7. Read back: status/logs Lambdas

`GET /executions/{id}` (`get_status.py`) is a plain `ExecutionsTable` read.
`GET /executions/{id}/logs` (`get_logs.py`) has no per-job log stream to
look up (the containers log continuously across every execution), so it
finds each node's *current* stream via `DescribeLogStreams`
latest-by-event-time, then filters by `FilterLogEvents`'
`startTime`/`endTime` bounded to this execution's `created_at`/`finished_at`
- a best-effort time window, since concurrently-running executions on the
same node share that one stream and stoffel-run doesn't tag lines with an
execution_id. The response also carries `created_at`/`finished_at` back out
so the `get-logs` script can name its local archive after the execution's
own start time rather than fetch time.

Archiving itself is client-side, not part of this Lambda: `./get-logs`
writes its formatted output to `logs/<timestamp>-<execution_id>.txt` on
every call, and `run-program`/`wait-for-execution` call it automatically
once an execution reaches a terminal state - mirroring
`../stoffel-ec2-cross-region-deployment` and
`../stoffel-docker-compose-one-off`'s `logs/<RUN_TS>.txt` snapshot of a
finished run, adapted for many concurrently-in-flight executions (hence the
execution_id suffix, which those single-run-at-a-time deployments don't
need).

### 8. API Gateway + auth

Identical shape to the one-off deployment: one `RestApi`, one default API
key + usage plan (rate limit 5 rps / burst 10 / 1000 req/day), `POST
/executions`, `GET /executions/{id}`, `GET /executions/{id}/logs`, `POST
/executions/{id}/cancel`, `GET /programs`, all `api_key_required=True`.
`./add-api-key <username>` / `./get-api-key [username]` /
`./revoke-api-key <username>` manage additional keys directly via the AWS
CLI, no redeploy needed - unchanged from the one-off deployment.

## End-to-end execution lifecycle

1. Operator: `cdk deploy` → `./upload-ids` → `./upload-program aes` →
   `./deploy`. Mesh is up, `aes` is admissible.
2. User: `POST /executions {"program_name": "aes", "n_clients": 2}` →
   `submit_execution.py` resolves `aes` → `program_id`, writes
   `ExecutionsTable[execution_id].status = PREPARING`, starts a state
   machine execution, returns `202 {execution_id, status: PREPARING}`.
3. State machine: `AdmitExecution` publishes `prepare` to every party,
   waits for acks, writes `status = RUNNING` + `endpoints`.
4. State machine: `WaitPoll(10s)` → `CheckExecutionStatus` loop until every
   party's `completed`/`failed`/`cancelled` event appears.
5. State machine: `FinishExecution` best-effort cancels (if not
   `SUCCEEDED`) and writes the final status.
6. User: `GET /executions/{id}` any time for status/endpoints; runs their
   own MPC client (or `./run-client ... --program aes --execution-id
   <id>`) against the party/coordinator endpoints once `RUNNING`; `GET
   /executions/{id}/logs` and `POST /executions/{id}/cancel` as needed.

## Key design decisions

- **No queue, no cluster lock.** The one-off deployment needs both because
  the container *is* the job. Here the mesh already supports concurrent
  executions, so the only thing worth serializing is delivering a control
  command to one party (see "Admit" above) - a few seconds, not a job's
  entire runtime.
- **Program staging is operator-only, deliberately.** Not a limitation of
  what could be built (S3 presign + a content-addressed key would work
  technically), but a product decision: letting any user's upload force a
  mesh-wide restart that interrupts every other in-flight execution isn't
  something a shared multi-tenant deployment should hand to every caller.
- **DynamoDB conditional updates, not a distributed lock service.** The
  per-party lease in `party_control.py` reuses the same conditional-write
  primitive the one-off deployment already used for its (much
  coarser-grained, now-removed) cluster lock - same mechanism, much smaller
  scope.
- **`get-results`/`ping-matrix` don't exist here.** Both depended on the
  one-off deployment's containers pinging peers and printing benchmark
  markers right before exiting - there is no "container exits" event in
  standing mode to hang that on.

## Where things live

| Concern | File |
|---|---|
| Infra (VPC, instances, tables, Lambdas, state machine, API) | `app.py` |
| Standing-control SSM/DynamoDB-lease helpers | `lambdas/party_control.py` |
| Submit / admit / poll / finish / cancel Lambdas | `lambdas/*.py` |
| Start/restart the standing mesh | `deploy` |
| Stage a program (operator) | `upload-program` |
| Sync identity certs/keys (operator) | `upload-ids` |
| Direct debug run, bypassing the API (operator) | `run` |
| Submit/poll/fetch logs/cancel (users) | `run-program`, `get-execution-status`, `wait-for-execution`, `get-logs`, `cancel-execution`, `list-programs` |
| Run an MPC client against a live execution | `run-client` |
