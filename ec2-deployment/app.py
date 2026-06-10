#!/usr/bin/env python3

import base64
import os
import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Stack,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_logs as logs,
)
from constructs import Construct


class StoffelVMCoordinatorStack(Stack):
    """
    Off-chain coordinator stack for StoffelVM on EC2.
    Each node (coordinator + N parties) runs as a Docker container
    on a dedicated EC2 instance managed via SSM.

    CDK context (pass via --context key=value):
      auth_token      - STOFFEL_AUTH_TOKEN (required)

    After deploying, run ./run-nodes to start containers on the instances,
    then ./run-client to connect as a client.
    """

    N_PARTIES = 5
    THRESHOLD = 1
    PROGRAM = "/app/programs/client_sub_order.stflb"

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        vpc = ec2.Vpc(
            self, "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("172.29.0.0/16"),
            max_azs=2,
            nat_gateways=1,
        )

        # Build images and push to ECR; run-nodes pulls them onto the instances.
        party_asset = ecr_assets.DockerImageAsset(
            self, "PartyImage",
            directory="../StoffelVM",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )
        coordinator_asset = ecr_assets.DockerImageAsset(
            self, "CoordImage",
            directory="../stoffel-mpc-coordinator",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        pub_key_file = os.path.join(os.path.dirname(__file__), "aws-key.pub")
        if os.path.exists(pub_key_file):
            with open(pub_key_file) as f:
                pub_key = f.read().strip()
        elif self.node.try_get_context("destroy"):
            pub_key = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAAgQC placeholder-for-destroy"
        else:
            raise FileNotFoundError(
                "aws-key.pub not found — run ./gen-key first"
            )

        key_pair = ec2.KeyPair(
            self, "KeyPair",
            key_pair_name="stoffel-ec2-key",
            public_key_material=pub_key,
        )

        sg = ec2.SecurityGroup(self, "SG", vpc=vpc, allow_all_outbound=True)
        cidr = ec2.Peer.ipv4(vpc.vpc_cidr_block)
        # Internal ports: party gossip/bind
        for port in [9000, 9001, 9002, 9003, 9004, 10000]:
            sg.add_ingress_rule(cidr, ec2.Port.tcp(port))
            sg.add_ingress_rule(cidr, ec2.Port.udp(port))
        # External ports: coordinator and party RPC ports reachable by clients outside the VPC
        for port in [31415, 16180, 16181, 16182, 16183, 16184]:
            sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(port))
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(22))

        # IAM role: SSM (for run-nodes) + ECR read (for docker pull)
        role = iam.Role(
            self, "InstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryReadOnly"),
            ],
        )

        ami = ec2.MachineImage.latest_amazon_linux2023()

        coord_instance = self._add_instance("Coord", vpc, sg, role, ami, key_pair)
        party_instances = [
            self._add_instance(f"Party{i}", vpc, sg, role, ami, key_pair)
            for i in range(self.N_PARTIES)
        ]
        pos_instance = self._add_pos_instance("Pos", vpc, sg, role, ami, key_pair)

        CfnOutput(self, "CoordInstanceId", value=coord_instance.instance_id)
        CfnOutput(self, "CoordPublicIp", value=coord_instance.instance_public_ip)
        CfnOutput(self, "CoordImageUri", value=coordinator_asset.image_uri)
        CfnOutput(self, "PartyImageUri", value=party_asset.image_uri)

        for i, inst in enumerate(party_instances):
            CfnOutput(self, f"Party{i}InstanceId", value=inst.instance_id)
            CfnOutput(self, f"Party{i}PublicIp", value=inst.instance_public_ip)
        CfnOutput(self, "PosInstanceId", value=pos_instance.instance_id)
        CfnOutput(self, "PosPublicIp", value=pos_instance.instance_public_ip)

    def _add_instance(
        self,
        name: str,
        vpc: ec2.Vpc,
        sg: ec2.SecurityGroup,
        role: iam.Role,
        ami: ec2.IMachineImage,
        key_pair: ec2.KeyPair,
    ) -> ec2.Instance:
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "yum install -y docker",
            "systemctl enable docker",
            "systemctl start docker",
        )
        return ec2.Instance(
            self, f"{name}Instance",
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.SMALL),
            machine_image=ami,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=sg,
            role=role,
            user_data=user_data,
            associate_public_ip_address=True,
            key_pair=key_pair,
        )

    def _add_pos_instance(
        self,
        name: str,
        vpc: ec2.Vpc,
        sg: ec2.SecurityGroup,
        role: iam.Role,
        ami: ec2.IMachineImage,
        key_pair: ec2.KeyPair,
    ) -> ec2.Instance:
        key_file = os.path.join(os.path.dirname(__file__), "aws-key")
        if os.path.exists(key_file):
            with open(key_file) as f:
                key_b64 = base64.b64encode(f.read().encode()).decode()
        elif self.node.try_get_context("destroy"):
            key_b64 = ""
        else:
            raise FileNotFoundError(
                "aws-key not found — run ./gen-key first"
            )

        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "yum install -y docker",
            "systemctl enable docker",
            "systemctl start docker",
            "mkdir -p /home/ec2-user/.ssh",
            f"echo '{key_b64}' | base64 -d > /home/ec2-user/.ssh/id_rsa",
            "chmod 600 /home/ec2-user/.ssh/id_rsa",
            "chown ec2-user:ec2-user /home/ec2-user/.ssh/id_rsa",
            # Skip host-key verification for VPC-internal addresses (172.29.0.0/16).
            "printf 'Host 172.29.*\\n  StrictHostKeyChecking no\\n  UserKnownHostsFile /dev/null\\n'"
            " > /home/ec2-user/.ssh/config",
            "chmod 600 /home/ec2-user/.ssh/config",
            "chown ec2-user:ec2-user /home/ec2-user/.ssh/config",
        )

        return ec2.Instance(
            self, f"{name}Instance",
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.SMALL),
            machine_image=ami,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=sg,
            role=role,
            user_data=user_data,
            associate_public_ip_address=True,
            key_pair=key_pair,
        )


app = cdk.App()
StoffelVMCoordinatorStack(
    app, "StoffelVMCoordinatorEC2Stack",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)
app.synth()
