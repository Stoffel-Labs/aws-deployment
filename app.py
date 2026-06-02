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

    Clients run externally (e.g. on a laptop). See run-client.
    """

    N_PARTIES = 5
    THRESHOLD = 1
    PROGRAM = "/app/programs/client_sub_order.stflb"
    NAMESPACE = "stoffel-coord.local"

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        auth_token = self.node.try_get_context("auth_token") or ""

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
            "StoffelVM",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        # Off-chain coordinator
        coordinator_image = ecs.ContainerImage.from_asset(
            "stoffel-mpc-coordinator",
            platform=ecr_assets.Platform.LINUX_AMD64,
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

        log_group = logs.LogGroup(
            self, "Logs",
            retention=logs.RetentionDays.ONE_WEEK,
        )

        coord_addr = f"coordinator.{self.NAMESPACE}:31415"

        common_env = {
            "STOFFEL_AUTH_TOKEN": auth_token,
            "STOFFEL_N_PARTIES": str(self.N_PARTIES),
            "STOFFEL_THRESHOLD": str(self.THRESHOLD),
            "STOFFEL_PROGRAM": self.PROGRAM,
            "STOFFEL_ENTRY": "main",
            "STOFFEL_COORD_ADDR": coord_addr,
            "STOFFEL_EXPECTED_CLIENTS": "/app/ids/pub/clients/client0.crt,/app/ids/pub/clients/client1.crt",
            "RUST_LOG": "info",
            "RUST_BACKTRACE": "1",  # TODO: needed?
        }

        rpc_servers = ",".join(
            f"party{i}.{self.NAMESPACE}:{16180 + i}" for i in range(self.N_PARTIES)
        )

        # Finally, add all the necessary entities.

        coord_service = self._add_coordinator(cluster, coordinator_image, sg, log_group)

        party_results = [
            self._add_party(i, cluster, party_image, sg, log_group, common_env)
            for i in range(self.N_PARTIES)
        ]
        party_tasks = [r[0] for r in party_results]
        party_cm_services = [r[1] for r in party_results]

        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "CoordServiceName", value=coord_service.service_name)
        CfnOutput(self, "SecurityGroupId", value=sg.security_group_id)
        CfnOutput(self, "SubnetIds", value=",".join(s.subnet_id for s in vpc.public_subnets))
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
    ) -> None:
        task = ecs.FargateTaskDefinition(self, "CoordTask", cpu=512, memory_limit_mib=1024)
        task.add_container(
            "Container",
            image=image,
            command=[
                "--addr", "0.0.0.0",
                "--hash", "0000000000000000000000000000000000000000000000000000000000000000",
                "--server-cert", "/app/ids/pub/coord.crt",
                "--server-key", "/app/ids/priv/coord.der",
                "--n", str(self.N_PARTIES),
                "--t", str(self.THRESHOLD),
                "--n-inputs", "2",
                "--output-clients", "/app/ids/pub/clients/client0.crt,/app/ids/pub/clients/client1.crt",
                "--initial-mpc-nodes",
                ",".join(f"/app/ids/pub/nodes/node{i}.crt" for i in range(self.N_PARTIES)),
            ],
            port_mappings=[
                ecs.PortMapping(container_port=31415, protocol=ecs.Protocol.TCP),
            ],
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "netstat -tuln | grep -q ':31415' || exit 1"],
                interval=Duration.seconds(5),
                timeout=Duration.seconds(3),
                retries=10,
                start_period=Duration.seconds(10),
            ),
            logging=self._log_driver(log_group, "coordinator"),
        )
        service = ecs.FargateService(
            self, "CoordService",
            cluster=cluster,
            task_definition=task,
            desired_count=1,
            security_groups=[sg],
            assign_public_ip=True,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            min_healthy_percent=0,
            cloud_map_options=ecs.CloudMapOptions(
                name="coordinator",
                dns_record_type=sd.DnsRecordType.A,
                dns_ttl=Duration.seconds(10),
            ),
        )
        return service

    def _add_party(
        self,
        party_id: int,
        cluster: ecs.Cluster,
        image: ecs.ContainerImage,
        sg: ec2.SecurityGroup,
        log_group: logs.LogGroup,
        common_env: dict,
    ) -> None:
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
        container_kwargs = dict(
            image=image,
            environment=environment,
            port_mappings=port_mappings,
            logging=self._log_driver(log_group, f"party{party_id}"),
        )
        if party_id == 0:
            container_kwargs["health_check"] = ecs.HealthCheck(
                command=["CMD-SHELL", "netstat -tuln | grep -q ':9000' || exit 1"],
                interval=Duration.seconds(5),
                timeout=Duration.seconds(3),
                retries=10,
                start_period=Duration.seconds(10),
            )
        task.add_container("Container", **container_kwargs)

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
