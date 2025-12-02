"""An AWS Python Pulumi program"""

import json
from typing import Literal

import pulumi
import pulumi_aws as aws
import requests
from pulumi.resource import ComponentResource
from pydantic import BaseModel
from zenml_stack import ZenMLRemoteStack


def my_ip() -> str:
    return requests.get("https://api.ipify.org").text


class ZenMLMetadataStoreConfig(BaseModel):
    publicly_accessible: bool = True
    engine: Literal["aurora-mysql", "aurora-postgresql", "mysql", "postgres"] = "mysql"
    instance_class: Literal["db.t4g.micro"] = "db.t4g.micro"
    allocated_storage: int = 1024
    port: int = 3306
    db_name: str = "zenml"
    username: str = "zenml"
    password: str = "password"
    skip_final_snapshot: bool = True


class ZenMLServerConfig(BaseModel):
    container_image: str = "zenmldocker/zenml-server:0.91.0"
    container_port: int = 8080
    cpu: int = 256
    memory: int = 512
    service_assign_public_ip: bool = True
    service_network_default: bool = True


class ZenMLDeploymentConfig(BaseModel):
    metadata_store: ZenMLMetadataStoreConfig = ZenMLMetadataStoreConfig()
    server: ZenMLServerConfig = ZenMLServerConfig()


class ZenMLDeployment(ComponentResource):
    """Implements the OSS self hosted ZenML deployment. Main components include:
    - zenml dashboard server on ecs
    - zenml store on RDS"""

    name: str = "zenml-deployment"

    config: ZenMLDeploymentConfig
    metadata_store: aws.rds.Instance
    metadata_store_secret: aws.secretsmanager.Secret
    metadata_store_secret_value: aws.secretsmanager.SecretVersion
    server_cluster: aws.ecs.Cluster
    server_task_definition: aws.ecs.TaskDefinition
    server_server: aws.ecs.Service

    def __init__(
        self,
        config: ZenMLDeploymentConfig,
        zenml_remote_stack: ZenMLRemoteStack,
        vpc: aws.ec2.Vpc | None = None,
        subnets: aws.ec2.GetSubnetsResult | None = None,
        security_group: aws.ec2.GetSecurityGroupResult | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__(
            "zenml-aws:components:ZenMLDeployment",
            self.name,
            opts,
        )

        self.config = config

        self.get_network_configuration(vpc, subnets, security_group)
        self.create_metadata_store()
        self.create_server(zenml_remote_stack)

    def get_network_configuration(
        self,
        vpc: aws.ec2.Vpc | None,
        subnets: list[aws.ec2.Subnet] | None,
        security_group: list[aws.ec2.SecurityGroup] | None,
    ):
        """Resolve the server network configuration."""
        if not self.config.server.service_assign_public_ip:
            if any([spec is None for spec in (vpc, subnets, security_group)]):
                raise ValueError(
                    "If default network configuration is disabled, vpc, subnets and security groups must be specified."
                )
            self.vpc = vpc
            self.subnets = subnets
            self.security_group = security_group
        else:
            self.vpc = aws.ec2.get_vpc(default=True)
            self.security_group = aws.ec2.SecurityGroup(
                "zenml-server-sg",
                description="Security group for ZenML server and metadata store",
                vpc_id=self.vpc.id,  # must be the VPC where ECS & RDS are
                egress=[
                    aws.ec2.SecurityGroupEgressArgs(
                        protocol="-1",
                        from_port=0,
                        to_port=0,
                        cidr_blocks=["0.0.0.0/0"],  # allow all outbound
                        description="Allow all outbound traffic",
                    )
                ],
            )
            aws.ec2.SecurityGroupRule(
                "zenml-sg-ingress-server",
                type="ingress",
                from_port=self.config.server.container_port,
                to_port=self.config.server.container_port,
                protocol="tcp",
                security_group_id=self.security_group.id,
                cidr_blocks=[f"{my_ip()}/32"],
            )
            aws.ec2.SecurityGroupRule(
                "zenml-sg-ingress-metadata-store",
                type="ingress",
                from_port=self.config.metadata_store.port,
                to_port=self.config.metadata_store.port,
                protocol="tcp",
                security_group_id=self.security_group.id,
                cidr_blocks=[f"{my_ip()}/32"],
            )
            sn_public = aws.ec2.get_subnets(
                filters=[
                    aws.ec2.GetSubnetsFilterArgs(
                        name="vpc-id",
                        values=[self.vpc.id],
                    ),
                    aws.ec2.GetSubnetsFilterArgs(
                        name="map-public-ip-on-launch",
                        values=["true"],
                    ),
                ]
            )

            sn_private = aws.ec2.get_subnets(
                filters=[
                    aws.ec2.GetSubnetsFilterArgs(
                        name="vpc-id",
                        values=[self.vpc.id],
                    ),
                    aws.ec2.GetSubnetsFilterArgs(
                        name="map-public-ip-on-launch",
                        values=["false"],
                    ),
                ]
            )
            if self.config.server.service_assign_public_ip:
                self.subnets = sn_public
            else:
                self.subnets = sn_private

    def create_metadata_store(self):
        self.metadata_store = aws.rds.Instance(
            "zenml-metadata-store",
            **self.config.metadata_store.model_dump(),
            vpc_security_group_ids=[self.security_group.id],
            opts=pulumi.ResourceOptions(parent=self),
        )

        self.metadata_store_secret = aws.secretsmanager.Secret(
            "zenml-metadata-store-connection-string",
            description="ZenML metadata store connection string",
            opts=pulumi.ResourceOptions(parent=self),
        )

        secret_connection_string = pulumi.Output.all(
            self.metadata_store.engine.apply(
                lambda engine_str: engine_str.split("-")[-1]
            ),
            self.metadata_store.username,
            self.metadata_store.password,
            self.metadata_store.endpoint,
            self.metadata_store.db_name,
        ).apply(lambda args: f"{args[0]}://{args[1]}:{args[2]}@{args[3]}/{args[4]}")

        self.metadata_store_secret_value = aws.secretsmanager.SecretVersion(
            "zenml-metadata-store-connection-string-value",
            secret_id=self.metadata_store_secret.id,
            secret_string=secret_connection_string,
            opts=pulumi.ResourceOptions(parent=self.metadata_store_secret),
        )

    def create_server(self, zenml_remote_stack: ZenMLRemoteStack):
        # --- roles
        # execution role
        execution_role = aws.iam.Role(
            "zenml-server-ecs-execution-role",
            assume_role_policy=aws.iam.get_policy_document(
                statements=[
                    aws.iam.GetPolicyDocumentStatementArgs(
                        effect="Allow",
                        principals=[
                            aws.iam.GetPolicyDocumentStatementPrincipalArgs(
                                type="Service",
                                identifiers=["ecs-tasks.amazonaws.com"],
                            )
                        ],
                        actions=["sts:AssumeRole"],
                    )
                ]
            ).json,
            opts=pulumi.ResourceOptions(parent=self),
        )

        # Attach the AWS-managed ECS execution policy
        aws.iam.RolePolicyAttachment(
            "zenml-server-ecs-execution-role-policy-attachment",
            role=execution_role.name,
            policy_arn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
            opts=pulumi.ResourceOptions(parent=self),
        )

        # Attach custom metadata store connection string secret read policy
        execution_secret_policy = aws.iam.Policy(
            "zenml-server-ecs-execution-secret-read-policy",
            policy=aws.iam.get_policy_document(
                version="2012-10-17",
                statements=[
                    aws.iam.GetPolicyDocumentStatementArgs(
                        effect="Allow",
                        actions=["secretsmanager:GetSecretValue"],
                        resources=[
                            self.metadata_store_secret.arn.apply(lambda x: x),
                            self.metadata_store_secret.arn.apply(lambda x: f"{x}*"),
                        ],
                        sid="ZenMLMetaDataStoreConnectionSecretRead",
                    )
                ],
            ).json,
            opts=pulumi.ResourceOptions(parent=self),
        )

        aws.iam.RolePolicyAttachment(
            "zenml-server-ecs-execution-secret-read-role-policy-attachment",
            policy_arn=execution_secret_policy.arn,
            role=execution_role.name,
            opts=pulumi.ResourceOptions(parent=self),
        )

        # task role
        task_role = aws.iam.Role(
            "zenml-server-ecs-task-role",
            assume_role_policy=aws.iam.get_policy_document(
                statements=[
                    aws.iam.GetPolicyDocumentStatementArgs(
                        effect="Allow",
                        principals=[
                            aws.iam.GetPolicyDocumentStatementPrincipalArgs(
                                type="Service",
                                identifiers=["ecs-tasks.amazonaws.com"],
                            )
                        ],
                        actions=["sts:AssumeRole"],
                    )
                ]
            ).json,
            opts=pulumi.ResourceOptions(parent=self),
        )

        task_policy = aws.iam.Policy(
            "zenml-server-ecs-task-policy",
            policy=aws.iam.get_policy_document(
                version="2012-10-17",
                statements=[
                    aws.iam.GetPolicyDocumentStatementArgs(
                        effect="Allow",
                        actions=[
                            "s3:GetObject",
                            "s3:PutObject",
                            "s3:DeleteObject",
                            "s3:ListBucket",
                        ],
                        resources=[
                            zenml_remote_stack.artifact_store.arn.apply(lambda x: x),
                            zenml_remote_stack.artifact_store.arn.apply(
                                lambda x: f"{x}/*"
                            ),
                        ],
                        sid="ZenMLArtifactStorageReadWrite",
                    )
                ],
            ).json,
            opts=pulumi.ResourceOptions(parent=self),
        )

        aws.iam.RolePolicyAttachment(
            "zenml-server-ecs-task-policy-attachment",
            policy_arn=task_policy.arn,
            role=task_role.name,
            opts=pulumi.ResourceOptions(parent=self),
        )

        # --- ecs
        self.server_cluster = aws.ecs.Cluster(
            "zenml-server-cluster", opts=pulumi.ResourceOptions(parent=self)
        )

        self.server_task_definition = aws.ecs.TaskDefinition(
            "zenml-server-task-definition",
            family="service",
            network_mode="awsvpc",
            requires_compatibilities=["FARGATE"],
            cpu=self.config.server.cpu,
            memory=self.config.server.memory,
            container_definitions=self.metadata_store_secret.arn.apply(
                lambda secret_arn: json.dumps(
                    [
                        {
                            "name": "zenml-server",
                            "image": self.config.server.container_image,
                            "cpu": self.config.server.cpu,
                            "memory": self.config.server.memory,
                            "essential": True,
                            "portMappings": [
                                {
                                    "containerPort": self.config.server.container_port,
                                    "hostPort": self.config.server.container_port,
                                }
                            ],
                            "secrets": [
                                {"name": "ZENML_STORE_URL", "valueFrom": secret_arn}
                            ],
                        },
                    ],
                ),
            ),
            execution_role_arn=execution_role.arn,
            task_role_arn=task_role.arn,
            opts=pulumi.ResourceOptions(parent=self),
        )

        self.server_service = aws.ecs.Service(
            "zenml-server-service",
            cluster=self.server_cluster.id,
            task_definition=self.server_task_definition.arn,
            launch_type="FARGATE",
            network_configuration=aws.ecs.ServiceNetworkConfigurationArgs(
                assign_public_ip=self.config.server.service_assign_public_ip,
                subnets=self.subnets.ids,
                security_groups=[self.security_group.id],
            ),
            desired_count=1,
            opts=pulumi.ResourceOptions(parent=self),
        )

    def export_outputs(self):
        pulumi.export("zenml-deployment-metadata-store-arn", self.metadata_store.arn)
        pulumi.export("zenml-deployment-metadata-store-urn", self.metadata_store.urn)
        pulumi.export(
            "zenml-deployment-metadata-store-username", self.metadata_store.username
        )
        pulumi.export(
            "zenml-deployment-metadata-store-password", self.metadata_store.password
        )
        pulumi.export(
            "zenml-deployment-metadata-store-dbname", self.metadata_store.db_name
        )
        pulumi.export("zenml-deployment-server-cluster-arn", self.server_cluster.arn)
        pulumi.export(
            "zenml-deployment-server-task-definition-urn",
            self.server_task_definition.arn,
        )
        pulumi.export("zenml-deployment-server-service-urn", self.server_service.arn)
