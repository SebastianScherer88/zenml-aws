"""AWS Step Functions orchestrator flavor."""

from typing import Literal, Optional, Type

from pydantic import Field, PositiveInt
from zenml.config.base_settings import BaseSettings
from zenml.integrations.aws import (
    AWS_RESOURCE_TYPE,
)
from zenml.models import ServiceConnectorRequirements
from zenml.orchestrators import BaseOrchestratorConfig
from zenml.orchestrators.base_orchestrator import BaseOrchestratorFlavor
from zenml.utils.secret_utils import SecretField

from zenml_aws.constants import AWS_STEP_FUNCTIONS_ORCHESTRATOR_FLAVOR


class AWSStepFunctionsOrchestratorSettings(BaseSettings):
    """Settings for the AWS Step Functions Orchestrator."""

    job_queue_name: str = Field(
        default="",
        description="The default AWS Batch job queue to submit each step's AWS"
        " Batch job to. Can be overriden at the step level. Must be compatible"
        " with `default_backend`.",
    )
    backend: Literal["EC2", "FARGATE"] = Field(
        default="FARGATE",
        description="The default AWS Batch platform capability for each step's"
        " AWS Batch job. Must be compatible with `job_queue_name`. Defaults to"
        " 'FARGATE'.",
    )
    tags: dict[str, str] = Field(
        default=dict(),
        description="The tags for all steps' AWS BatchJobDefinition and"
        "Stepfunctions resources. For zenml meta tags added automatically, see"
        " the zenml.constants.AWSBatchTags class.",
    )

    assign_public_ip: Literal["ENABLED", "DISABLED"] = Field(
        default="ENABLED",
        description="Sets the network configuration's assignPublicIp field."
        "Only relevant for FARGATE backend steps.",
    )
    timeout_seconds: PositiveInt = Field(
        default=3600,
        description="The number of seconds before AWS Batch times out a step's" " job.",
    )


class AWSStepFunctionsOrchestratorConfig(
    BaseOrchestratorConfig, AWSStepFunctionsOrchestratorSettings
):
    """Configuration for the AWS Step Functions Orchestrator.

    Attributes:
        name: Name of the orchestrator
    """

    stepfunctions_execution_role: str = Field(
        description="The IAM role arn of the Stepfunctions execution role."
    )

    batch_execution_role: str = Field(
        description="The IAM role arn of the ECS execution role."
    )
    batch_job_role: str = Field(description="The IAM role arn of the ECS job role.")

    aws_access_key_id: Optional[str] = SecretField(
        default=None,
        description="The AWS access key ID to use to authenticate to AWS. "
        "If not provided, the value from the default AWS config will be used.",
    )
    aws_secret_access_key: Optional[str] = SecretField(
        default=None,
        description="The AWS secret access key to use to authenticate to AWS. "
        "If not provided, the value from the default AWS config will be used.",
    )
    aws_profile: Optional[str] = Field(
        None,
        description="The AWS profile to use for authentication if not using "
        "service connectors or explicit credentials. If not provided, the "
        "default profile will be used.",
    )
    aws_auth_role_arn: Optional[str] = Field(
        None,
        description="The ARN of an intermediate IAM role to assume when "
        "authenticating to AWS.",
    )
    region: Optional[str] = Field(
        None,
        description="The AWS region where the processing job will be run. "
        "If not provided, the value from the default AWS config will be used.",
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

    @property
    def is_synchronous(self) -> bool:
        """Whether the orchestrator runs synchronous or not.

        Returns:
            Whether the orchestrator runs synchronous or not.
        """
        return self.synchronous


class AWSStepFunctionsOrchestratorFlavor(BaseOrchestratorFlavor):
    """Flavor for the AWS Step Functions orchestrator."""

    @property
    def name(self) -> str:
        """Name of the flavor.

        Returns:
            The name of the flavor.
        """
        return AWS_STEP_FUNCTIONS_ORCHESTRATOR_FLAVOR

    @property
    def service_connector_requirements(
        self,
    ) -> Optional[ServiceConnectorRequirements]:
        """Service connector resource requirements for service connectors.

        Returns:
            Requirements for compatible service connectors.
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
        return "https://public-flavor-logos.s3.eu-central-1.amazonaws.com/orchestrator/aws.png"

    @property
    def config_class(self) -> Type[AWSStepFunctionsOrchestratorConfig]:
        """Returns StepFunctionsOrchestratorConfig config class.

        Returns:
            The config class.
        """
        return AWSStepFunctionsOrchestratorConfig

    @property
    def implementation_class(self):
        """Implementation class.

        Returns:
            The implementation class.
        """
        from zenml_aws.orchestrator.aws_stepfunctions_batch_orchestrator import (
            AWSStepFunctionsOrchestrator,
        )

        return AWSStepFunctionsOrchestrator
