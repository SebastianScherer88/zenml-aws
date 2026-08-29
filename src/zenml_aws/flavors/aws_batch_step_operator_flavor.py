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
"""AWS Batch Step operator flavor."""

from typing import Literal, Optional, Type

from pydantic import Field, PositiveInt, model_validator
from zenml.config.base_settings import BaseSettings
from zenml.integrations.aws import (
    AWS_RESOURCE_TYPE,
)
from zenml.models import ServiceConnectorRequirements
from zenml.step_operators.base_step_operator import (
    BaseStepOperatorConfig,
    BaseStepOperatorFlavor,
)

from zenml_aws.constants import AWS_BATCH_STEP_OPERATOR_FLAVOR, AWSBatchJobStatus


class AWSBatchStepOperatorSettings(BaseSettings):
    """Settings for the Sagemaker step operator."""

    job_queue_name: str | None = Field(
        default=None,
        description="The AWS Batch job queue to submit the step AWS Batch job"
        " to. If not provided, falls back to the default job_queue_name "
        "specified at component registration time. Must be compatible with"
        "`backend`.",
    )
    backend: Literal["EC2", "FARGATE"] | None = Field(
        default=None,
        description="The AWS Batch backend for the step AWS Batch job. If not "
        "provided, falls back to the backend  specified at component "
        "registration time. Must be compatible with `job_queue_name`.",
    )
    execution_role: str | None = Field(
        default=None,
        description="The IAM role arn of the AWS Batch's underlying ECS "
        "execution role. If not provided, falls back to the execution_role "
        "specified at component registration time.",
    )
    job_role: str | None = Field(
        default=None,
        description="The IAM role arn of the AWS Batch's underlying ECS task "
        "role. If not provided, falls back to the job_role specified at "
        "component registration time.",
    )
    tags: dict[str, str] = Field(
        default=dict(),
        description="The tags for this step's AWS BatchJobDefinition resource."
        "For zenml meta tags added automatically, see the "
        "zenml.constants.AWSBatchTags class.",
    )
    timeout_seconds: PositiveInt = Field(
        default=3600,
        description="The number of seconds before AWS Batch times out the "
        "step's job.",
    )
    poll_interval_seconds: float = Field(
        default=20,
        description="The number of seconds to wait between pipeline status "
        "polling calls. Only relevant if `wait_for_completion` was set to True.",
    )
    delete_resources_on: list[AWSBatchJobStatus] = Field(
        description="The AWS Batch job outcomes that will trigger the"
        " clean up of all associated AWS Batch resources. Supported values are:"
        " SUCCEEDED, FAILED. Defaults to SUCCEEDED.",
        default=[
            AWSBatchJobStatus.succeeded,
        ],
    )


class AWSBatchStepOperatorConfig(BaseStepOperatorConfig, AWSBatchStepOperatorSettings):
    """Config for the AWS Batch step operator.

    Note: We use ECS as a backend (not EKS), and EC2 as a compute engine (not
    Fargate). This is because
     - users can avoid the complexity of setting up an EKS cluster, and
     - we can AWS Batch multinode type job support later, which requires EC2
    """

    log_group: str = Field(
        description="The log group for AWS Batch jobs.",
        default="/aws/batch/job/zenml-aws",
    )
    aws_region: str = Field(
        description="The AWS region where the processing job will be run. If "
        "not provided, the standard boto3 resolution chain will be used to "
        "resolve this value."
    )

    @property
    def is_remote(self) -> bool:
        """Checks if this stack component is running remotely.

        This designation is used to determine if the stack component can be
        used with a local ZenML database or if it requires a remote ZenML
        server.

        Returns:
            True if this config is for a remote component, False otherwise.
        """
        return True

    @model_validator(mode="after")
    def require_aws_iam_fields(self):
        required_fields = ("job_queue_name", "backend", "execution_role", "job_role")
        missing_fields = [
            required_field
            for required_field in required_fields
            if not getattr(self, required_field)
        ]
        if missing_fields:
            raise ValueError(
                f"Missing configuration values for fields {missing_fields} for"
                f" step operator flavour {AWS_BATCH_STEP_OPERATOR_FLAVOR}."
            )
        return self


class AWSBatchStepOperatorFlavor(BaseStepOperatorFlavor):
    """Flavor for the AWS Batch step operator."""

    @property
    def name(self) -> str:
        """Name of the flavor.

        Returns:
            The name of the flavor.
        """
        return AWS_BATCH_STEP_OPERATOR_FLAVOR

    @property
    def service_connector_requirements(
        self,
    ) -> Optional[ServiceConnectorRequirements]:
        """Service connector resource requirements for service connectors.

        Specifies resource requirements that are used to filter the available
        service connector types that are compatible with this flavor.

        Returns:
            Requirements for compatible service connectors, if a service
            connector is required for this flavor.
        """
        return ServiceConnectorRequirements(resource_type=AWS_RESOURCE_TYPE)

    @property
    def docs_url(self) -> Optional[str]:
        """A url to point at docs explaining this flavor.

        Returns:
            A flavor docs url.
        """
        return self.generate_default_docs_url()

    @property
    def sdk_docs_url(self) -> Optional[str]:
        """A url to point at SDK docs explaining this flavor.

        Returns:
            A flavor SDK docs url.
        """
        return self.generate_default_sdk_docs_url()

    @property
    def logo_url(self) -> str:
        """A url to represent the flavor in the dashboard.

        Returns:
            The flavor logo.
        """
        return "https://zenml-aws-public.s3.eu-west-1.amazonaws.com/aws-batch-logo.png"

    @property
    def config_class(self) -> Type[AWSBatchStepOperatorConfig]:
        """Returns BatchStepOperatorConfig config class.

        Returns:
            The config class.
        """
        return AWSBatchStepOperatorConfig

    @property
    def implementation_class(self):
        """Implementation class.

        Returns:
            The implementation class.
        """
        from zenml_aws.step_operator.aws_batch_step_operator import AWSBatchStepOperator

        return AWSBatchStepOperator
