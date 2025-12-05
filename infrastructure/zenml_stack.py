"""An AWS Python Pulumi program"""

import json

import pulumi
import pulumi_aws as aws
from network import NetworkStack
from pulumi.resource import ComponentResource
from pydantic import BaseModel


class ZenMLContainerRegistryConfig(BaseModel):
    force_delete: bool = True


class ZenMLArtifactStoreConfig(BaseModel):
    force_destroy: bool = True


class ZenMLAWSStackConfig(BaseModel):
    container_registry: ZenMLContainerRegistryConfig = ZenMLContainerRegistryConfig()
    artifact_store: ZenMLArtifactStoreConfig = ZenMLArtifactStoreConfig()


class ZenMLAWSStack(ComponentResource):
    name: str = "zenml-aws-stack"

    config: ZenMLAWSStackConfig
    container_registry: aws.ecr.Repository
    artifact_store: aws.s3.Bucket

    def __init__(
        self,
        config: ZenMLAWSStackConfig,
        network_stack: NetworkStack,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__(
            "zenml-aws:components:ZenMLAWSStack",
            self.name,
            opts,
        )

        self.config = config
        self.create_artifact_store()
        self.create_container_registry()
        self.create_batch_resources(network_stack)

    def create_artifact_store(self):
        self.artifact_store = aws.s3.Bucket(
            "zenml-artifact-store",
            force_destroy=self.config.artifact_store.force_destroy,
            opts=pulumi.ResourceOptions(parent=self),
        )

    def create_container_registry(self):
        self.container_registry = aws.ecr.Repository(
            "zenml-container-registry",
            force_delete=self.config.container_registry.force_delete,
            opts=pulumi.ResourceOptions(parent=self),
        )

    def create_batch_resources(self, network_stack: NetworkStack):
        # --- ec2 instance profile
        self.ec2_role = aws.iam.Role(
            "zenml-batch-ec2-role",
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
            opts=pulumi.ResourceOptions(parent=self),
        )
        aws.iam.RolePolicyAttachment(
            "zenml-batch-ec2-role-policy-attachment",
            role=self.ec2_role.name,
            policy_arn="arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role",
            opts=pulumi.ResourceOptions(parent=self),
        )
        self.ec2_instance_profile = aws.iam.InstanceProfile(
            "zenml-batch-ec2-instance-profile",
            role=self.ec2_role.name,
            opts=pulumi.ResourceOptions(parent=self),
        )

        # --- batch job queues
        self.batch_service_role = aws.iam.Role(
            "zenml-batch-service-role",
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
            opts=pulumi.ResourceOptions(parent=self),
        )
        aws.iam.RolePolicyAttachment(
            "zenml-batch-service-role-policy",
            role=self.batch_service_role.name,
            policy_arn="arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole",
            opts=pulumi.ResourceOptions(parent=self),
        )
        self.batch_ec2_compute_environment = aws.batch.ComputeEnvironment(
            "zenml-batch-ec2-compute-environment",
            compute_resources=aws.batch.ComputeEnvironmentComputeResourcesArgs(
                min_vcpus=0,
                desired_vcpus=0,
                max_vcpus=10,
                type="EC2",
                subnets=network_stack.subnets.ids,
                security_group_ids=[network_stack.security_group.id],
                instance_role=self.ec2_instance_profile.arn,
                instance_types=["m5.large"],
            ),
            type="MANAGED",
            service_role=self.batch_service_role.arn,
            opts=pulumi.ResourceOptions(parent=self),
        )
        self.batch_fargate_compute_environment = aws.batch.ComputeEnvironment(
            "zenml-batch-fargate-compute-environment",
            compute_resources=aws.batch.ComputeEnvironmentComputeResourcesArgs(
                min_vcpus=0,
                max_vcpus=10,
                desired_vcpus=0,
                type="FARGATE",
                subnets=network_stack.subnets.ids,
                security_group_ids=[network_stack.security_group.id],
            ),
            type="MANAGED",
            service_role=self.batch_service_role.arn,
            opts=pulumi.ResourceOptions(parent=self),
        )
        self.batch_ec2_job_queue = aws.batch.JobQueue(
            "zenml-batch-ec2-job-queue",
            name="zenml-test-ec2-job-queue",
            priority=1,
            state="ENABLED",
            compute_environment_orders=[
                aws.batch.JobQueueComputeEnvironmentOrderArgs(
                    compute_environment=self.batch_ec2_compute_environment.arn, order=1
                ),
            ],
            opts=pulumi.ResourceOptions(parent=self),
        )
        self.batch_fargate_job_queue = aws.batch.JobQueue(
            "zenml-batch-fargate-job-queue",
            name="zenml-test-fargate-job-queue",
            priority=1,
            state="ENABLED",
            compute_environment_orders=[
                aws.batch.JobQueueComputeEnvironmentOrderArgs(
                    compute_environment=self.batch_fargate_compute_environment.arn,
                    order=1,
                )
            ],
            opts=pulumi.ResourceOptions(parent=self),
        )

        # --- batch ecs roles
        self.batch_ecs_job_role = aws.iam.Role(
            "zenml-batch-job-role",
            name="batch-job-role",
            assume_role_policy=json.dumps(
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
            ),
            opts=pulumi.ResourceOptions(parent=self),
        )
        aws.iam.RolePolicyAttachment(
            "zenml-batch-job-role-s3",
            role=self.batch_ecs_job_role.name,
            policy_arn="arn:aws:iam::aws:policy/AmazonS3FullAccess",
        )
        self.batch_ecs_execution_role = aws.iam.Role(
            "zenml-batch-execution-role",
            name="batch-execution-role",
            assume_role_policy=json.dumps(
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
            ),
            opts=pulumi.ResourceOptions(parent=self),
        )
        aws.iam.RolePolicyAttachment(
            "zenml-batch-execution-role-policy",
            role=self.batch_ecs_execution_role.name,
            policy_arn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
            opts=pulumi.ResourceOptions(parent=self),
        )
        # --- stepfunctions role
        self.stepfunctions_execution_role = aws.iam.Role(
            "zenml-stepfunctions-execution-role",
            assume_role_policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {
                                "AWS": "arn:aws:iam::743582000746:root",
                                "Service": [
                                    "states.amazonaws.com",
                                    "states.eu-west-1.amazonaws.com",
                                ],
                            },
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
            opts=pulumi.ResourceOptions(parent=self),
        )
        self.stepfunctions_execution_policy = aws.iam.Policy(
            "zenml-stepfunctions-execution-policy",
            policy=pulumi.Output.json_dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "states:StartExecution",
                                "states:DescribeExecution",
                                "states:StopExecution",
                                "states:GetExecutionHistory",
                            ],
                            "Resource": "*",
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "batch:SubmitJob",
                                "batch:DescribeJobs",
                                "batch:TerminateJob",
                                "batch:CancelJob",
                            ],
                            "Resource": "*",
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "logs:CreateLogGroup",
                                "logs:CreateLogStream",
                                "logs:PutLogEvents",
                            ],
                            "Resource": "*",
                        },
                        {
                            "Effect": "Allow",
                            "Action": "iam:PassRole",
                            "Resource": self.batch_ecs_execution_role.arn,
                        },
                    ],
                }
            ),
            opts=pulumi.ResourceOptions(parent=self),
        )
        aws.iam.PolicyAttachment(
            "zenml-stepfunctions-execution-role-policy-attachment",
            roles=[self.stepfunctions_execution_role.name],
            policy_arn=self.stepfunctions_execution_policy.arn,
            opts=pulumi.ResourceOptions(parent=self),
        )

    def export_outputs(self):
        pulumi.export("zenml-artifact-store-bucket-arn", self.artifact_store.arn)
        pulumi.export("zenml-artifact-store-bucket-name", self.artifact_store.bucket)
        pulumi.export("zenml-container-registry-arn", self.container_registry.arn)
        pulumi.export("zenml-container-registry-name", self.container_registry.name)
        pulumi.export(
            "zenml-batch-ec2-compute-environment-arn",
            self.batch_ec2_compute_environment.arn,
        )
        pulumi.export(
            "zenml-batch-fargate-compute-environment-arn",
            self.batch_fargate_compute_environment.arn,
        )
        pulumi.export("zenml-batch-ec2-job-queue-arn", self.batch_ec2_job_queue.arn)
        pulumi.export(
            "zenml-batch-fargate-job-queue-arn", self.batch_fargate_job_queue.arn
        )
        pulumi.export("zenml-batch-ecs-job-role-arn", self.batch_ecs_job_role.arn)
        pulumi.export(
            "zenml-batch-ecs-execution-role-arn", self.batch_ecs_execution_role.arn
        )
        pulumi.export(
            "zenml-stepfunctions-execution-role-arn",
            self.stepfunctions_execution_role.arn,
        )
