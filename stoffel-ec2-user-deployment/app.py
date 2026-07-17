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
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_pipes as pipes,
    aws_s3 as s3,
    aws_sqs as sqs,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as sfn_tasks,
)
from constructs import Construct


class StoffelEc2UserDeploymentStack(Stack):
    """
    User-facing StoffelVM MPC deployment on long-lived EC2 instances instead
    of ECS Fargate - both for the coordinator/party compute layer *and* the
    orchestrator, unlike a straight EC2 port of ../stoffel-fargate-user-deployment
    would be. The goal driving every difference from that stack is minimizing
    the time between "user submits a job" and "nodes are actually running":

      - Coordinator + party nodes are persistent EC2 instances (one process
        per node, started once by `cdk deploy` and left running - see
        ../stoffel-ec2-cross-region-deployment, which this compute layer
        mirrors). Each has an Elastic IP allocated independently of instance
        launch, so every node's address is a plain CloudFormation output the
        moment `cdk deploy` finishes - no ENI-attachment wait, no DNS
        propagation wait, unlike a fresh-per-run Fargate task. A job just
        docker-runs a fresh container on an already-running instance via SSM
        Run Command; there is no task launch or image pull in the hot path
        (images are pre-pulled into every instance's user data at boot).
      - The orchestrator is NOT a Fargate task (which would reintroduce the
        exact ENI/image-pull latency this design avoids on the compute side).
        It's three short-lived Lambda functions instead - StartJob,
        CheckJobStatus, FinishJob - driven by a Step Functions poll loop
        (Wait + CheckJobStatus + Choice) rather than one long-blocking
        process. Lambda cold starts are low-single-digit seconds at worst,
        far below a Fargate task's ENI attachment + image pull; and because
        the loop is a Step Functions Wait state, not a running Lambda, a job
        that takes an hour to finish costs nothing extra in orchestration
        compute while it waits.
      - Programs are uploaded straight to S3 via a presigned URL, same as
        the Fargate deployment, then pulled directly onto each party
        instance's local disk by the SSM-delivered start script (`aws s3
        cp`) - no EFS. EFS made sense for the Fargate deployment because
        party tasks are ephemeral and need a *shared* persistent volume
        between runs; these instances are themselves persistent, so a local
        disk copy refreshed per job is simpler and matches
        stoffel-ec2-cross-region-deployment's approach.
      - Job requests are still queued on a FIFO SQS queue, and the state
        machine still acquires a DynamoDB lock (binary semaphore) before
        starting a job and releases it after - only one job's coordinator +
        parties run at a time, for the same reason as the Fargate
        deployment: the shared cluster uses fixed ports, so true parallel
        execution isn't supported regardless of compute layer.

    Users still run their own MPC client (run-client) against the party
    endpoints returned by the job-status endpoint - this stack does not run
    the client role or return captured results on a user's behalf.

    CDK context (pass via --context key=value):
      auth_token   - STOFFEL_AUTH_TOKEN, baked into every instance's
                     /etc/stoffel-env at first boot (optional, default: "")
      num_parties  - number of always-on party instances to deploy
                     (optional, default: 5); must be between 2*threshold+1
                     and N_PARTIES_MAX (10)
      threshold    - MPC threshold t (optional, default: 1); bounds
                     num_parties at deploy time. A given job can still use
                     fewer parties than are deployed (down to 2t+1 for
                     whatever threshold that job specifies), via run-program
                     / POST /jobs's own --num-parties/--threshold.

    Operator workflow for manually starting nodes/clients against an
    already-uploaded program (run-nodes, run-client) still works directly
    against this stack's outputs. Program upload happens exclusively via the
    API (presign + StartJob Lambda) - there is no bastion/SSH path onto any
    instance in this stack.
    """

    N_PARTIES_MAX = 10
    DEFAULT_NUM_PARTIES = 5
    DEFAULT_THRESHOLD = 1
    COORD_INSTANCE_TYPE = "t3.small"
    NODE_INSTANCE_TYPE = "t3.small"
    CONTAINER_NAME = "stoffel-run"

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        auth_token = self.node.try_get_context("auth_token") or ""

        threshold_ctx = self.node.try_get_context("threshold")
        threshold = self.DEFAULT_THRESHOLD if threshold_ctx is None else int(threshold_ctx)
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1; got {threshold}")

        num_parties_ctx = self.node.try_get_context("num_parties")
        num_parties = self.DEFAULT_NUM_PARTIES if num_parties_ctx is None else int(num_parties_ctx)
        min_parties = 2 * threshold + 1
        if not (min_parties <= num_parties <= self.N_PARTIES_MAX):
            raise ValueError(
                f"num_parties must be between {min_parties} and {self.N_PARTIES_MAX} "
                f"for threshold {threshold}; got {num_parties}"
            )

        # No NAT Gateway: every instance runs in a public subnet with its own
        # Elastic IP - coordinator/parties need that anyway for external MPC
        # clients to reach them directly, and there's nothing in a private
        # subnet here that would need one.
        vpc = ec2.Vpc(
            self, "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("172.32.0.0/16"),
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
            ],
        )
        # Lets party instances fetch uploaded programs from S3 without
        # routing over the public internet (free, no data-processing charge).
        vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)

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

        log_group = logs.LogGroup(self, "Logs", retention=logs.RetentionDays.ONE_WEEK)

        coord_image = ecr_assets.DockerImageAsset(
            self, "CoordImage",
            directory="../stoffel-mpc-coordinator",
            platform=ecr_assets.Platform.LINUX_AMD64,
            file="Dockerfile.benchmark",
        )
        party_image = ecr_assets.DockerImageAsset(
            self, "PartyImage",
            directory="../StoffelVM",
            platform=ecr_assets.Platform.LINUX_AMD64,
            file="Dockerfile.benchmark-flexible",
        )

        uploads_bucket = self._add_uploads_bucket()

        coord_instance, coord_eip = self._add_coordinator(vpc, sg, log_group, coord_image, auth_token)
        parties = [
            self._add_party(i, vpc, sg, log_group, party_image, auth_token, uploads_bucket)
            for i in range(num_parties)
        ]

        CfnOutput(self, "VpcId", value=vpc.vpc_id)
        CfnOutput(self, "SecurityGroupId", value=sg.security_group_id)
        CfnOutput(self, "LogGroupName", value=log_group.log_group_name)
        CfnOutput(self, "DeployedParties", value=str(num_parties))
        CfnOutput(self, "Threshold", value=str(threshold))
        CfnOutput(self, "CoordImageUri", value=coord_image.image_uri)
        CfnOutput(self, "PartyImageUri", value=party_image.image_uri)
        CfnOutput(self, "CoordInstanceId", value=coord_instance.instance_id)
        CfnOutput(self, "CoordPublicIp", value=coord_eip.attr_public_ip)
        for i, (instance, eip) in enumerate(parties):
            CfnOutput(self, f"Party{i}InstanceId", value=instance.instance_id)
            CfnOutput(self, f"Party{i}PublicIp", value=eip.attr_public_ip)

        # ------------------------------------------------------------------ #
        # User-facing layer: uploads bucket (above), job/lock tables, queue, #
        # orchestration Lambdas, state machine, pipe, API.                  #
        # ------------------------------------------------------------------ #
        jobs_table, lock_table = self._add_tables()
        jobs_queue, jobs_dlq = self._add_queue()

        start_job_fn, check_job_status_fn, finish_job_fn = self._add_orchestration_lambdas(
            jobs_table, uploads_bucket, log_group,
            coord_instance, coord_eip, coord_image,
            parties, party_image,
        )

        state_machine = self._add_state_machine(lock_table, start_job_fn, check_job_status_fn, finish_job_fn)

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

    # ---------------------------------------------------------------------- #
    # Compute layer: persistent EC2 instances                                #
    # ---------------------------------------------------------------------- #

    def _base_user_data(self, instance: ec2.Instance, image: ecr_assets.DockerImageAsset, auth_token: str):
        # Pre-pulling the image at boot means the very first real job never
        # pays a cold `docker pull` - the whole point of this deployment is
        # minimizing launch latency, and a multi-hundred-MB pull would
        # otherwise dominate the first run after every `cdk deploy`.
        registry = image.image_uri.split("/")[0]
        instance.user_data.add_commands(
            "dnf install -y docker",
            "systemctl enable --now docker",
            "usermod -aG docker ec2-user",
            f"echo 'STOFFEL_AUTH_TOKEN={auth_token}' > /etc/stoffel-env",
            f"aws ecr get-login-password --region {self.region} | docker login --username AWS --password-stdin {registry}",
            f"docker pull {image.image_uri}",
        )

    def _add_eip(self, id_prefix: str, instance: ec2.Instance) -> ec2.CfnEIP:
        # Allocated independently of the instance - this is what makes every
        # node's address known right after `cdk deploy`, before any
        # container ever runs, instead of only after a task/container
        # starts. See stoffel-ec2-cross-region-deployment/app.py for the
        # same pattern applied per-region.
        eip = ec2.CfnEIP(self, f"{id_prefix}Eip", domain="vpc")
        ec2.CfnEIPAssociation(
            self, f"{id_prefix}EipAssoc",
            allocation_id=eip.attr_allocation_id,
            instance_id=instance.instance_id,
        )
        return eip

    def _add_coordinator(
        self,
        vpc: ec2.Vpc,
        sg: ec2.SecurityGroup,
        log_group: logs.LogGroup,
        image: ecr_assets.DockerImageAsset,
        auth_token: str,
    ):
        instance = ec2.Instance(
            self, "CoordInstance",
            instance_type=ec2.InstanceType(self.COORD_INSTANCE_TYPE),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=sg,
            associate_public_ip_address=True,
            # SSM Run Command is how StartJob/CheckJobStatus/FinishJob drive
            # this instance's container for each job - no SSH key needed.
            ssm_session_permissions=True,
        )
        image.repository.grant_pull(instance.role)
        log_group.grant_write(instance.role)
        self._base_user_data(instance, image, auth_token)

        eip = self._add_eip("Coord", instance)
        return instance, eip

    def _add_party(
        self,
        party_id: int,
        vpc: ec2.Vpc,
        sg: ec2.SecurityGroup,
        log_group: logs.LogGroup,
        image: ecr_assets.DockerImageAsset,
        auth_token: str,
        uploads_bucket: s3.Bucket,
    ):
        instance = ec2.Instance(
            self, f"Party{party_id}Instance",
            instance_type=ec2.InstanceType(self.NODE_INSTANCE_TYPE),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=sg,
            associate_public_ip_address=True,
            ssm_session_permissions=True,
        )
        image.repository.grant_pull(instance.role)
        log_group.grant_write(instance.role)
        uploads_bucket.grant_read(instance.role)
        self._base_user_data(instance, image, auth_token)
        instance.user_data.add_commands("mkdir -p /home/ec2-user/programs")

        eip = self._add_eip(f"Party{party_id}", instance)
        return instance, eip

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
            # retaining once a party instance has copied them onto local disk.
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

    # ---------------------------------------------------------------------- #
    # Orchestration: Lambda functions driving SSM Run Command on the        #
    # persistent instances above, in place of the Fargate deployment's      #
    # single orchestrator ECS task.                                         #
    # ---------------------------------------------------------------------- #

    def _add_orchestration_lambdas(
        self,
        jobs_table: dynamodb.Table,
        uploads_bucket: s3.Bucket,
        log_group: logs.LogGroup,
        coord_instance: ec2.Instance,
        coord_eip: ec2.CfnEIP,
        coord_image: ecr_assets.DockerImageAsset,
        parties: list,
        party_image: ecr_assets.DockerImageAsset,
    ):
        instance_ids = [coord_instance.instance_id] + [p[0].instance_id for p in parties]
        instance_arns = [
            f"arn:{self.partition}:ec2:{self.region}:{self.account}:instance/{iid}"
            for iid in instance_ids
        ]
        document_arn = f"arn:{self.partition}:ssm:{self.region}::document/AWS-RunShellScript"

        env = {
            "JOBS_TABLE_NAME": jobs_table.table_name,
            "PROGRAM_S3_BUCKET": uploads_bucket.bucket_name,
            "LOG_GROUP_NAME": log_group.log_group_name,
            "CONTAINER_NAME": self.CONTAINER_NAME,
            "COORD_INSTANCE_ID": coord_instance.instance_id,
            "COORD_PRIVATE_IP": coord_instance.instance_private_ip,
            "COORD_PUBLIC_IP": coord_eip.attr_public_ip,
            "COORD_IMAGE_URI": coord_image.image_uri,
            "PARTY_INSTANCE_IDS": ",".join(p[0].instance_id for p in parties),
            "PARTY_PRIVATE_IPS": ",".join(p[0].instance_private_ip for p in parties),
            "PARTY_PUBLIC_IPS": ",".join(p[1].attr_public_ip for p in parties),
            "PARTY_IMAGE_URI": party_image.image_uri,
        }

        def make_fn(name: str, handler: str) -> _lambda.Function:
            fn = _lambda.Function(
                self, name,
                runtime=_lambda.Runtime.PYTHON_3_12,
                handler=handler,
                code=_lambda.Code.from_asset("lambdas"),
                environment=env,
                timeout=Duration.seconds(120),
                memory_size=256,
            )
            jobs_table.grant_read_write_data(fn)
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["ssm:SendCommand"],
                resources=instance_arns + [document_arn],
            ))
            # GetCommandInvocation doesn't support resource-level scoping.
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["ssm:GetCommandInvocation"],
                resources=["*"],
            ))
            return fn

        start_job_fn = make_fn("StartJobFn", "start_job.handler")
        check_job_status_fn = make_fn("CheckJobStatusFn", "check_job_status.handler")
        finish_job_fn = make_fn("FinishJobFn", "finish_job.handler")
        return start_job_fn, check_job_status_fn, finish_job_fn

    def _add_state_machine(
        self,
        lock_table: dynamodb.Table,
        start_job_fn: _lambda.Function,
        check_job_status_fn: _lambda.Function,
        finish_job_fn: _lambda.Function,
    ) -> sfn.StateMachine:
        lock_key = sfn_tasks.DynamoAttributeValue.from_string("jobs")

        # The pipe always hands Step Functions targets an array-wrapped
        # payload ([{"job": {...}}]), even for a single record (see
        # _add_pipe) - unwrap it as the very first state.
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

        # Shared failure path for StartJob/CheckJobStatus/FinishJob: best-
        # effort stop the coordinator container and mark the job FAILED in
        # DynamoDB (FinishJob does both, keyed off "outcome.status"), then
        # release the lock so the next queued job isn't blocked forever.
        handle_failure = sfn_tasks.LambdaInvoke(
            self, "HandleFailure",
            lambda_function=finish_job_fn,
            payload=sfn.TaskInput.from_object({
                "job": sfn.JsonPath.string_at("$.job"),
                "outcome": {
                    "status": "FAILED",
                    "error": sfn.JsonPath.string_at("$.run_error"),
                },
            }),
            payload_response_only=True,
            result_path=sfn.JsonPath.DISCARD,
        )
        handle_failure.next(release_lock_on_failure)

        start_job = sfn_tasks.LambdaInvoke(
            self, "StartJob",
            lambda_function=start_job_fn,
            payload=sfn.TaskInput.from_object({"job": sfn.JsonPath.string_at("$.job")}),
            payload_response_only=True,
            result_path="$.start_result",
        )
        start_job.add_catch(handle_failure, errors=["States.ALL"], result_path="$.run_error")

        wait_poll = sfn.Wait(self, "WaitPoll", time=sfn.WaitTime.duration(Duration.seconds(10)))

        check_job_status = sfn_tasks.LambdaInvoke(
            self, "CheckJobStatus",
            lambda_function=check_job_status_fn,
            payload=sfn.TaskInput.from_object({"job": sfn.JsonPath.string_at("$.job")}),
            payload_response_only=True,
            result_path="$.check_result",
        )
        check_job_status.add_catch(handle_failure, errors=["States.ALL"], result_path="$.run_error")

        finish_job = sfn_tasks.LambdaInvoke(
            self, "FinishJob",
            lambda_function=finish_job_fn,
            payload=sfn.TaskInput.from_object({
                "job": sfn.JsonPath.string_at("$.job"),
                "outcome": sfn.JsonPath.string_at("$.check_result"),
            }),
            payload_response_only=True,
            result_path=sfn.JsonPath.DISCARD,
        )
        finish_job.add_catch(handle_failure, errors=["States.ALL"], result_path="$.run_error")
        finish_job.next(release_lock)

        is_running = sfn.Choice(self, "IsRunning")
        is_running.when(
            sfn.Condition.string_equals("$.check_result.job_status", "RUNNING"),
            wait_poll,
        ).otherwise(finish_job)

        start_job.next(wait_poll)
        wait_poll.next(check_job_status)
        check_job_status.next(is_running)

        chain = unwrap_input.next(acquire_lock).next(start_job)

        return sfn.StateMachine(
            self, "JobStateMachine",
            definition_body=sfn.DefinitionBody.from_chainable(chain),
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
                # object and produce invalid JSON - wrapping it under a key
                # avoids that.
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
            rest_api_name="stoffel-ec2-user-api",
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

        # One key/user by default; onboard additional users with
        # ./add-api-key <username> (no redeploy needed - see README).
        api_key = api.add_api_key("DefaultApiKey")
        plan = api.add_usage_plan(
            "UsagePlan",
            name="stoffel-ec2-user-plan",
            throttle=apigateway.ThrottleSettings(rate_limit=5, burst_limit=10),
            quota=apigateway.QuotaSettings(limit=1000, period=apigateway.Period.DAY),
        )
        plan.add_api_key(api_key)
        plan.add_api_stage(stage=api.deployment_stage)

        return api, api_key, plan


app = cdk.App()
StoffelEc2UserDeploymentStack(
    app, "StoffelEc2UserDeploymentStack",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)
app.synth()
