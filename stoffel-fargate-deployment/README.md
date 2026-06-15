# Stoffel Fargate Deployment

Runs a 5-party StoffelVM MPC cluster on AWS ECS Fargate with an off-chain coordinator. Clients connect from outside the cluster (e.g. from a laptop).

## Architecture

- **Coordinator** — always-on ECS Fargate service; orchestrates protocol rounds
- **5 party nodes** — started per-run via `./run-nodes`; party 0 is the leader and bootnode
- **EFS** — shared volume for `.stflb` program files, mounted at `/app/programs/` in each container
- **Bastion EC2** — SSH gateway for uploading programs to EFS
- **Cloud Map** — internal DNS (`*.stoffel-coord.local`) for party-to-party discovery

## Prerequisites

- AWS CLI configured (`aws configure`)
- CDK bootstrapped (`cdk bootstrap`)
- Python virtualenv activated (`source .venv/bin/activate`)
- StoffelVM built locally (`cargo build --release` in `../StoffelVM/`)
- Identity files in `../ids/`

## First-time setup

```sh
./gen-keypair          # generate bastion SSH key pair
cdk deploy             # provision VPC, cluster, coordinator service, EFS, bastion
```

## Workflow

```sh
# 1. Upload a compiled program to EFS
./upload-program client_mul.stflb

# 2. Start the 5 party nodes (writes client-env.sh and log-env.sh)
./run-nodes client_mul.stflb

# 3. Run a client (in a separate terminal, once nodes are up)
./run-client 0 "input_value"   # client 0
./run-client 1 "input_value"   # client 1
```

## Benchmarking

```sh
./run-exp client_mul.stflb [n_iterations]
```

Runs `n_iterations` rounds and appends timing and memory metrics per node to `results.csv`.

## Scripts

| Script | Description |
|---|---|
| `gen-keypair` | Generate `bastion-key` / `bastion-key.pub` for SSH access |
| `upload-program <file.stflb>` | Upload a compiled program to EFS via the bastion |
| `run-nodes <file.stflb>` | Start party 0, register it with Cloud Map, then start parties 1–4 |
| `run-client <id> [inputs]` | Run a client locally against the deployed nodes |
| `run-exp <file.stflb> [n]` | Run `n` benchmark iterations, write results to `results.csv` |

## CDK context

No required context variables. The auth token can be passed at deploy time:

```sh
cdk deploy --context auth_token=<token>
```

## Notes

- The coordinator stays running between experiments; only parties are restarted per run.
- Parties 1–4 bootstrap via `party0.stoffel-coord.local`; party 0 must be registered in Cloud Map before they start. `run-nodes` handles this ordering automatically.
- Log streams for each run are written to `log-env.sh`.
