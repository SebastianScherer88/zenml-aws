import hashlib

# from zenml_aws.step_operator.aws_batch_step_operator import AWSBatchStepOperator
# from zenml_aws.orchestrator.aws_stepfunctions_batch_orchestrator_flavor import AWSStepFunctionsOrchestratorSettings
# from zenml_aws.orchestrator.aws_stepfunctions_batch_orchestrator import AWSStepFunctionsOrchestrator
import json
import math
from string import ascii_letters, digits
from typing import Callable, Dict, List, Literal, cast

from pydantic import (
    BaseModel,
    PositiveInt,
    SerializerFunctionWrapHandler,
    field_serializer,
    field_validator,
    model_serializer,
)
from zenml.config import ResourceSettings
from zenml.config.step_configurations import Step
from zenml.config.step_run_info import StepRunInfo
from zenml.entrypoints import StepEntrypointConfiguration
from zenml.logger import get_logger
from zenml.models import PipelineSnapshotResponse
from zenml.orchestrators import ContainerizedOrchestrator
from zenml.step_operators import BaseStepOperator

from zenml_aws.constants import (
    AWS_BATCH_JOB_DEFAULT_NAME,
    BATCH_DOCKER_IMAGE_KEY,
    AWSBatchTag,
)
from zenml_aws.orchestrator.aws_stepfunctions_batch_orchestrator_flavor import (
    AWSStepFunctionsOrchestratorConfig,
)
from zenml_aws.step_operator.aws_batch_step_operator_flavor import (
    AWSBatchStepOperatorConfig,
    AWSBatchStepOperatorSettings,
)

logger = get_logger(__name__)

VALID_FARGATE_VCPU = ("0.25", "0.5", "1.0", "2.0", "4.0", "8.0", "16.0")
VALID_FARGATE_MEMORY = {
    "0.25": ("512", "1024", "2048"),
    "0.5": ("1024", "2048", "3072", "4096"),
    "1.0": ("2048", "3072", "4096", "5120", "6144", "7168", "8192"),
    "2.0": tuple(str(m) for m in range(4096, 16385, 1024)),
    "4.0": tuple(str(m) for m in range(8192, 30721, 1024)),
    "8.0": tuple(str(m) for m in range(16384, 61441, 4096)),
    "16.0": tuple(str(m) for m in range(32768, 122881, 8192)),
}


class ResourceRequirement(BaseModel):
    type: Literal["MEMORY", "VCPU", "GPU"]
    value: str


class AWSBatchJobDefinitionContainerProperties(BaseModel):
    """An AWS Batch job subconfiguration model for a container type job's container specification."""

    image: str
    command: List[str]
    jobRoleArn: str
    executionRoleArn: str
    environment: List[Dict[str, str]] = []  # keys: 'name','value'
    resourceRequirements: List[
        ResourceRequirement
    ] = []  # keys: 'value','type', with type one of 'GPU','VCPU','MEMORY'
    secrets: List[Dict[str, str]] = []  # keys: 'name','value'

    @model_serializer(mode="wrap")
    def sort_model(self, handler, info):
        """We add sorting of environment, secret and resource specs to make
        this process deterministic."""
        sorted_model_dict = handler(self)
        sorted_model_dict["resourceRequirements"] = sorted(
            sorted_model_dict["resourceRequirements"], key=lambda x: x["type"]
        )
        sorted_model_dict["environment"] = sorted(
            sorted_model_dict["environment"], key=lambda x: x["name"]
        )
        sorted_model_dict["secrets"] = sorted(
            sorted_model_dict["secrets"], key=lambda x: x["name"]
        )

        return sorted_model_dict


class AWSBatchJobDefinitionEC2ContainerProperties(
    AWSBatchJobDefinitionContainerProperties
):
    logConfiguration: dict[
        Literal["logDriver"],
        Literal[
            "awsfirelens",
            "awslogs",
            "fluentd",
            "gelf",
            "json-file",
            "journald",
            "logentries",
            "syslog",
            "splunk",
        ],
    ] = {"logDriver": "awslogs"}

    @field_validator("resourceRequirements")
    def check_resource_requirements(
        cls, resource_requirements: List[ResourceRequirement]
    ) -> List[ResourceRequirement]:
        gpu_requirement = [req for req in resource_requirements if req.type == "GPU"]
        cpu_requirement = [req for req in resource_requirements if req.type == "VCPU"][
            0
        ]
        memory_requirement = [
            req for req in resource_requirements if req.type == "MEMORY"
        ][0]

        cpu_float = float(cpu_requirement.value)
        cpu_rounded_int = int(math.ceil(cpu_float))

        if cpu_float != cpu_rounded_int:
            logger.info(
                f"Rounded fractional EC2 resource VCPU vale from {cpu_float} to {cpu_rounded_int} "
                "since AWS Batch on EC2 requires whole integer VCPU count value."
            )

        resource_requirements = [
            ResourceRequirement(type="VCPU", value=str(cpu_rounded_int)),
            memory_requirement,
        ]
        resource_requirements.extend(gpu_requirement)

        return resource_requirements


class AWSBatchJobDefinitionFargateContainerProperties(
    AWSBatchJobDefinitionContainerProperties
):
    logConfiguration: dict[Literal["logDriver"], Literal["awslogs", "splunk"]] = {
        "logDriver": "awslogs"
    }
    networkConfiguration: dict[
        Literal["assignPublicIp"], Literal["ENABLED", "DISABLED"]
    ] = {"assignPublicIp": "ENABLED"}

    @field_validator("resourceRequirements")
    def check_resource_requirements(
        cls, resource_requirements: List[ResourceRequirement]
    ) -> List[ResourceRequirement]:
        gpu_requirement = [req for req in resource_requirements if req.type == "GPU"]

        if gpu_requirement:
            raise ValueError(
                "Invalid fargate resource requirement: GPU. Use EC2 "
                "platform capability if you need custom devices."
            )

        cpu_requirement = [req for req in resource_requirements if req.type == "VCPU"][
            0
        ]
        memory_requirement = [
            req for req in resource_requirements if req.type == "MEMORY"
        ][0]

        if cpu_requirement.value not in VALID_FARGATE_VCPU:
            raise ValueError(
                f"Invalid fargate resource requirement VCPU value {cpu_requirement.value}."
                f"Must be one of {VALID_FARGATE_VCPU}"
            )

        if memory_requirement.value not in VALID_FARGATE_MEMORY[cpu_requirement.value]:
            raise ValueError(
                f"Invalid fargate resource requirement MEMORY value {memory_requirement.value}."
                f"For VCPU={cpu_requirement.value}, MEMORY must be one of {VALID_FARGATE_MEMORY[cpu_requirement.value]}"
            )

        return resource_requirements


class AWSBatchJobDefinitionRetryStrategy(BaseModel):
    """An AWS Batch job subconfiguration model for retry specifications."""

    attempts: PositiveInt = 2
    evaluateOnExit: List[Dict[str, str]] = [
        {
            "onExitCode": "137",  # out-of-memory killed
            "action": "RETRY",
        },
        {
            "onReason": "Host EC2 terminated",  # host EC2 rugpulled->try again
            "action": "RETRY",
        },
    ]

    @field_serializer("evaluateOnExit")
    def sort_evaluate_on_exit(value: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """We add sorting of evaluateOnExit to make serialization of this
        class deterministic."""
        list_of_sorted_dicts = [dict(sorted(setting.items())) for setting in value]
        sorted_list_of_sorted_dicts = sorted(
            list_of_sorted_dicts, key=lambda d: tuple(sorted(d.items()))
        )
        return sorted_list_of_sorted_dicts


class AWSBatchJobDefinition(BaseModel):
    """A utility to validate AWS Batch job descriptions. Base class
    for container and multinode job definition types."""

    jobDefinitionName: str = AWS_BATCH_JOB_DEFAULT_NAME
    type: str = "container"
    containerProperties: (
        AWSBatchJobDefinitionEC2ContainerProperties
        | AWSBatchJobDefinitionFargateContainerProperties
    )
    parameters: Dict[str, str] = {}
    # schedulingPriority: int = 0 # ignored in FIFO queues
    retryStrategy: AWSBatchJobDefinitionRetryStrategy = (
        AWSBatchJobDefinitionRetryStrategy()
    )
    propagateTags: bool = False
    timeout: Dict[str, int] = {
        "attemptDurationSeconds": 3600
    }  # key 'attemptDurationSeconds'
    tags: Dict[str, str] = {}
    platformCapabilities: List[Literal["EC2", "FARGATE"]]

    @classmethod
    def from_orchestrator(
        cls,
        orchestrator: ContainerizedOrchestrator,  # | "AWSBatchOrchestrator",
        step_name: str,
        snapshot: PipelineSnapshotResponse,
        base_environment: Dict[str, str],
        get_image_fn: Callable[[PipelineSnapshotResponse, str], str],
    ) -> "AWSBatchJobDefinition":
        """Utility to instantiate a class instance from the arguments
        accessible inside the AWSStepFunctionsOrchestrator's `submit_pipeline`
        method.."""

        pipeline_config: AWSStepFunctionsOrchestratorConfig = orchestrator.config
        step_config: Step = snapshot.step_configurations[step_name]
        step_resource_settings: ResourceSettings = step_config.config.resource_settings

        # assemble container command
        command = StepEntrypointConfiguration.get_entrypoint_command()
        arguments = StepEntrypointConfiguration.get_entrypoint_arguments(
            step_name=step_name,
            deployment_id=snapshot.id,
        )
        command_and_arguments = command + arguments

        # step settings (AWSBatchStepOperatorSettings)
        step_settings: AWSBatchStepOperatorSettings = step_config.config.settings

        # assemble environment
        step_environment = step_config.config.environment
        environment = {**base_environment, **step_environment}

        container_kwargs = {}

        if step_settings.backend == "EC2":
            AWSBatchJobDefinitionClass = AWSBatchJobEC2Definition
            AWSBatchContainerProperties = AWSBatchJobDefinitionEC2ContainerProperties

        elif step_settings.backend == "FARGATE":
            AWSBatchJobDefinitionClass = AWSBatchJobFargateDefinition
            AWSBatchContainerProperties = (
                AWSBatchJobDefinitionFargateContainerProperties
            )
            container_kwargs["networkConfiguration"] = {
                "assignPublicIp": step_settings.assign_public_ip
            }

        return AWSBatchJobDefinitionClass(
            timeout={"attemptDurationSeconds": step_settings.timeout_seconds},
            type="container",
            tags=step_settings.tags,
            containerProperties=AWSBatchContainerProperties(
                executionRoleArn=pipeline_config.execution_role,
                jobRoleArn=pipeline_config.job_role,
                image=get_image_fn(snapshot, step_name),
                command=command_and_arguments,
                environment=map_environment(environment),
                resourceRequirements=map_resource_settings(step_resource_settings),
                **container_kwargs,
            ),
        )

    @classmethod
    def from_step_operator(
        cls,
        step_operator: BaseStepOperator,
        info: StepRunInfo,
        entrypoint_command: List[str],
        environment: Dict[str, str],
    ) -> "AWSBatchJobDefinition":
        """Utility to instantiate a class instance from the arguments
        accessible inside the AWSBatchStepOperator's `launch` method.."""

        step_settings = cast(
            AWSBatchStepOperatorSettings, step_operator.get_settings(info)
        )

        step_config: AWSBatchStepOperatorConfig = step_operator.config

        # if the step's settings include tags, update the system tags before
        # submitting
        tags = cls.generate_tags(info)
        if step_settings.tags:
            tags.update(step_settings.tags)

        container_kwargs = {}

        if step_settings.backend == "EC2":
            AWSBatchJobDefinitionClass = AWSBatchJobEC2Definition
            AWSBatchContainerProperties = AWSBatchJobDefinitionEC2ContainerProperties

        elif step_settings.backend == "FARGATE":
            AWSBatchJobDefinitionClass = AWSBatchJobFargateDefinition
            AWSBatchContainerProperties = (
                AWSBatchJobDefinitionFargateContainerProperties
            )
            container_kwargs["networkConfiguration"] = {
                "assignPublicIp": step_settings.assign_public_ip
            }

        return AWSBatchJobDefinitionClass(
            timeout={"attemptDurationSeconds": step_settings.timeout_seconds},
            type="container",
            tags=tags,
            containerProperties=AWSBatchContainerProperties(
                executionRoleArn=step_config.execution_role,
                jobRoleArn=step_config.job_role,
                image=info.get_image(key=BATCH_DOCKER_IMAGE_KEY),
                command=entrypoint_command,
                environment=map_environment(environment),
                resourceRequirements=map_resource_settings(
                    info.config.resource_settings
                ),
                **container_kwargs,
            ),
        )

    @staticmethod
    def generate_tags(info: StepRunInfo) -> dict[str, str]:
        return {
            AWSBatchTag.pipeline_name: info.pipeline.name,
            AWSBatchTag.pipeline_run_id: str(info.run_id),
            AWSBatchTag.pipeline_run_name: info.run_name,
            AWSBatchTag.step_name: info.pipeline_step_name,
            AWSBatchTag.step_run_id: str(info.step_run_id),
        }

    def generate_name(self, pipeline_name: str, step_name: str) -> str:
        """Utility to generate a unique AWS Batch job name.

        Args:
            info: The step run information.

        Returns:
            A unique name for the step's AWS Batch job definition
        """

        # Batch allows 128 alphanumeric characters at maximum for job name.
        # We sanitize the pipeline and step names before concatenating,
        # capping at 115 chars and finally suffixing with a 10 character random
        # string, which (with the two hyphens) leaves us at 127 chars.
        sanitized_pipeline_name = sanitize_name(pipeline_name, 30)
        sanitized_step_name = sanitize_name(step_name, 30)

        job_name = f"{sanitized_pipeline_name}-{sanitized_step_name}"
        return f"{job_name}-{self.to_hash()}"

    def to_hash(self) -> str:
        """Hashes the AWS Batch Job definition instance to help disambiguate
        them to avoid needlessly duplicating resources on the AWS side.

        Returns:
            str: The hash encoding the entire AWS Batch job description.
        """

        # we exclude the name before hashing as it will include a randomly
        # generated suffix - see the `generate_name` classmethod.
        sorted_model_dict = self.model_dump(exclude="jobDefinitionName")

        # we remove the system tags before hashing, as run related metadata
        # will trivially be different each time
        non_system_tags = dict(
            [
                (k, v)
                for k, v in sorted_model_dict["tags"].items()
                if k not in AWSBatchTag.list()
            ]
        )
        sorted_model_dict["tags"] = non_system_tags

        sorted_model_json = json.dumps(sorted_model_dict, sort_keys=True).encode()
        sorted_model_hash = hashlib.sha256(sorted_model_json).hexdigest()

        return sorted_model_hash

    @model_serializer(mode="wrap")
    def sort_model(self, handler: SerializerFunctionWrapHandler):
        """We add sorting of parameters and tags to make the hashing
        deterministic."""
        sorted_model_dict = handler(self)
        sorted_model_dict["tags"] = dict(sorted(sorted_model_dict["tags"].items()))
        sorted_model_dict["parameters"] = dict(
            sorted(sorted_model_dict["parameters"].items())
        )
        sorted_model_dict["timeout"] = dict(
            sorted(sorted_model_dict["timeout"].items())
        )

        return sorted_model_dict


class AWSBatchJobEC2Definition(AWSBatchJobDefinition):
    containerProperties: AWSBatchJobDefinitionEC2ContainerProperties
    platformCapabilities: list[Literal["EC2"]] = ["EC2"]


class AWSBatchJobFargateDefinition(AWSBatchJobDefinition):
    containerProperties: AWSBatchJobDefinitionFargateContainerProperties
    platformCapabilities: list[Literal["FARGATE"]] = ["FARGATE"]


def map_environment(environment: Dict[str, str]) -> List[Dict[str, str]]:
    """Utility to map the {name:value} environment to the
    [{"name":name,"value":value},] convention used in the AWS Batch job
    definition spec.

    Args:
        environment: The step's environment variable
        specification

    Returns:
        The mapped environment variable specification
    """

    return [{"name": k, "value": v} for k, v in environment.items()]


def map_resource_settings(
    resource_settings: "ResourceSettings",
) -> List["ResourceRequirement"]:
    """Utility to map the resource_settings to the resource convention used
    in the AWS Batch Job definition spec.

    Args:
        resource_settings: The step's resource settings.

    Returns:
        The mapped resource settings.
    """
    mapped_resource_settings = []

    # handle cpu requirements
    if resource_settings.cpu_count is not None:
        cpu_requirement = ResourceRequirement(
            value=str(resource_settings.cpu_count), type="VCPU"
        )
    else:
        cpu_requirement = ResourceRequirement(value="1", type="VCPU")

    mapped_resource_settings.append(cpu_requirement)

    # handle memory requirements
    memory = resource_settings.get_memory(unit="MiB")
    if memory:
        memory_requirement = ResourceRequirement(value=str(int(memory)), type="MEMORY")
    else:
        memory_requirement = ResourceRequirement(value="1024", type="MEMORY")
    mapped_resource_settings.append(memory_requirement)

    # handle gpu requirements
    if resource_settings.gpu_count is not None and resource_settings.gpu_count != 0:
        mapped_resource_settings.append(
            ResourceRequirement(value=str(resource_settings.gpu_count), type="GPU")
        )

    return mapped_resource_settings


def sanitize_name(name: str, max_length: int) -> bool:
    valid_characters = ascii_letters + digits + "-_"
    sanitized_name = ""
    for char in name:
        sanitized_name += char if char in valid_characters else "-"

    return sanitized_name[:max_length]
