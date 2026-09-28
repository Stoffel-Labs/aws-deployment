# Stoffel EC2 User Deployment (Persistent / Standing Nodes)

Runs a StoffelVM MPC cluster on EC2 instances with a self-service API-key
layer, same as `../stoffel-ec2-user-deployment` - but the coordinator and
every party are started **once** and stay running indefinitely
(`stoffel-run --standing-node`), instead of a fresh container per job.
"Running a program" admits a new *execution* onto the already-running mesh
via a control command; many executions run concurrently, so there is no
cluster-wide job queue or lock. See `app.py`'s docstring for the full
comparison.

The one deliberate restriction that comes with this: **programs are staged
exclusively by the operator**, never by API users. StoffelVM's standing-node
program catalog is loaded once when a party's container starts and never
rescanned afterward, so a newly staged program only becomes runnable after
the mesh is redeployed - an operation that briefly interrupts every other
execution in flight. Letting any API user trigger that at will would mean
one user's upload could kill every other user's running job at an
unpredictable time, so program upload has no API-key path here at all.
Users can only ask to *run* a program the operator has already staged (`GET
/programs` to discover what's available).

## Setup

1. Load the submodules: `git submodule update --init --recursive`.
2. Build the `stoffel-run` binary: `cd StoffelVM && cargo build --release`.
3. Build the MPC programs used by the deployments below: `./build-programs`
   (compiles everything under `src/` into `.stflb` bytecode in `programs/`).

New programs can be added by creating `*.stfl` and `Stoffel.toml` files under a new directory in `src/`.

## Operator workflow

```sh
cd stoffel-ec2-user-deployment-persistent
cdk deploy                              # provisions instances - no container runs yet
./upload-ids ../ids                     # sync identity certs/keys to the assets bucket
./upload-program aes                    # stage a program (looks for ../programs/aes.stflb), registers it as "aes"
./deploy                                # start the standing coordinator + every party
./add-api-key <username>                # hand out an API key (repeatable, no redeploy needed)
```

Re-run `./deploy` whenever you stage a new program, rotate `ids/`, or publish
a new image - it's the only script that ever restarts the containers, and
doing so interrupts every execution currently in flight.

Other operator tools: `./node-status`, `./start-nodes` / `./stop-nodes`
(pause/resume compute billing - standing containers use `--restart
unless-stopped` and come back on their own), `./get-api-key [username]`,
`./revoke-api-key <username>`, and `./run` (admit an execution directly with
your own AWS credentials, bypassing the API - useful for debugging).

## Running MPC Programs (users)

Needs only the API key given to you by the operator - no AWS CLI, no
credentials. `API_URL` defaults to this deployment's endpoint, so you only
need to set it if pointing at a different deployment:

```sh
API_KEY=<api-key-value> ./list-programs
API_KEY=<api-key-value> ./run-program aes --n-clients 2
```

`run-program` submits the execution, prints a message every 3s while it's
`PREPARING`, then exits once it's `RUNNING` (printing the party/coordinator
endpoints and writing `client-env.sh`) or reaches a terminal state before
ever running. There is no `--num-parties`/`--threshold`/`--backend`/`--curve`
choice per run - those are fixed by whatever `./deploy` last used, not
something each execution can pick.

Execution status values: `PREPARING` → `RUNNING` → `SUCCEEDED` | `FAILED` |
`CANCELLED`. More scripts cover what happens after `run-program` exits at
`RUNNING`:

```sh
./get-execution-status <execution_id>   # one-shot status check
./wait-for-execution <execution_id>     # polls every 5s, printing each status,
                                         # until it reaches a terminal state
./get-logs <execution_id>               # fetch coordinator/party logs, readable format
./cancel-execution <execution_id>       # cancel a still-preparing or running execution
```

`get-logs` is best-effort: the coordinator/party containers log continuously
across every execution they ever run, not one stream per job, so this
time-windows the current log stream around this execution's lifetime -
lines from other executions overlapping in time on the same node can appear
interleaved. There's no ping-RTT/benchmark CSV aggregation tool here (unlike
`../stoffel-ec2-user-deployment`'s `get-results`) - that depended on each
one-off container pinging its peers and printing benchmark markers right
before exiting, neither of which applies to a long-lived standing container.

Every `get-logs` call archives its formatted output to
`logs/<timestamp>-<execution_id>.txt` (the timestamp is the execution's own
start time, not fetch time, so re-fetching the same execution overwrites the
same file) - the same idea as `../stoffel-ec2-cross-region-deployment` and
`../stoffel-docker-compose-one-off` snapshotting a finished run's logs, just
suffixed with the execution_id since many executions can be in flight at
once here. `run-program` and `wait-for-execution` both call this
automatically once an execution reaches a terminal state, so you don't need
to remember to archive it yourself - manual `./get-logs` is for re-viewing
or re-archiving a specific execution on demand.

Once `RUNNING`, run your own MPC client against the endpoints, or use
`./run-client <client_id> <inputs> --program <name>` (needs the matching
local `.stflb` and `../StoffelVM/target/release/stoffel-run` built).

## Full Example

This example runs the AES program that is in the `src` directory. We assume
you have cloned the repository and are in the root directory, and that the
operator steps above (`cdk deploy`, `./upload-ids`, `./upload-program aes`,
`./deploy`) have already been done.

```sh
git submodule update --init --recursive
cd StoffelVM
cargo build --release
cd ..
./build-programs
cd stoffel-ec2-user-deployment-persistent
export API_KEY=<api-key-value>
./run-program aes --n-clients 2       # prints execution ID, writes client-env.sh
./run-client 0 "1 2 3" --program aes
./wait-for-execution <execution_id>
./get-logs <execution_id>
```
