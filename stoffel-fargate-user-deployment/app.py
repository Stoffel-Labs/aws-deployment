#!/usr/bin/env python3

import os
import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigateway,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecr_assets as ecr_assets,
    aws_efs as efs,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_pipes as pipes,
    aws_s3 as s3,
    aws_servicediscovery as sd,
    aws_sqs as sqs,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as sfn_tasks,
)
from constructs import Construct

class StoffelUserDeploymentStack(Stack):
    """
    User-facing StoffelVM MPC deployment on ECS Fargate.

    This is the same compute layer as ../stoffel-fargate-deployment (coordinator
    + party Fargate task definitions, EFS program storage, Cloud Map discovery),
    deployed as its own independent copy of the stack rather than a cross-stack
    reference. On top of that, it adds a self-service layer so external users
    can run MPC programs without holding AWS credentials of their own and
    without racing each other for the shared coordinator/party infrastructure:

      - API Gateway (API key auth) exposes presign-upload / submit-job /
        job-status endpoints.
      - Programs are uploaded straight to S3 via a presigned URL (never
        through the API's own payload limit).
      - Job requests are queued on a FIFO SQS queue.
      - An EventBridge Pipe starts one Step Functions execution per queued
        job (fire-and-forget StartExecution - no Lambda in this hop).
      - The state machine acquires a DynamoDB lock (binary semaphore) before
        running the job and releases it after, so only one job's
        coordinator+parties ever run at a time - the queue's FIFO ordering
        alone does NOT guarantee this, since Pipes acks the SQS message as
        soon as the execution *starts*, not when it finishes.
      - The actual job (launch coordinator+parties, wait for completion,
        report status to DynamoDB) runs inside a single orchestrator Fargate
        task, invoked via Step Functions' ECS RunTask.sync integration so the
        state machine (and therefore the lock) doesn't release until the run
        truly stops.

    Users still run their own MPC client (run-client) against the party
    endpoints returned by the job-status endpoint - this stack does not run
    the client role or return captured results on a user's behalf.

    CDK context (pass via --context key=value):
      auth_token      - STOFFEL_AUTH_TOKEN (required)

    Operator workflow for manually starting nodes/clients against an already-uploaded
    program (run-nodes, run-client) still works directly against this stack's outputs.
    Program upload happens exclusively via the API (presign + orchestrator) - there is
    no bastion/SSH path onto EFS in this stack.
    """

    N_PARTIES = 10
    THRESHOLD = 1
    NAMESPACE = "stoffel-coord.local"
    PROGRAM_MOUNT = "/app/programs"

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        auth_token = self.node.try_get_context("auth_token") or ""

        # No NAT Gateway: every task here (coordinator, parties, orchestrator)
        # runs in a public subnet with its own public IP - coordinator/parties
        # need that anyway for external MPC clients to reach them directly,
        # and the orchestrator only makes outbound AWS API calls, which a
        # public subnet already routes via the Internet Gateway for free.
        # A NAT Gateway would only serve a private subnet nothing uses here.
        vpc = ec2.Vpc(
            self, "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("172.29.0.0/16"),
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=18,
                ),
            ],
        )
        # Lets the orchestrator task fetch uploaded programs from S3 without
        # routing through the internet (free, no data-processing charge).
        vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)

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
        # Users upload via presigned S3 URL; the orchestrator copies the      #
        # file onto EFS before starting a run - there is no other path onto   #
        # EFS in this stack (no bastion/SSH).                                 #
        # ------------------------------------------------------------------ #
        efs_sg = ec2.SecurityGroup(self, "EfsSG", vpc=vpc, allow_all_outbound=False)
        efs_sg.add_ingress_rule(cidr, ec2.Port.tcp(2049))

        file_system = efs.FileSystem(
            self, "ProgramsFs",
            vpc=vpc,
            security_group=efs_sg,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        access_point = file_system.add_access_point("AP", path="/")

        coord_addr = f"coordinator.{self.NAMESPACE}:31415"

        # STOFFEL_PROGRAM is intentionally omitted; the orchestrator (or,
        # for operator-driven runs, run-nodes) passes it as a container
        # override so each run can specify a different program.
        common_env = {
            "STOFFEL_AUTH_TOKEN": auth_token,
            "STOFFEL_N_PARTIES": str(self.N_PARTIES),
            "STOFFEL_THRESHOLD": str(self.THRESHOLD),
            "STOFFEL_ENTRY": "main",
            "STOFFEL_COORD_ADDR": coord_addr,
            "RUST_LOG": "info",
            "RUST_BACKTRACE": "1",
            "STOFFEL_SKIP_HOST_WAIT": "true",
        }

        coord_task, coord_cm_service = self._add_coordinator(cluster, coordinator_image, sg, log_group)

        party_results = [
            self._add_party(i, cluster, party_image, sg, log_group, common_env, file_system, access_point)
            for i in range(self.N_PARTIES)
        ]
        party_tasks = [r[0] for r in party_results]
        party_cm_services = [r[1] for r in party_results]

        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "CoordTaskDef", value=coord_task.task_definition_arn)
        CfnOutput(self, "CoordCloudMapArn", value=coord_cm_service.service_arn)
        CfnOutput(self, "SecurityGroupId", value=sg.security_group_id)
        CfnOutput(self, "SubnetIds", value=",".join(s.subnet_id for s in vpc.public_subnets))
        CfnOutput(self, "EfsId", value=file_system.file_system_id)
        for i, task in enumerate(party_tasks):
            CfnOutput(self, f"Party{i}TaskDef", value=task.task_definition_arn)
        for i, svc in enumerate(party_cm_services):
            CfnOutput(self, f"Party{i}CloudMapArn", value=svc.service_arn)

        # ------------------------------------------------------------------ #
        # User-facing layer: uploads bucket, job/lock tables, queue,         #
        # orchestrator, state machine, pipe, API.                           #
        # ------------------------------------------------------------------ #
        uploads_bucket = self._add_uploads_bucket()
        jobs_table, lock_table = self._add_tables()
        jobs_queue, jobs_dlq = self._add_queue()

        orchestrator_task, orchestrator_container = self._add_orchestrator(
            cluster, sg, log_group, vpc, file_system, access_point,
            uploads_bucket, jobs_table,
            coord_task, coord_cm_service, party_tasks, party_cm_services,
        )

        state_machine = self._add_state_machine(
            cluster, sg, vpc, orchestrator_task, orchestrator_container, lock_table,
        )

        self._add_pipe(jobs_queue, state_machine)

        api, api_key, usage_plan = self._add_api(uploads_bucket, jobs_table, jobs_queue, log_group)

        CfnOutput(self, "UploadsBucketName", value=uploads_bucket.bucket_name)
        CfnOutput(self, "JobsTableName", value=jobs_table.table_name)
        CfnOutput(self, "JobsQueueUrl", value=jobs_queue.queue_url)
        CfnOutput(self, "JobsDlqUrl", value=jobs_dlq.queue_url)
        CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
        CfnOutput(self, "ApiUrl", value=api.url)
        CfnOutput(self, "ApiKeyId", value=api_key.key_id)
        CfnOutput(self, "UsagePlanId", value=usage_plan.usage_plan_id)

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
            )
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

    def _add_uploads_bucket(self) -> s3.Bucket:
        return s3.Bucket(
            self, "UploadsBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            cors=[s3.CorsRule(
                allowed_methods=[s3.HttpMethods.PUT],
                allowed_origins=["*"],
                allowed_headers=["*"],
            )],
            # Programs are transient per-job artifacts, not something worth
            # retaining once the orchestrator has copied them onto EFS.
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(3))],
        )

    def _add_tables(self):
        jobs_table = dynamodb.Table(
            self, "JobsTable",
            partition_key=dynamodb.Attribute(name="job_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Binary semaphore for the state machine: one item, "jobs", whose
        # presence means a job is currently running. See _add_state_machine.
        lock_table = dynamodb.Table(
            self, "LockTable",
            partition_key=dynamodb.Attribute(name="lock_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        return jobs_table, lock_table

    def _add_queue(self):
        jobs_dlq = sqs.Queue(
            self, "JobsDLQ",
            queue_name="stoffel-jobs-dlq.fifo",
            fifo=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        jobs_queue = sqs.Queue(
            self, "JobsQueue",
            queue_name="stoffel-jobs.fifo",
            fifo=True,
            content_based_deduplication=False,
            visibility_timeout=Duration.minutes(5),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=jobs_dlq),
            removal_policy=RemovalPolicy.DESTROY,
        )
        return jobs_queue, jobs_dlq

    def _add_orchestrator(
        self,
        cluster: ecs.Cluster,
        sg: ec2.SecurityGroup,
        log_group: logs.LogGroup,
        vpc: ec2.Vpc,
        file_system: efs.FileSystem,
        access_point: efs.AccessPoint,
        uploads_bucket: s3.Bucket,
        jobs_table: dynamodb.Table,
        coord_task: ecs.FargateTaskDefinition,
        coord_cm_service: sd.Service,
        party_tasks: list,
        party_cm_services: list,
    ):
        orchestrator_image = ecs.ContainerImage.from_asset(
            "orchestrator",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        task = ecs.FargateTaskDefinition(self, "OrchestratorTask", cpu=256, memory_limit_mib=512)
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
        file_system.grant_read_write(task.task_role)

        container = task.add_container(
            "Container",
            image=orchestrator_image,
            logging=self._log_driver(log_group, "orchestrator"),
            environment={
                "CLUSTER_NAME": cluster.cluster_name,
                "SECURITY_GROUP_ID": sg.security_group_id,
                "SUBNET_IDS": ",".join(s.subnet_id for s in vpc.public_subnets),
                "COORD_TASK_DEF_ARN": coord_task.task_definition_arn,
                "COORD_CLOUDMAP_SERVICE_ID": coord_cm_service.service_id,
                "PARTY_TASK_DEF_ARNS": ",".join(t.task_definition_arn for t in party_tasks),
                "PARTY_CLOUDMAP_SERVICE_IDS": ",".join(s.service_id for s in party_cm_services),
                "JOBS_TABLE_NAME": jobs_table.table_name,
                "PROGRAM_S3_BUCKET": uploads_bucket.bucket_name,
            },
        )
        container.add_mount_points(
            ecs.MountPoint(container_path=self.PROGRAM_MOUNT, source_volume="programs", read_only=False)
        )

        uploads_bucket.grant_read(task.task_role)
        jobs_table.grant_read_write_data(task.task_role)

        task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["ecs:RunTask"],
            resources=[coord_task.task_definition_arn] + [t.task_definition_arn for t in party_tasks],
        ))
        task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["ecs:DescribeTasks", "ecs:StopTask"],
            resources=["*"],
        ))
        task.task_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "servicediscovery:RegisterInstance",
                "servicediscovery:DeregisterInstance",
                "servicediscovery:ListInstances",
                "servicediscovery:GetOperation",
            ],
            resources=["*"],
        ))
        # Cloud Map's RegisterInstance/DeregisterInstance create/delete a
        # Route 53 health check per instance under the hood (needed for the
        # MULTIVALUE routing policy), even with a custom health check config -
        # that happens as the calling identity, so the orchestrator's own
        # role needs these, not just servicediscovery permissions.
        task.task_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "route53:CreateHealthCheck",
                "route53:DeleteHealthCheck",
                "route53:UpdateHealthCheck",
                "route53:GetHealthCheck",
            ],
            resources=["*"],
        ))
        task.task_role.add_to_policy(iam.PolicyStatement(
            actions=["ec2:DescribeNetworkInterfaces"],
            resources=["*"],
        ))

        # RunTask requires iam:PassRole on the roles attached to the task
        # definitions it launches.
        for t in [coord_task] + party_tasks:
            if t.execution_role:
                t.execution_role.grant_pass_role(task.task_role)
            t.task_role.grant_pass_role(task.task_role)

        return task, container

    def _add_state_machine(
        self,
        cluster: ecs.Cluster,
        sg: ec2.SecurityGroup,
        vpc: ec2.Vpc,
        orchestrator_task: ecs.FargateTaskDefinition,
        orchestrator_container: ecs.ContainerDefinition,
        lock_table: dynamodb.Table,
    ) -> sfn.StateMachine:
        lock_key = sfn_tasks.DynamoAttributeValue.from_string("jobs")

        # The pipe always hands Step Functions targets an array-wrapped
        # payload ([{"job": {...}}]), even for a single record (see
        # _add_pipe) - unwrap it as the very first state so every ResultPath
        # below operates on the plain object instead of hitting
        # States.ReferencePathConflict trying to set a field on an array root.
        unwrap_input = sfn.Pass(self, "UnwrapInput", input_path="$[0]")

        acquire_lock = sfn_tasks.DynamoPutItem(
            self, "AcquireLock",
            table=lock_table,
            item={"lock_id": lock_key},
            condition_expression="attribute_not_exists(lock_id)",
            result_path=sfn.JsonPath.DISCARD,
        )
        wait_for_lock = sfn.Wait(
            self, "WaitForLock",
            time=sfn.WaitTime.duration(Duration.seconds(15)),
        )
        acquire_lock.add_catch(
            wait_for_lock.next(acquire_lock),
            errors=["DynamoDB.ConditionalCheckFailedException"],
            result_path="$.lock_error",
        )

        release_lock = sfn_tasks.DynamoDeleteItem(
            self, "ReleaseLock",
            table=lock_table,
            key={"lock_id": lock_key},
            result_path=sfn.JsonPath.DISCARD,
        )
        release_lock_on_failure = sfn_tasks.DynamoDeleteItem(
            self, "ReleaseLockOnFailure",
            table=lock_table,
            key={"lock_id": lock_key},
            result_path=sfn.JsonPath.DISCARD,
        )
        job_failed = sfn.Fail(self, "JobFailed")
        release_lock_on_failure.next(job_failed)

        run_orchestrator = sfn_tasks.EcsRunTask(
            self, "RunOrchestrator",
            integration_pattern=sfn.IntegrationPattern.RUN_JOB,
            cluster=cluster,
            task_definition=orchestrator_task,
            launch_target=sfn_tasks.EcsFargateLaunchTarget(
                platform_version=ecs.FargatePlatformVersion.LATEST,
            ),
            assign_public_ip=True,
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[sg],
            container_overrides=[
                sfn_tasks.ContainerOverride(
                    container_definition=orchestrator_container,
                    environment=[
                        sfn_tasks.TaskEnvironmentVariable(name="JOB_ID", value=sfn.JsonPath.string_at("$.job.job_id")),
                        sfn_tasks.TaskEnvironmentVariable(name="PROGRAM_S3_KEY", value=sfn.JsonPath.string_at("$.job.program_s3_key")),
                        sfn_tasks.TaskEnvironmentVariable(name="NUM_PARTIES", value=sfn.JsonPath.string_at("$.job.num_parties")),
                        sfn_tasks.TaskEnvironmentVariable(name="THRESHOLD", value=sfn.JsonPath.string_at("$.job.threshold")),
                        sfn_tasks.TaskEnvironmentVariable(name="BACKEND", value=sfn.JsonPath.string_at("$.job.backend")),
                        sfn_tasks.TaskEnvironmentVariable(name="CURVE", value=sfn.JsonPath.string_at("$.job.curve")),
                        sfn_tasks.TaskEnvironmentVariable(name="N_INPUTS", value=sfn.JsonPath.string_at("$.job.n_inputs")),
                    ],
                ),
            ],
            result_path=sfn.JsonPath.DISCARD,
        )
        run_orchestrator.add_catch(
            release_lock_on_failure,
            errors=["States.ALL"],
            result_path="$.run_error",
        )

        definition = unwrap_input.next(acquire_lock).next(run_orchestrator).next(release_lock)

        return sfn.StateMachine(
            self, "JobStateMachine",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            state_machine_type=sfn.StateMachineType.STANDARD,
            timeout=Duration.hours(2),
        )

    def _add_pipe(self, jobs_queue: sqs.Queue, state_machine: sfn.StateMachine) -> pipes.CfnPipe:
        pipe_role = iam.Role(self, "PipeRole", assumed_by=iam.ServicePrincipal("pipes.amazonaws.com"))
        jobs_queue.grant_consume_messages(pipe_role)
        state_machine.grant_start_execution(pipe_role)

        return pipes.CfnPipe(
            self, "JobsPipe",
            role_arn=pipe_role.role_arn,
            source=jobs_queue.queue_arn,
            source_parameters=pipes.CfnPipe.PipeSourceParametersProperty(
                sqs_queue_parameters=pipes.CfnPipe.PipeSourceSqsQueueParametersProperty(batch_size=1),
            ),
            target=state_machine.state_machine_arn,
            target_parameters=pipes.CfnPipe.PipeTargetParametersProperty(
                # Standard-workflow executions are asynchronous, so this is a
                # fire-and-forget StartExecution - it does NOT wait for the
                # execution (or the job it runs) to finish. That's why
                # serialization is enforced by the DynamoDB lock inside the
                # state machine, not by this pipe or the queue's FIFO group.
                step_function_state_machine_parameters=pipes.CfnPipe.PipeTargetStateMachineParametersProperty(
                    invocation_type="FIRE_AND_FORGET",
                ),
                # For Step Functions targets, EventBridge applies the input
                # template per record, not to the batch array as a whole, so
                # $ here is a single SQS record (no [0] indexing needed/valid).
                # A bare <$.body> would strip the quotes off the parsed JSON
                # object and produce invalid JSON, which AWS docs call out as
                # a documented failure mode for JSON targets like Step
                # Functions - wrapping it under a key avoids that.
                input_template='{"job": <$.body>}',
            ),
        )

    def _add_api(self, uploads_bucket: s3.Bucket, jobs_table: dynamodb.Table, jobs_queue: sqs.Queue, log_group: logs.LogGroup):
        presign_fn = _lambda.Function(
            self, "PresignFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="presign.handler",
            code=_lambda.Code.from_asset("lambdas"),
            environment={"UPLOADS_BUCKET": uploads_bucket.bucket_name},
            timeout=Duration.seconds(10),
        )
        uploads_bucket.grant_put(presign_fn)

        submit_fn = _lambda.Function(
            self, "SubmitJobFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="submit_job.handler",
            code=_lambda.Code.from_asset("lambdas"),
            environment={
                "JOBS_TABLE_NAME": jobs_table.table_name,
                "QUEUE_URL": jobs_queue.queue_url,
            },
            timeout=Duration.seconds(10),
        )
        jobs_table.grant_write_data(submit_fn)
        jobs_queue.grant_send_messages(submit_fn)

        status_fn = _lambda.Function(
            self, "GetJobStatusFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="get_status.handler",
            code=_lambda.Code.from_asset("lambdas"),
            environment={"JOBS_TABLE_NAME": jobs_table.table_name},
            timeout=Duration.seconds(10),
        )
        jobs_table.grant_read_data(status_fn)

        logs_fn = _lambda.Function(
            self, "GetJobLogsFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="get_logs.handler",
            code=_lambda.Code.from_asset("lambdas"),
            environment={
                "JOBS_TABLE_NAME": jobs_table.table_name,
                "LOG_GROUP_NAME": log_group.log_group_name,
            },
            timeout=Duration.seconds(10),
        )
        jobs_table.grant_read_data(logs_fn)
        log_group.grant(logs_fn, "logs:GetLogEvents")

        api = apigateway.RestApi(
            self, "UserApi",
            rest_api_name="stoffel-user-api",
            deploy_options=apigateway.StageOptions(stage_name="prod"),
        )

        programs = api.root.add_resource("programs")
        programs.add_resource("presign").add_method(
            "POST", apigateway.LambdaIntegration(presign_fn), api_key_required=True,
        )

        jobs_resource = api.root.add_resource("jobs")
        jobs_resource.add_method(
            "POST", apigateway.LambdaIntegration(submit_fn), api_key_required=True,
        )
        job_resource = jobs_resource.add_resource("{job_id}")
        job_resource.add_method(
            "GET", apigateway.LambdaIntegration(status_fn), api_key_required=True,
        )
        job_resource.add_resource("logs").add_method(
            "GET", apigateway.LambdaIntegration(logs_fn), api_key_required=True,
        )

        # This default key/plan are CDK-managed so `cdk deploy` never breaks on
        # a fresh stack. Additional per-user keys don't need a redeploy - they're
        # created directly against this same usage plan with `./add-api-key
        # <username>` (see README's "Onboarding additional users").
        api_key = api.add_api_key("DefaultApiKey")
        plan = api.add_usage_plan(
            "UsagePlan",
            name="stoffel-user-plan",
            throttle=apigateway.ThrottleSettings(rate_limit=5, burst_limit=10),
            quota=apigateway.QuotaSettings(limit=1000, period=apigateway.Period.DAY),
        )
        plan.add_api_key(api_key)
        plan.add_api_stage(stage=api.deployment_stage)

        return api, api_key, plan


app = cdk.App()
StoffelUserDeploymentStack(
    app, "StoffelUserDeploymentStack",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)
app.synth()
