#!/usr/bin/env python3

import os
import aws_cdk as cdk
from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    DockerImage,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigateway,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as cloudfront_origins,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
)
from constructs import Construct

# Absolute, not "../stoffel-mpc-coordinator": DockerImageAsset stages
# PartyImage's build context (../StoffelVM) into a temp/cdk.out directory
# before invoking `docker build`, so a relative --build-context path
# computed against app.py's own cwd no longer points at the right place
# once docker actually runs from that staged copy - see PartyImage below.
COORDINATOR_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "stoffel-mpc-coordinator")
)

# Same reasoning as COORDINATOR_DIR - _add_web_site's bundling below also
# runs `docker build` against this path.
STOFFEL_VM_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "StoffelVM")
)


class StoffelEc2UserDeploymentPersistentStack(Stack):
    """
    User-facing StoffelVM MPC deployment on long-lived EC2 instances running
    stoffel-run in *standing* mode - see ../StoffelVM's
    StandingProgramCatalog / standing_control.rs and
    ../stoffel-docker-compose-persistent's README for the underlying
    concept. This is the standing-node counterpart of
    ../stoffel-ec2-user-deployment (the "one-off" deployment): both give
    external users a self-service, API-key-authenticated way to run MPC
    programs on a shared EC2 cluster, but they differ in what "running a
    program" actually restarts:

      - stoffel-ec2-user-deployment: every job gets a fresh `docker run` of
        stoffel-run/run-coord that exits when the job finishes - only one
        job's coordinator+parties run at a time (a DynamoDB lock + FIFO SQS
        queue serialize submissions), because the container itself IS the
        job.
      - This deployment: the coordinator + every party are started ONCE by
        the operator (./deploy) and stay running indefinitely, using
        stoffel-run's `--standing-node` control plane
        (../stoffel-ec2-cross-region-deployment/standing-deploy applies the
        same idea across regions; this stack mirrors it for a single
        region). "Running a program" becomes admitting a fresh *execution*
        onto the already-running mesh via a control command, not restarting
        any container - so many executions run concurrently, and there is
        no cluster-wide queue or lock for job execution.

    Programs are staged exclusively by the operator (./upload-program),
    never by API users: StoffelVM's standing-node program catalog is loaded
    once when a party's container starts and never rescanned afterward
    (StandingProgramCatalog::load), so a newly staged program only becomes
    admissible after the operator redeploys the mesh (./deploy) - an
    operation that briefly interrupts every other in-flight execution on
    the shared mesh. Letting arbitrary API users trigger that at will would
    mean any one user's upload could kill every other user's running job at
    an unpredictable time, so program upload deliberately has no API-key
    path at all here (contrast stoffel-ec2-user-deployment, where every job
    is already an isolated container and this tradeoff doesn't exist).
    API users can only ask to *run* an already-staged program (GET
    /programs to discover what's staged, POST /executions to run one).

    Admitting an execution still requires serializing *delivery* of control
    commands to each party (StoffelVM's standing control journal requires a
    strictly contiguous per-party sequence number - see
    lambdas/party_control.py for why AWS SSM Run Command alone can't
    guarantee that under concurrent requests), but that serialization is
    per-party and held only for the few seconds of one SSM round trip, not
    for an execution's entire runtime - a fundamentally smaller and
    shorter-lived form of contention than the one-off deployment's
    whole-cluster job lock.

    CDK context (pass via --context key=value):
      auth_token   - STOFFEL_AUTH_TOKEN, baked into every instance's
                     /etc/stoffel-env at first boot (optional, default: "")
      num_parties  - number of always-on party instances to deploy
                     (optional, default: 5); must be between 2*threshold+1
                     and N_PARTIES_MAX (10). Unlike the one-off deployment,
                     this is also the party count the standing mesh always
                     runs with - ./deploy does not support starting a
                     subset of the deployed parties.
      threshold    - MPC threshold t (optional, default: 1); bounds
                     num_parties at deploy time. ./deploy can still choose
                     a different (valid) threshold for the live mesh
                     without a `cdk deploy` - see its --threshold flag -
                     since threshold is a `--standing-node` process flag,
                     not infrastructure.

    Operator workflow: `cdk deploy` (provisions instances, empty of any
    running container) -> `./upload-ids` (syncs ids/ to the assets bucket)
    -> `./upload-program` for each program users should be able to run ->
    `./deploy` (starts/restarts the standing coordinator + every party,
    picking up whatever's currently staged) -> hand out API keys
    (./add-api-key). There is no bastion/SSH path onto any instance in this
    stack; all operator actions go through SSM Run Command, same as
    ../stoffel-ec2-user-deployment.
    """

    N_PARTIES_MAX = 10
    DEFAULT_NUM_PARTIES = 5
    DEFAULT_THRESHOLD = 1
    # Graviton (arm64), not the t3 (x86_64) family used by the other
    # deployments in this repo - both Dockerfile.benchmark builds compile
    # from source against generic multi-arch base images with no
    # architecture-specific code, so this runs natively end-to-end: no QEMU
    # cross-compilation for an Apple Silicon operator building locally, and
    # no emulation on the instances themselves either (unlike forcing
    # linux/amd64 images onto t3 while building on an arm64 laptop, which
    # cross-compiles on the way in and runs natively on the way out - or the
    # reverse mismatch of arm64 images on x86_64 instances).
    COORD_INSTANCE_TYPE = "t4g.small"
    NODE_INSTANCE_TYPE = "t4g.small"

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
            ip_addresses=ec2.IpAddresses.cidr("172.33.0.0/16"),
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
        # Lets party instances fetch staged programs/ids from S3 without
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
        # External browser-TLS ports (optional; only listened on when ./upload-browser-tls-cert
        # has staged a cert - see ./deploy). Browsers can't use the native mTLS ports above at
        # all (no browser API can present a TLS client certificate), so they get their own
        # ordinary server-authenticated-TLS ports instead: 31416 for the coordinator, 18180+<party
        # id> for each party, mirroring the native 31415/16180+<party id> pairing above.
        for port in [31416, 18180, 18181, 18182, 18183, 18184, 18185, 18186, 18187, 18188, 18189]:
            sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(port))

        log_group = logs.LogGroup(self, "Logs", retention=logs.RetentionDays.ONE_WEEK)

        coord_image = ecr_assets.DockerImageAsset(
            self, "CoordImage",
            directory="../stoffel-mpc-coordinator",
            platform=ecr_assets.Platform.LINUX_ARM64,
            file="Dockerfile.benchmark",
        )
        party_image = ecr_assets.DockerImageAsset(
            self, "PartyImage",
            directory="../StoffelVM",
            platform=ecr_assets.Platform.LINUX_ARM64,
            file="Dockerfile.benchmark",
            # Dockerfile.benchmark's `COPY --from=coordinator . /coordinator`
            # names an external build context, not a stage in this
            # Dockerfile (see its own header comment) - without this, Docker
            # treats "coordinator" as an image reference and tries to pull
            # docker.io/library/coordinator:latest, which doesn't exist.
            build_contexts={"coordinator": COORDINATOR_DIR},
        )

        assets_bucket = self._add_assets_bucket()

        coord_instance, coord_eip = self._add_coordinator(vpc, sg, log_group, coord_image, auth_token, assets_bucket)
        parties = [
            self._add_party(i, vpc, sg, log_group, party_image, auth_token, assets_bucket)
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
        # User-facing layer: program catalog + execution tracking tables,   #
        # orchestration Lambdas, state machine, API.                        #
        # ------------------------------------------------------------------ #
        programs_table, executions_table, party_control_table, client_registrations_table = self._add_tables()

        admit_execution_fn = self._add_orchestration_lambdas(executions_table, party_control_table, parties)

        # Created before _add_api (rather than as part of one _add_web_site call, like
        # before) purely so its domain name - this system's WebAuthn RP ID, same value
        # ./deploy derives for the coordinator/parties - is available to pass into
        # _add_api's registration Lambdas. See _add_web_distribution's own comment for why
        # this doesn't actually require api.url to exist yet, even though the site's own
        # deployment step (_add_web_deployment, below) does.
        web_bucket, web_distribution = self._add_web_distribution()
        webauthn_rp_id = web_distribution.distribution_domain_name

        api, api_key, usage_plan = self._add_api(
            programs_table, executions_table, party_control_table, client_registrations_table,
            log_group, admit_execution_fn, parties, webauthn_rp_id,
        )

        # Every node's address is an Elastic IP fixed at `cdk deploy` time
        # (see _add_eip), identical for every execution ever admitted on
        # this mesh - unlike the operator API URL/key, there's no reason for
        # a browser to fetch this per-execution from admission's response,
        # so it rides along in the same config.json as apiUrl instead (see
        # _add_web_deployment) and start.js/app.js build the vote link from that.
        endpoints = {
            "coordinator": f"{coord_eip.attr_public_ip}:31415",
            "coordinator_browser": f"{coord_eip.attr_public_ip}:31416",
        }
        for i, (_, eip) in enumerate(parties):
            endpoints[f"party{i}"] = f"{eip.attr_public_ip}:{16180 + i}"
            endpoints[f"party{i}_browser"] = f"{eip.attr_public_ip}:{18180 + i}"

        self._add_web_deployment(web_bucket, web_distribution, api.url, endpoints)

        CfnOutput(self, "AssetsBucketName", value=assets_bucket.bucket_name)
        CfnOutput(self, "WebUrl", value=f"https://{web_distribution.distribution_domain_name}")
        # Operator steps, not part of any API flow: identity certs/keys and
        # staged programs land here via ./upload-ids / ./upload-program
        # before the first ./deploy - see _add_party.
        CfnOutput(self, "IdsS3Uri", value=f"s3://{assets_bucket.bucket_name}/ids/")
        CfnOutput(self, "ProgramsS3Uri", value=f"s3://{assets_bucket.bucket_name}/standing-programs/")
        CfnOutput(self, "ProgramsTableName", value=programs_table.table_name)
        CfnOutput(self, "ExecutionsTableName", value=executions_table.table_name)
        # Read by ./deploy to self-heal each party's DynamoDB command-sequence counter to
        # match its actual local control journal right after that party reports ready - see
        # the sync step after wait_ssm below for why this is needed (party_control.py's `seq`
        # is a standalone counter that outlives any one EC2 instance/local-state generation,
        # while StandingNodeControl's own cursor - StoffelVM's standing_control.rs load_cursor
        # - is derived purely from that instance's local events/party{N}/ directory; the two
        # can drift apart whenever a party's local state is fresher than the counter, which
        # wedges admission forever since every future command is delivered at a sequence
        # number no party is still waiting for).
        CfnOutput(self, "PartyControlTableName", value=party_control_table.table_name)
        CfnOutput(self, "ClientRegistrationsTableName", value=client_registrations_table.table_name)
        CfnOutput(self, "ApiUrl", value=api.url)
        # Read by ./add-api-key to create a dedicated per-key usage plan (rate limits apply
        # per plan, identically to every key in it - there's no per-key override within one
        # shared plan, hence a whole new plan per key that wants its own limit).
        CfnOutput(self, "ApiId", value=api.rest_api_id)
        CfnOutput(self, "ApiKeyId", value=api_key.key_id)
        CfnOutput(self, "UsagePlanId", value=usage_plan.usage_plan_id)

    # ---------------------------------------------------------------------- #
    # Compute layer: persistent EC2 instances, prepared but not started -   #
    # ./deploy (operator, over SSM) is the only thing that ever runs the    #
    # standing coordinator/party containers, same division of responsibility#
    # as stoffel-ec2-cross-region-deployment/standing-deploy.               #
    # ---------------------------------------------------------------------- #

    def _base_user_data(self, instance: ec2.Instance, image: ecr_assets.DockerImageAsset, auth_token: str):
        # Pre-pulling the image at boot means the first ./deploy never pays a
        # cold `docker pull` for it. ./deploy re-pulls before every start
        # anyway (so a redeployed image is always picked up without
        # replacing the instance), but a multi-hundred-MB pull on top of an
        # otherwise-fast SSM round trip would make that first ./deploy after
        # `cdk deploy` needlessly slow.
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
        # Allocated independently of the instance - every node's address is
        # known right after `cdk deploy`, before ./deploy ever runs a
        # container. See stoffel-ec2-cross-region-deployment/app.py for the
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
        assets_bucket: s3.Bucket,
    ):
        instance = ec2.Instance(
            self, "CoordInstance",
            instance_type=ec2.InstanceType(self.COORD_INSTANCE_TYPE),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(cpu_type=ec2.AmazonLinuxCpuType.ARM_64),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=sg,
            associate_public_ip_address=True,
            # SSM Run Command is how ./deploy starts this instance's
            # container, and how the orchestration Lambdas drive party
            # control commands - no SSH key needed anywhere in this stack.
            ssm_session_permissions=True,
        )
        image.repository.grant_pull(instance.role)
        log_group.grant_write(instance.role)
        # ./deploy's build_coord_script does `aws s3 cp --recursive
        # s3://<bucket>/ids/ ...` on this instance too (the coordinator
        # needs the roster's public certs for --initial-mpc-nodes, plus its
        # own cert/key) - same reasoning as _add_party's grant below.
        assets_bucket.grant_read(instance.role)
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
        assets_bucket: s3.Bucket,
    ):
        instance = ec2.Instance(
            self, f"Party{party_id}Instance",
            instance_type=ec2.InstanceType(self.NODE_INSTANCE_TYPE),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(cpu_type=ec2.AmazonLinuxCpuType.ARM_64),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=sg,
            associate_public_ip_address=True,
            ssm_session_permissions=True,
        )
        image.repository.grant_pull(instance.role)
        log_group.grant_write(instance.role)
        # ./deploy syncs both the ids/ tree and staged standing-programs/
        # onto this instance's local disk fresh on every run (see its
        # comments) - identity certs/keys are not baked into the party image
        # (../StoffelVM/Dockerfile.benchmark-flexible mounts /app/ids at
        # `docker run` time instead), and programs must be local for
        # StandingProgramCatalog::load's --program-dir scan.
        assets_bucket.grant_read(instance.role)
        self._base_user_data(instance, image, auth_token)

        eip = self._add_eip(f"Party{party_id}", instance)
        return instance, eip

    def _add_assets_bucket(self) -> s3.Bucket:
        return s3.Bucket(
            self, "AssetsBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            # No CORS/presigned-PUT config: unlike the one-off deployment's
            # uploads bucket, nothing ever PUTs to this bucket directly from
            # a browser/API caller - ids/ and standing-programs/ are staged
            # exclusively by the operator via the AWS CLI (./upload-ids,
            # ./upload-program). No lifecycle expiration either - staged
            # programs and ids are long-lived, not transient per-job
            # artifacts like the one-off deployment's uploads/ prefix.
        )

    def _add_web_distribution(self) -> tuple[s3.Bucket, cloudfront.Distribution]:
        # Split from the deployment step below (_add_web_deployment) so the distribution's
        # domain name - needed as WEBAUTHN_RP_ID for _add_api's registration Lambdas - is
        # available before _add_api runs, while the deployment step itself still runs after
        # _add_api (it needs api.url for config.json). Both are just CloudFormation tokens
        # under the hood - CDK/CloudFormation resolve the actual dependency graph from these
        # cross-references regardless of Python call order, so this split only needs to make
        # each piece's *object* exist before the other reads an attribute off it.
        #
        # Bucket is private; CloudFront reaches it via Origin Access Control,
        # not a public bucket policy, so the only public HTTP surface is
        # CloudFront's own default *.cloudfront.net domain - which also gets
        # HTTPS for free, unlike a plain S3 static-website-hosting endpoint.
        bucket = s3.Bucket(
            self, "WebBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )
        distribution = cloudfront.Distribution(
            self, "WebDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=cloudfront_origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            default_root_object="index.html",
        )
        return bucket, distribution

    def _add_web_deployment(
        self, bucket: s3.Bucket, distribution: cloudfront.Distribution, api_url: str, endpoints: dict,
    ) -> None:
        # Hosts the voting example's static web/ folder (index.html/start.html
        # + app.js/start.js/styles.css) - a mirror of
        # ../../stoffel-browser-examples/examples/voting/web, kept in sync by
        # hand the same way src/voting/main.stfl mirrors that repo's program.
        # The site is fully static: start.js/app.js resolve the coordinator
        # and API endpoints from URL query params or manual operator input
        # (see resolveEndpoints() in app.js), so nothing here needs
        # server-side rendering or a deploy-time secret baked into the page -
        # the API key stays something the operator hands out separately
        # (./add-api-key), never something this bucket serves.
        #
        # Re-syncs web/ and invalidates CloudFront on every `cdk deploy`, so
        # editing a page and redeploying is enough - no separate publish step.
        # config.json rides along as a second source in the same deployment:
        # api_url is a deploy-time CloudFormation value (the API Gateway
        # isn't provisioned yet at `cdk synth` time), so it can't be baked
        # into web/start.html as a literal at synth time the way the rest of
        # the page is authored - json_data lets CloudFormation resolve it
        # once the API actually exists, same deploy, no separate step. Only
        # the URL rides along, never the API key - that stays an operator-
        # entered field (see start.js), never baked into a public bucket.
        # endpoints (coordinator/party addresses) rides along too - those
        # are Elastic IPs fixed at `cdk deploy` time, identical for every
        # execution, so there is no reason to fetch them per-execution from
        # admission's response the way this used to work (see
        # admit_execution.py) - they're public IPs already handed to anyone
        # who ever admits an execution today, not a secret either way.
        #
        # index.html's app.js needs a fourth thing web/ doesn't have: the
        # compiled WASM client (`import ... from "./pkg/stoffel_wasm_client.js"`),
        # built from StoffelVM's crates/stoffel-wasm-client - not a static
        # file, so it can't just live under web/ like the rest of the site.
        # Rather than a manually-run-and-copied build artifact (which would
        # silently go stale the next time that crate changes), this reruns
        # ../StoffelVM/Dockerfile.wasm-client every `cdk deploy`, straight
        # from the same checkout PartyImage already builds the party binary
        # from below - same idea as that image building from source instead
        # of shipping a prebuilt binary, and no dependency beyond what this
        # stack already required.
        wasm_pkg_source = s3_deployment.Source.asset(
            STOFFEL_VM_DIR,
            bundling=BundlingOptions(
                image=DockerImage.from_build(STOFFEL_VM_DIR, file="Dockerfile.wasm-client"),
                command=["bash", "-c", "mkdir -p /asset-output/pkg && cp -r /build/web-pkg/. /asset-output/pkg/"],
            ),
        )
        s3_deployment.BucketDeployment(
            self, "WebDeployment",
            sources=[
                s3_deployment.Source.asset("./web"),
                s3_deployment.Source.json_data("config.json", {"apiUrl": api_url, "endpoints": endpoints}),
                wasm_pkg_source,
            ],
            destination_bucket=bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

    def _add_tables(self):
        # Operator-managed program catalog: name -> program_id. Populated by
        # ./upload-program (direct AWS CLI/boto3, no Lambda in the write
        # path - see that script). Read by submit_execution.py (name
        # lookup) and list_programs.py (GET /programs).
        programs_table = dynamodb.Table(
            self, "ProgramsTable",
            partition_key=dynamodb.Attribute(name="name", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        executions_table = dynamodb.Table(
            self, "ExecutionsTable",
            partition_key=dynamodb.Attribute(name="execution_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # One item per deployed party (party_id "0".."N-1", created lazily on
        # first use). Each item's `seq` is that party's next standing-control
        # command sequence number, and `lock_holder`/`lock_expires_at` is a
        # short-lived lease serializing *delivery* of that command over SSM -
        # see lambdas/party_control.py for why both are needed together.
        party_control_table = dynamodb.Table(
            self, "PartyControlTable",
            partition_key=dynamodb.Attribute(name="party_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # One row per operator-issued registration link (token -> {client_name, label,
        # created_at}, written directly by ./generate-registration-link - no Lambda in that
        # write path, same pattern as ProgramsTable/./upload-program). register_client.py
        # atomically claims a row (adds credential_id/public_key/used_at) the first and only
        # time its token is redeemed. See lambdas/register_client.py's own docstring for how
        # a claimed row later becomes a `.crt` file admission can reference.
        client_registrations_table = dynamodb.Table(
            self, "ClientRegistrationsTable",
            partition_key=dynamodb.Attribute(name="token", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        return programs_table, executions_table, party_control_table, client_registrations_table

    # ---------------------------------------------------------------------- #
    # Orchestration: Lambda functions admitting/tracking executions on the  #
    # standing mesh above via its control plane, driven over SSM Run        #
    # Command - in place of the one-off deployment's docker-run-per-job.    #
    # ---------------------------------------------------------------------- #

    def _party_instance_arns(self, parties: list) -> list:
        return [
            f"arn:{self.partition}:ec2:{self.region}:{self.account}:instance/{p[0].instance_id}"
            for p in parties
        ]

    def _grant_ssm(self, fn: _lambda.Function, parties: list):
        document_arn = f"arn:{self.partition}:ssm:{self.region}::document/AWS-RunShellScript"
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:SendCommand"],
            resources=self._party_instance_arns(parties) + [document_arn],
        ))
        # GetCommandInvocation doesn't support resource-level scoping.
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetCommandInvocation"],
            resources=["*"],
        ))

    def _add_orchestration_lambdas(
        self,
        executions_table: dynamodb.Table,
        party_control_table: dynamodb.Table,
        parties: list,
    ):
        # Admission is the only orchestration step: it publishes `prepare` to
        # every party and records RUNNING/FAILED on the execution itself
        # (see admit_execution.py's own update_execution/fail helpers) - there
        # is no separate status-polling/cleanup step. The standing mesh's own
        # round-quorum (tolerates up to t missing/crashed parties) and the
        # coordinator's lazy retirement-on-capacity-pressure eviction already
        # handle an execution nobody ever finishes without any help from
        # here; nothing reads a terminal SUCCEEDED/FAILED-after-admission
        # status once RUNNING (voters talk to the coordinator/parties
        # directly - see app.js - and start.js stops polling once RUNNING).
        # No coordinator/party IPs in its environment either - see
        # admit_execution.py's docstring for where those come from instead.
        admit_execution_fn = _lambda.Function(
            self, "AdmitExecutionFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="admit_execution.handler",
            code=_lambda.Code.from_asset("lambdas"),
            environment={
                "EXECUTIONS_TABLE_NAME": executions_table.table_name,
                "PARTY_CONTROL_TABLE_NAME": party_control_table.table_name,
                "PARTY_INSTANCE_IDS": ",".join(p[0].instance_id for p in parties),
            },
            timeout=Duration.seconds(120),
            memory_size=256,
        )
        executions_table.grant_read_write_data(admit_execution_fn)
        party_control_table.grant_read_write_data(admit_execution_fn)
        self._grant_ssm(admit_execution_fn, parties)
        # submit_execution_fn invokes this asynchronously (InvocationType=
        # "Event") - by default AWS Lambda itself silently retries a failed
        # async invocation up to twice more, replaying the exact same
        # execution_id. Admission is not safe to replay: whichever parties
        # already succeeded have marked that execution_id retired (see
        # StoffelVM's standing_control.rs dispatch_command/Prepare), so a
        # Lambda-initiated retry either fails loudly with a confusing
        # "execution is retired" error (if the first attempt fully
        # succeeded) or corrupts the FAILED status already recorded in
        # DynamoDB with a second, unrelated attempt. A genuine failure
        # should surface once, not get silently replayed minutes later
        # against a mesh that has already moved on.
        admit_execution_fn.configure_async_invoke(retry_attempts=0)

        return admit_execution_fn

    def _add_api(
        self,
        programs_table: dynamodb.Table,
        executions_table: dynamodb.Table,
        party_control_table: dynamodb.Table,
        client_registrations_table: dynamodb.Table,
        log_group: logs.LogGroup,
        admit_execution_fn: _lambda.Function,
        parties: list,
        webauthn_rp_id: str,
    ):
        submit_fn = _lambda.Function(
            self, "SubmitExecutionFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="submit_execution.handler",
            code=_lambda.Code.from_asset("lambdas"),
            environment={
                "EXECUTIONS_TABLE_NAME": executions_table.table_name,
                "PROGRAMS_TABLE_NAME": programs_table.table_name,
                "ADMIT_EXECUTION_FUNCTION_NAME": admit_execution_fn.function_name,
            },
            timeout=Duration.seconds(10),
        )
        executions_table.grant_write_data(submit_fn)
        programs_table.grant_read_data(submit_fn)
        admit_execution_fn.grant_invoke(submit_fn)

        status_fn = _lambda.Function(
            self, "GetExecutionStatusFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="get_status.handler",
            code=_lambda.Code.from_asset("lambdas"),
            environment={"EXECUTIONS_TABLE_NAME": executions_table.table_name},
            timeout=Duration.seconds(10),
        )
        executions_table.grant_read_data(status_fn)

        logs_fn = _lambda.Function(
            self, "GetExecutionLogsFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="get_logs.handler",
            code=_lambda.Code.from_asset("lambdas"),
            environment={
                "EXECUTIONS_TABLE_NAME": executions_table.table_name,
                "LOG_GROUP_NAME": log_group.log_group_name,
                "PARTY_INSTANCE_IDS": ",".join(p[0].instance_id for p in parties),
            },
            timeout=Duration.seconds(20),
        )
        executions_table.grant_read_data(logs_fn)
        log_group.grant(logs_fn, "logs:FilterLogEvents", "logs:DescribeLogStreams")

        list_programs_fn = _lambda.Function(
            self, "ListProgramsFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="list_programs.handler",
            code=_lambda.Code.from_asset("lambdas"),
            environment={"PROGRAMS_TABLE_NAME": programs_table.table_name},
            timeout=Duration.seconds(10),
        )
        programs_table.grant_read_data(list_programs_fn)

        list_client_registrations_fn = _lambda.Function(
            self, "ListClientRegistrationsFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="list_client_registrations.handler",
            code=_lambda.Code.from_asset("lambdas"),
            environment={"CLIENT_REGISTRATIONS_TABLE_NAME": client_registrations_table.table_name},
            timeout=Duration.seconds(10),
        )
        client_registrations_table.grant_read_data(list_client_registrations_fn)

        cancel_common_env = {
            "EXECUTIONS_TABLE_NAME": executions_table.table_name,
            "PARTY_CONTROL_TABLE_NAME": party_control_table.table_name,
            "PARTY_INSTANCE_IDS": ",".join(p[0].instance_id for p in parties),
        }
        cancel_worker_fn = _lambda.Function(
            self, "CancelExecutionWorkerFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="cancel_execution.worker_handler",
            code=_lambda.Code.from_asset("lambdas"),
            environment=cancel_common_env,
            timeout=Duration.seconds(60),
            memory_size=256,
        )
        executions_table.grant_read_write_data(cancel_worker_fn)
        party_control_table.grant_read_write_data(cancel_worker_fn)
        self._grant_ssm(cancel_worker_fn, parties)

        # Split into api_handler (fast: validate + hand off) and
        # worker_handler (the actual per-party SSM round trips) as two
        # separate Lambda resources sharing lambdas/cancel_execution.py, so
        # the API response never risks API Gateway's 29s integration
        # timeout waiting on N parties' control locks - see that file.
        cancel_api_fn = _lambda.Function(
            self, "CancelExecutionApiFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="cancel_execution.api_handler",
            code=_lambda.Code.from_asset("lambdas"),
            environment={
                "EXECUTIONS_TABLE_NAME": executions_table.table_name,
                "WORKER_FUNCTION_NAME": cancel_worker_fn.function_name,
                # api_handler itself never touches party_control.py's
                # functions, but that module (imported by cancel_execution.py
                # at load time, for worker_handler's use) reads this env var
                # at import time - set it here too so importing the module
                # doesn't KeyError for this function.
                "PARTY_CONTROL_TABLE_NAME": party_control_table.table_name,
            },
            timeout=Duration.seconds(10),
        )
        executions_table.grant_read_write_data(cancel_api_fn)
        cancel_worker_fn.grant_invoke(cancel_api_fn)

        # Both registration Lambdas need the `webauthn` PyPI package (server-side WebAuthn
        # verification - see the design plan) - unlike every other Lambda here, they can't
        # use a bare `code=_lambda.Code.from_asset("lambdas")` (stdlib + preinstalled boto3
        # only); this bundling step pip-installs lambdas/requirements.txt into the deployed
        # package, the standard CDK pattern for a Python Lambda with real dependencies. Same
        # idea as wasm_pkg_source's Docker-based bundling above, just a prebuilt Lambda
        # build image instead of a custom Dockerfile.
        webauthn_lambda_code = _lambda.Code.from_asset(
            "lambdas",
            bundling=BundlingOptions(
                image=_lambda.Runtime.PYTHON_3_12.bundling_image,
                command=[
                    "bash", "-c",
                    "pip install -r requirements.txt -t /asset-output && cp -au . /asset-output",
                ],
            ),
        )

        registration_options_fn = _lambda.Function(
            self, "RegistrationOptionsFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="registration_options.handler",
            code=webauthn_lambda_code,
            environment={
                "CLIENT_REGISTRATIONS_TABLE_NAME": client_registrations_table.table_name,
                "WEBAUTHN_RP_ID": webauthn_rp_id,
            },
            timeout=Duration.seconds(10),
        )
        client_registrations_table.grant_read_write_data(registration_options_fn)

        register_client_fn = _lambda.Function(
            self, "RegisterClientFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="register_client.handler",
            code=webauthn_lambda_code,
            environment={
                "CLIENT_REGISTRATIONS_TABLE_NAME": client_registrations_table.table_name,
                "WEBAUTHN_RP_ID": webauthn_rp_id,
            },
            timeout=Duration.seconds(10),
        )
        client_registrations_table.grant_read_write_data(register_client_fn)

        api = apigateway.RestApi(
            self, "UserApi",
            rest_api_name="stoffel-ec2-user-persistent-api",
            deploy_options=apigateway.StageOptions(stage_name="prod"),
            # Lets a browser page (e.g. the voting example's start.html) call this API with
            # fetch() directly, not just curl/AWS CLI: CDK auto-adds an OPTIONS method with a
            # mock integration to every resource added below, answering the CORS preflight
            # request a browser sends before the real GET/POST because of the custom x-api-key
            # header. The preflight alone isn't enough, though - each Lambda's own response also
            # needs an Access-Control-Allow-Origin header for the browser to let JS read it (see
            # lambdas/*.py's _response helper).
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=apigateway.Cors.ALL_ORIGINS,
                allow_methods=["GET", "POST"],
                allow_headers=["Content-Type", "x-api-key"],
            ),
        )

        programs = api.root.add_resource("programs")
        programs.add_method(
            "GET", apigateway.LambdaIntegration(list_programs_fn), api_key_required=True,
        )

        executions_resource = api.root.add_resource("executions")
        executions_resource.add_method(
            "POST", apigateway.LambdaIntegration(submit_fn), api_key_required=True,
        )
        execution_resource = executions_resource.add_resource("{execution_id}")
        execution_resource.add_method(
            "GET", apigateway.LambdaIntegration(status_fn), api_key_required=True,
        )
        execution_resource.add_resource("logs").add_method(
            "GET", apigateway.LambdaIntegration(logs_fn), api_key_required=True,
        )
        execution_resource.add_resource("cancel").add_method(
            "POST", apigateway.LambdaIntegration(cancel_api_fn), api_key_required=True,
        )

        # No API key: a voter reaching this route only ever has the one-time token from their
        # own registration link (see register_client.py's docstring) - not a deployment-wide
        # API key, which only the operator holds and which a voter has no legitimate way to
        # obtain anyway (unlike the routes above, all operator-only).
        client_registrations_resource = api.root.add_resource("client-registrations")
        client_registrations_resource.add_method(
            "POST", apigateway.LambdaIntegration(register_client_fn), api_key_required=False,
        )
        # No API key, same reasoning as the POST above: this is the first half of the
        # standard two-step WebAuthn registration ceremony (generate options, then verify
        # the response against them - see registration_options.py), gated by the same
        # one-time token the second half already requires.
        client_registrations_resource.add_resource("options").add_method(
            "POST", apigateway.LambdaIntegration(registration_options_fn), api_key_required=False,
        )
        # API key required here, unlike the POST above: this exposes who has registered
        # (start.html's voter picker), which is operator-facing information.
        client_registrations_resource.add_method(
            "GET", apigateway.LambdaIntegration(list_client_registrations_fn), api_key_required=True,
        )

        # One key/user by default; onboard additional users with
        # ./add-api-key <username> (no redeploy needed - see README).
        api_key = api.add_api_key("DefaultApiKey")
        plan = api.add_usage_plan(
            "UsagePlan",
            name="stoffel-ec2-user-persistent-plan",
            throttle=apigateway.ThrottleSettings(rate_limit=5, burst_limit=10),
            quota=apigateway.QuotaSettings(limit=1000, period=apigateway.Period.DAY),
        )
        plan.add_api_key(api_key)
        plan.add_api_stage(stage=api.deployment_stage)

        return api, api_key, plan


app = cdk.App()
StoffelEc2UserDeploymentPersistentStack(
    app, "StoffelEc2UserDeploymentPersistentStack",
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)
app.synth()
