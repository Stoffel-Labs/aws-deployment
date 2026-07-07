#!/usr/bin/env python3

import os
import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Stack,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
)
from constructs import Construct

# ---------------------------------------------------------------------- #
# Region assignment: one AWS region per party, plus a dedicated region   #
# for the coordinator. Keep this in sync with regions — the shell        #
# scripts (run-nodes, upload-program, ...) can't import this file, so    #
# they carry their own copy of the same mapping.                        #
# ---------------------------------------------------------------------- #
COORD_REGION = "us-east-1"
PARTY_REGIONS = [
    "us-east-2",       # party0
    "us-west-1",       # party1
    "us-west-2",       # party2
    "ca-central-1",    # party3
    "eu-west-1",       # party4
    "eu-west-2",       # party5
    "eu-central-1",    # party6
    "ap-southeast-1",  # party7
    "ap-southeast-2",  # party8
    "ap-northeast-1",  # party9
]
N_PARTIES = len(PARTY_REGIONS)
THRESHOLD = 1

# One constant to change instance size later (e.g. "c5.2xlarge" for real
# benchmark runs) — cheap default for now since instances are persistent
# (always-on) rather than billed per-run like the Fargate deployment.
INSTANCE_TYPE = "t3.small"


class StoffelCoordinatorStack(Stack):
    """
    Off-chain coordinator for a cross-region StoffelVM MPC cluster, running
    on a single long-lived EC2 instance instead of an ECS Fargate task.

    Unlike stoffel-fargate-cross-region-deployment, this deployment doesn't
    need an S3-published "address book" + AddressWaiter sidecar polling loop
    to work around not knowing peer addresses before launch: every node's
    Elastic IP is allocated by this CDK app at `cdk deploy` time, so every
    address is a plain CloudFormation output available immediately — well
    before any container ever runs. run-nodes reads every stack's PublicIp
    output up front and bakes the complete peer list directly into each
    node's `docker run` invocation (sent over SSM), so pings can fire
    immediately after each node finishes its work instead of waiting on a
    file that shows up later.

    The instance is provisioned once by `cdk deploy` and left running.
    run-nodes uses SSM Run Command (`AWS-RunShellScript`) to pull the image
    and (re)start the container for each experiment, the same way the
    Fargate deployment calls `ecs run-task` for each run — the difference is
    the compute underneath is long-lived, so there's no per-run boot time,
    but you pay for 11 running instances whether or not a run is active.
    """

    def __init__(self, scope: Construct, construct_id: str, *, programs_bucket_name: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        vpc = ec2.Vpc(
            self, "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("172.30.0.0/16"),
            max_azs=2,
            nat_gateways=0,
        )

        sg = ec2.SecurityGroup(self, "SG", vpc=vpc, allow_all_outbound=True)
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(31415))
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.icmp_ping())

        log_group = logs.LogGroup(self, "Logs", retention=logs.RetentionDays.ONE_WEEK)

        # Central program storage. Party instances in every region pull from
        # here at run-nodes time. S3 ARNs carry no region/account component,
        # so cross-region reads need no special bucket policy.
        bucket = s3.Bucket(
            self, "ProgramsBucket",
            bucket_name=programs_bucket_name,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            enforce_ssl=True,
        )

        image = ecr_assets.DockerImageAsset(
            self, "Image",
            directory="../stoffel-mpc-coordinator",
            platform=ecr_assets.Platform.LINUX_AMD64,
            file="Dockerfile.benchmark",
        )

        instance = ec2.Instance(
            self, "Instance",
            instance_type=ec2.InstanceType(INSTANCE_TYPE),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=sg,
            associate_public_ip_address=True,
            # SSM Run Command is how run-nodes starts/restarts the container
            # on this instance for each experiment (see run-nodes) — no SSH
            # key needed.
            ssm_session_permissions=True,
        )
        image.repository.grant_pull(instance.role)
        log_group.grant_write(instance.role)

        instance.user_data.add_commands(
            "dnf install -y docker",
            "systemctl enable --now docker",
            "usermod -aG docker ec2-user",
        )

        # Elastic IP, allocated independently of instance launch — this is
        # what makes every node's address known right after `cdk deploy`
        # rather than only after a container starts. See the class docstring.
        eip = ec2.CfnEIP(self, "Eip", domain="vpc")
        ec2.CfnEIPAssociation(
            self, "EipAssoc",
            allocation_id=eip.attr_allocation_id,
            instance_id=instance.instance_id,
        )

        CfnOutput(self, "PublicIp", value=eip.attr_public_ip)
        CfnOutput(self, "InstanceId", value=instance.instance_id)
        CfnOutput(self, "ImageUri", value=image.image_uri)
        CfnOutput(self, "LogGroupName", value=log_group.log_group_name)
        CfnOutput(self, "ProgramsBucketName", value=bucket.bucket_name)


class StoffelPartyStack(Stack):
    """
    A single StoffelVM party node, deployed as a long-lived EC2 instance in
    its own AWS region. See StoffelCoordinatorStack's docstring for why this
    deployment doesn't need the S3 address-book/AddressWaiter mechanism used
    by stoffel-fargate-cross-region-deployment.

    Programs live in the central S3 bucket owned by StoffelCoordinatorStack.
    run-nodes downloads the selected program directly to the instance (via
    `aws s3 cp` in the SSM-delivered startup script) before starting the
    container, rather than using an init container as the Fargate deployment
    does — there's no task-local ephemeral volume to share between
    containers here, just the instance's own disk.
    """

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

        vpc = ec2.Vpc(
            self, "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("172.31.0.0/16"),
            max_azs=2,
            nat_gateways=0,
        )

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

        bucket = s3.Bucket.from_bucket_name(self, "ProgramsBucket", programs_bucket_name)

        image = ecr_assets.DockerImageAsset(
            self, "Image",
            directory="../StoffelVM",
            platform=ecr_assets.Platform.LINUX_AMD64,
            file="Dockerfile.benchmark-flexible",
            # All 10 party stacks build from identical source — see the
            # matching note in stoffel-fargate-cross-region-deployment/app.py
            # for why extra_hash is required to get each stack its own
            # distinct, independently-publishable asset.
            extra_hash=str(party_id),
        )

        instance = ec2.Instance(
            self, "Instance",
            instance_type=ec2.InstanceType(INSTANCE_TYPE),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=sg,
            associate_public_ip_address=True,
            ssm_session_permissions=True,
        )
        image.repository.grant_pull(instance.role)
        log_group.grant_write(instance.role)
        bucket.grant_read(instance.role)

        instance.user_data.add_commands(
            "dnf install -y docker",
            "systemctl enable --now docker",
            "usermod -aG docker ec2-user",
            "mkdir -p /home/ec2-user/programs",
        )

        eip = ec2.CfnEIP(self, "Eip", domain="vpc")
        ec2.CfnEIPAssociation(
            self, "EipAssoc",
            allocation_id=eip.attr_allocation_id,
            instance_id=instance.instance_id,
        )

        CfnOutput(self, "PublicIp", value=eip.attr_public_ip)
        CfnOutput(self, "InstanceId", value=instance.instance_id)
        CfnOutput(self, "ImageUri", value=image.image_uri)
        CfnOutput(self, "LogGroupName", value=log_group.log_group_name)
        CfnOutput(self, "BindPort", value=str(bind_port))
        CfnOutput(self, "RpcPort", value=str(rpc_port))


if __name__ == "__main__":
    app = cdk.App()
    account = os.getenv("CDK_DEFAULT_ACCOUNT")
    programs_bucket_name = f"stoffel-ec2-cross-region-programs-{account}-{COORD_REGION}"

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
