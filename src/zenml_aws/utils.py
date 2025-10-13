import math
from string import ascii_letters, digits
from typing import Dict, List, Literal

from pydantic import BaseModel, PositiveInt, field_validator
from zenml.config import ResourceSettings
from zenml.logger import get_logger

logger = get_logger(__name__)

VALID_FARGATE_VCPU = ("0.25", "0.5", "1", "2", "4", "8", "16")
VALID_FARGATE_MEMORY = {
    "0.25": ("512", "1024", "2048"),
    "0.5": ("1024", "2048", "3072", "4096"),
    "1": ("2048", "3072", "4096", "5120", "6144", "7168", "8192"),
    "2": tuple(str(m) for m in range(4096, 16385, 1024)),
    "4": tuple(str(m) for m in range(8192, 30721, 1024)),
    "8": tuple(str(m) for m in range(16384, 61441, 4096)),
    "16": tuple(str(m) for m in range(32768, 122881, 8192)),
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
        cpu_rounded_int = math.ceil(cpu_float)

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


class AWSBatchJobDefinition(BaseModel):
    """A utility to validate AWS Batch job descriptions. Base class
    for container and multinode job definition types."""

    jobDefinitionName: str
    type: str = "container"
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


def sanitize_name(name: str) -> bool:
    valid_characters = ascii_letters + digits + "-_"
    sanitized_name = ""
    for char in name:
        sanitized_name += char if char in valid_characters else "-"

    return sanitized_name
