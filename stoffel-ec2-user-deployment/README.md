# Stoffel EC2 User Deployment

Runs a StoffelVM MPC cluster on EC2 instances with a self-service API
layer so external users can submit and run programs without AWS
credentials of their own and without racing each other for the shared cluster.

## Setup

1. Load the submodules: `git submodule update --init --recursive`.
2. Build the `stoffel-run` binary: `cd StoffelVM && cargo build --release`.
3. Build the MPC programs used by the deployments below: `./build-programs`
   (compiles everything under `src/` into `.stflb` bytecode in `programs/`).

New programs can be added by creating `*.stfl` and `Stoffel.toml` files under a new directory in `src/`.

## Running MPC Programs

Needs only the API key given to you by the operator - no AWS CLI, no
credentials. `API_URL` defaults to this deployment's endpoint, so you only
need to set it if pointing at a different deployment:

```sh
API_KEY=<api-key-value> ./run-program aes.stflb --num-parties 5 --threshold 1 --input-total 0
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

## Full Example

This example runs the AES program that is in the `src` directory.
We assume you have cloned the repository and are in the root directory.

```
git submodule update --init --recursive
cd StoffelVM
cargo build --release
cd ..
./build-programs
cd stoffel-ec2-user-deployment
export API_KEY=<api-key-value>
./run-program ../programs/aes.stflb --num-parties 5 --threshold 1 --input-total 0   # prints job ID
./wait-for-program <job_id>
./get-logs <job_id>
./get-results <job_id>
```
