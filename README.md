# aws-deployment

Deployment configurations for running StoffelVM MPC clusters and MP-SPDZ
benchmarks on AWS (EC2 and ECS Fargate) and locally via Docker Compose.

## Setup

0. (Only for development:) Set up AWS credentials using `aws configure`.
1. Load the submodules: `git submodule update --init --recursive`.
2. Build the `stoffel-run` binary: `cd StoffelVM && cargo build --release`.
3. Build the MPC programs used by the deployments below: `./build-programs`
   (compiles everything under `src/` into `.stflb` bytecode in `programs/`).

Each deployment directory below has its own setup/usage instructions in its
README (CDK bootstrap, Python virtualenv, etc.) — start there once you know
which one you need.

## Core code (git submodules)

| Directory | Purpose |
|---|---|
| [`StoffelVM`](StoffelVM) | The Stoffel Virtual Machine — register-based VM for local execution and MPC. Branch `aws-fixes`. |
| [`stoffel-mpc-coordinator`](stoffel-mpc-coordinator) | Coordinator primitives for the MPC protocol lifecycle (preprocessing, input collection, execution, output distribution), with on-chain and off-chain transports. Branch `aws-deployment`. |
| [`mp-spdz`](mp-spdz) | The [MP-SPDZ](https://github.com/data61/MP-SPDZ) MPC framework, used for comparison benchmarks. |

`StoffelVM-persistent` and `stoffel-test` are separate local working copies
of the Stoffel toolchain (not submodules) used for persistent-deployment
and test iteration respectively.

## Local Docker Compose (no AWS)

| Directory | Purpose |
|---|---|
| [`stoffel-docker-compose`](stoffel-docker-compose) | Runs a full StoffelVM cluster (coordinator + party nodes) locally in containers. Used for local experiments (`run-exp`) and network-condition testing (`ping-matrix`). |
| [`stoffel-docker-compose-without-coord`](stoffel-docker-compose-without-coord) | Same, but without the coordinator — parties only. |
| [`mp-spdz-docker-compose`](mp-spdz-docker-compose) | Docker Compose equivalent of `mp-spdz/sig_bench.sh`, reproducing the Dalskov et al. Table 1 `Sig(ms)` benchmark methodology with each party in its own container. |

## AWS deployments

Each of these is an independent CDK app (own `app.py`, `cdk.json`, virtualenv)
under `<dir>/README.md` with full setup and usage instructions.

### EC2

| Directory | Purpose |
|---|---|
| [`ec2-deployment`](ec2-deployment) | Baseline: runs a StoffelVM MPC cluster on EC2 instances within a single VPC/region. |
| [`stoffel-ec2-cross-region-deployment`](stoffel-ec2-cross-region-deployment) | 10-party cluster with **each party in its own AWS region** plus a dedicated coordinator region, on long-lived EC2 instances with Elastic IPs known before deploy. |
| [`stoffel-ec2-user-deployment`](stoffel-ec2-user-deployment) | EC2 cluster with a self-service API layer (API key auth) so external users can submit/run programs without AWS credentials of their own, without racing each other for the shared cluster. See its `ARCHITECTURE.md`. |

### ECS Fargate

| Directory | Purpose |
|---|---|
| [`stoffel-fargate-deployment`](stoffel-fargate-deployment) | Baseline: 5-party StoffelVM cluster on ECS Fargate with an always-on coordinator service, EFS for program files, Cloud Map for service discovery, and a bastion EC2 host for EFS uploads. |
| [`stoffel-fargate-cross-region-deployment`](stoffel-fargate-cross-region-deployment) | Cross-region counterpart to `stoffel-fargate-deployment` — 10 parties, each in its own VPC/region (11 VPCs total), plus a dedicated coordinator region. Replaces Cloud Map/EFS (which can't span regions) with cross-region equivalents. |
| [`stoffel-fargate-user-deployment`](stoffel-fargate-user-deployment) | Independent copy of `stoffel-fargate-deployment` with a self-service API layer added (API key auth + presigned S3 upload + orchestrator), no bastion — external users submit jobs without AWS credentials. |
| [`mp-spdz-fargate-deployment`](mp-spdz-fargate-deployment) | Runs MP-SPDZ MPC parties on ECS Fargate for a chosen protocol/party count, with EFS for compiled bytecode/inputs and a bastion for uploads. |

## Shared assets and utilities

| Path | Purpose |
|---|---|
| `src/` | MPC program sources (e.g. `adkg`, `adkg-sign`, `aes`, `thresh-ecdsa`). |
| `programs/` | Compiled `.stflb` bytecode output of `./build-programs`, consumed by the deployments above. |
| `ids/` | Generated TLS keys/certs (`priv/`, `pub/`) for cluster nodes and clients. |
| `build-programs` | Builds every program under `src/` into `programs/`. |
| `cleanup-tasks` | Stops all running ECS tasks in a given CloudFormation stack's cluster (e.g. after a stuck run). |
| `cleanup-services` | Scans Cloud Map services for stuck/orphaned instance registrations and clears them. |
| `delete-stack` | Deletes a CloudFormation stack and waits for completion. |
