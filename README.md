# Setup

0. Set up AWS using `aws configure`.
1. Load the submodules using `git submodule update --init --recursive`.
2. Build the `stoffel-run` binary using `cd StoffelVM && cargo build --release`.

# Running the MPC Program

1. Start the nodes using `./run-nodes`.
1. Start each client using `./run-client`.

Example:

```
./run-nodes
./run-client 0 0 0 &
./run-client 1 1 1
```

# Deploying the CDK App

To use the `cdk` binary for deployment, a virtual environment is needed that can be installed as follows:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
