# Architecture

This document explains how `StoffelUserDeploymentStack` (`app.py`) is put together and
why. For usage instructions see [README.md](README.md).

## Overview

The stack has two layers:

1. **Compute layer** — an MPC cluster (one coordinator + N party Fargate tasks) plus
   shared storage (EFS) and service discovery (Cloud Map). This is an independent copy
   of `../stoffel-fargate-deployment`'s compute layer, minus its bastion host.
2. **User-facing layer** — an API, queue, and orchestration pipeline that lets an
   external user submit a program and run it on that cluster via HTTPS + an API key,
   with no AWS credentials and no risk of colliding with another user's run.

The two layers are connected by a single Fargate task, the **orchestrator**, which is
the only thing that ever actually starts the coordinator/party tasks in this stack. All
resources live in one CDK stack / one `Stack.__init__` in `app.py`, built up by a series
of `_add_*` helper methods.

```
                       ┌─────────────────────────────────────────────────────────┐
                       │                     User-facing layer                    │
 user's machine        │                                                           │
 (run-program, curl) ──┼─► API Gateway (API key) ─► Lambdas (presign/submit/       │
                       │        │                     status/logs)                 │
                       │        │ POST /jobs                                       │
                       │        ▼                                                  │
                       │   SQS FIFO queue (stoffel-jobs.fifo) ──► DLQ (3 retries)   │
                       │        │                                                  │
                       │        ▼ EventBridge Pipe (fire-and-forget StartExecution)│
                       │   Step Functions state machine                            │
                       │     acquire DynamoDB lock → RunTask.sync → release lock    │
                       │        │                                                  │
                       └────────┼──────────────────────────────────────────────────┘
                                ▼
                       ┌─────────────────────────────────────────────────────────┐
                       │                      Compute layer                       │
                       │  Orchestrator Fargate task                               │
                       │    - downloads program S3 → EFS                          │
                       │    - ecs:RunTask coordinator + N parties                 │
                       │    - registers each in Cloud Map, waits RUNNING → STOPPED│
                       │    - writes status/endpoints/log streams to JobsTable    │
                       │                                                          │
                       │  Coordinator task ── Cloud Map (*.stoffel-coord.local) ──│─ Party
                       │  Party 0..N-1 tasks (mount EFS read-only for the program)│  tasks
                       └─────────────────────────────────────────────────────────┘
                                                    ▲
                                                    │ direct TCP (party/coord RPC ports)
                                                    │
                                          external MPC client (run-client)
```

## Requirements this design satisfies

This stack turns `stoffel-fargate-deployment`'s operator-driven model — a bastion host
and `run-nodes`/`run-client` invoked by whoever holds AWS credentials for the
account — into a self-service one. Three requirements drove the two-layer split above:

1. **Users must not need an AWS account.** Everything an external user touches is
   HTTPS + an API key (§7, "API Gateway + auth") — never IAM credentials, never
   console access. This is why auth is API keys rather than Cognito/IAM: Cognito would
   still mean the user authenticates *as* an AWS-adjacent identity, whereas an API key
   is just an opaque string an operator hands out of band. It's also why program
   upload goes through a presigned S3 URL (§1) instead of `aws s3 cp` — the user never
   needs credentials scoped to the bucket, just the URL the presign Lambda hands back.
2. **Many users must be able to use it at once, without their jobs colliding.** The
   compute layer (coordinator + parties) is a single shared cluster with fixed Cloud
   Map DNS names and fixed RPC ports — it cannot run two jobs at once. Rather than
   provisioning a cluster per user (expensive, slow to spin up) or rejecting concurrent
   submissions outright, the user-facing layer lets any number of users submit
   concurrently and queues the work: the SQS FIFO queue accepts submissions from
   everyone and preserves their order, and the Step Functions DynamoDB lock (§4)
   serializes actual execution one job at a time, queueing the rest instead of racing
   them or erroring out. From a user's perspective they just call `POST /jobs` and
   poll `GET /jobs/{id}`; the fact that another user's job runs first is invisible
   except as wait time.
3. **Programs execute on cloud nodes while the client stays offline (MPC-as-a-service).**
   The coordinator and party tasks *are* the MPC nodes, and they run entirely inside
   AWS on infrastructure the user never provisions, sizes, or logs into — the user
   supplies a compiled program and the parameters (`num_parties`, `threshold`, ...) and
   the cluster does the rest, unlike a self-hosted setup where the user would run some
   or all parties on their own machines. Submission is also decoupled from execution
   in time: `POST /jobs` returns as soon as the job is queued (§2), and the
   EventBridge Pipe → Step Functions dispatch (§3) runs fire-and-forget from the
   client's point of view, so the user's machine does not need to stay connected while
   the orchestrator brings up the coordinator/parties and waits for them to finish
   (§5) — it can disconnect and later poll `GET /jobs/{id}` and `/logs` (§6) for the
   outcome. The one point where the user's machine *does* talk directly to the
   cluster is the actual MPC protocol itself: once a job is `RUNNING`, `run-client`
   connects over the open RPC ports (coordinator 31415, parties 16180-16189) to submit
   inputs and collect outputs — that direct link is inherent to MPC as a protocol, not
   a gap in the "offline client" model, since results are just as out-of-scope for
   this stack to capture as they are for `run-nodes`/`run-client` in the original
   operator-driven deployment.

## Compute layer

Built by `_add_coordinator` / `_add_party`, deployed into a fresh VPC (`172.29.0.0/16`,
public subnets only, no NAT Gateway).

- **VPC, no NAT**: every task (coordinator, parties, orchestrator) runs in a public
  subnet with its own public IP. Coordinator/parties need a public IP anyway so external
  MPC clients can reach them directly; the orchestrator only makes outbound AWS API
  calls, which a public subnet already routes for free via the Internet Gateway. An S3
  Gateway VPC Endpoint (`vpc.add_gateway_endpoint`) additionally keeps the
  orchestrator's program downloads off the public internet path and off S3
  data-processing billing.
- **ECS Cluster + Cloud Map namespace** (`stoffel-coord.local`): every coordinator/party
  task registers a DNS `A` record here (`coordinator.stoffel-coord.local`,
  `party0.stoffel-coord.local`, ...) so parties can find each other and the coordinator
  without hardcoding IPs. TTL is 10s and health checks are custom (`failure_threshold=1`)
  because registration happens dynamically per run, not via an ECS Service.
- **Coordinator task** — one Fargate task definition (512 CPU / 1024 MiB), the
  off-chain coordinator image built from `../stoffel-mpc-coordinator`. Its entrypoint
  wraps the coordinator binary so that, after the binary exits, it pings every party for
  RTT measurement before the container itself exits.
- **Party tasks** — `N_PARTIES` (10) Fargate task definitions (512 CPU / 1024 MiB each),
  built from `../StoffelVM`'s `Dockerfile.benchmark-flexible`. Party 0 is the "leader"
  and the bootstrap point other parties dial into
  (`party0.stoffel-coord.local:9000`); it also gets a health check
  (`netstat` for its bind port) since other parties depend on it being reachable
  first. Each party mounts the EFS volume read-only at `/app/programs`.
  `STOFFEL_PROGRAM` is deliberately left out of each task definition's environment —
  it's injected as a container override per run (by `run-nodes` for operator-driven
  runs, or by the orchestrator for API-driven runs), since the program differs per job.
- **Security group**: internal gossip/bind ports (9000-9009, 10000, TCP+UDP) and ICMP
  ping are restricted to the VPC CIDR; external RPC ports (31415 coordinator,
  16180-16189 parties) are open to `0.0.0.0/0` so a user's `run-client` can reach the
  cluster directly from anywhere.
- **EFS** (`ProgramsFs`, `RemovalPolicy.RETAIN`): the shared volume holding uploaded
  `.stflb` program files, mounted read-only into every party container and read-write
  into the orchestrator. It's the *only* place party containers ever read a program
  from — there is no bastion in this stack, so the only way to get a file onto EFS is
  through the API (presign → S3 → orchestrator copies it over).

This layer is unmodified in spirit from `stoffel-fargate-deployment`; the only
subtraction is the bastion EC2 instance (and the `0.0.0.0/0` SSH ingress that came with
it), since program upload here goes exclusively through the API path instead.

## User-facing layer

Built by `_add_uploads_bucket`, `_add_tables`, `_add_queue`, `_add_orchestrator`,
`_add_state_machine`, `_add_pipe`, `_add_api`, in that call order from `__init__`.

### 1. Upload: S3 + presign Lambda

`POST /programs/presign` (`lambdas/presign.py`) returns a presigned S3 PUT URL for a
random `uploads/<uuid>.stflb` key in `UploadsBucket`. The client then `PUT`s its
compiled program straight to S3. This sidesteps API Gateway's request payload limit —
the program bytes never pass through Lambda or API Gateway. `UploadsBucket` blocks all
public access, enforces SSL, and expires objects after 3 days (they're transient —
once the orchestrator copies a program onto EFS, the S3 copy has no further purpose).

### 2. Submit: SQS + submit Lambda

`POST /jobs` (`lambdas/submit_job.py`) takes the `program_key` from step 1 plus
optional params (`num_parties`, `threshold`, `backend`, `curve`, `n_inputs` — defaults
mirror `run-nodes`), writes a `QUEUED` item to `JobsTable` keyed by a fresh `job_id`,
and sends the same payload to `stoffel-jobs.fifo`. It's a FIFO queue with a single
message group (`"jobs"`) so submissions are strictly ordered, and
`content_based_deduplication=False` with an explicit `MessageDeduplicationId=job_id`.
A DLQ (`stoffel-jobs-dlq.fifo`) catches anything that fails delivery 3 times.

**FIFO ordering is not the same as serialized execution** — see the state machine
below for why a lock is still needed.

### 3. Dispatch: EventBridge Pipe → Step Functions

An `aws_pipes.CfnPipe` (`_add_pipe`) reads off the SQS queue (batch size 1) and starts
one Step Functions execution per message. The invocation type is
`FIRE_AND_FORGET`: Pipes acks the SQS message as soon as `StartExecution` returns, not
when the execution finishes. This is deliberate (Standard-workflow executions are
async by nature) but it's also *why* the state machine needs its own locking — without
it, a burst of queued jobs would all start executions back-to-back and race for the
shared coordinator/party cluster.

The input template wraps the SQS body as `{"job": <$.body>}`; a bare `<$.body>`
would strip the JSON object's quoting and produce invalid JSON, so the wrap is required
for a JSON-shaped target like Step Functions.

### 4. Serialize + run: Step Functions state machine

`_add_state_machine` builds a Standard workflow (2h timeout):

1. **UnwrapInput** — the Pipe always hands Step Functions an array-wrapped payload
   (`[{"job": {...}}]`) even for a single record; this `Pass` state unwraps `$[0]` so
   later states can address `$.job.*` directly.
2. **AcquireLock** — `DynamoPutItem` into `LockTable` with
   `condition_expression="attribute_not_exists(lock_id)"`, on a single well-known item
   (`lock_id = "jobs"`). This is a binary semaphore: only one execution can ever
   successfully put that item at a time. On `ConditionalCheckFailedException`, the
   execution waits 15s (`WaitForLock`) and retries, looping until the lock is free.
3. **RunOrchestrator** — `EcsRunTask` with `IntegrationPattern.RUN_JOB` (i.e.
   `RunTask.sync`), launching the orchestrator task with the job's fields
   (`JOB_ID`, `PROGRAM_S3_KEY`, `NUM_PARTIES`, `THRESHOLD`, `BACKEND`, `CURVE`,
   `N_INPUTS`) injected as container environment overrides. Because it's a `.sync`
   integration, this state — and the whole execution — blocks until the orchestrator
   task exits, not just until it starts.
4. **ReleaseLock** — `DynamoDeleteItem` removes the lock item once the orchestrator
   task finishes successfully. On any error in step 3 (`States.ALL`), the execution
   instead goes through **ReleaseLockOnFailure** (same delete) before failing, so a
   crashed job never leaves the cluster permanently locked.

This is what actually enforces "only one job's coordinator+parties run at a time" —
not the SQS FIFO group, and not the Pipe. The queue guarantees submission order; the
lock guarantees mutual exclusion of execution.

### 5. Execute: the orchestrator task

`orchestrator/orchestrator.py`, run as a one-shot Fargate task (256 CPU / 512 MiB)
under the state machine's `RunTask.sync`. It is effectively `run-nodes` and
`get-program-status`-style polling ported to boto3, minus a human at a terminal:

1. Marks the job `RUNNING` in `JobsTable`, validates `NUM_PARTIES` against
   `2*THRESHOLD + 1 .. DEPLOYED_PARTIES`.
2. Downloads the program from S3 onto the shared EFS mount.
3. Cleans up any stale Cloud Map registrations left over from a previous failed run
   for the same service (each run gets a new instance ID, so old dead registrations
   would otherwise coexist with the new one and cause flaky DNS).
4. Starts the coordinator task, waits for it to reach `RUNNING`, registers it in Cloud
   Map, and *actively polls DNS resolution* before proceeding — Cloud Map registration
   succeeding doesn't mean the record has propagated yet, and party containers dial
   their bootstrap host immediately at startup with no retry.
5. Starts party0 the same way (register + wait for DNS), then starts parties 1..N-1 in
   parallel.
6. Records `endpoints` (public IP:port per node) and `log_streams` (deterministic
   `awslogs` stream names, `<prefix>/Container/<task-id>`) into `JobsTable` — this is
   what `GET /jobs/{job_id}` and `/logs` later read.
7. Waits for the *party* tasks (not the coordinator, which never exits on its own) to
   reach `STOPPED`, checks their exit codes, and marks the job `SUCCEEDED` or `FAILED`
   accordingly.
8. In a `finally` block, always stops the coordinator task — it's long-running and
   would otherwise leak as a permanently-billing task regardless of how the job ended.

The orchestrator's task role is scoped narrowly: `ecs:RunTask` only on the specific
coordinator/party task definition ARNs (not `*`), read on the uploads bucket,
read-write on `JobsTable`, plus the Cloud Map/Route53-health-check/ENI-describe
permissions `register_instance`/`deregister_instance` need under the hood (Cloud Map's
`RegisterInstance` creates a Route 53 health check as the *calling* identity, so the
orchestrator's own role needs those permissions, not just `servicediscovery:*`). It
also needs `iam:PassRole` granted from each coordinator/party task's execution and task
roles, since `ecs:RunTask` requires the caller be able to pass those roles.

### 6. Read back: status/logs Lambdas

- `GET /jobs/{job_id}` (`get_status.py`) returns status, timestamps, `endpoints`, and
  `error` from `JobsTable`. No MPC results are returned — result capture is out of
  scope for this stack; the user runs their own `run-client` against `endpoints`.
- `GET /jobs/{job_id}/logs` (`get_logs.py`) reads the recorded `log_streams` and
  fetches each via `logs:GetLogEvents`, grouped by node. Available once the job is
  `RUNNING` (streams are recorded at the same point as `endpoints`), though incomplete
  until the job finishes.

### 7. API Gateway + auth

A single `RestApi` (`stoffel-user-api`, stage `prod`) fronts all four Lambdas, each
behind `api_key_required=True`. One `UsagePlan` (5 req/s steady, burst 10, 1000/day
quota) is shared across API keys; `_add_api` CDK-manages one default key/plan pair
(its `UsagePlanId` is a stack output) so `cdk deploy` always has a working key on a
fresh stack. Onboarding more users does *not* need a redeploy: `./add-api-key
<username>` creates a key named `stoffel-<username>` and attaches it to that same
usage plan directly via `aws apigateway create-api-key` / `create-usage-plan-key`;
`./revoke-api-key <username>` deletes one (see README's "Onboarding additional
users"). This is deliberately API keys rather than Cognito/IAM — external users need
no AWS account or credentials, just the two values (`ApiUrl`, key value) the operator
hands them out of band.

## End-to-end job lifecycle

```
run-program
  │
  ├─ POST /programs/presign ──► presign Lambda ──► S3 presigned PUT URL
  ├─ PUT program bytes ───────► S3 (uploads/<uuid>.stflb)
  ├─ POST /jobs ───────────────► submit Lambda ──► JobsTable[QUEUED] + SQS FIFO
  │                                                        │
  │                                          EventBridge Pipe (fire-and-forget)
  │                                                        ▼
  │                                     Step Functions: AcquireLock (retry/wait)
  │                                                        │
  │                                          EcsRunTask.sync: orchestrator task
  │                                                        │
  │                              orchestrator: S3→EFS, RunTask coord+parties,
  │                              Cloud Map register + DNS wait, JobsTable[RUNNING,
  │                              endpoints, log_streams] → wait party STOPPED →
  │                              JobsTable[SUCCEEDED|FAILED] → stop coordinator
  │                                                        │
  │                                          Step Functions: ReleaseLock
  │
  ├─ (polls) GET /jobs/{id} ──► status Lambda ──► QUEUED → RUNNING → SUCCEEDED|FAILED
  └─ GET /jobs/{id}/logs ─────► logs Lambda ──► per-node CloudWatch log events

Meanwhile, once RUNNING: user's own run-client ──(direct TCP)──► coordinator/party
                                                                   public endpoints
```

## Running a program via the scripts

The scripts in the repo root are thin `curl`/boto3 wrappers around the API and AWS
primitives described above — this section maps each one to the pipeline stage it
drives. For flag/env-var reference see README's "Scripts" table; this is about *why*
each script does what it does, in terms of the components already introduced.

- **`get-api-key [username]`** / **`add-api-key <username>`** / **`revoke-api-key
  <username>`** — operator-only. `add-api-key` creates a new key against the usage
  plan from §7 and prints its value (no redeploy); `get-api-key` re-prints an
  existing key (the CDK-managed default with no argument, or a user's by name);
  `revoke-api-key` deletes one. All need AWS credentials. The operator hands
  `ApiUrl` + a key value to each user out of band; nothing after that needs AWS
  credentials.
- **`run-program <file.stflb> [opts]`** — drives steps 1-2 end to end: calls
  `POST /programs/presign` (§1) to get a presigned PUT URL, `PUT`s the file straight to
  S3, then `POST /jobs` (§2) to enqueue it. It then polls `GET /jobs/{id}` every 5s
  *only* while the status is `QUEUED` — once the state machine's `AcquireLock` succeeds
  and the orchestrator starts (§3-§5), the job leaves `QUEUED` and the script exits,
  printing the endpoints if it reached `RUNNING`. It does not wait for the job to
  finish; that's deliberate; see requirement 3 in "Requirements this design satisfies"
  regarding the client not needing to stay connected.
- **`get-program-status <job_id>`** — a single `GET /jobs/{id}` call (§6). Useful any
  time after submission, including while still `QUEUED` (waiting on the lock) or
  `RUNNING` (waiting on the party tasks to reach `STOPPED`).
- **`wait-for-program <job_id>`** — the same call as `get-program-status`, looped every
  5s until the orchestrator writes `SUCCEEDED` or `FAILED` (§5 step 7). Use this instead
  of `run-program`'s built-in polling when you want to block past `RUNNING`.
- **`get-logs <job_id> [output-file]`** — calls `GET /jobs/{id}/logs` (§6), which reads
  the `log_streams` the orchestrator recorded in `JobsTable` at the same point it
  recorded `endpoints` (§5 step 6). It works as soon as the job is `RUNNING`, but the
  output is necessarily partial until the job reaches a terminal state, since the nodes
  are still writing to those streams.
- **`run-nodes <file.stflb>` / `run-client <id> [inputs]`** — operator-only, bypass the
  API entirely and drive the compute layer (§Compute layer) directly with AWS
  credentials, the same way they would in `stoffel-fargate-deployment`. There's no
  upload step here: since this stack has no bastion, the *only* way a program gets onto
  EFS is through the API path above (presign → S3 → orchestrator copy), even for a
  program you intend to run manually with `run-nodes`.
- **`drain-and-stop`** — operator-only emergency stop: purges the SQS queue, stops any
  running orchestrator/coordinator/party tasks, deletes the `LockTable` lock item, and
  aborts in-flight Step Functions executions. Affects every user's in-flight job, not
  just one; it exists because the lock (§4) has no other way to be force-cleared short
  of waiting out a stuck execution.

## Key design decisions

- **SQS FIFO orders submissions; a DynamoDB lock serializes execution.** These are two
  different guarantees and the queue alone can't provide the second one, because
  EventBridge Pipes acks a message (and therefore lets the *next* one dispatch) as soon
  as the triggered Step Functions execution *starts* — not when it finishes. Only one
  job's coordinator+parties can run at a time regardless, since the shared cluster uses
  fixed ports/DNS names — true parallel execution isn't supported.
- **`RunTask.sync` end-to-end.** Both the state machine's ECS integration and the
  orchestrator's own waiters block until the underlying task/tasks actually stop, not
  just start. This is what lets a simple "acquire lock, run, release lock" state
  machine correctly serialize jobs without extra polling machinery.

## Where things live

| Concern | File |
|---|---|
| Full stack definition (compute + user-facing layers) | `app.py` |
| Orchestrator (per-job launch/monitor/report) | `orchestrator/orchestrator.py`, `orchestrator/Dockerfile` |
| API Lambdas | `lambdas/presign.py`, `lambdas/submit_job.py`, `lambdas/get_status.py`, `lambdas/get_logs.py` |
| Operator/user CLI scripts | `run-program`, `get-program-status`, `wait-for-program`, `get-logs`, `get-api-key`, `add-api-key`, `revoke-api-key`, `drain-and-stop`, `run-nodes`, `run-client`, `run-exp` |
| Compute layer this stack copies from | `../stoffel-fargate-deployment` |
| MPC engine image source | `../StoffelVM` |
| Coordinator image source | `../stoffel-mpc-coordinator` |
