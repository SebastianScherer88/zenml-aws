"""An AWS Python Pulumi program"""

import json

import pulumi
import pulumi_aws as aws

# --- zenml: meta data store
test_meta_data = aws.rds.Instance(
    "zenml-metdata-store",
    port=3306,
    password="password",
    db_name="zenml",
    publicly_accessible=True,
    engine="mysql",
    instance_class="db.t4g.micro",
    allocated_storage=1024,
    username="zenml",
    skip_final_snapshot=True,
)

# --- zenml: container-registry
test_ecr_repo = aws.ecr.Repository(
    "zenml-container-registry", name="zenml", force_delete=True
)

# --- zenml: artifact-store
test_s3_bucket = aws.s3.Bucket(
    "zenml-artifact-store",
    # bucket="zenml-artifact-store",
    force_destroy=True,
)

# --- batch: instance profile
test_batch_ec2_role = aws.iam.Role(
    "batch-ec2-role",
    name="batch-ec2-role",
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
)

aws.iam.RolePolicyAttachment(
    "batchEc2Role_ECSAttach",
    role=test_batch_ec2_role.name,
    policy_arn="arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role",
)

test_batch_ec2_instance_profile = aws.iam.InstanceProfile(
    "zenml-test-ec2-instance-profile",
    # name="zenml-test-instance-profile",
    role=test_batch_ec2_role.name,
)

# # --- batch: compute environments
test_vpc = aws.ec2.get_vpc(default=True)

test_subnets = aws.ec2.get_subnets(
    filters=[aws.ec2.GetSubnetsFilterArgs(name="default-for-az", values=["true"])]
)

test_security_group = aws.ec2.get_security_group(
    filters=[
        {"name": "group-name", "values": ["default"]},
        {"name": "vpc-id", "values": [test_vpc.id]},
    ]
)


test_batch_service_role = aws.iam.Role(
    "batch-service-role",
    name="batch-service-role",
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
)
aws.iam.RolePolicyAttachment(
    "batchServiceRole_Policy",
    role=test_batch_service_role.name,
    policy_arn="arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole",
)

# --- batch: compute environments
# ec2
test_batch_ec2_compute_environment = aws.batch.ComputeEnvironment(
    "zenml-test-ec2-compute-environment",
    # name="zenml-test-compute-environment",
    compute_resources=aws.batch.ComputeEnvironmentComputeResourcesArgs(
        min_vcpus=0,
        desired_vcpus=0,
        max_vcpus=10,
        type="EC2",
        subnets=test_subnets.ids,
        security_group_ids=[test_security_group.id],
        instance_role=test_batch_ec2_instance_profile.arn,
        instance_types=["m5.large"],
    ),
    type="MANAGED",
    service_role=test_batch_service_role.arn,
)

# fargate
test_batch_fargate_compute_environment = aws.batch.ComputeEnvironment(
    "zenml-test-fargate-compute-environment",
    # name="zenml-test-compute-environment",
    compute_resources=aws.batch.ComputeEnvironmentComputeResourcesArgs(
        min_vcpus=0,
        max_vcpus=10,
        desired_vcpus=0,
        type="FARGATE",
        subnets=test_subnets.ids,
        security_group_ids=[test_security_group.id],
    ),
    type="MANAGED",
    service_role=test_batch_service_role.arn,
)

# --- batch: job queues
# ec2
test_ec2_job_queue = aws.batch.JobQueue(
    "zenml-test-ec2-job-queue",
    name="zenml-test-ec2-job-queue",
    priority=1,
    state="ENABLED",
    compute_environment_orders=[
        aws.batch.JobQueueComputeEnvironmentOrderArgs(
            compute_environment=test_batch_ec2_compute_environment.arn, order=1
        ),
    ],
)

# fargate
test_fargate_job_queue = aws.batch.JobQueue(
    "zenml-test-fargate-job-queue",
    name="zenml-test-fargate-job-queue",
    priority=1,
    state="ENABLED",
    compute_environment_orders=[
        aws.batch.JobQueueComputeEnvironmentOrderArgs(
            compute_environment=test_batch_fargate_compute_environment.arn, order=1
        )
    ],
)

# --- batch: submission role
test_submission_role = aws.iam.Role(
    "batch-submission-role",
    name="batch-submission-role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": "arn:aws:iam::743582000746:root"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    ),
)

test_submission_policy_attachment = aws.iam.PolicyAttachment(
    resource_name="AWSBatchTestAdminPolicyAttachment",
    name="AWSBatchTestAdminPolicyAttachment",
    roles=[test_submission_role.name],
    policy_arn="arn:aws:iam::aws:policy/AdministratorAccess",
)

# --- ecs: job role
test_job_role = aws.iam.Role(
    "batch-job-role",
    name="batch-job-role",
    assume_role_policy="""{
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": { "Service": "ecs-tasks.amazonaws.com" },
                "Action": "sts:AssumeRole"
            }
        ]
    }""",
)

# Attach minimal policy (e.g., S3 read-only)
aws.iam.RolePolicyAttachment(
    "batch-job-role-s3",
    role=test_job_role.name,
    policy_arn="arn:aws:iam::aws:policy/AmazonS3FullAccess",
)

# --- ecs: execution role
# Execution Role: ECS agent pulls images / pushes logs
test_execution_role = aws.iam.Role(
    "batch-execution-role",
    name="batch-execution-role",
    assume_role_policy="""{
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": { "Service": "ecs-tasks.amazonaws.com" },
                "Action": "sts:AssumeRole"
            }
        ]
    }""",
)

aws.iam.RolePolicyAttachment(
    "batch-execution-role-policy",
    role=test_execution_role.name,
    policy_arn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
)

pulumi.export("test-subnets", test_subnets.ids)
pulumi.export("test-submission-role-arn", test_submission_role.arn)
pulumi.export("zenml-metadata-store-url", test_meta_data.urn)
pulumi.export("zenml-metadata-store-address", test_meta_data.address)
pulumi.export("zenml-container-registry-arn", test_ecr_repo.arn)
pulumi.export("zenml-container-registry-url", test_ecr_repo.repository_url)
pulumi.export("zenml-artifact-store-arn", test_s3_bucket.arn)
pulumi.export("zenml-artifact-store-bucket-name", test_s3_bucket.bucket)
pulumi.export("test-job-ec2-queue-name", test_ec2_job_queue.name)
pulumi.export("test-job-fargate-queue-name", test_fargate_job_queue.name)
pulumi.export("test-job-role-arn", test_job_role.arn)
pulumi.export("test-execution-role-arn", test_execution_role.arn)
