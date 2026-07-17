# Stoffel EC2 User Deployment

Runs a StoffelVM MPC cluster on EC2 instances with a self-service API
layer so external users can submit and run programs without AWS
credentials of their own and without racing each other for the shared cluster.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design and rationale.

## User workflow (API-driven, no AWS account needed)

Needs only the two values given to you by the operator - no AWS CLI, no
credentials:

```sh
API_URL=<ApiUrl> API_KEY=<api-key-value> ./run-program aes.stflb --num-parties 5 --threshold 1 --input-total 0
```

This presigns an upload, PUTs the file to S3, submits the job, and prints a
message every 5s while it's `QUEUED`. It exits as soon as the job is no
longer `QUEUED` - normally that means `RUNNING` (printing the party/
coordinator endpoints), but it also exits immediately if the job reaches a
terminal state before ever running.

Job status values: `QUEUED` → `RUNNING` → `SUCCEEDED` | `FAILED`. Three more
scripts cover what happens after `run-program` exits at `RUNNING`:

```sh
./get-program-status <job_id>   # one-shot status check
./wait-for-program <job_id>     # polls every 5s, printing each status, until it
                                 # reaches SUCCEEDED or FAILED
./get-logs <job_id>             # fetch coordinator/party logs, readable format
./get-results <job_id>          # wait for the job to finish, then aggregate
                                 # its ping RTTs + benchmark timings into a
                                 # CSV (results.csv by default)
```

`get-logs` works once the job has reached `RUNNING` (log stream names are
recorded at the same time as `endpoints`), though the output is
partial/incomplete until the job actually finishes.

## Scripts

| Script | Description |
|---|---|
| `run-program <file.stflb> [opts]` | Presign upload, upload to S3, submit the job, print a message every 5s while QUEUED, exit once it's RUNNING (or a terminal state). User-facing - needs only `API_URL`/`API_KEY` env vars, no AWS credentials |
| `get-program-status <job_id>` | One-shot status check for a job. Same env vars as `run-program` |
| `wait-for-program <job_id>` | Poll a job every 5s, printing each status, until SUCCEEDED or FAILED. Same env vars as `run-program` |
| `get-logs <job_id> [output-file]` | Fetch a job's logs and pretty-print them (grouped by node, one line per message) instead of the raw JSON response. Same env vars as `run-program` |
| `get-results <job_id> [output-csv]` | Wait for a job to reach a terminal state, then aggregate its ping RTTs and BENCH_PP_SECS/BENCH_EXEC_SECS benchmark lines into an RTT matrix + timing stats CSV (default `results.csv`) - the job-API equivalent of `../stoffel-ec2-cross-region-deployment/get-results`. Same env vars as `run-program` |
