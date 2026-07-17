# Stoffel Fargate User Deployment

Runs a StoffelVM MPC cluster on AWS ECS Fargate with an off-chain coordinator, plus a
self-service API layer so external users can submit and run programs without AWS
credentials of their own and without racing each other for the shared cluster.

This is an independent copy of `../stoffel-fargate-deployment` (same compute layer:
coordinator + party task definitions, EFS, Cloud Map) with a user-facing layer added on
top — not a stack that cross-references the original deployment. Unlike
`stoffel-fargate-deployment`, there is no bastion here: program upload happens
exclusively through the API (presigned S3 URL + orchestrator), since the orchestrator
already mounts EFS read-write to do that. Dropping the bastion also removes an
always-on ~$9-10/month EC2 instance and an SSH port open to `0.0.0.0/0`.

## Architecture

**Compute layer** (unchanged from `stoffel-fargate-deployment`, minus the bastion):
- **Coordinator** and **party nodes** — Fargate tasks, one run per job
- **EFS** — shared volume for `.stflb` program files, mounted at `/app/programs/`
- **Cloud Map** — internal DNS (`*.stoffel-coord.local`) for party-to-party discovery

**User-facing layer** (new):
- **S3 uploads bucket** — users upload compiled programs via a presigned URL (avoids
  API Gateway's payload limit); objects expire after 3 days. An S3 Gateway VPC Endpoint
  keeps the orchestrator's downloads free of data-processing charges.
- **API Gateway** (API-key auth) — `POST /programs/presign`, `POST /jobs`,
  `GET /jobs/{job_id}`, `GET /jobs/{job_id}/logs`
- **DynamoDB `JobsTable`** — one item per job: status, params, endpoints, log stream
  names, error
- **SQS FIFO queue** (`stoffel-jobs.fifo`) — incoming job requests, single message group
  for submission ordering, with a dead-letter queue after 3 failed deliveries
- **EventBridge Pipe** — triggers one Step Functions execution per queued job
  (fire-and-forget `StartExecution`, no Lambda in this hop)
- **Step Functions state machine** — acquires a binary lock in DynamoDB
  (`LockTable`) before running a job and releases it after, so only one job's
  coordinator+parties ever run at a time. This is necessary because Pipes acks the SQS
  message as soon as the execution *starts*, not when it finishes — FIFO ordering alone
  does not serialize execution.
- **Orchestrator Fargate task** — the actual per-job work: downloads the program from
  S3 onto EFS, starts the coordinator + N party tasks (mirroring `run-nodes`), waits for
  them to reach `RUNNING`, waits for them to reach `STOPPED` (the run actually
  finishing), and reports status/endpoints to DynamoDB. Invoked via Step Functions' ECS
  `RunTask.sync` integration, so the state machine — and therefore the lock — doesn't
  release until the run truly stops.

Users still run their own MPC client (`run-client`) against the party endpoints
returned by the job-status endpoint. This deployment does not run the client role or
return captured results on a user's behalf.

## Prerequisites

Same as `stoffel-fargate-deployment`:
- AWS CLI configured (`aws configure`)
- CDK bootstrapped (`cdk bootstrap`)
- Python virtualenv activated (`source .venv/bin/activate`)
- StoffelVM built locally (`cargo build --release` in `../StoffelVM/`) — only needed for
  the operator-driven `run-client` workflow, not for the API
- Identity files in `../ids/`
- Docker running locally (for building the coordinator/party/orchestrator image assets)

## First-time setup

```sh
cdk deploy              # provisions the full stack: compute layer + API/queue/orchestration layer
```

`cdk deploy` prints `ApiUrl` and `ApiKeyId` in its outputs. As the operator, fetch the
key value (requires your AWS credentials) and hand `ApiUrl` + the key value to the user
out of band (Slack, email, etc.) - from that point on, they need neither:

```sh
./get-api-key
```

## User workflow (API-driven, no AWS account needed)

Needs only the two values given to you by the operator - no AWS CLI, no credentials:

```sh
API_URL=<ApiUrl> API_KEY=<api-key-value> ./run-program client_mul.stflb --num-parties 5 --threshold 1
```

This presigns an upload, PUTs the file to S3, submits the job, and prints a message
every 5s while it's `QUEUED`. It exits as soon as the job is no longer `QUEUED` -
normally that means `RUNNING` (printing the party/coordinator endpoints), but it
also exits immediately if the job reaches a terminal state before ever running.

Job status values: `QUEUED` → `RUNNING` → `SUCCEEDED` | `FAILED`. Three more scripts
cover what happens after `run-program` exits at `RUNNING`:

```sh
./get-program-status <job_id>   # one-shot status check
./wait-for-program <job_id>     # polls every 5s, printing each status, until it
                                 # reaches SUCCEEDED or FAILED
./get-logs <job_id>             # fetch coordinator/party logs, readable format
```

`get-logs` works once the job has reached `RUNNING` (log stream names are recorded at
the same time as `endpoints`), though the output is partial/incomplete until the job
actually finishes.

## Operator workflow (manual node/client control)

`run-nodes`/`run-client` still work directly against this stack's outputs for manually
starting nodes or running a client against them - useful for debugging or re-running
against a file already on EFS. There is no standalone upload step anymore: getting a
program onto EFS in the first place requires going through the API (presign + submit),
same as any other user, since there's no bastion here.

```sh
./run-nodes client_mul.stflb   # client_mul.stflb must already be on EFS (via the API)
./run-client 0 "input_value"
```

## Onboarding additional users

Each user should get their own API key so usage/quota can be tracked and revoked
independently. Keys are created directly against the stack's usage plan via the
AWS CLI - no `cdk deploy` needed:

```sh
./add-api-key alice      # creates + prints alice's key, attaches it to the usage plan
./get-api-key alice      # re-print it later
./revoke-api-key alice   # delete it
```

## Scripts

| Script | Description |
|---|---|
| `get-api-key [username]` | Print an API key's value - the stack's default key with no argument, or a user's key by name (operator use, needs AWS credentials) |
| `add-api-key <username>` | Create a new API key for a user and attach it to the usage plan, no redeploy needed (operator use, needs AWS credentials) |
| `revoke-api-key <username>` | Delete a user's API key, immediately invalidating it (operator use, needs AWS credentials) |
| `run-program <file.stflb> [opts]` | Presign upload, upload to S3, submit the job, print a message every 5s while QUEUED, exit once it's RUNNING (or a terminal state). User-facing - needs only `API_URL`/`API_KEY` env vars, no AWS credentials |
| `get-program-status <job_id>` | One-shot status check for a job. Same env vars as `run-program` |
| `wait-for-program <job_id>` | Poll a job every 5s, printing each status, until SUCCEEDED or FAILED. Same env vars as `run-program` |
| `get-logs <job_id> [output-file]` | Fetch a job's logs and pretty-print them (grouped by node, one line per message) instead of the raw JSON response. Same env vars as `run-program` |
| `drain-and-stop` | Emergency stop: purge the queue, stop all running tasks/executions, release the lock. Runs immediately, no confirmation. Affects every user. |
| `run-nodes <file.stflb>` | Start party 0, register it with Cloud Map, then start the rest (operator use; file must already be on EFS via the API) |
| `run-client <id> [inputs]` | Run a client locally against deployed nodes |
| `run-exp <file.stflb> [n]` | Run `n` benchmark iterations, write results to `results.csv` |

## CDK context

```sh
cdk deploy --context auth_token=<token>
```

## Notes

- The compute layer (coordinator, parties, EFS) is a full independent copy of
  `stoffel-fargate-deployment`, not a cross-stack reference — the two can be deployed
  side by side without conflicting. This stack has no bastion, unlike
  `stoffel-fargate-deployment` — program upload is API-only here.
- Only one job runs at a time, matching the current one-program-after-another usage
  pattern. Concurrent execution would require per-job isolated compute (fixed ports/DNS
  names in the shared cluster make true parallelism a bigger lift) — not implemented.
- Uploaded program objects in S3 expire after 3 days; the orchestrator's own copy lives
  on EFS same as an operator-uploaded program would.
