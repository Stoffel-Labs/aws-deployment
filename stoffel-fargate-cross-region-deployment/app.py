#!/usr/bin/env python3

import os
import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecr_assets as ecr_assets,
    aws_logs as logs,
    aws_s3 as s3,
)
from constructs import Construct


def _load_regions():
    """
    Region assignment: one AWS region per party, plus a dedicated region for
    the coordinator. Read from regions.conf — the single source of truth also
    used by the shell scripts (run-nodes, upload-program, ...), which can't
    import this file directly.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regions.conf")
    values = {}
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    return values["COORD_REGION"], values["PARTY_REGIONS"].split()


COORD_REGION, PARTY_REGIONS = _load_regions()
N_PARTIES = len(PARTY_REGIONS)
THRESHOLD = 1


class StoffelCoordinatorStack(Stack):
    """
    Off-chain coordinator for a cross-region StoffelVM MPC cluster.

    Unlike stoffel-fargate-deployment (single VPC, Cloud Map private DNS,
    EFS-mounted programs), each party here lives in its own AWS region with
    its own VPC. Neither Cloud Map private DNS namespaces nor EFS
    filesystems can span regions, so:
      - party-to-party / party-to-coordinator addressing uses public IPs,
        discovered and injected as container overrides at `run-nodes` time
        (see STOFFEL_COORD_ADDR / STOFFEL_BOOTSTRAP_ADDR in StoffelPartyStack).
      - programs are distributed via a single S3 bucket (this stack) instead
        of EFS; every party task downloads its program from S3 into a
        task-local scratch volume via an init container before starting.

    CDK context (pass via --context key=value):
      auth_token - STOFFEL_AUTH_TOKEN (required)
    """

    def __init__(self, scope: Construct, construct_id: str, *, programs_bucket_name: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        auth_token = self.node.try_get_context("auth_token") or ""

        vpc = ec2.Vpc(
            self, "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("172.30.0.0/16"),
            max_azs=2,
            nat_gateways=0,
        )
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc)

        sg = ec2.SecurityGroup(self, "SG", vpc=vpc, allow_all_outbound=True)
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(31415))
        # ICMP echo (ping) for RTT measurement — see the full-mesh ping-sweep
        # logic baked into this container's entry_point below.
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.icmp_ping())

        log_group = logs.LogGroup(self, "Logs", retention=logs.RetentionDays.ONE_WEEK)

        # Central program storage. Party tasks in every region pull from
        # here at run-nodes time. S3 ARNs carry no region/account component,
        # so cross-region reads need no special bucket policy — an ordinary
        # grant_read() on the task role is enough.
        bucket = s3.Bucket(
            self, "ProgramsBucket",
            bucket_name=programs_bucket_name,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            enforce_ssl=True,
        )

        coordinator_image = ecs.ContainerImage.from_asset(
            "../stoffel-mpc-coordinator",
            platform=ecr_assets.Platform.LINUX_AMD64,
            file="Dockerfile.benchmark",
        )

        task = ecs.FargateTaskDefinition(self, "CoordTask", cpu=512, memory_limit_mib=1024)
        # Shared scratch volume between the coordinator and AddressWaiter
        # below: run-nodes only learns every entity's public IP after every
        # party has launched, so the full peer list can't be baked into any
        # container's startup overrides. Instead, once run-nodes knows every
        # IP, it uploads a "name=ip" address book to S3; AddressWaiter polls
        # for it and drops it on this volume; the coordinator's own entry_point
        # waits for that file to appear (after its real work is done) and then
        # pings every party from it. Because the pinging happens inside this
        # same long-lived container, there's no race with the container's own
        # public IP disappearing — see ../ping-matrix.
        task.add_volume(name="addresses")
        bucket.grant_read(task.task_role)

        container = task.add_container(
            "Container",
            image=coordinator_image,
            environment={"STOFFEL_AUTH_TOKEN": auth_token},
            command=[
                "--addr", "0.0.0.0",
                "--hash", "0000000000000000000000000000000000000000000000000000000000000000",
                "--server-cert", "/app/ids/pub/coord.crt",
                "--server-key", "/app/ids/priv/coord.der",
                "--n", str(N_PARTIES),
                "--t", str(THRESHOLD),
                "--n-inputs", "0",
                "--backend", "honeybadger",
                "--output-clients", "/app/ids/pub/clients/client0.crt,/app/ids/pub/clients/client1.crt",
                "--initial-mpc-nodes",
                ",".join(f"/app/ids/pub/nodes/node{i}.crt" for i in range(N_PARTIES)),
            ],
            # "run-coord" is a $0 placeholder so that "command" above lands in
            # "$@" starting at $1, letting the args be overridden as normal.
            entry_point=[
                "/bin/bash", "-c",
                '/app/run-coord "$@"; EXIT=$?; '
                'for i in $(seq 1 150); do [ -f /shared/addresses.csv ] && break; sleep 2; done; '
                'if [ -f /shared/addresses.csv ]; then '
                '  while IFS="=" read -r NAME ADDR; do '
                '    [ "$NAME" = "coord" ] && continue; '
                '    echo "Pinging $NAME..."; '
                '    ping -c 4 "$ADDR" || true; '
                '  done < /shared/addresses.csv; '
                'fi; '
                'exit $EXIT',
                "run-coord",
            ],
            port_mappings=[
                ecs.PortMapping(container_port=31415, protocol=ecs.Protocol.TCP),
            ],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="coordinator", log_group=log_group),
        )
        container.add_mount_points(
            ecs.MountPoint(container_path="/shared", source_volume="addresses", read_only=True)
        )

        address_waiter = task.add_container(
            "AddressWaiter",
            image=ecs.ContainerImage.from_registry("public.ecr.aws/aws-cli/aws-cli"),
            essential=False,
            entry_point=["/bin/sh", "-c"],
            # Placeholder — run-nodes overrides this with the real bucket and
            # this run's unique PING_ADDR_KEY.
            command=[
                "for i in $(seq 1 60); do aws s3 cp s3://PLACEHOLDER/PLACEHOLDER /shared/addresses.csv "
                ">/dev/null 2>&1 && exit 0; sleep 5; done; exit 1",
            ],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="address-waiter", log_group=log_group),
        )
        address_waiter.add_mount_points(
            ecs.MountPoint(container_path="/shared", source_volume="addresses", read_only=False)
        )

        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "CoordTaskDef", value=task.task_definition_arn)
        CfnOutput(self, "SecurityGroupId", value=sg.security_group_id)
        CfnOutput(self, "SubnetIds", value=",".join(s.subnet_id for s in vpc.public_subnets))
        CfnOutput(self, "ProgramsBucketName", value=bucket.bucket_name)


class StoffelPartyStack(Stack):
    """
    A single StoffelVM party node, deployed in its own AWS region.

    Programs live in the central S3 bucket owned by StoffelCoordinatorStack.
    Fargate tasks can't mount an S3 bucket like a filesystem, and the
    StoffelVM runtime image doesn't bundle the AWS CLI, so each run's task
    definition pairs the party container with a small "Downloader" init
    container (public amazon/aws-cli image) that pulls the selected program
    into a task-scoped scratch volume before the party container starts.

    STOFFEL_COORD_ADDR / STOFFEL_BOOTSTRAP_ADDR are intentionally left out
    of the baked-in environment: they depend on the coordinator's and
    party0's public IPs, which aren't known until those tasks are running.
    run-nodes supplies them as container overrides at launch time.
    """

    PROGRAM_MOUNT = "/app/programs"

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        party_id: int,
        programs_bucket_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bind_port = 9000 + party_id
        rpc_port = 16180 + party_id

        auth_token = self.node.try_get_context("auth_token") or ""

        vpc = ec2.Vpc(
            self, "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("172.31.0.0/16"),
            max_azs=2,
            nat_gateways=0,
        )
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc)

        # Each region hosts exactly one party, so the security group only
        # needs to open that party's own ports — unlike the single-VPC
        # deployment, there's no need to open the full 9000-9009 range.
        sg = ec2.SecurityGroup(self, "SG", vpc=vpc, allow_all_outbound=True)
        any4 = ec2.Peer.any_ipv4()
        sg.add_ingress_rule(any4, ec2.Port.tcp(bind_port))
        sg.add_ingress_rule(any4, ec2.Port.udp(bind_port))
        sg.add_ingress_rule(any4, ec2.Port.tcp(rpc_port))
        sg.add_ingress_rule(any4, ec2.Port.udp(rpc_port))
        sg.add_ingress_rule(any4, ec2.Port.icmp_ping())
        if party_id == 0:
            sg.add_ingress_rule(any4, ec2.Port.tcp(10000))
            sg.add_ingress_rule(any4, ec2.Port.udp(10000))

        log_group = logs.LogGroup(self, "Logs", retention=logs.RetentionDays.ONE_WEEK)

        party_image = ecs.ContainerImage.from_asset(
            "../StoffelVM",
            platform=ecr_assets.Platform.LINUX_AMD64,
            file="Dockerfile.benchmark-flexible",
            # All 10 party stacks build from identical source, so without
            # this they'd share one CDK asset hash across 10 regions/stacks.
            # cdk-assets dedupes the *build* correctly in that case but does
            # not reliably re-tag/re-push per destination when the shared
            # asset spans multiple stacks, producing "tag does not exist" on
            # push. extra_hash forces a distinct asset per party so each
            # stack publishes independently.
            extra_hash=str(party_id),
        )

        environment = {
            "STOFFEL_AUTH_TOKEN": auth_token,
            "STOFFEL_N_PARTIES": str(N_PARTIES),
            "STOFFEL_THRESHOLD": str(THRESHOLD),
            "STOFFEL_ENTRY": "main",
            "RUST_LOG": "info",
            "RUST_BACKTRACE": "1",
            "STOFFEL_SKIP_HOST_WAIT": "true",
            "STOFFEL_ROLE": "leader" if party_id == 0 else "party",
            "STOFFEL_PARTY_ID": str(party_id),
            # Each party is in its own VPC/region with no peering — only the
            # public IP (1:1 NAT'd, no NAT gateway) is reachable cross-region,
            # so entrypoint.sh must advertise that instead of its private IP.
            "STOFFEL_ADVERTISE_PUBLIC": "true",
            "STOFFEL_BIND_ADDR": f"0.0.0.0:{bind_port}",
            "STOFFEL_RPC_ADDR": f"0.0.0.0:{rpc_port}",
            "STOFFEL_CERT": f"/app/ids/pub/nodes/node{party_id}.crt",
            "STOFFEL_KEY": f"/app/ids/priv/nodes/node{party_id}.der",
        }

        task = ecs.FargateTaskDefinition(self, "PartyTask", cpu=512, memory_limit_mib=1024)
        task.add_volume(name="programs")
        # See the matching volume/AddressWaiter in StoffelCoordinatorStack for
        # why the full-mesh ping list arrives this way instead of via a
        # startup override.
        task.add_volume(name="addresses")

        bucket = s3.Bucket.from_bucket_name(self, "ProgramsBucket", programs_bucket_name)
        bucket.grant_read(task.task_role)

        downloader = task.add_container(
            "Downloader",
            image=ecs.ContainerImage.from_registry("public.ecr.aws/aws-cli/aws-cli"),
            essential=False,
            # Placeholder — run-nodes overrides this with the real S3 URI
            # and destination path for the selected program on every run.
            command=[
                "s3", "cp",
                f"s3://{programs_bucket_name}/PLACEHOLDER",
                f"{self.PROGRAM_MOUNT}/PLACEHOLDER",
            ],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="downloader", log_group=log_group),
        )
        downloader.add_mount_points(
            ecs.MountPoint(container_path=self.PROGRAM_MOUNT, source_volume="programs", read_only=False)
        )

        port_mappings = [
            ecs.PortMapping(container_port=bind_port, protocol=ecs.Protocol.TCP),
            ecs.PortMapping(container_port=bind_port, protocol=ecs.Protocol.UDP),
        ]
        if party_id == 0:
            port_mappings += [
                ecs.PortMapping(container_port=10000, protocol=ecs.Protocol.TCP),
                ecs.PortMapping(container_port=10000, protocol=ecs.Protocol.UDP),
            ]
        port_mappings += [
            ecs.PortMapping(container_port=rpc_port, protocol=ecs.Protocol.TCP),
            ecs.PortMapping(container_port=rpc_port, protocol=ecs.Protocol.UDP),
        ]

        container_kwargs = dict(
            image=party_image,
            environment=environment,
            port_mappings=port_mappings,
            logging=ecs.LogDrivers.aws_logs(stream_prefix=f"party{party_id}", log_group=log_group),
            # After the real work is done, wait for AddressWaiter (below) to
            # drop the full-mesh address book on the shared volume, then ping
            # every other entity from it. Doing this inside the same
            # long-lived container (rather than a separate task) means it
            # can't race the container's own public IP disappearing.
            entry_point=[
                "/bin/bash", "-c",
                '/app/entrypoint.sh; EXIT=$?; '
                'for i in $(seq 1 150); do [ -f /shared/addresses.csv ] && break; sleep 2; done; '
                'if [ -f /shared/addresses.csv ]; then '
                f'  while IFS="=" read -r NAME ADDR; do '
                f'    [ "$NAME" = "node{party_id}" ] && continue; '
                '    echo "Pinging $NAME..."; '
                '    ping -c 4 "$ADDR" || true; '
                '  done < /shared/addresses.csv; '
                'fi; '
                'MEM=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null'
                ' || cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes 2>/dev/null'
                ' || cat /sys/fs/cgroup/memory.current 2>/dev/null);'
                ' [ -n "$MEM" ] && echo "PEAK_MEM_BYTES: $MEM";'
                ' exit $EXIT',
            ],
        )
        if party_id == 0:
            container_kwargs["health_check"] = ecs.HealthCheck(
                command=["CMD-SHELL", "netstat -tuln | grep -q ':9000' || exit 1"],
                interval=Duration.seconds(5),
                timeout=Duration.seconds(3),
                retries=10,
                start_period=Duration.seconds(10),
            )
        container = task.add_container("Container", **container_kwargs)
        container.add_mount_points(
            ecs.MountPoint(container_path=self.PROGRAM_MOUNT, source_volume="programs", read_only=True),
            ecs.MountPoint(container_path="/shared", source_volume="addresses", read_only=True),
        )
        container.add_container_dependencies(
            ecs.ContainerDependency(
                container=downloader,
                condition=ecs.ContainerDependencyCondition.SUCCESS,
            )
        )

        address_waiter = task.add_container(
            "AddressWaiter",
            image=ecs.ContainerImage.from_registry("public.ecr.aws/aws-cli/aws-cli"),
            essential=False,
            entry_point=["/bin/sh", "-c"],
            # Placeholder — run-nodes overrides this with the real bucket/key
            # for this run once it's chosen one.
            command=[
                "for i in $(seq 1 60); do aws s3 cp s3://PLACEHOLDER/PLACEHOLDER /shared/addresses.csv "
                ">/dev/null 2>&1 && exit 0; sleep 5; done; exit 1",
            ],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="address-waiter", log_group=log_group),
        )
        address_waiter.add_mount_points(
            ecs.MountPoint(container_path="/shared", source_volume="addresses", read_only=False)
        )

        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "PartyTaskDef", value=task.task_definition_arn)
        CfnOutput(self, "SecurityGroupId", value=sg.security_group_id)
        CfnOutput(self, "SubnetIds", value=",".join(s.subnet_id for s in vpc.public_subnets))


if __name__ == "__main__":
    app = cdk.App()
    account = os.getenv("CDK_DEFAULT_ACCOUNT")
    programs_bucket_name = f"stoffel-cross-region-programs-{account}-{COORD_REGION}"

    StoffelCoordinatorStack(
        app, "StoffelCoordinatorStack",
        programs_bucket_name=programs_bucket_name,
        env=cdk.Environment(account=account, region=COORD_REGION),
    )

    for party_id, region in enumerate(PARTY_REGIONS):
        StoffelPartyStack(
            app, f"StoffelParty{party_id}Stack",
            party_id=party_id,
            programs_bucket_name=programs_bucket_name,
            env=cdk.Environment(account=account, region=region),
        )

    app.synth()
