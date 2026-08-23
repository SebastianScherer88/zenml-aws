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
from datetime import datetime
from typing import (
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    cast,
)

from boto3 import Session
from zenml import log_metadata
from zenml.config.base_settings import BaseSettings
from zenml.config.build_configuration import BuildConfiguration
from zenml.config.step_run_info import StepRunInfo
from zenml.enums import StackComponentType
from zenml.logger import get_logger
from zenml.models import PipelineSnapshotBase
from zenml.stack import Stack, StackValidator
from zenml.step_operators import BaseStepOperator

from zenml_aws.aws_batch_job_definition import (
    AWSBatchJobDefinition,
    check_existing_batch_job_definition,
    register_new_batch_job_definition,
)
from zenml_aws.constants import (
    _ENTRYPOINT_ENV_VARIABLE,
    BATCH_DOCKER_IMAGE_KEY,
    AWSBatchJobStatus,
)
from zenml_aws.flavors.aws_batch_step_operator_flavor import (
    AWSBatchStepOperatorConfig,
    AWSBatchStepOperatorSettings,
)
from zenml_aws.schema import AWSBatchStepStepMetadata

logger = get_logger(__name__)


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

    def get_docker_builds(
        self, snapshot: PipelineSnapshotBase
    ) -> List["BuildConfiguration"]:
        """Gets the Docker builds required for the component.

        Args:
            deployment: The pipeline deployment for which to get the builds.

        Returns:
            The required Docker builds.
        """
        builds = []

        for step_name, step in snapshot.step_configurations.items():
            if step.config.uses_step_operator(self.name):
                build = BuildConfiguration(
                    key=BATCH_DOCKER_IMAGE_KEY,
                    settings=step.config.docker_settings,
                    step_name=step_name,
                    entrypoint=f"${_ENTRYPOINT_ENV_VARIABLE}",
                )
                builds.append(build)

        return builds

    def submit_job(
        self,
        batch_client,
        job_definition: AWSBatchJobDefinition,
        info: StepRunInfo,
    ) -> tuple[str, str]:
        """Submits the AWS Batch job that implements this zenml pipeline step.

        Args:
            batch_client (_type_): The AWS Batch client instance
            job_definition (AWSBatchJobDefinition): The name of the AWS Batch job definition
            info (StepRunInfo): The step operator's info.
            wait (bool, optional): Whether to wait for the completion of the AWS
                Batch job. Defaults to True.

        Raises:
            RuntimeError: If the AWS Batch job has failed.
            ClientError: If the call to the AWS Batch client's `describe_jobs`
                fails.
        """

        step_settings = cast(AWSBatchStepOperatorSettings, self.get_settings(info))

        response = batch_client.submit_job(
            jobName=job_definition.jobDefinitionName,
            jobQueue=step_settings.job_queue_name
            if step_settings.job_queue_name
            else self.config.job_queue_name,
            jobDefinition=job_definition.jobDefinitionName,
            tags=job_definition.tags,
        )

        job_id = response["jobId"]
        job_arn = response["jobArn"]
        return job_id, job_arn

    def await_job(self, batch_client, job_id: str, job_definition_arn: str):
        while True:
            now = datetime.now()
            response = batch_client.describe_jobs(jobs=[job_id])
            status: AWSBatchJobStatus = response["jobs"][0]["status"]
            status_reason = response["jobs"][0].get("statusReason", "Unknown")

            logger.info(f"Status of batch job {job_id}: [{status}] @{now}.")

            if status in (
                AWSBatchJobStatus.submitted,
                AWSBatchJobStatus.runnable,
                AWSBatchJobStatus.pending,
                AWSBatchJobStatus.starting,
                AWSBatchJobStatus.running,
            ):
                time.sleep(self.config.poll_interval_seconds)
            elif status in (AWSBatchJobStatus.succeeded, AWSBatchJobStatus.failed):
                break

        if status in self.config.delete_resources_on:
            # clean up job description
            try:
                batch_client.deregister_job_definition(jobDefinition=job_definition_arn)
                logger.info(
                    f"Successfully deleted job definition {job_definition_arn} @ {now}"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to delete job definition {job_definition_arn}: {e}"
                )

        if status == AWSBatchJobStatus.failed:
            raise RuntimeError(
                f"Job {job_id} failed with status reason {status_reason} @{now}"
            )

    def launch(
        self,
        info: StepRunInfo,
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

        batch_job_definition = AWSBatchJobDefinition.from_step_operator(
            step_operator=self,
            info=info,
            entrypoint_command=entrypoint_command,
            environment=environment,
        )
        unique_batch_job_definition_name = batch_job_definition.generate_name(
            info.pipeline.name, info.pipeline_step_name
        )
        batch_job_definition = AWSBatchJobDefinition(
            jobDefinitionName=unique_batch_job_definition_name,
            **batch_job_definition.model_dump(exclude="jobDefinitionName"),
        )

        logger.info(f"AWS Batch job definition: {unique_batch_job_definition_name}")

        boto_session = self._get_aws_session()
        batch_client = boto_session.client("batch")

        # check if this job definition already exists
        job_definition_arn = check_existing_batch_job_definition(
            batch_client, batch_job_definition.jobDefinitionName
        )

        # register new job definition if necessary
        if job_definition_arn is None:
            logger.info(
                f"AWS Batch job definition {unique_batch_job_definition_name} doesnt exist yet. Registering..."
            )

            job_definition_arn = register_new_batch_job_definition(
                batch_client, batch_job_definition
            )

        # submit AWS Batch job
        job_id, job_arn = self.submit_job(batch_client, batch_job_definition, info)

        step_settings: AWSBatchStepOperatorSettings = self.get_settings(info)

        # update step metadata
        log_metadata(
            step_name=info.pipeline_step_name,
            run_id_name_or_prefix=info.run_id,
            metadata={
                "AWS Batch": AWSBatchStepStepMetadata.from_arns(
                    job_definition_arn,
                    step_settings.backend.lower(),
                    step_settings.job_queue_name,
                    job_arn,
                ).model_dump(),
            },
        )

        # await job outcome
        self.await_job(batch_client, job_id, job_definition_arn)
