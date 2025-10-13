#  Copyright (c) ZenML GmbH 2022. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.
"""Implementation of the AWS Batch Step Operator."""

import time
from typing import (
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    cast,
)

from boto3 import Session
from botocore.exceptions import ClientError
from zenml.config.base_settings import BaseSettings
from zenml.config.build_configuration import BuildConfiguration
from zenml.config.step_run_info import StepRunInfo
from zenml.enums import StackComponentType
from zenml.logger import get_logger
from zenml.stack import Stack, StackValidator
from zenml.step_operators import BaseStepOperator
from zenml.utils.string_utils import random_str

from zenml_aws.step_operator.aws_batch_step_operator_flavor import (
    AWSBatchStepOperatorConfig,
    AWSBatchStepOperatorSettings,
)
from zenml_aws.utils import (
    AWSBatchJobDefinition,
    AWSBatchJobDefinitionEC2ContainerProperties,
    AWSBatchJobDefinitionFargateContainerProperties,
    AWSBatchJobEC2Definition,
    AWSBatchJobFargateDefinition,
    map_environment,
    map_resource_settings,
    sanitize_name,
)

logger = get_logger(__name__)

BATCH_DOCKER_IMAGE_KEY = "aws_batch_step_operator"
_ENTRYPOINT_ENV_VARIABLE = "__ZENML_ENTRYPOINT"


class AWSBatchStepOperator(BaseStepOperator):
    """Step operator to run a step on AWS Batch.

    This class defines code that builds an image with the ZenML entrypoint
    to run using AWS Batch.
    """

    @property
    def config(self) -> AWSBatchStepOperatorConfig:
        """Returns the `AWSBatchStepOperatorConfig` config.

        Returns:
            The configuration.
        """
        return cast(AWSBatchStepOperatorConfig, self._config)

    @property
    def settings_class(self) -> Optional[Type["BaseSettings"]]:
        """Settings class for the AWS Batch step operator.

        Returns:
            The settings class.
        """
        return AWSBatchStepOperatorSettings

    def _get_aws_session(self) -> Session:
        """Method to create the AWS Batch session with proper authentication.

        Returns:
            The AWS Batch session.

        Raises:
            RuntimeError: If the connector returns the wrong type for the
                session.
        """
        # Get authenticated session
        # Option 1: Service connector
        boto_session: Session
        if connector := self.get_connector():
            boto_session = connector.connect()
            if not isinstance(boto_session, Session):
                raise RuntimeError(
                    f"Expected to receive a `boto3.Session` object from the "
                    f"linked connector, but got type `{type(boto_session)}`."
                )
        # Option 2: Explicit configuration
        # Args that are not provided will be taken from the default AWS config.
        else:
            boto_session = Session(
                aws_access_key_id=self.config.aws_access_key_id,
                aws_secret_access_key=self.config.aws_secret_access_key,
                region_name=self.config.region,
                profile_name=self.config.aws_profile,
            )
            # If a role ARN is provided for authentication, assume the role
            if self.config.aws_auth_role_arn:
                sts = boto_session.client("sts")
                response = sts.assume_role(
                    RoleArn=self.config.aws_auth_role_arn,
                    RoleSessionName="zenml-aws-batch-step-operator",
                )
                credentials = response["Credentials"]
                boto_session = Session(
                    aws_access_key_id=credentials["AccessKeyId"],
                    aws_secret_access_key=credentials["SecretAccessKey"],
                    aws_session_token=credentials["SessionToken"],
                    region_name=self.config.region,
                )
        return boto_session

    @property
    def validator(self) -> Optional[StackValidator]:
        """Validates the stack.

        Returns:
            A validator that checks that the stack contains a remote container
            registry and a remote artifact store.
        """

        def _validate_remote_components(stack: "Stack") -> Tuple[bool, str]:
            if stack.artifact_store.config.is_local:
                return False, (
                    "The Batch step operator runs code remotely and "
                    "needs to write files into the artifact store, but the "
                    f"artifact store `{stack.artifact_store.name}` of the "
                    "active stack is local. Please ensure that your stack "
                    "contains a remote artifact store when using the Batch "
                    "step operator."
                )

            container_registry = stack.container_registry
            assert container_registry is not None

            if container_registry.config.is_local:
                return False, (
                    "The Batch step operator runs code remotely and "
                    "needs to push/pull Docker images, but the "
                    f"container registry `{container_registry.name}` of the "
                    "active stack is local. Please ensure that your stack "
                    "contains a remote container registry when using the "
                    "Batch step operator."
                )

            return True, ""

        return StackValidator(
            required_components={
                StackComponentType.CONTAINER_REGISTRY,
                StackComponentType.IMAGE_BUILDER,
            },
            custom_validation_function=_validate_remote_components,
        )

    def generate_unique_batch_job_name(self, info: "StepRunInfo") -> str:
        """Utility to generate a unique AWS Batch job name.

        Args:
            info: The step run information.

        Returns:
            A unique name for the step's AWS Batch job definition
        """

        # Batch allows 128 alphanumeric characters at maximum for job name.
        # We sanitize the pipeline and step names before concatenating,
        # capping at 115 chars and finally suffixing with a 6 character random
        # string

        sanitized_pipeline_name = sanitize_name(info.pipeline.name)
        sanitized_step_name = sanitize_name(sanitized_pipeline_name)

        job_name = f"{sanitized_pipeline_name}-{sanitized_step_name}"[:115]
        suffix = random_str(6)
        return f"{job_name}-{suffix}"

    def generate_job_definition(
        self,
        info: "StepRunInfo",
        entrypoint_command: List[str],
        environment: Dict[str, str],
    ) -> AWSBatchJobDefinition:
        """Utility to map zenml internal configurations to a valid AWS Batch
        job definition."""

        image_name = info.get_image(key=BATCH_DOCKER_IMAGE_KEY)

        resource_settings = info.config.resource_settings
        step_settings = cast(AWSBatchStepOperatorSettings, self.get_settings(info))

        if step_settings.environment:
            environment.update(step_settings.environment)

        job_name = self.generate_unique_batch_job_name(info)

        if step_settings.backend == "EC2":
            AWSBatchJobDefinitionClass = AWSBatchJobEC2Definition
            AWSBatchContainerProperties = AWSBatchJobDefinitionEC2ContainerProperties
            container_kwargs = {}
        elif step_settings.backend == "FARGATE":
            AWSBatchJobDefinitionClass = AWSBatchJobFargateDefinition
            AWSBatchContainerProperties = (
                AWSBatchJobDefinitionFargateContainerProperties
            )
            container_kwargs = {
                "networkConfiguration": {
                    "assignPublicIp": step_settings.assign_public_ip
                }
            }

        return AWSBatchJobDefinitionClass(
            jobDefinitionName=job_name,
            timeout={"attemptDurationSeconds": step_settings.timeout_seconds},
            type="container",
            containerProperties=AWSBatchContainerProperties(
                executionRoleArn=self.config.execution_role,
                jobRoleArn=self.config.job_role,
                image=image_name,
                command=entrypoint_command,
                environment=map_environment(environment),
                resourceRequirements=map_resource_settings(resource_settings),
                **container_kwargs,
            ),
        )

    def get_docker_builds(self, deployment) -> List["BuildConfiguration"]:
        """Gets the Docker builds required for the component.

        Args:
            deployment: The pipeline deployment for which to get the builds.

        Returns:
            The required Docker builds.
        """
        builds = []
        for step_name, step in deployment.step_configurations.items():
            if step.config.uses_step_operator(self.name):
                build = BuildConfiguration(
                    key=BATCH_DOCKER_IMAGE_KEY,
                    settings=step.config.docker_settings,
                    step_name=step_name,
                    entrypoint=f"${_ENTRYPOINT_ENV_VARIABLE}",
                )
                builds.append(build)

        return builds

    def launch(
        self,
        info: "StepRunInfo",
        entrypoint_command: List[str],
        environment: Dict[str, str],
    ) -> None:
        """Launches a step on AWS Batch.

        Args:
            info: Information about the step run.
            entrypoint_command: Command that executes the step.
            environment: Environment variables to set in the step operator
                environment.

        Raises:
            RuntimeError: If the connector returns an object that is not a
                `boto3.Session`.
        """

        job_definition = self.generate_job_definition(
            info, entrypoint_command, environment
        )

        logger.info(f"Job definition: {job_definition}")

        boto_session = self._get_aws_session()
        batch_client = boto_session.client("batch")

        response = batch_client.register_job_definition(**job_definition.model_dump())

        job_definition_name = response["jobDefinitionName"]

        step_settings = cast(AWSBatchStepOperatorSettings, self.get_settings(info))

        response = batch_client.submit_job(
            jobName=job_definition.jobDefinitionName,
            jobQueue=step_settings.job_queue_name
            if step_settings.job_queue_name
            else self.config.default_job_queue_name,
            jobDefinition=job_definition_name,
        )

        job_id = response["jobId"]

        while True:
            try:
                response = batch_client.describe_jobs(jobs=[job_id])
                status = response["jobs"][0]["status"]
                status_reason = response["jobs"][0].get("statusReason", "Unknown")

                if status == "SUCCEEDED":
                    logger.info(f"Job completed successfully: {job_id}")
                    break
                elif status == "FAILED":
                    raise RuntimeError(f"Job {job_id} failed: {status_reason}")
                else:
                    logger.info(
                        f"Job {job_id} neither failed nor succeeded. Status: "
                        f"{status}. Status reason: {status_reason}. Waiting "
                        "another 10 seconds."
                    )
                    time.sleep(10)
            except ClientError as e:
                logger.error(f"Failed to describe job {job_id}: {e}")
                raise
