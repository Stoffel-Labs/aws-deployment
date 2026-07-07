# Stoffel Fargate Cross-Region Deployment

Runs a 10-party StoffelVM MPC cluster on AWS ECS Fargate with **each party
node in its own AWS region**, plus an off-chain coordinator in a dedicated
region. This is the cross-region counterpart to `../stoffel-fargate-deployment`,
which runs everything in a single VPC.

## How this differs from stoffel-fargate-deployment

A single VPC gives you two things for free that don't exist across regions:
Cloud Map private DNS namespaces and EFS filesystems. Neither can span
regions, so this deployment replaces them:

| | stoffel-fargate-deployment | stoffel-fargate-cross-region-deployment |
|---|---|---|
| Topology | 1 VPC, all nodes | 1 VPC per node (11 VPCs, 11 regions) |
| Party/coordinator addressing | Cloud Map private DNS (`party0.stoffel-coord.local`) | Public IPs, injected as container overrides at `run-nodes` time |
| Program distribution | EFS volume + bastion (scp) | S3 bucket + per-run download into a task-scoped scratch volume |
| Internal ports (9000-9009 etc.) | Open to the VPC CIDR only | Open to `0.0.0.0/0` (no cross-region private networking) |

Each party's task definition runs two containers: a non-essential
`Downloader` (public `amazon/aws-cli` image) that pulls the selected program
from S3 into a task-local volume, and the `Container` (StoffelVM party
image) that waits for it to exit successfully before starting. This avoids
needing a per-region EFS/bastion or a custom-built StoffelVM image with the
AWS CLI baked in.

The full mesh RTT ping diagnostic from stoffel-fargate-deployment (each
party pinging every other party by Cloud Map name) isn't reproducible here
without knowing every peer's public IP before any of them start. Each party
instead pings just the coordinator and its bootstrap peer — the two
addresses it's actually given at launch.

## Regions

Defined in `regions.conf` (`COORD_REGION` / `PARTY_REGIONS`) — the single
source of truth read by both `app.py` (CDK) and `regions` (sourced by the
other shell scripts). Edit only `regions.conf` to add, remove, or move a
party's region; `N_PARTIES` in `app.py` derives from the length of
`PARTY_REGIONS` automatically.

| Node | Region |
|---|---|
| Coordinator | us-east-1 |
| party0 | us-east-2 |
| party1 | us-west-1 |
| party2 | us-west-2 |
| party3 | ca-central-1 |
| party4 | eu-west-1 |
| party5 | eu-west-2 |
| party6 | eu-central-1 |
| party7 | ap-southeast-1 |
| party8 | ap-southeast-2 |
| party9 | ap-northeast-1 |

## Prerequisites

- AWS CLI configured (`aws configure`)
- CDK bootstrapped in **every region above** (`cdk bootstrap aws://ACCOUNT/REGION` per region, or `cdk bootstrap --trust ACCOUNT aws://ACCOUNT/us-east-1 aws://ACCOUNT/us-east-2 ...`)
- Python virtualenv activated (`source .venv/bin/activate`)
- StoffelVM built locally (`cargo build --release` in `../StoffelVM/`) — needed for `run-client`
- Identity files in `../ids/`

## First-time setup

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

cdk deploy "Stoffel*"
# or, faster:
cdk deploy "Stoffel*" --concurrency 4
```

Every party builds from the same StoffelVM source, but each `from_asset()`
call in `app.py` sets `extra_hash=str(party_id)` so every party still gets
its own distinct CDK asset identity even though the built image is
identical. Without this, all 10 party stacks would share one asset hash
across 10 regions/stacks, and `cdk-assets` doesn't reliably re-tag/re-push
a shared asset per destination when it spans multiple stacks — it fails
with `tag does not exist` on push (concurrency-independent; it happened
even with `--asset-parallelism false`). With distinct hashes, each stack
publishes fully independently, so `--concurrency` (and the default
`--asset-parallelism true`) are safe to use — nothing shares a Docker tag
across stacks anymore. First deploy pushes 11 separate images and is
slower regardless; later deploys only push changed layers.

If a deploy fails partway through for an unrelated reason (throttling,
network blip), just re-run `cdk deploy "Stoffel*"` — CDK skips stacks that
are already up to date and retries the ones that failed.

## Workflow

```sh
# 1. Upload a compiled program to the shared S3 bucket
./upload-program client_mul.stflb

# 2. Start the party nodes (writes client-env.sh and log-env.sh)
./run-nodes client_mul.stflb

# 3. Run a client (in a separate terminal, once nodes are up)
./run-client 0 "input_value"   # client 0
./run-client 1 "input_value"   # client 1
```

## Benchmarking

```sh
./run-exp client_mul.stflb [n_iterations]
```

Runs `n_iterations` rounds and appends timing and memory metrics per node
(now labeled by region) to `results.csv`.

## Scripts

| Script | Description |
|---|---|
| `upload-program <file.stflb>` | Upload a compiled program to the shared S3 bucket |
| `run-nodes <file.stflb>` | Start the coordinator, then party0, then parties 1-9 (one per region) |
| `run-client <id> [inputs]` | Run a client locally against the deployed nodes |
| `run-exp <file.stflb> [n]` | Run `n` benchmark iterations, write results to `results.csv` |
| `get-aws-logs` | List CloudWatch log groups for the last run, across all regions involved |
| `cleanup-tasks` | Stop all running tasks across the coordinator + all 10 party regions |
| `regions` | Loads `regions.conf` and provides `party_region()`; sourced by the other scripts |

## CDK context

No required context variables. The auth token can be passed at deploy time:

```sh
cdk deploy --context auth_token=<token> "Stoffel*"
```

## Notes

- The coordinator and party0 are restarted on every `run-nodes` run (not
  long-running ECS services), same as stoffel-fargate-deployment.
- `STOFFEL_COORD_ADDR` and `STOFFEL_BOOTSTRAP_ADDR` are supplied as
  container overrides at `run-nodes` time rather than baked into the task
  definitions, since they depend on public IPs only known once those tasks
  are running.
- No NAT gateways: every task runs in a public subnet with a public IP
  (`assignPublicIp=ENABLED`), so there's no need to pay for one per region.
- The S3 programs bucket and its objects are retained on stack deletion
  (`RemovalPolicy.RETAIN`), matching the EFS behavior in
  stoffel-fargate-deployment.
