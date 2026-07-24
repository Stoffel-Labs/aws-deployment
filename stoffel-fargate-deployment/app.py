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

class StoffelVMCoordinatorStack(Stack):
    """
    Off-chain coordinator stack for StoffelVM on ECS Fargate.

    Translation notes:
      - Static IPs -> Cloud Map DNS (coordinator.stoffel-coord.local,
        party0.stoffel-coord.local, ...)
      - STOFFEL_BIND_ADDR / STOFFEL_RPC_ADDR → 0.0.0.0:{port}; Fargate assigns
        IPs dynamically via awsvpc. If StoffelVM requires its own IP here, retrieve
        it at startup from the ECS task metadata endpoint.

    CDK context (pass via --context key=value):
      auth_token      - STOFFEL_AUTH_TOKEN (required)
      n_inputs        - total client inputs passed to coordinator and nodes (default: 0)

    Programs are stored on EFS. To deploy a program:
      1. Generate a key pair:   ./gen-keypair
      2. Deploy the stack:      cdk deploy
      3. Upload the program:    ./upload-program program.stflb
      4. Run nodes:             ./run-nodes program.stflb

    Clients run externally (e.g. on a laptop). See run-client.
    """

    N_PARTIES = 10
    THRESHOLD = 1
    NAMESPACE = "stoffel-coord.local"
    PROGRAM_MOUNT = "/app/programs"

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        auth_token = self.node.try_get_context("auth_token") or ""

        pub_key_file = os.path.join(os.path.dirname(__file__), "bastion-key.pub")
        if os.path.exists(pub_key_file):
            with open(pub_key_file) as f:
                pub_key = f.read().strip()
        else:
            pub_key = None
            print("WARNING: bastion-key.pub not found — run ./gen-keypair first")

        vpc = ec2.Vpc(
            self, "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("172.29.0.0/16"),
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

        # stoffel-run binary
        party_image = ecs.ContainerImage.from_asset(
            "../StoffelVM",
            platform=ecr_assets.Platform.LINUX_AMD64,
            file="Dockerfile.benchmark-flexible",
        )

        # Off-chain coordinator
        coordinator_image = ecs.ContainerImage.from_asset(
            "../stoffel-mpc-coordinator",
            platform=ecr_assets.Platform.LINUX_AMD64,
            file="Dockerfile.benchmark",
        )

        sg = ec2.SecurityGroup(self, "SG", vpc=vpc, allow_all_outbound=True)
        cidr = ec2.Peer.ipv4(vpc.vpc_cidr_block)
        # Internal ports: party gossip/bind
        for port in [9000, 9001, 9002, 9003, 9004, 9005, 9006, 9007, 9008, 9009, 10000]:
            sg.add_ingress_rule(cidr, ec2.Port.tcp(port))
            sg.add_ingress_rule(cidr, ec2.Port.udp(port))
        # ICMP echo (ping) between nodes, for RTT measurement
        sg.add_ingress_rule(cidr, ec2.Port.icmp_ping())
        # External ports: coordinator and party RPC ports reachable by clients outside the VPC
        for port in [31415, 16180, 16181, 16182, 16183, 16184, 16185, 16186, 16187, 16188, 16189]:
            sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(port))

        log_group = logs.LogGroup(
            self, "Logs",
            retention=logs.RetentionDays.ONE_WEEK,
        )

        # ------------------------------------------------------------------ #
        # EFS: shared program storage mounted into every party container.     #
        # Upload programs via the bastion; run-nodes selects one at launch.   #
        # ------------------------------------------------------------------ #
        efs_sg = ec2.SecurityGroup(self, "EfsSG", vpc=vpc, allow_all_outbound=False)
        efs_sg.add_ingress_rule(cidr, ec2.Port.tcp(2049))

        file_system = efs.FileSystem(
            self, "ProgramsFs",
            vpc=vpc,
            security_group=efs_sg,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # "programs" and "ids" are separate subtrees of the same filesystem —
        # each access point is scoped to its own directory so mounting one
        # into a container never exposes the other's contents.
        efs_acl = efs.Acl(owner_uid="1000", owner_gid="1000", permissions="0755")
        access_point = file_system.add_access_point("AP", path="/programs", create_acl=efs_acl)
        # Identity certs/keys are no longer baked into the party image (see
        # StoffelVM's Dockerfile.benchmark-flexible) — they're read from this
        # access point at runtime instead, same as programs are.
        ids_access_point = file_system.add_access_point("APIds", path="/ids", create_acl=efs_acl)

        # ------------------------------------------------------------------ #
        # Bastion EC2: SSH target for uploading .stflb files and ids onto     #
        # EFS. The bastion mounts the filesystem root directly (bypassing    #
        # access points), so /programs and /ids show up as sibling dirs:     #
        # scp foo.stflb ec2-user@<BastionPublicIp>:/mnt/efs/programs/        #
        # scp -r ids ec2-user@<BastionPublicIp>:/mnt/efs/                    #
        # Fargate tasks see these at /app/programs/ and /app/ids/ respectively.#
        # Run ./gen-keypair before deploying to generate bastion-key.         #
        # ------------------------------------------------------------------ #
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
            "mkdir -p /mnt/efs",
            f"mount -t efs -o tls,iam {file_system.file_system_id}:/ /mnt/efs",
            f"echo '{file_system.file_system_id}:/ /mnt/efs efs defaults,tls,iam,_netdev 0 0' >> /etc/fstab",
            "mkdir -p /mnt/efs/programs /mnt/efs/ids",
            "chmod 777 /mnt/efs /mnt/efs/programs /mnt/efs/ids",
        )
        bastion.node.add_dependency(file_system)

        coord_addr = f"coordinator.{self.NAMESPACE}:31415"

        # STOFFEL_PROGRAM is intentionally omitted; run-nodes passes it as a
        # container override so each run can specify a different program.
        common_env = {
            "STOFFEL_AUTH_TOKEN": auth_token,
            "STOFFEL_N_PARTIES": str(self.N_PARTIES),
            "STOFFEL_THRESHOLD": str(self.THRESHOLD),
            "STOFFEL_ENTRY": "main",
            "STOFFEL_COORD_ADDR": coord_addr,
        #    "STOFFEL_EXPECTED_CLIENTS": "/app/ids/pub/clients/client0.crt,/app/ids/pub/clients/client1.crt",
            "RUST_LOG": "info",
            "RUST_BACKTRACE": "1",
            "STOFFEL_SKIP_HOST_WAIT": "true",
        }

        coord_task, coord_cm_service = self._add_coordinator(cluster, coordinator_image, sg, log_group)

        party_results = [
            self._add_party(i, cluster, party_image, sg, log_group, common_env, file_system, access_point, ids_access_point)
            for i in range(self.N_PARTIES)
        ]
        party_tasks = [r[0] for r in party_results]
        party_cm_services = [r[1] for r in party_results]

        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "CoordTaskDef", value=coord_task.task_definition_arn)
        CfnOutput(self, "CoordCloudMapArn", value=coord_cm_service.service_arn)
        CfnOutput(self, "SecurityGroupId", value=sg.security_group_id)
        CfnOutput(self, "SubnetIds", value=",".join(s.subnet_id for s in vpc.public_subnets))
        CfnOutput(self, "BastionPublicIp", value=bastion.instance_public_ip)
        CfnOutput(self, "EfsId", value=file_system.file_system_id)
        for i, task in enumerate(party_tasks):
            CfnOutput(self, f"Party{i}TaskDef", value=task.task_definition_arn)
        for i, svc in enumerate(party_cm_services):
            CfnOutput(self, f"Party{i}CloudMapArn", value=svc.service_arn)

    def _log_driver(self, log_group: logs.LogGroup, prefix: str) -> ecs.LogDriver:
        return ecs.LogDrivers.aws_logs(stream_prefix=prefix, log_group=log_group)

    def _add_coordinator(
        self,
        cluster: ecs.Cluster,
        image: ecs.ContainerImage,
        sg: ec2.SecurityGroup,
        log_group: logs.LogGroup,
    ):
        task = ecs.FargateTaskDefinition(self, "CoordTask", cpu=512, memory_limit_mib=1024)
        task.add_container(
            "Container",
            image=image,
            environment={
                "STOFFEL_N_PARTIES": str(self.N_PARTIES),
            },
            command=[
                "--addr", "0.0.0.0",
                "--hash", "0000000000000000000000000000000000000000000000000000000000000000",
                "--server-cert", "/app/ids/pub/coord.crt",
                "--server-key", "/app/ids/priv/coord.der",
                "--n", str(self.N_PARTIES),
                "--t", str(self.THRESHOLD),
                "--n-inputs", "0",
                "--backend", "honeybadger",
                "--output-clients", "/app/ids/pub/clients/client0.crt,/app/ids/pub/clients/client1.crt",
                "--initial-mpc-nodes",
                ",".join(f"/app/ids/pub/nodes/node{i}.crt" for i in range(self.N_PARTIES)),
            ],
            # entry_point wraps the coordinator binary so that, once it exits,
            # it pings every party for RTT measurement (mirrors the party
            # containers' post-run ping loop). "run-coord" is a $0 placeholder
            # so that "command" lands in "$@" starting at $1.
            entry_point=[
                "/bin/bash", "-c",
                '/app/run-coord "$@"; EXIT=$?; '
                "sleep 10; "
                "for i in $(seq 0 $((STOFFEL_N_PARTIES - 1))); do "
                '  echo "Pinging party$i..."; '
                "  ping -c 4 party$i.stoffel-coord.local || true; "
                "done; "
                "exit $EXIT",
                "run-coord",
            ],
            port_mappings=[
                ecs.PortMapping(container_port=31415, protocol=ecs.Protocol.TCP),
            ],
            logging=self._log_driver(log_group, "coordinator"),
        )
        cm_service = sd.Service(
            self, "CoordCloudMap",
            namespace=cluster.default_cloud_map_namespace,
            name="coordinator",
            dns_record_type=sd.DnsRecordType.A,
            dns_ttl=Duration.seconds(10),
            custom_health_check=sd.HealthCheckCustomConfig(failure_threshold=1),
        )
        return task, cm_service

    def _add_party(
        self,
        party_id: int,
        cluster: ecs.Cluster,
        image: ecs.ContainerImage,
        sg: ec2.SecurityGroup,
        log_group: logs.LogGroup,
        common_env: dict,
        file_system: efs.FileSystem,
        access_point: efs.AccessPoint,
        ids_access_point: efs.AccessPoint,
    ):
        bind_port = 9000 + party_id
        rpc_port  = 16180 + party_id

        environment = {
            **common_env,
            "STOFFEL_ROLE": "leader" if party_id == 0 else "party",
            "STOFFEL_PARTY_ID": str(party_id),
            "STOFFEL_BIND_ADDR": f"0.0.0.0:{bind_port}",
            "STOFFEL_RPC_ADDR": f"0.0.0.0:{rpc_port}",
            "STOFFEL_CERT": f"/app/ids/pub/nodes/node{party_id}.crt",
            "STOFFEL_KEY": f"/app/ids/priv/nodes/node{party_id}.der",
        }
        if party_id != 0:
            environment["STOFFEL_BOOTSTRAP_ADDR"] = f"party0.{self.NAMESPACE}:9000"

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

        task = ecs.FargateTaskDefinition(
            self, f"Party{party_id}Task", cpu=512, memory_limit_mib=1024
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
        task.add_volume(
            name="ids",
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                file_system_id=file_system.file_system_id,
                transit_encryption="ENABLED",
                authorization_config=ecs.AuthorizationConfig(
                    access_point_id=ids_access_point.access_point_id,
                    iam="ENABLED",
                ),
            ),
        )
        file_system.grant_read(task.task_role)

        container_kwargs = dict(
            image=image,
            environment=environment,
            port_mappings=port_mappings,
            logging=self._log_driver(log_group, f"party{party_id}"),
            entry_point=[
                "/bin/bash", "-c",
                "/app/entrypoint.sh; EXIT=$?; "
                "sleep 10; "
                'echo "Pinging coordinator..."; '
                "ping -c 4 coordinator.stoffel-coord.local || true; "
                "for i in $(seq 0 $((STOFFEL_N_PARTIES - 1))); do "
                '  [ "$i" = "$STOFFEL_PARTY_ID" ] && continue; '
                '  echo "Pinging party$i..."; '
                "  ping -c 4 party$i.stoffel-coord.local || true; "
                "done; "
                "MEM=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null"
                " || cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes 2>/dev/null"
                " || cat /sys/fs/cgroup/memory.current 2>/dev/null);"
                ' [ -n "$MEM" ] && echo "PEAK_MEM_BYTES: $MEM";'
                " exit $EXIT",
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
            ecs.MountPoint(
                container_path=self.PROGRAM_MOUNT,
                source_volume="programs",
                read_only=True,
            ),
            ecs.MountPoint(
                container_path="/app/ids",
                source_volume="ids",
                read_only=True,
            ),
        )

        cm_service = sd.Service(
            self, f"Party{party_id}CloudMap",
            namespace=cluster.default_cloud_map_namespace,
            name=f"party{party_id}",
            dns_record_type=sd.DnsRecordType.A,
            dns_ttl=Duration.seconds(10),
            custom_health_check=sd.HealthCheckCustomConfig(failure_threshold=1),
        )
        return task, cm_service


app = cdk.App()
StoffelVMCoordinatorStack(
    app, "StoffelVMCoordinatorStack",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)
app.synth()
