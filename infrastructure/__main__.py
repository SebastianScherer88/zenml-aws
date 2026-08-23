"""
ZenML OSS on AWS — Pulumi (Python)

Modeled after github.com/SebastianScherer88/metaflow-aws/tree/main/infrastructure
(networking / config / ALB-dev-IP pattern) and extends
github.com/SebastianScherer88/zenml-aws/tree/main/infrastructure (RDS metadata
store, ECR, S3 artifact store, Batch compute resources) with an actual ECS
Fargate deployment of the ZenML server (app server + dashboard, one container,
one port — ZenML doesn't split these the way Metaflow splits metadata/UI).

Implements:
  - VPC (2 AZs, public + private subnets, single NAT gateway)
  - RDS MySQL                              -> ZenML metadata/ZenStore backend.
                                               SG only allows inbound from the
                                               ECS service — NOT from developer
                                               CIDRs. Developers never connect
                                               to it directly.
  - S3 bucket                              -> ZenML artifact store
  - ECR repo                               -> custom pipeline/step images
  - ECS Fargate service: ZenML server (zenmldocker/zenml-server)
      -> serves both the REST API and the dashboard UI on one port
      -> sits in private subnets, behind an ALB in the public subnets
      -> ALB security group ingress is restricted to `devAllowedCidrs`
         (the same "surface via ALB, lock down by developer IP" approach
         used in metaflow-aws, rather than metaflow-aws's alternative
         no-ALB / public-IP dev mode)
  - AWS Batch (Fargate + EC2 compute envs, queues)
                                           -> compute for the ZenML AWS Batch
                                              step operator (runs individual
                                              @step functions as Batch jobs)
  - IAM roles:
      * ECS task execution role (pull image, write logs, read secrets)
      * ZenML server task role (S3 artifact store R/W, ECR pull for the
        Docker-image-builder-produced pipeline images it may need to reason
        about)
      * Batch job role (S3 artifact store R/W — what your @step code runs as)
      * Batch execution role (ECS-level, for Batch's own Fargate tasks)
      * Batch service role

Developer workflow (per official ZenML docs — no direct DB access, ever):
    zenml login http://<alb_dns_name>          # or the exported `server_url`
    # authenticate with the ZENML_DEFAULT_USER_NAME / generated password
    # (see the `server_default_user_password_secret_arn` output)

Config:
    pulumi config set aws:region <region>
    pulumi config set zenml-ecs:security '{"allowed_cidrs": ["<your-ip>/32"]}'
    See Pulumi.zenml-ecs.yaml for the full config shape (server image/port/
    resources, RDS sizing, Batch queues/compute environments).
"""

import json

import pulumi
import pulumi_aws as aws
import pulumi_random as random

config = pulumi.Config("zenml-aws")
project = pulumi.get_project()
stack = pulumi.get_stack()
prefix = f"{stack}"

security_config = config.require_object("security")
server_config = config.require_object("server")
metadata_store_config = config.require_object("metadata-store")
batch_config = config.require_object("batch")

tags = {"project": project, "stack": stack, "managed-by": "pulumi"}

# Comma/list of CIDRs allowed to reach the ALB (dashboard + API). This is the
# *only* network path into the stack for a developer — there is no dev-mode
# public-IP bypass and no direct RDS exposure.
allowed_cidrs = [c.strip() for c in security_config["allowed_cidrs"]]

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

vpc = aws.ec2.Vpc(
    f"{prefix}-vpc",
    cidr_block="10.0.0.0/16",
    enable_dns_hostnames=True,
    enable_dns_support=True,
    tags={**tags, "Name": f"{prefix}-vpc"},
)

azs = aws.get_availability_zones(state="available")

igw = aws.ec2.InternetGateway(
    f"{prefix}-igw", vpc_id=vpc.id, tags={**tags, "Name": f"{prefix}-igw"}
)

public_subnets = []
private_subnets = []
for i in range(2):
    az = azs.names[i]
    public_subnets.append(
        aws.ec2.Subnet(
            f"{prefix}-public-{i}",
            vpc_id=vpc.id,
            cidr_block=f"10.0.{i}.0/24",
            availability_zone=az,
            map_public_ip_on_launch=True,
            tags={**tags, "Name": f"{prefix}-public-{i}"},
        )
    )
    private_subnets.append(
        aws.ec2.Subnet(
            f"{prefix}-private-{i}",
            vpc_id=vpc.id,
            cidr_block=f"10.0.{i + 10}.0/24",
            availability_zone=az,
            tags={**tags, "Name": f"{prefix}-private-{i}"},
        )
    )

public_rt = aws.ec2.RouteTable(
    f"{prefix}-public-rt",
    vpc_id=vpc.id,
    routes=[aws.ec2.RouteTableRouteArgs(cidr_block="0.0.0.0/0", gateway_id=igw.id)],
    tags={**tags, "Name": f"{prefix}-public-rt"},
)
for i, subnet in enumerate(public_subnets):
    aws.ec2.RouteTableAssociation(
        f"{prefix}-public-rta-{i}", subnet_id=subnet.id, route_table_id=public_rt.id
    )

nat_eip = aws.ec2.Eip(f"{prefix}-nat-eip", domain="vpc", tags=tags)
nat_gw = aws.ec2.NatGateway(
    f"{prefix}-nat",
    allocation_id=nat_eip.id,
    subnet_id=public_subnets[0].id,
    tags={**tags, "Name": f"{prefix}-nat"},
    opts=pulumi.ResourceOptions(depends_on=[igw]),
)

private_rt = aws.ec2.RouteTable(
    f"{prefix}-private-rt",
    vpc_id=vpc.id,
    routes=[
        aws.ec2.RouteTableRouteArgs(cidr_block="0.0.0.0/0", nat_gateway_id=nat_gw.id)
    ],
    tags={**tags, "Name": f"{prefix}-private-rt"},
)
for i, subnet in enumerate(private_subnets):
    aws.ec2.RouteTableAssociation(
        f"{prefix}-private-rta-{i}", subnet_id=subnet.id, route_table_id=private_rt.id
    )

# ---------------------------------------------------------------------------
# Security groups
# ---------------------------------------------------------------------------

# ALB: ingress only from developer CIDRs, on the server port. This is the
# metaflow-aws "surface via ALB, restrict by dev IP" pattern.
alb_sg = aws.ec2.SecurityGroup(
    f"{prefix}-alb-sg",
    vpc_id=vpc.id,
    description="Ingress for the ZenML server ALB, restricted to developer CIDRs",
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=server_config["port"],
            to_port=server_config["port"],
            cidr_blocks=allowed_cidrs,
            description="ZenML dashboard + REST API",
        ),
    ],
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"]
        )
    ],
    tags={**tags, "Name": f"{prefix}-alb-sg"},
)

# ZenML server ECS task: only reachable from the ALB. No direct dev-IP path.
ecs_services_sg = aws.ec2.SecurityGroup(
    f"{prefix}-ecs-services-sg",
    vpc_id=vpc.id,
    description="ZenML server ECS task. Reachable only via the ALB",
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"]
        )
    ],
    tags={**tags, "Name": f"{prefix}-ecs-services-sg"},
)

# Batch job containers: outbound only (S3, ECR pull, and HTTPS calls back to
# the ZenML server's public ALB URL to log pipeline/run metadata).
batch_sg = aws.ec2.SecurityGroup(
    f"{prefix}-batch-sg",
    vpc_id=vpc.id,
    description="AWS Batch Fargate/EC2 tasks running ZenML step-operator jobs",
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"]
        )
    ],
    tags={**tags, "Name": f"{prefix}-batch-sg"},
)

aws.ec2.SecurityGroupRule(
    f"{prefix}-ecs-server-from-alb",
    type="ingress",
    security_group_id=ecs_services_sg.id,
    protocol="tcp",
    from_port=server_config["port"],
    to_port=server_config["port"],
    source_security_group_id=alb_sg.id,
)

# Batch job containers -> metadata service (internal, via Cloud Map)
aws.ec2.SecurityGroupRule(
    f"{prefix}-ecs-metadata-service-from-batch",
    type="ingress",
    security_group_id=ecs_services_sg.id,
    protocol="tcp",
    from_port=server_config["port"],
    to_port=server_config["port"],
    source_security_group_id=batch_sg.id,
)

# RDS: reachable ONLY from the ZenML server ECS task. Not from developer
# CIDRs, not from Batch jobs — Batch jobs talk to the ZenML server over
# HTTP(S), never to the DB. This is what removes the "login to RDS directly"
# workflow entirely; `zenml login` against the server is the only path in.
rds_sg = aws.ec2.SecurityGroup(
    f"{prefix}-rds-sg",
    vpc_id=vpc.id,
    description="MySQL, reachable only from the ZenML server ECS task",
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            protocol="-1", from_port=0, to_port=0, cidr_blocks=["0.0.0.0/0"]
        )
    ],
    tags={**tags, "Name": f"{prefix}-rds-sg"},
)
aws.ec2.SecurityGroupRule(
    f"{prefix}-rds-from-ecs",
    type="ingress",
    security_group_id=rds_sg.id,
    protocol="tcp",
    from_port=metadata_store_config["port"],
    to_port=metadata_store_config["port"],
    source_security_group_id=ecs_services_sg.id,
)

# ---------------------------------------------------------------------------
# S3 artifact store
# ---------------------------------------------------------------------------

artifact_bucket = aws.s3.Bucket(
    f"{prefix}-artifact-store", force_destroy=True, tags=tags
)
aws.s3.BucketVersioning(
    f"{prefix}-artifact-store-versioning",
    bucket=artifact_bucket.id,
    versioning_configuration=aws.s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)
aws.s3.BucketPublicAccessBlock(
    f"{prefix}-artifact-store-block-public",
    bucket=artifact_bucket.id,
    block_public_acls=True,
    block_public_policy=True,
    ignore_public_acls=True,
    restrict_public_buckets=True,
)

current = aws.get_caller_identity()
artifact_bucket_policy = aws.s3.BucketPolicy(
    f"{prefix}-artifact-store-policy",
    bucket=artifact_bucket.id,
    policy=pulumi.Output.all(artifact_bucket.arn, current.account_id).apply(
        lambda args: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "AllowSameAccountAccess",
                        "Effect": "Allow",
                        "Principal": {"AWS": f"arn:aws:iam::{args[1]}:root"},
                        "Action": [
                            "s3:GetObject",
                            "s3:PutObject",
                            "s3:DeleteObject",
                            "s3:ListBucket",
                        ],
                        "Resource": [args[0], f"{args[0]}/*"],
                    }
                ],
            }
        )
    ),
)

# ---------------------------------------------------------------------------
# ECR — registry for custom pipeline / step images
# ---------------------------------------------------------------------------

ecr_repo = aws.ecr.Repository(
    f"{prefix}-container-registry",
    name=f"{prefix}-zenml",
    force_delete=True,
    image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(
        scan_on_push=True
    ),
    tags=tags,
)

# ---------------------------------------------------------------------------
# RDS potgres (ZenML metadata / ZenStore backend)
# ---------------------------------------------------------------------------

db_password = random.RandomPassword(f"{prefix}-db-password", length=24, special=False)

db_subnet_group = aws.rds.SubnetGroup(
    f"{prefix}-db-subnets",
    subnet_ids=[s.id for s in private_subnets],
    tags=tags,
)

db_instance = aws.rds.Instance(
    f"{prefix}-db",
    engine="mysql",
    engine_version=metadata_store_config["engine-version"],
    instance_class=metadata_store_config["instance-class"],
    allocated_storage=metadata_store_config["allocated-storage"],
    storage_encrypted=True,
    db_name=metadata_store_config["name"],
    username=metadata_store_config["username"],
    password=db_password.result,
    port=metadata_store_config["port"],
    db_subnet_group_name=db_subnet_group.name,
    vpc_security_group_ids=[rds_sg.id],
    publicly_accessible=False,  # <-- crucial: no direct developer access, ever
    skip_final_snapshot=True,
    tags=tags,
)

# Full ZENML_STORE_URL is precomputed and stored as a secret so the ECS task
# can inject it directly as one env var — nobody (including the developer)
# needs to assemble or see the raw DB credentials to use the server.
zenml_store_url = pulumi.Output.all(
    metadata_store_config["username"],
    db_password.result,
    db_instance.address,
    db_instance.port,
    metadata_store_config["name"],
).apply(lambda a: f"mysql://{a[0]}:{a[1]}@{a[2]}:{a[3]}/{a[4]}")

db_secret = aws.secretsmanager.Secret(f"{prefix}-db-secret", tags=tags)
aws.secretsmanager.SecretVersion(
    f"{prefix}-db-secret-version",
    secret_id=db_secret.id,
    secret_string=pulumi.Output.json_dumps(
        {
            "username": db_instance.username,
            "password": db_password.result,
            "host": db_instance.address,
            "port": db_instance.port,
            "dbname": db_instance.db_name,
            "store_url": zenml_store_url,
        }
    ),
)

# Separate secret for the ZenML server's own auth material — the JWT signing
# key and the initial admin password. Generated, never typed in by a human.
server_jwt_secret = random.RandomPassword(
    f"{prefix}-server-jwt-secret", length=32, special=False
)
server_default_user_password = random.RandomPassword(
    f"{prefix}-server-default-user-password",
    length=20,
    special=True,
    override_special="!#$%&*()-_=+",
)
server_secret = aws.secretsmanager.Secret(f"{prefix}-server-secret", tags=tags)
aws.secretsmanager.SecretVersion(
    f"{prefix}-server-secret-version",
    secret_id=server_secret.id,
    secret_string=pulumi.Output.json_dumps(
        {
            "jwt_secret_key": server_jwt_secret.result,
            "default_user_password": server_default_user_password.result,
        }
    ),
)

# ---------------------------------------------------------------------------
# Dynamo
# ---------------------------------------------------------------------------

# DynamoDB table used by the Step Functions orchestrator to track
# foreach / parallel-split state across a run
sfn_state_table = aws.dynamodb.Table(
    f"{prefix}-sfn-state",
    billing_mode="PAY_PER_REQUEST",
    hash_key="pathspec",
    attributes=[
        aws.dynamodb.TableAttributeArgs(name="pathspec", type="S"),
    ],
    tags=tags,
)

# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------

ecs_assume_role_policy = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)

# Shared ECS task execution role: pull the image, write logs, read secrets
ecs_execution_role = aws.iam.Role(
    f"{prefix}-ecs-execution-role", assume_role_policy=ecs_assume_role_policy, tags=tags
)
aws.iam.RolePolicyAttachment(
    f"{prefix}-ecs-execution-role-managed",
    role=ecs_execution_role.name,
    policy_arn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
)
ecs_execution_secret_policy = aws.iam.RolePolicy(
    f"{prefix}-ecs-execution-role-secrets",
    role=ecs_execution_role.id,
    policy=pulumi.Output.all(db_secret.arn, server_secret.arn).apply(
        lambda arns: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["secretsmanager:GetSecretValue"],
                        "Resource": arns,
                    }
                ],
            }
        )
    ),
)

# ZenML server task role: R/W on the artifact bucket (the server's dashboard
# reads/writes artifact metadata & previews) and pull access on the ECR repo.
server_task_role = aws.iam.Role(
    f"{prefix}-server-task-role", assume_role_policy=ecs_assume_role_policy, tags=tags
)
aws.iam.RolePolicy(
    f"{prefix}-server-task-role-s3",
    role=server_task_role.id,
    policy=artifact_bucket.arn.apply(
        lambda arn: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "s3:GetObject",
                            "s3:PutObject",
                            "s3:DeleteObject",
                            "s3:ListBucket",
                        ],
                        "Resource": [arn, f"{arn}/*"],
                    }
                ],
            }
        )
    ),
)

# Batch's EC2 instance profile role (for EC2-type compute environments)
batch_ec2_service_role = aws.iam.Role(
    f"{prefix}-batch-instance-role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    ),
    tags=tags,
)
aws.iam.RolePolicyAttachment(
    f"{prefix}-batch-instance-role-policy",
    role=batch_ec2_service_role.name,
    policy_arn="arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role",
)

# Batch job role: what your @step code runs as inside a Batch job — S3
# artifact store R/W. No DB access, no special IAM needed to reach the ZenML
# server (plain HTTPS to the ALB).
batch_job_role = aws.iam.Role(
    f"{prefix}-batch-job-role", assume_role_policy=ecs_assume_role_policy, tags=tags
)
aws.iam.RolePolicy(
    f"{prefix}-batch-job-role-s3",
    role=batch_job_role.id,
    policy=pulumi.Output.json_dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                        "s3:ListBucket",
                    ],
                    "Resource": [
                        artifact_bucket.arn,
                        pulumi.Output.concat(artifact_bucket.arn, "/*"),
                    ],
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "dynamodb:PutItem",
                        "dynamodb:GetItem",
                        "dynamodb:UpdateItem",
                    ],
                    "Resource": [sfn_state_table.arn],
                },
            ],
        }
    ),
)

# Batch execution role: ECS-level role for the Fargate-type compute envs
# (pull image, write logs)
batch_execution_role = aws.iam.Role(
    f"{prefix}-batch-execution-role",
    assume_role_policy=ecs_assume_role_policy,
    tags=tags,
)
aws.iam.RolePolicyAttachment(
    f"{prefix}-batch-execution-role-managed",
    role=batch_execution_role.name,
    policy_arn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
)

# Batch's own service role (required by every compute environment)
batch_service_role = aws.iam.Role(
    f"{prefix}-batch-service-role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "batch.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    ),
    tags=tags,
)
aws.iam.RolePolicyAttachment(
    f"{prefix}-batch-service-role-managed",
    role=batch_service_role.name,
    policy_arn="arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole",
)

# Step Functions state machine role: submits/monitors Batch jobs, needs the
# Events permissions for the .sync ("run job, wait for completion") pattern,
# and read/write on the foreach-state DynamoDB table
sfn_role = aws.iam.Role(
    f"{prefix}-sfn-role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "states.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    ),
    tags=tags,
)
aws.iam.RolePolicy(
    f"{prefix}-sfn-role-policy",
    role=sfn_role.id,
    policy=sfn_state_table.arn.apply(
        lambda ddb_arn: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "batch:SubmitJob",
                            "batch:DescribeJobs",
                            "batch:TerminateJob",
                            "batch:CancelJob",
                            "batch:TagResource",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "logs:CreateLogGroup",
                            "logs:CreateLogStream",
                            "logs:PutLogEvents",
                            "logs:CreateLogDelivery",
                            "logs:GetLogDelivery",
                            "logs:UpdateLogDelivery",
                            "logs:DeleteLogDelivery",
                            "logs:ListLogDeliveries",
                            "logs:PutResourcePolicy",
                            "logs:DescribeResourcePolicies",
                            "logs:DescribeLogGroups",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "events:PutTargets",
                            "events:PutRule",
                            "events:DescribeRule",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "dynamodb:GetItem",
                            "dynamodb:PutItem",
                            "dynamodb:UpdateItem",
                            "dynamodb:DeleteItem",
                            "dynamodb:Query",
                        ],
                        "Resource": [ddb_arn, f"{ddb_arn}/index/*"],
                    },
                ],
            }
        )
    ),
)

# IAM role assumed by Amazon EventBridge when invoking Metaflow
# Step Functions state machines.
events_sfn_role = aws.iam.Role(
    f"{prefix}-events-sfn-access-role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Service": "events.amazonaws.com",
                    },
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    ),
    tags=tags,
)

aws.iam.RolePolicy(
    f"{prefix}-events-sfn-access-policy",
    role=events_sfn_role.id,
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "states:StartExecution",
                        "states:DescribeExecution",
                        "states:ListExecutions",
                        "states:DescribeStateMachine",
                        "states:ListStateMachines",
                    ],
                    "Resource": "*",
                }
            ],
        }
    ),
)

# ---------------------------------------------------------------------------
# ECS cluster, ALB (restricted to developer CIDRs), ZenML server service
# ---------------------------------------------------------------------------

cluster = aws.ecs.Cluster(f"{prefix}-cluster", tags=tags)

log_group = aws.cloudwatch.LogGroup(f"{prefix}-logs", retention_in_days=14, tags=tags)

server_alb = aws.lb.LoadBalancer(
    f"{prefix}-alb",
    internal=False,
    load_balancer_type="application",
    security_groups=[alb_sg.id],
    subnets=[s.id for s in public_subnets],
    tags=tags,
)

server_tg = aws.lb.TargetGroup(
    f"{prefix}-server-tg",
    port=server_config["port"],
    protocol="HTTP",
    vpc_id=vpc.id,
    target_type="ip",
    health_check=aws.lb.TargetGroupHealthCheckArgs(
        path="/health",
        matcher="200-399",
        interval=30,
        timeout=10,
        healthy_threshold=2,
        unhealthy_threshold=5,
    ),
    tags=tags,
)
server_listener = aws.lb.Listener(
    f"{prefix}-server-listener",
    load_balancer_arn=server_alb.arn,
    port=server_config["port"],
    protocol="HTTP",
    default_actions=[
        aws.lb.ListenerDefaultActionArgs(type="forward", target_group_arn=server_tg.arn)
    ],
)

# The server needs to know its own externally-reachable URL (used in emails/
# links/OAuth redirects and by the CLI's `zenml login` device-auth flow).
server_external_url = pulumi.Output.concat(
    "http://", server_alb.dns_name, ":", str(server_config["port"])
)

server_namespace = aws.servicediscovery.PrivateDnsNamespace(
    f"{prefix}-namespace",
    name="zenml.local",
    vpc=vpc.id,
    description="Private service discovery for Zenml server",
    tags=tags,
)

server_discovery_service = aws.servicediscovery.Service(
    f"{prefix}-metadata-discovery",
    name="metadata",
    dns_config=aws.servicediscovery.ServiceDnsConfigArgs(
        namespace_id=server_namespace.id,
        routing_policy="MULTIVALUE",
        dns_records=[
            aws.servicediscovery.ServiceDnsConfigDnsRecordArgs(
                ttl=10,
                type="A",
            )
        ],
    ),
    health_check_custom_config=aws.servicediscovery.ServiceHealthCheckCustomConfigArgs(
        failure_threshold=1,
    ),
    tags=tags,
)
server_internal_url = pulumi.Output.concat(
    "http://",
    server_discovery_service.name,
    ".",
    server_namespace.name,
    ":",
    str(server_config["port"]),
)
service_registries = aws.ecs.ServiceServiceRegistriesArgs(
    registry_arn=server_discovery_service.arn,
    container_name="zenml-server",
    container_port=server_config["port"],
)

server_task_def = aws.ecs.TaskDefinition(
    f"{prefix}-server-task",
    family=f"{prefix}-server",
    cpu=server_config["resources"]["cpu"],
    memory=server_config["resources"]["memory"],
    network_mode="awsvpc",
    requires_compatibilities=["FARGATE"],
    execution_role_arn=ecs_execution_role.arn,
    task_role_arn=server_task_role.arn,
    container_definitions=pulumi.Output.json_dumps(
        [
            {
                "name": "zenml-server",
                "image": server_config["image"],
                "essential": True,
                "portMappings": [
                    {"containerPort": server_config["port"], "protocol": "tcp"}
                ],
                "environment": [
                    {"name": "ZENML_SERVER_AUTO_ACTIVATE", "value": "1"},
                    {
                        "name": "ZENML_DEFAULT_USER_NAME",
                        "value": server_config["default-user-name"],
                    },
                    {
                        "name": "ZENML_DEFAULT_PROJECT_NAME",
                        "value": server_config["default-project-name"],
                    },
                    {"name": "ZENML_SERVER_URL", "value": server_internal_url},
                    {"name": "ZENML_STORE_BACKUP_STRATEGY", "value": "disabled"},
                ],
                "secrets": [
                    {
                        "name": "ZENML_STORE_URL",
                        "valueFrom": pulumi.Output.concat(
                            db_secret.arn, ":store_url::"
                        ),
                    },
                    {
                        "name": "ZENML_DEFAULT_USER_PASSWORD",
                        "valueFrom": pulumi.Output.concat(
                            server_secret.arn, ":default_user_password::"
                        ),
                    },
                    {
                        "name": "ZENML_AUTH_JWT_SECRET_KEY",
                        "valueFrom": pulumi.Output.concat(
                            server_secret.arn, ":jwt_secret_key::"
                        ),
                    },
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": log_group.name,
                        "awslogs-region": aws.get_region().name,
                        "awslogs-stream-prefix": "zenml-server",
                    },
                },
            }
        ]
    ),
    tags=tags,
)

server_service = aws.ecs.Service(
    f"{prefix}-server-service",
    cluster=cluster.arn,
    task_definition=server_task_def.arn,
    desired_count=server_config["desired-count"],
    launch_type="FARGATE",
    network_configuration=aws.ecs.ServiceNetworkConfigurationArgs(
        subnets=[s.id for s in private_subnets],
        security_groups=[ecs_services_sg.id],
        assign_public_ip=False,
    ),
    load_balancers=[
        aws.ecs.ServiceLoadBalancerArgs(
            target_group_arn=server_tg.arn,
            container_name="zenml-server",
            container_port=server_config["port"],
        )
    ],
    service_registries=aws.ecs.ServiceServiceRegistriesArgs(
        registry_arn=server_discovery_service.arn, container_name="zenml-server"
    ),
    opts=pulumi.ResourceOptions(depends_on=[server_listener, db_instance]),
    tags=tags,
)

# ---------------------------------------------------------------------------
# AWS Batch — the compute layer Step Functions submits flow steps to
# ---------------------------------------------------------------------------
batch_ec2_instance_profile = aws.iam.InstanceProfile(
    "batch-instance-profile",
    role=batch_ec2_service_role.name,
)

batch_queue_names = []
batch_queue_arns = []

for batch_queue_config in batch_config["queues"]:
    batch_compute_env_arns = []

    for batch_compute_env_config in batch_queue_config["compute-environments"]:
        batch_compute_env_name = batch_compute_env_config.pop("name")

        batch_compute_env = aws.batch.ComputeEnvironment(
            f"{prefix}-batch-{batch_compute_env_name}",
            name=f"{prefix}-{batch_compute_env_name}",
            type="MANAGED",
            service_role=batch_service_role.arn,
            compute_resources=aws.batch.ComputeEnvironmentComputeResourcesArgs(
                **batch_compute_env_config,
                instance_role=batch_ec2_instance_profile.arn
                if batch_compute_env_config["type"] in ("EC2", "SPOT")
                else None,
                subnets=[s.id for s in private_subnets],
                security_group_ids=[batch_sg.id],
            ),
            opts=pulumi.ResourceOptions(depends_on=[batch_service_role]),
            tags=tags,
        )

        batch_compute_env_arns.append(batch_compute_env.arn)

    batch_queue = aws.batch.JobQueue(
        f"{prefix}-batch-{batch_queue_config['name']}",
        name=f"{prefix}-{batch_queue_config['name']}",
        priority=1,
        state="ENABLED",
        compute_environment_orders=[
            aws.batch.JobQueueComputeEnvironmentOrderArgs(
                order=order_index + 1, compute_environment=batch_compute_env_arn
            )
            for order_index, batch_compute_env_arn in enumerate(batch_compute_env_arns)
        ],
        tags=tags,
    )
    batch_queue_names.append(batch_queue.name)
    batch_queue_arns.append(batch_queue.arn)

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
load_balancer_url = pulumi.Output.concat("http://", server_alb.dns_name)
server_external_url = server_alb.dns_name.apply(
    lambda d: f"http://{d}:{str(server_config['port'])}"
)

pulumi.export("ecr_repository_url", ecr_repo.repository_url)
pulumi.export("load_balancer_url", load_balancer_url)
pulumi.export("server_external_url", server_external_url)
pulumi.export("server_internal_url", server_internal_url)
pulumi.export("server_default_user_password_secret_arn", server_secret.arn)
pulumi.export(
    "artifact_store_s3_bucket", artifact_bucket.bucket.apply(lambda b: f"s3://{b}")
)
pulumi.export("ecs_execution_role_arn", ecs_execution_role.arn)
pulumi.export("server_task_role_arn", server_task_role.arn)
pulumi.export("batch_job_role_arn", batch_job_role.arn)
pulumi.export("batch_execution_role_arn", batch_execution_role.arn)
pulumi.export("batch_compute_env_arns", batch_compute_env_arns)
pulumi.export("batch_job_queue_names", batch_queue_names)
pulumi.export("batch_job_queue_arns", batch_queue_arns)
pulumi.export("batch_default_job_queue_name", batch_queue_names[0])
pulumi.export("sfn_role_arn", sfn_role.arn)
pulumi.export("events_bridge_sfn_role_arn", events_sfn_role.arn)
# Internal-only — not reachable by developers (SG allows only the ECS task).
# Exported for ops/troubleshooting via `aws rds` / break-glass access, not
# for `zenml` client configuration.
pulumi.export("rds_internal_endpoint", db_instance.endpoint)
pulumi.export("rds_secret_arn", db_secret.arn)
