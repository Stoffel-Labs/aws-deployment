# Architecture

This document explains how `StoffelEc2UserDeploymentStack` (`app.py`) is put
together and why. For usage instructions see [README.md](README.md).

## Overview

The stack has two layers, same split as `../stoffel-fargate-user-deployment`:

1. **Compute layer** - an MPC cluster (one coordinator + N party EC2
   instances), all in one VPC. Unlike the Fargate deployment, instances are
   long-lived: `cdk deploy` creates them once and they stay running,
   whether or not a job is active.
2. **User-facing layer** - an API, queue, and orchestration pipeline that
   lets an external user submit a program and run it on that cluster via
   HTTPS + an API key, with no AWS credentials and no risk of colliding
   with another user's run.

The layers are connected by three short Lambda functions - `StartJob`,
`CheckJobStatus`, `FinishJob` - driven by a Step Functions poll loop, in
place of the Fargate deployment's single long-running orchestrator task.

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
                       │   Step Functions: AcquireLock (retry/wait)                 │
                       │        │                                                  │
                       │        ▼                                                  │
                       │   StartJob (Lambda) ──► SSM RunCommand ──► docker run -d   │
                       │        │                  on coordinator + every party     │
                       │        ▼                  instance, in parallel            │
                       │   Wait(10s) ⟲ CheckJobStatus (Lambda, SSM docker inspect)  │
                       │        │  loops while RUNNING                              │
                       │        ▼                                                  │
                       │   FinishJob (Lambda) ──► SSM: stop coordinator container   │
                       │        │                  + record SUCCEEDED/FAILED        │
                       │        ▼                                                  │
                       │   ReleaseLock                                             │
                       └────────┼──────────────────────────────────────────────────┘
                                ▼
                       ┌─────────────────────────────────────────────────────────┐
                       │                      Compute layer                       │
                       │  Coordinator EC2 instance (t3.small, always-on)          │
                       │    - Elastic IP known at `cdk deploy` time               │
                       │    - each job docker-runs a fresh container on it        │
                       │                                                          │
                       │  Party0..N-1 EC2 instances (t3.small, always-on)         │
                       │    - Elastic IP + private IP known at `cdk deploy` time  │
                       │    - program pulled onto local disk via `aws s3 cp`      │
                       │    - each job docker-runs a fresh container on them      │
                       └─────────────────────────────────────────────────────────┘
                                                    ▲
                                                    │ direct TCP (party/coord RPC ports)
                                                    │
                                          external MPC client (run-client)
```

## Requirements this design satisfies

This stack keeps the two requirements the Fargate deployment already
solved - no AWS account needed by users (§6 below), and many users sharing
one serialized queue (§4) - while adding a third:

1. **Users must not need an AWS account.** Same mechanism as the Fargate
   deployment: API keys, not Cognito/IAM, and program upload via a
   presigned S3 URL rather than `aws s3 cp` with real credentials.
2. **Many users must be able to use it at once, without their jobs
   colliding.** Same mechanism too: the compute layer is a single shared
   cluster with fixed RPC ports, so it can't run two jobs at once. A DynamoDB
   lock (`AcquireLock`/`ReleaseLock` in the state machine) serializes actual
   execution one job at a time; the SQS FIFO queue only orders submissions.
3. **Minimize the time between "job submitted" and "nodes running."** This
   is the requirement that makes this stack different from a plain
   find-and-replace port of the Fargate deployment onto EC2. Two design
   choices exist purely to serve it:
   - **Persistent, addressed-in-advance compute.** A Fargate task's ENI
     (and therefore its IP) doesn't exist until the task starts - the
     Fargate deployment has to launch a task, wait for `RUNNING`, look up
     its ENI's public/private IP, register it in Cloud Map, and then wait
     for DNS to propagate before any dependent node can use that address.
     None of that exists here: every instance and its Elastic IP are
     created once by `cdk deploy`, so every address is a plain
     CloudFormation output before any job ever runs (see
     `stoffel-ec2-cross-region-deployment`, which pioneered this pattern
     for the same reason). A job just runs a container on an
     already-running instance.
   - **No Fargate orchestrator.** Reusing the Fargate deployment's
     "one orchestrator task per job, invoked via `EcsRunTask.sync`" pattern
     here would reintroduce exactly the latency the compute-layer change
     just removed - a Fargate task still needs ENI attachment and an image
     pull before it does anything. Instead, `StartJob`/`CheckJobStatus`/
     `FinishJob` are ordinary Lambda functions, invoked directly by Step
     Functions. Lambda cold starts are low-single-digit seconds at worst,
     and a warm one is much faster still - either way, far below a Fargate
     task's launch time. The "waiting for the job to finish" part (which
     can legitimately take much longer, up to the state machine's 2h
     timeout) happens in a Step Functions `Wait` state polling
     `CheckJobStatus`, not inside a long-running Lambda - so a slow job
     costs nothing extra in orchestration compute while it waits, and no
     single Lambda invocation risks hitting its own timeout.

## Compute layer

Built by `_add_coordinator`/`_add_party`, deployed into a fresh VPC
(`172.32.0.0/16`, public subnets only, no NAT Gateway) - same no-NAT
reasoning as the Fargate deployment: every instance needs a public IP
anyway (coordinator/parties for external MPC clients, parties for `aws s3
cp`), so there's nothing that would need a NAT Gateway's private-subnet
routing.

- **Coordinator instance**: one `t3.small` EC2 instance running Amazon
  Linux 2023 with Docker. `_base_user_data` installs Docker, logs into ECR,
  and pre-pulls the coordinator image - all at first boot, so the very
  first real job never pays for that pull. `/etc/stoffel-env` is written
  once at boot with `STOFFEL_AUTH_TOKEN` from CDK context, consumed by every
  `docker run --env-file /etc/stoffel-env` a job or `run-nodes` issues
  later.
- **Party instances**: `num_parties` (CDK context, default 5, max 10)
  `t3.small` EC2 instances, same base setup plus `PROGRAM_S3_BUCKET` read
  access and a `/home/ec2-user/programs` directory for downloaded programs.
  Party0 is the "leader" other parties dial into, same role as in the
  Fargate deployment - just addressed by a stable private IP now instead of
  a Cloud Map DNS name.
- **Elastic IPs**: allocated independently of instance launch
  (`ec2.CfnEIP` + `ec2.CfnEIPAssociation`), one per instance. This is what
  makes every node's public address a plain CloudFormation output the
  moment `cdk deploy` finishes, before any container ever runs - see
  `stoffel-ec2-cross-region-deployment/app.py`'s docstring for the same
  pattern applied per-region.
- **Security group**: identical port layout to the Fargate deployment -
  internal gossip/bind ports (9000-9009, 10000, TCP+UDP) and ICMP ping
  restricted to the VPC CIDR; external RPC ports (31415 coordinator,
  16180-16189 parties) open to `0.0.0.0/0` so `run-client` can reach the
  cluster directly from anywhere.
- **No EFS.** The Fargate deployment uses EFS because party *tasks* are
  ephemeral and need a shared, persistent volume across runs. These
  instances are themselves persistent, so each party instance just keeps
  its own local copy of whatever program it last downloaded
  (`/home/ec2-user/programs/<basename>`, refreshed via `aws s3 cp` on every
  API-driven job) - simpler, and it's the same approach
  `stoffel-ec2-cross-region-deployment` uses for the same reason.
- **No Cloud Map.** The Fargate deployment needs Cloud Map DNS because a
  fresh task's IP isn't known until it starts. Here, every node's private
  IP is already a stack output before any job runs, so `StartJob` and
  `run-nodes` just bake plain IPs directly into each `docker run`
  invocation's environment variables - no DNS record, no propagation wait,
  no `servicediscovery:*`/Route 53 health-check IAM permissions needed at
  all.

## User-facing layer

Built by `_add_uploads_bucket`, `_add_tables`, `_add_queue`,
`_add_orchestration_lambdas`, `_add_state_machine`, `_add_pipe`, `_add_api`,
in that call order from `__init__`.

### 1. Upload: S3 + presign Lambda

Identical to the Fargate deployment (`lambdas/presign.py`, unchanged):
`POST /programs/presign` returns a presigned S3 PUT URL for a random
`uploads/<uuid>.stflb` key; the client `PUT`s its compiled program straight
to S3, sidestepping API Gateway's payload limit. Objects expire after 3
days - transient, since the party instance that downloads one keeps its own
copy afterward.

### 2. Submit: SQS + submit Lambda

Identical to the Fargate deployment (`lambdas/submit_job.py`, unchanged):
`POST /jobs` writes a `QUEUED` item to `JobsTable` and sends the same
payload to the FIFO queue `stoffel-jobs.fifo` (single message group `jobs`,
explicit `MessageDeduplicationId`). A DLQ catches anything that fails
delivery 3 times. FIFO ordering here is submission order, not execution
order - see the state machine below for why a lock is still needed.

### 3. Dispatch: EventBridge Pipe → Step Functions

Identical to the Fargate deployment's `_add_pipe`: a `CfnPipe` reads off the
queue (batch size 1) and starts one Step Functions execution per message,
`FIRE_AND_FORGET` - Pipes acks the message as soon as `StartExecution`
returns, not when the execution finishes, which is why the state machine
still needs its own lock (a burst of queued jobs would otherwise all start
executions back-to-back and race for the shared cluster). The input
template wraps the SQS body as `{"job": <$.body>}` for the same
JSON-quoting reason as the Fargate deployment.

### 4. Serialize: DynamoDB lock (unchanged shape, same reasoning)

`AcquireLock`/`WaitForLock`/`ReleaseLock`/`ReleaseLockOnFailure` are the
same `DynamoPutItem`/`DynamoDeleteItem` binary-semaphore pattern as the
Fargate deployment, on the same kind of `LockTable` (single item,
`lock_id = "jobs"`, `condition_expression="attribute_not_exists(lock_id)"`).
This is what actually enforces "only one job's coordinator+parties run at a
time" - not the SQS FIFO group, and not the Pipe.

### 5. Execute: StartJob → poll loop → FinishJob

This is the part that replaces the Fargate deployment's single orchestrator
task, split into three Lambdas because Step Functions' native `Wait` state
lets the "wait for a job to finish" part cost nothing while it waits,
instead of paying for a running process the whole time (see requirement 3
above).

- **`StartJob`** (`lambdas/start_job.py`): validates `NUM_PARTIES` against
  `2*THRESHOLD+1..DEPLOYED_PARTIES` (same bounds check as the Fargate
  orchestrator), marks the job `RUNNING`, then builds and sends one
  `AWS-RunShellScript` SSM command per node - coordinator and every active
  party, all fired without waiting on each other, since (unlike the Fargate
  deployment) no node's address depends on another node having started
  first. Each party's script `aws s3 cp`s the program onto local disk, then
  `docker rm -f`+`docker run -d`s a fresh container (`--network host`,
  `--log-driver awslogs` straight to CloudWatch, `--env-file
  /etc/stoffel-env` for the baked-in auth token). It deliberately skips
  `docker login`/`docker pull` - the image is already cached from boot, and
  `docker run` against an already-present local tag needs neither; this is
  what keeps each SSM round trip on the order of a second instead of paying
  for a registry pull on every job. `wait_all_ssm` then polls all the
  just-sent commands concurrently (bounded by the slowest single one, not
  their sum) until every container is confirmed started, before recording
  `endpoints`/`log_streams` into `JobsTable` and returning `RUNNING` -
  because every address was already known, this doesn't need the Fargate
  orchestrator's ENI lookup or DNS-propagation wait at all.
- **`CheckJobStatus`** (`lambdas/check_job_status.py`): invoked repeatedly
  by the state machine's `Wait(10s)` → `CheckJobStatus` → `Choice` loop.
  Each invocation sends a fresh, non-blocking `docker inspect` SSM command
  to every active party instance (not the coordinator, which never exits on
  its own - same reasoning as the Fargate orchestrator) and reports
  `RUNNING` until every party container has exited, then `SUCCEEDED` or
  `FAILED` based on their exit codes.
- **`FinishJob`** (`lambdas/finish_job.py`): the loop's terminal step (and
  also the shared failure-catch target for `StartJob`/`CheckJobStatus`
  themselves). Best-effort stops the coordinator's container via SSM - it
  never exits on its own, so something has to, same as the Fargate
  orchestrator's `finally: stop_task(coord_arn)` - and writes the final
  `status`/`error`/`finished_at` to `JobsTable`.

### 6. Read back: status/logs Lambdas

Identical to the Fargate deployment (`lambdas/get_status.py`,
`lambdas/get_logs.py`, unchanged) - they only ever read `JobsTable` and
CloudWatch Logs, with no ECS/EC2-specific logic to begin with. `get_logs`
works off whatever `log_streams` names were written by `StartJob`
(`coordinator/<job_id>`, `party0/<job_id>`, ...), same shape as the Fargate
deployment's `<prefix>/Container/<task-id>` naming, just keyed by job ID
instead of task ID since there's no per-run task ARN anymore.

### 7. API Gateway + auth

Identical to the Fargate deployment's `_add_api` - one `RestApi`
(`stoffel-ec2-user-api`, stage `prod`) fronting the four Lambdas, each
behind `api_key_required=True`, one shared `UsagePlan` (5 req/s steady,
burst 10, 1000/day quota), a default key created for the first user.

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
  │                                          StartJob (Lambda): validate, mark
  │                                          RUNNING, SSM RunCommand every node
  │                                          in parallel, wait for all to start,
  │                                          record endpoints/log_streams
  │                                                        │
  │                              Wait(10s) ⟲ CheckJobStatus (Lambda, SSM docker
  │                              inspect on every party) - loops while RUNNING
  │                                                        │
  │                                          FinishJob (Lambda): stop coordinator
  │                                          container, JobsTable[SUCCEEDED|FAILED]
  │                                                        │
  │                                          Step Functions: ReleaseLock
  │
  ├─ (polls) GET /jobs/{id} ──► status Lambda ──► QUEUED → RUNNING → SUCCEEDED|FAILED
  └─ GET /jobs/{id}/logs ─────► logs Lambda ──► per-node CloudWatch log events

Meanwhile, once RUNNING: user's own run-client ──(direct TCP)──► coordinator/party
                                                                   public endpoints
```

## Key design decisions

- **SQS FIFO orders submissions; a DynamoDB lock serializes execution.**
  Same split, same reasoning as the Fargate deployment - EventBridge Pipes
  acks a message as soon as the triggered Step Functions execution
  *starts*, not when it finishes, so FIFO ordering alone can't serialize
  actual runs.
- **Addressing is resolved once, at deploy time, not per job.** This is the
  single biggest structural difference from the Fargate deployment, and the
  reason a Lambda-driven poll loop is fast enough to replace a dedicated
  orchestrator task: there is no ENI lookup, no Cloud Map registration, and
  no DNS-propagation wait anywhere in the job-start path, because every
  instance's Elastic IP and private IP already exist as CloudFormation
  outputs before the first job is ever submitted.
- **The "wait for completion" step is a Step Functions `Wait` state, not a
  running process.** A job that takes an hour to finish costs the same in
  orchestration compute as one that takes a minute - `CheckJobStatus` only
  runs for the few seconds it takes to poll, once every 10 seconds, the
  whole time in between is free.
- **No `docker pull` on the job-start hot path.** Every instance pre-pulls
  its image once, at boot. `StartJob`'s per-job scripts trust that cache
  unconditionally; only the operator-facing `run-nodes` re-pulls (so
  redeploys are still picked up without replacing an instance) - a
  deliberate asymmetry between the latency-critical API path and the
  correctness-critical debugging path.

## Where things live

| Concern | File |
|---|---|
| Full stack definition (compute + user-facing layers) | `app.py` |
| Job orchestration (start/poll/finish) | `lambdas/start_job.py`, `lambdas/check_job_status.py`, `lambdas/finish_job.py` |
| API Lambdas | `lambdas/presign.py`, `lambdas/submit_job.py`, `lambdas/get_status.py`, `lambdas/get_logs.py` |
| Operator/user CLI scripts | `run-program`, `get-program-status`, `wait-for-program`, `get-logs`, `get-results`, `get-api-key`, `drain-and-stop`, `run-nodes`, `run-client`, `run-exp`, `ping-matrix`, `node-status`, `stop-nodes`, `start-nodes` |
| Compute-layer design this stack draws from | `../stoffel-ec2-cross-region-deployment` |
| User-facing layer design this stack draws from | `../stoffel-fargate-user-deployment` |
| MPC engine image source | `../StoffelVM` |
| Coordinator image source | `../stoffel-mpc-coordinator` |
