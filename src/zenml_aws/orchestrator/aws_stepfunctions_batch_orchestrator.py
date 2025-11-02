"""Improved AWS Step Functions Orchestrator with Parallel Execution and Safety Checks"""

import json
import os
import time
from collections import deque
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    cast,
)

import boto3
from rich import print
from zenml.config.base_settings import BaseSettings
from zenml.constants import (
    METADATA_ORCHESTRATOR_LOGS_URL,
    METADATA_ORCHESTRATOR_RUN_ID,
    METADATA_ORCHESTRATOR_URL,
)
from zenml.enums import StackComponentType
from zenml.logger import get_logger
from zenml.metadata.metadata_types import MetadataType
from zenml.models import PipelineRunResponse, PipelineSnapshotResponse
from zenml.orchestrators import ContainerizedOrchestrator, SubmissionResult
from zenml.stack import Stack, StackValidator

from zenml_aws.batch_job_definitions import register_equivalent_job_definitions

# Custom imports
from zenml_aws.orchestrator.aws_stepfunctions_batch_orchestrator_flavor import (
    AWSStepFunctionsOrchestratorConfig,
    AWSStepFunctionsOrchestratorSettings,
)

logger = get_logger(__name__)

ENV_ZENML_STEP_FUNCTIONS_RUN_ID = "ZENML_STEP_FUNCTIONS_RUN_ID"
MAX_TASK_DEFINITION_VERSIONS = 50


class AWSStepFunctionsOrchestrator(ContainerizedOrchestrator):
    """Orchestrator responsible for running pipelines on AWS Step Functions."""

    @property
    def config(self) -> AWSStepFunctionsOrchestratorConfig:
        """Returns the `StepFunctionsOrchestratorConfig` config.

        Returns:
            The configuration.
        """
        return cast(AWSStepFunctionsOrchestratorConfig, self._config)

    @property
    def validator(self) -> Optional[StackValidator]:
        """Validates the stack.

        In the remote case, checks that the stack contains a container registry,
        image builder and only remote components.

        Returns:
            A `StackValidator` instance.
        """

        def _validate_remote_components(
            stack: Stack,
        ) -> Tuple[bool, str]:
            for component in stack.components.values():
                if not component.config.is_local:
                    continue

                return False, (
                    f"The Stepfunctions orchestrator runs pipelines remotely, "
                    f"but the '{component.name}' {component.type.value} is "
                    "a local stack component and will not be available in "
                    "the Stepfunctions step.\nPlease ensure that you always "
                    "use non-local stack components with the Stepfunctions "
                    "orchestrator."
                )

            return True, ""

        return StackValidator(
            required_components={
                StackComponentType.CONTAINER_REGISTRY,
                StackComponentType.IMAGE_BUILDER,
            },
            custom_validation_function=_validate_remote_components,
        )

    def get_orchestrator_run_id(self) -> str:
        """Returns the run id of the active orchestrator run.

        Important: This needs to be a unique ID and return the same value for
        all steps of a pipeline run.

        Returns:
            The orchestrator run id.

        Raises:
            RuntimeError: If the run id cannot be read from the environment.
        """
        try:
            return os.environ[ENV_ZENML_STEP_FUNCTIONS_RUN_ID]
        except KeyError:
            raise RuntimeError(
                "Unable to read run id from environment variable "
                f"{ENV_ZENML_STEP_FUNCTIONS_RUN_ID}."
            )

    @property
    def settings_class(self) -> Optional[Type["BaseSettings"]]:
        """Settings class for the Step Functions orchestrator.

        Returns:
            The settings class.
        """
        return AWSStepFunctionsOrchestratorSettings

    def submit_pipeline(
        self,
        snapshot: PipelineSnapshotResponse,
        stack: Stack,
        base_environment: Dict[str, str],
        step_environments: Dict[str, Dict[str, str]],
        placeholder_run: Optional["PipelineRunResponse"] = None,
    ) -> SubmissionResult:
        """Submits a pipeline to the orchestrator.

        This method should only submit the pipeline and not wait for it to
        complete. If the orchestrator is configured to wait for the pipeline run
        to complete, a function that waits for the pipeline run to complete can
        be passed as part of the submission result.

        Args:
            snapshot: The pipeline snapshot to submit.
            stack: The stack the pipeline will run on.
            base_environment: Base environment shared by all steps. This should
                be set if your orchestrator for example runs one container that
                is responsible for starting all the steps.
            step_environments: Environment variables to set when executing
                specific steps.
            placeholder_run: An optional placeholder run for the snapshot.

        Raises:
            RuntimeError: If there is an error creating or scheduling the
                pipeline.
            TypeError: If the network_config passed is not compatible with the
                AWS SageMaker NetworkConfig class.
            ValueError: If the schedule is not valid.

        Returns:
            Optional submission result.
        """

        self.config

        STEP_FUNCTIONS_ROLE_ARN = (
            "arn:aws:iam::847068433460:role/zenml-hackathon-step-functions-role"
        )

        step_names_to_job_defs: Dict[str, str] = (  # noqa: F841
            register_equivalent_job_definitions(
                batch_client=boto3.client("batch", region_name="us-west-2"),
                execution_role_arn=STEP_FUNCTIONS_ROLE_ARN,
                snapshot=snapshot,
                base_environment=base_environment,
                step_environments=step_environments,
                get_image_fn=self.get_image,
            )
        )

        sfn = boto3.client("stepfunctions", region_name="us-west-2")
        name = "ZenML_Batch_Job_StateMachine_DAG_Script"
        state_machine_definition = self.create_state_machine_definition(snapshot)

        print(state_machine_definition)

        # Create and execute state machine using helper functions
        state_machine_arn = self.create_state_machine_from_definition(
            sfn_client=sfn,
            name=name,
            definition=state_machine_definition,
            role_arn=STEP_FUNCTIONS_ROLE_ARN,
        )

        execution_arn = self.start_state_machine_execution(
            sfn_client=sfn,
            state_machine_arn=state_machine_arn,
            pipeline_name=snapshot.pipeline_configuration.name,
        )

        # Generate metadata using the standalone function

        SubmissionResult(metadata=generate_step_functions_metadata(execution_arn))

        # finally:
        #     # Clean up state machine after execution starts
        #     try:
        #         # sfn.delete_state_machine(stateMachineArn=state_machine_arn)
        #         ...
        #     except Exception as e:
        #         logger.warning(f"Failed to delete state machine: {e}")

    @staticmethod
    def build_dag_levels(
        snapshot: PipelineSnapshotResponse,
    ) -> List[List[str]]:
        """Constructs a DAG level representation of pipeline steps."""
        step_dependencies = {
            step_name: set(step_config.spec.upstream_steps)
            for step_name, step_config in snapshot.step_configurations.items()
        }

        in_degree = {step: len(deps) for step, deps in step_dependencies.items()}
        queue = deque([step for step, count in in_degree.items() if count == 0])
        levels = []

        while queue:
            level = []
            for _ in range(len(queue)):
                step = queue.popleft()
                level.append(step)

                for dependent in step_dependencies:
                    if step in step_dependencies[dependent]:
                        in_degree[dependent] -= 1
                        if in_degree[dependent] == 0:
                            queue.append(dependent)

            levels.append(level)

        if sum(len(level) for level in levels) != len(snapshot.step_configurations):
            raise RuntimeError("Pipeline contains cycles or invalid dependencies")

        return levels

    def create_state_machine_from_definition(
        self,
        sfn_client: boto3.client,
        name: str,
        definition: Dict[str, Any],
        role_arn: str,
    ) -> str:
        """Create an AWS Step Functions state machine.

        Args:
            sfn_client: Boto3 Step Functions client
            name: Name of the state machine
            definition: State machine definition dictionary
            role_arn: ARN of the IAM role for the state machine

        Returns:
            The ARN of the created state machine
        """
        response = sfn_client.create_state_machine(
            name=name,
            definition=json.dumps(definition),
            roleArn=role_arn,
            type="STANDARD",
            tags=[],
        )
        state_machine_arn = response["stateMachineArn"]
        print(f"State Machine ARN: {state_machine_arn}")
        return state_machine_arn

    def start_state_machine_execution(
        self,
        sfn_client: boto3.client,
        state_machine_arn: str,
        pipeline_name: str,
    ) -> str:
        """Start execution of an AWS Step Functions state machine.

        Args:
            sfn_client: Boto3 Step Functions client
            state_machine_arn: ARN of the state machine to execute
            pipeline_name: Name of the ZenML pipeline

        Returns:
            The ARN of the execution
        """
        execution_name = f"zenml-{pipeline_name}-{int(time.time())}"
        response = sfn_client.start_execution(
            stateMachineArn=state_machine_arn,
            name=execution_name,
        )
        return response["executionArn"]

    def create_state_machine_definition(
        self,
        snapshot: PipelineSnapshotResponse,
    ) -> Dict[str, Any]:
        """
        Creates an AWS Step Functions state machine for the ZenML pipeline.

        - Uses a **static job definition and job queue**.
        - Supports **parallel execution** of pipeline steps.
        """

        # 🔹 Static AWS Batch settings
        JOB_DEFINITION_NAME = "zenml-fargate-job-def-from-python"
        JOB_QUEUE_NAME = "zenml-fargate-queue-manual"

        # 🔹 Build DAG levels for parallel execution
        dag_levels = self.build_dag_levels(snapshot)

        # 🔹 Define the Step Functions states
        states = {"Start": {"Type": "Pass", "Next": "Level_0"}}

        # Add each level with parallel branches
        for level_num, level in enumerate(dag_levels):
            states[f"Level_{level_num}"] = {
                "Type": "Parallel",
                "Branches": [
                    {
                        "StartAt": step,
                        "States": {
                            step: {
                                "Type": "Task",
                                "Resource": "arn:aws:states:::batch:submitJob.sync",
                                "Parameters": {
                                    "JobDefinition": JOB_DEFINITION_NAME,
                                    "JobQueue": JOB_QUEUE_NAME,
                                    "JobName": step,
                                },
                                "End": True,
                            }
                        },
                    }
                    for step in level
                ],
                "Next": f"Level_{level_num + 1}"
                if level_num < len(dag_levels) - 1
                else "Success",
            }

        # 🔹 Add Success state
        states.update({"Success": {"Type": "Succeed"}})

        # 🔹 Define the full state machine JSON
        definition = {
            "Comment": f"ZenML Pipeline: {snapshot.pipeline_configuration.name}",
            "StartAt": "Start",
            "States": states,
        }

        return definition


def generate_step_functions_metadata(
    execution_arn: str,
) -> Dict[str, MetadataType]:
    region = execution_arn.split(":")[3]
    return {
        METADATA_ORCHESTRATOR_RUN_ID: execution_arn,
        METADATA_ORCHESTRATOR_URL: (
            f"https://{region}.console.aws.amazon.com/states/home"
            f"?region={region}#/executions/details/{execution_arn}"
        ),
        METADATA_ORCHESTRATOR_LOGS_URL: (
            f"https://{region}.console.aws.amazon.com/cloudwatch/home"
            f"?region={region}#logsV2:log-groups/log-group/$252Faws$252Fbatch$252Fjob"
        ),
    }
