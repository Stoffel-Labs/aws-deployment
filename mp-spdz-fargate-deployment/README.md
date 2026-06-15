# MP-SPDZ Fargate Deployment

Runs MP-SPDZ MPC protocol parties on AWS ECS Fargate. The protocol and party count are chosen per run; programs are compiled locally and uploaded to EFS.

## Architecture

- **Player tasks** — one Fargate task per player, started per-run via `./run-nodes`; player 0 is the server that other players connect to
- **EFS** — shared volume for compiled bytecode and player inputs, mounted at `/usr/src/MP-SPDZ/Programs/`
- **Bastion EC2** — SSH gateway for uploading programs and inputs to EFS
- **Cloud Map** — internal DNS (`player{i}.mp-spdz.local`) for player discovery

## Prerequisites

- AWS CLI configured (`aws configure`)
- CDK bootstrapped (`cdk bootstrap`)
- Python virtualenv activated (`source .venv/bin/activate`)
- MP-SPDZ submodule initialised (`git submodule update --init mp-spdz`)

## First-time setup

```sh
./gen-keypair   # generate bastion SSH key pair
cdk deploy      # build Docker image, push to ECR, provision infrastructure
```

CDK context options (pass via `--context key=value`):

| Key | Default | Description |
|---|---|---|
| `n_max` | `5` | Maximum number of parties; SSL certificates are generated for this many players at image build time |
| `mpspdz_cpu` | `1024` | Fargate CPU units per task (1024 = 1 vCPU) |
| `mpspdz_memory` | `2048` | Fargate memory MiB per task |

## Supported protocols

The following protocol binaries are compiled into the image:

| Binary | Protocol |
|---|---|
| `mascot-party.x` | MASCOT (malicious, dishonest majority) |
| `semi-party.x` | Semi-honest, dishonest majority |
| `semi2k-party.x` | Semi-honest over rings |
| `shamir-party.x` | Shamir secret sharing (semi-honest) |
| `malicious-shamir-party.x` | Malicious Shamir |

## Workflow

```sh
# 1. Compile a program (requires mp-spdz submodule)
./compile-program Programs/Source/tutorial.mpc

# 2. Upload compiled bytecode to EFS
./upload-program tutorial

# 3. (Optional) Upload player inputs
./upload-input "1 2 3 4" "5 6 7 8" "9 10 11 12"

# 4. Start players and run
./run-nodes tutorial mascot-party.x 3
```

## Benchmarking

```sh
./run-exp tutorial mascot-party.x 3 [n_iterations]
```

Runs `n_iterations` rounds and appends timing, memory, and bandwidth metrics per player to `results.csv`.

## Scripts

| Script | Description |
|---|---|
| `gen-keypair` | Generate `bastion-key` / `bastion-key.pub` for SSH access |
| `compile-program <program.mpc> [flags]` | Compile a `.mpc` source file to bytecode using the local submodule |
| `upload-program <name>` | Upload compiled bytecode and schedule to EFS |
| `upload-input <p0-input> [p1-input] ...` | Upload player input strings to EFS (one argument per player, in order) |
| `cleanup-inputs` | Remove all player input files from EFS |
| `cleanup-tasks <stack-name>` | Stop all running Fargate tasks (frees vCPUs after a failed run) |
| `run-nodes <program> <protocol> <n_parties>` | Start player 0, register it with Cloud Map, then start remaining players |
| `run-exp <program> <protocol> <n_parties> [n]` | Run `n` benchmark iterations, write results to `results.csv` |

## Checking logs

`run-nodes` writes log stream names to `log-env.sh`. To read a player's full output:

```sh
. ./log-env.sh
aws logs get-log-events \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "$PLAYER0_LOG_STREAM" \
  --query 'events[*].message' --output text
```

## Notes

- The vCPU limit for Fargate On-Demand is low on new accounts. If `run-nodes` fails with a vCPU limit error, stop stale tasks first with `./cleanup-tasks MpSpdzStack`.
- Player 0 acts as the server; other players connect to `player0.mp-spdz.local`. `run-nodes` registers player 0 in Cloud Map before starting the others.
- Changing `n_max` at deploy time requires rebuilding the Docker image (SSL certificates are generated for all parties at build time).
