# Stoffel EC2 Cross-Region Deployment

Runs a 10-party StoffelVM MPC cluster with **each party node in its own AWS
region**, plus an off-chain coordinator in a dedicated region — the same
topology as `../stoffel-fargate-cross-region-deployment`, but on long-lived
EC2 instances instead of ECS Fargate tasks.

## How this differs from stoffel-fargate-cross-region-deployment

| | stoffel-fargate-cross-region-deployment | stoffel-ec2-cross-region-deployment |
|---|---|---|
| Compute | ECS Fargate task, launched fresh per run | One long-lived EC2 instance per node, created once by `cdk deploy` |
| Addressing | Public IP assigned when a task starts — unknown until launch | **Elastic IP allocated at `cdk deploy` time** — known immediately, before any node ever runs |
| Full-mesh ping | Deferred: S3-published "address book" + an `AddressWaiter` sidecar container polling for it | Immediate: `run-nodes` bakes every peer's address directly into each node's `docker run` invocation, since every address is already known |
| Starting a run | `ecs run-task` per node, per run | `aws ssm send-command` (`AWS-RunShellScript`) per node, per run — pulls the image, downloads the program, and does `docker run -d` |
| Logging | ECS `awslogs` log driver | Docker's built-in `awslogs` log driver, same CloudWatch Logs API underneath |
| Program distribution | S3 bucket, downloaded by an init container into a task-scoped volume | S3 bucket, downloaded by the SSM-delivered script directly onto the instance's disk |
| Billing | Pay only while a task is running | Pay for 11 running instances continuously, whether or not a run is active |

### Why this fixes the "addresses aren't known" problem

The Fargate cross-region deployment can't know any node's public IP until
that node's Fargate task actually starts (ENI attachment happens at launch).
That forced a deferred design: every node pings blind, publishing an
"address book" to S3 only after every node has launched, with each
container's own entry_point polling S3 for it before it can ping anyone.

EC2 Elastic IPs don't have that constraint — `ec2.CfnEIP` is allocated as
its own resource, independent of instance launch, so it's a plain
CloudFormation output the moment `cdk deploy` finishes. `run-nodes` reads
every stack's `PublicIp` output up front (see "Resolve stack outputs" in
`run-nodes`) and builds the complete `name=ip` address book *before starting
anything*. That address book gets embedded directly into every node's
`docker run` command, so each node can ping every peer immediately after
finishing its own work — no waiting on a file, no sidecar container, no
polling loop.

This also simplifies orchestration: the Fargate version has to launch
party0 first, wait for its ENI's public IP, and only then compute the
bootstrap address for parties 1-9. Here, party0's address is already known,
so `run-nodes` only waits for party0's *container* to actually start (the
underlying P2P bootstrap protocol still needs party0 listening before others
dial it) — it never needs to look up an IP mid-run.

## Regions

Single source of truth is `regions.conf` (`COORD_REGION` / `PARTY_REGIONS`),
read by both `app.py` (CDK) and `regions` (shell scripts) — edit only there.
Same region assignment as the Fargate cross-region deployment:

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

## Instance type and cost

All 11 nodes use `INSTANCE_TYPE` in `app.py` (currently `t3.small`) — change
that one constant and `cdk deploy` to resize every node.

Because instances are long-lived rather than billed per-run, cost is driven
by how long the stack stays deployed, not how many experiments you run:

- `t3.small` (2 vCPU, 2 GiB, current default): roughly **$180-200/mo** if
  left running 24/7 (~$0.021-0.027/hr/instance x 11 instances, plus a few
  dollars for EBS root volumes). Elastic IPs are free while attached to a
  running instance.
- `c5.2xlarge` (8 vCPU, 16 GiB, compute-optimized, no CPU credits): roughly
  **$2,700/mo** at 24/7 — about 16x `t3.small`. Only worth it for real
  benchmark runs, not day-to-day development.

Since nothing about the CDK stacks depends on instance size, the cheap
default is safe to leave deployed for iterating on the deployment itself;
bump `INSTANCE_TYPE` only when you're about to run real benchmarks, and
`cdk destroy` (or stop the instances — see below) when you're done.

Pausing without destroying: `aws ec2 stop-instances --region <region>
--instance-ids <id>` stops billing for compute (EBS still bills a small
amount) while keeping the instance, its Elastic IP association, and
everything else intact — `start-instances` brings it back with the same
public IP. There's no bundled script for this since it's a two-line AWS CLI
call per region; ask if you want one added.

## Prerequisites

- AWS CLI configured (`aws configure`)
- CDK bootstrapped in **every region above** (`cdk bootstrap aws://ACCOUNT/REGION` per region, or `cdk bootstrap --trust ACCOUNT aws://ACCOUNT/us-east-1 aws://ACCOUNT/us-east-2 ...`)
- Python virtualenv activated (`source .venv/bin/activate`)
- StoffelVM built locally (`cargo build --release` in `../StoffelVM/`) — needed for `run-client`
- Identity files in `../ids/`
- Docker running locally (only needed at `cdk deploy` time, to build the two container images)

## First-time setup

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

cdk deploy "Stoffel*"
# or, faster:
cdk deploy "Stoffel*" --concurrency 4
```

Same `extra_hash=str(party_id)` trick as the Fargate deployment is used on
the party image asset, so all 10 party stacks publish independently and
`--concurrency` is safe.

`cdk deploy` provisions the EC2 instances and Elastic IPs but does **not**
start any containers — instances just sit there running Docker, waiting for
`run-nodes` to tell them what to do via SSM. It can take ~30-60s after an
instance first boots before its SSM agent registers; if `run-nodes` fails
with an SSM error immediately after a fresh deploy, wait a minute and retry.

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

If your deployment requires an auth token, pass it via CDK context at
deploy time — same as the Fargate deployment. It's written into each
instance's user data (`/etc/stoffel-env`) and picked up by `run-nodes` via
`docker run --env-file`, so it doesn't need to be re-supplied per run:

```sh
cdk deploy --all --context auth_token=<token>
```

Changing the token requires a redeploy that replaces the instance (user
data only runs on first boot) — a plain `cdk deploy` with a new
`auth_token` won't retroactively update an already-running instance.

## Benchmarking

```sh
./run-exp client_mul.stflb [n_iterations]
```

Runs `n_iterations` rounds and appends timing and memory metrics per node
(labeled by region) to `results.csv`.

## Scripts

| Script | Description |
|---|---|
| `upload-program <file.stflb>` | Upload a compiled program to the shared S3 bucket |
| `run-nodes <file.stflb>` | Resolve every node's address, then start the coordinator, party0, and parties 1-9 via SSM |
| `run-client <id> [inputs]` | Run a client locally against the deployed nodes |
| `run-exp <file.stflb> [n]` | Run `n` benchmark iterations, write results to `results.csv` |
| `ping-matrix` | Wait for the last run's containers to stop, then build a full-mesh RTT matrix from their ping output |
| `get-aws-logs` | Show CloudWatch log group/stream info for the last run, across all regions involved |
| `tail-logs <coord\|nodeN>...` | Tail CloudWatch logs for the coordinator and/or one or more parties, in parallel |
| `cleanup-tasks` | Stop the running container (if any) on every node's instance, across all 11 regions |
| `regions` | Loads `regions.conf` and provides the `party_region()` lookup helper, sourced by the other scripts |

## CDK context

| Context key | Default | Description |
|---|---|---|
| `auth_token` | `""` | `STOFFEL_AUTH_TOKEN`, baked into every instance's `/etc/stoffel-env` at deploy time |
| `num_nodes` | `4` | Number of party regions (from `regions.conf`) to deploy persistent instances for |
| `threshold` | `1` | MPC threshold `t`; `num_nodes` must be >= `2t+1` |

## Notes

- Every node's container is named `stoffel-run` and is replaced (`docker rm
  -f` then `docker run -d`) on every `run-nodes` call — same restart-per-run
  behavior as the Fargate deployment's coordinator/party0, just applied
  uniformly to every node here since compute is persistent.
- No NAT gateways: every instance runs in a public subnet with a public IP
  (`associate_public_ip_address=True`), so there's no need to pay for one
  per region.
- SSM Run Command (`ssm_session_permissions=True` on each instance) is used
  instead of SSH — no key pair to manage, and it works the same way whether
  you're on a laptop or CI.
- The S3 programs bucket and its objects are retained on stack deletion
  (`RemovalPolicy.RETAIN`), matching the Fargate deployment.
- `docker pull` runs before every `docker run`, so a `cdk deploy` that
  changes the image (StoffelVM/coordinator source changed) is picked up by
  the very next `run-nodes` — no need to replace or reboot the instance.
