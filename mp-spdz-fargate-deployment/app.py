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
    aws_efs as efs,
    aws_iam as iam,
    aws_logs as logs,
    aws_servicediscovery as sd,
)
from constructs import Construct


class MpSpdzStack(Stack):
    """
    MP-SPDZ benchmarking stack on ECS Fargate.

    N_MAX player task definitions are pre-provisioned at deploy time.
    Each experiment run launches 1..N_MAX of them via ./run-nodes,
    passing the protocol binary, program name, and actual party count
    as container-environment overrides.

    CDK context (pass via --context key=value):
      n_max          - max parties to pre-provision (default: 5)
      mpspdz_cpu     - Fargate CPU units per task (default: 1024)
      mpspdz_memory  - Fargate memory MiB per task (default: 2048)

    Workflow:
      1. ./gen-keypair
      2. cdk deploy
      3. ./compile-program Programs/Source/tutorial.mpc
      4. ./upload-program tutorial
      5. ./run-nodes tutorial mascot-party.x 3
         or: ./run-exp tutorial mascot-party.x 3 5
    """

    NAMESPACE = "mp-spdz.local"
    PORT_BASE = 5000
    PROGRAM_MOUNT = "/usr/src/MP-SPDZ/Programs"

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        n_max = int(self.node.try_get_context("n_max") or "5")
        cpu = int(self.node.try_get_context("mpspdz_cpu") or "1024")
        memory = int(self.node.try_get_context("mpspdz_memory") or "2048")

        pub_key_file = os.path.join(os.path.dirname(__file__), "bastion-key.pub")
        if os.path.exists(pub_key_file):
            with open(pub_key_file) as f:
                pub_key = f.read().strip()
        else:
            pub_key = None
            print("WARNING: bastion-key.pub not found — run ./gen-keypair first")

        vpc = ec2.Vpc(
            self, "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("172.30.0.0/16"),
            max_azs=2,
            nat_gateways=1,
        )

        cluster = ecs.Cluster(
            self, "Cluster",
            vpc=vpc,
            default_cloud_map_namespace=ecs.CloudMapNamespaceOptions(
                name=self.NAMESPACE,
                type=sd.NamespaceType.DNS_PRIVATE,
                vpc=vpc,
            ),
        )

        player_asset = ecr_assets.DockerImageAsset(
            self, "PlayerImage",
            directory=os.path.join(os.path.dirname(__file__), ".."),
            file="mp-spdz-fargate-deployment/Dockerfile",
            build_args={"N_MAX": str(n_max)},
            platform=ecr_assets.Platform.LINUX_AMD64,
        )
        player_image = ecs.ContainerImage.from_docker_image_asset(player_asset)

        sg = ec2.SecurityGroup(self, "SG", vpc=vpc, allow_all_outbound=True)
        cidr = ec2.Peer.ipv4(vpc.vpc_cidr_block)
        for i in range(n_max):
            sg.add_ingress_rule(cidr, ec2.Port.tcp(self.PORT_BASE + i))

        log_group = logs.LogGroup(
            self, "Logs",
            retention=logs.RetentionDays.ONE_WEEK,
        )

        efs_sg = ec2.SecurityGroup(self, "EfsSG", vpc=vpc, allow_all_outbound=False)
        efs_sg.add_ingress_rule(cidr, ec2.Port.tcp(2049))

        file_system = efs.FileSystem(
            self, "ProgramsFs",
            vpc=vpc,
            security_group=efs_sg,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        access_point = file_system.add_access_point("AP", path="/")

        bastion_key_pair = ec2.KeyPair(
            self, "BastionKeyPair",
            public_key_material=pub_key,
        ) if pub_key else None

        bastion_sg = ec2.SecurityGroup(self, "BastionSG", vpc=vpc, allow_all_outbound=True)
        bastion_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(22))

        bastion_role = iam.Role(
            self, "BastionRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
        )
        file_system.grant_read_write(bastion_role)

        bastion = ec2.Instance(
            self, "Bastion",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MICRO),
            machine_image=ec2.MachineImage.latest_amazon_linux2(),
            security_group=bastion_sg,
            key_pair=bastion_key_pair,
            role=bastion_role,
        )
        bastion.user_data.add_commands(
            "yum install -y amazon-efs-utils",
            "mkdir -p /mnt/programs",
            f"mount -t efs -o tls,iam {file_system.file_system_id}:/ /mnt/programs",
            f"echo '{file_system.file_system_id}:/ /mnt/programs efs defaults,tls,iam,_netdev 0 0' >> /etc/fstab",
            "chmod 777 /mnt/programs",
        )
        bastion.node.add_dependency(file_system)

        player_tasks = []
        player_cm_services = []
        for i in range(n_max):
            task, cm_svc = self._add_player(
                i, cluster, player_image, sg, log_group,
                file_system, access_point, cpu, memory,
            )
            player_tasks.append(task)
            player_cm_services.append(cm_svc)

        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "SecurityGroupId", value=sg.security_group_id)
        CfnOutput(self, "SubnetIds", value=",".join(s.subnet_id for s in vpc.public_subnets))
        CfnOutput(self, "BastionPublicIp", value=bastion.instance_public_ip)
        CfnOutput(self, "EfsId", value=file_system.file_system_id)
        for i, task in enumerate(player_tasks):
            CfnOutput(self, f"Player{i}TaskDef", value=task.task_definition_arn)
        for i, svc in enumerate(player_cm_services):
            CfnOutput(self, f"Player{i}CloudMapArn", value=svc.service_arn)

    def _log_driver(self, log_group: logs.LogGroup, prefix: str) -> ecs.LogDriver:
        return ecs.LogDrivers.aws_logs(stream_prefix=prefix, log_group=log_group)

    def _add_player(
        self,
        player_id: int,
        cluster: ecs.Cluster,
        image: ecs.ContainerImage,
        sg: ec2.SecurityGroup,
        log_group: logs.LogGroup,
        file_system: efs.FileSystem,
        access_point: efs.AccessPoint,
        cpu: int,
        memory: int,
    ):
        port = self.PORT_BASE + player_id

        task = ecs.FargateTaskDefinition(
            self, f"Player{player_id}Task",
            cpu=cpu,
            memory_limit_mib=memory,
        )
        task.add_volume(
            name="programs",
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                file_system_id=file_system.file_system_id,
                transit_encryption="ENABLED",
                authorization_config=ecs.AuthorizationConfig(
                    access_point_id=access_point.access_point_id,
                    iam="ENABLED",
                ),
            ),
        )
        file_system.grant_read(task.task_role)

        # MPSPDZ_PROTOCOL, MPSPDZ_PROGRAM, MPSPDZ_N_PARTIES are injected at
        # runtime via container overrides in ./run-nodes.
        container = task.add_container(
            "Container",
            image=image,
            environment={
                "MPSPDZ_PLAYER_ID": str(player_id),
                "MPSPDZ_NAMESPACE": self.NAMESPACE,
                "MPSPDZ_PORT_BASE": str(self.PORT_BASE),
            },
            port_mappings=[
                ecs.PortMapping(container_port=port, protocol=ecs.Protocol.TCP),
            ],
            entry_point=[
                "/bin/bash", "-c",
                "/entrypoint.sh; EXIT=$?; "
                "MEM=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null"
                " || cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes 2>/dev/null"
                " || cat /sys/fs/cgroup/memory.current 2>/dev/null);"
                ' [ -n "$MEM" ] && echo "PEAK_MEM_BYTES: $MEM";'
                " exit $EXIT",
            ],
            logging=self._log_driver(log_group, f"player{player_id}"),
        )
        container.add_mount_points(
            ecs.MountPoint(
                container_path=self.PROGRAM_MOUNT,
                source_volume="programs",
                read_only=True,
            )
        )

        cm_service = sd.Service(
            self, f"Player{player_id}CloudMap",
            namespace=cluster.default_cloud_map_namespace,
            name=f"player{player_id}",
            dns_record_type=sd.DnsRecordType.A,
            dns_ttl=Duration.seconds(10),
            custom_health_check=sd.HealthCheckCustomConfig(failure_threshold=1),
        )

        return task, cm_service


app = cdk.App()
MpSpdzStack(
    app, "MpSpdzStack",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)
app.synth()
