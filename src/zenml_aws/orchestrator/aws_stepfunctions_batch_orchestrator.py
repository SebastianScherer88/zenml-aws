"""Improved AWS Step Functions Orchestrator with Parallel Execution and Safety Checks"""

import hashlib
import json
import os
import time
from collections import deque
from datetime import datetime
from typing import (
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    Type,
    cast,
)

import boto3
from boto3 import Session
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

from zenml_aws.aws_batch_job_definition import (
    AWSBatchJobDefinition,
    check_existing_batch_job_definition,
    register_new_batch_job_definition,
    sanitize_name,
)
from zenml_aws.constants import AWS_BATCH_STEP_OPERATOR_FLAVOR

# Custom imports
from zenml_aws.orchestrator.aws_stepfunctions_batch_orchestrator_flavor import (
    AWSStepFunctionsOrchestratorConfig,
    AWSStepFunctionsOrchestratorSettings,
)
from zenml_aws.step_operator.aws_batch_step_operator_flavor import (
    AWSBatchStepOperatorSettings,
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

    def submit_pipeline(
        self,
        snapshot: PipelineSnapshotResponse,
        stack: Stack,
        base_environment: Dict[str, str],
        step_environments: Dict[str, Dict[str, str]],
        placeholder_run: Optional["PipelineRunResponse"] = None,
    ) -> Optional[SubmissionResult]:
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

        boto_session = self._get_aws_session()
        batch_client = boto_session.client("batch")

        step_name_to_unique_job_definition_name: dict[str, str] = {}

        for step_name, step in snapshot.step_configurations.items():
            step_environment = {
                **snapshot.pipeline_configuration.environment,
                **step_environments[step_name],
                ENV_ZENML_STEP_FUNCTIONS_RUN_ID: str(snapshot.id),
            }

            step_aws_batch_job_definition = AWSBatchJobDefinition.from_orchestrator(
                orchestrator=self,
                step=step,
                snapshot=snapshot,
                environment=step_environment,
                get_image_fn=self.get_image,
            )
            unique_batch_job_definition_name = (
                step_aws_batch_job_definition.generate_name(
                    snapshot.pipeline.name, step_name
                )
            )
            step_aws_batch_job_definition = AWSBatchJobDefinition(
                jobDefinitionName=unique_batch_job_definition_name,
                **step_aws_batch_job_definition.model_dump(exclude="jobDefinitionName"),
            )
            step_name_to_unique_job_definition_name[step_name] = (
                unique_batch_job_definition_name
            )

            existing_job_definition = check_existing_batch_job_definition(
                batch_client, unique_batch_job_definition_name
            )
            if not existing_job_definition:
                logger.info(
                    f"AWS Batch job definition {unique_batch_job_definition_name} doesnt exist yet. Registering..."
                )
                register_new_batch_job_definition(
                    batch_client,
                    step_aws_batch_job_definition,
                )

        # assemble and run as stepfunctions state machine
        state_machine_definition = self.create_state_machine_definition(
            snapshot, step_name_to_unique_job_definition_name
        )

        state_machine_definition_json = json.dumps(
            state_machine_definition, sort_keys=True
        ).encode()
        state_machine_definition_hash = hashlib.sha256(
            state_machine_definition_json
        ).hexdigest()

        # Create and execute state machine using helper functions
        stepfunction_client = boto_session.client("stepfunctions")
        state_machine_arn = self.create_state_machine_from_definition(
            stepfunction_client=stepfunction_client,
            name=f"{sanitize_name(snapshot.pipeline.name,30)}-{state_machine_definition_hash}",
            definition=state_machine_definition,
        )

        execution_arn = self.start_state_machine_execution(
            stepfunction_client=stepfunction_client,
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

    @staticmethod
    def map_tags(tags: dict[str, str]) -> list[dict[Literal["key"] | Literal["value"]]]:
        """Utility to map the {key:value} tags to the
        [{"key":key,"value":value},] convention used in the AWS Stepfunctions
        definition spec.

        Args:
            tags: The stepfunction definition's tags

        Returns:
            The mapped tag variable specification
        """

        return [{"key": k, "value": v} for k, v in tags.items()]

    def create_state_machine_from_definition(
        self,
        stepfunction_client: boto3.client,
        name: str,
        definition: Dict[str, Any],
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
        response = stepfunction_client.create_state_machine(
            name=name,
            definition=json.dumps(definition),
            roleArn=self.config.stepfunctions_execution_role,
            type="STANDARD",
            tags=self.map_tags(self.config.tags),
        )
        try:
            state_machine_arn = response["stateMachineArn"]
            logger.info(
                f"Created AWS Stepfunctions state machine. ARN: "
                f"{state_machine_arn} @{datetime.now()}"
            )
        except KeyError as e:
            logger.info(
                "Failed to create AWS Stepfunctions state machine @" f"{datetime.now()}"
            )
            raise e
        return state_machine_arn

    def start_state_machine_execution(
        self,
        stepfunction_client: boto3.client,
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
        response = stepfunction_client.start_execution(
            stateMachineArn=state_machine_arn,
            name=execution_name,
        )
        try:
            execution_arn = response["executionArn"]
            logger.info(
                f"Started execution of AWS Stepfunctions state machine "
                f"{state_machine_arn}. Execution ARN: {execution_arn} @"
                f"{datetime.now()}"
            )
            return execution_arn
        except KeyError as e:
            logger.info(
                f"Failed to execute AWS Stepfunctions state machine "
                f"{state_machine_arn} @{datetime.now()}"
            )
            raise e

    def create_state_machine_definition(
        self,
        snapshot: PipelineSnapshotResponse,
        step_name_to_unique_job_definition_name: dict[str, str],
    ) -> Dict[str, Any]:
        """
        Creates an AWS Step Functions state machine for the ZenML pipeline.

        - Uses a **static job definition and job queue**.
        - Supports **parallel execution** of pipeline steps.
        """

        # 🔹 Build DAG levels for parallel execution
        dag_levels = self.build_dag_levels(snapshot)

        # 🔹 Define the Step Functions states
        states = {"Start": {"Type": "Pass", "Next": "Level_0"}}

        # Add each level with parallel branches
        for level_num, level in enumerate(dag_levels):
            state_name = f"Level_{level_num}"
            states[state_name] = {
                "Type": "Parallel",
                "Branches": [],
                "Next": f"Level_{level_num + 1}"
                if level_num < len(dag_levels) - 1
                else "Success",
            }

            for step_name in level:
                # use step settings if AWSBatchStepOperator is configured for
                # the step, otherwise fall back to orchestrator defaults
                step_settings: AWSBatchStepOperatorSettings | None = (
                    snapshot.step_configurations[
                        step_name
                    ].config.settings.get(
                        f"step_operator.{AWS_BATCH_STEP_OPERATOR_FLAVOR}", None
                    )
                )
                try:
                    step_job_queue = step_settings.job_queue_name
                except AttributeError:
                    step_job_queue = self.config.job_queue_name

                states[state_name]["Branches"].append(
                    {
                        "StartAt": step_name,
                        "States": {
                            step_name: {
                                "Type": "Task",
                                "Resource": "arn:aws:states:::batch:submitJob.sync",
                                "Parameters": {
                                    "JobDefinition": step_name_to_unique_job_definition_name[
                                        step_name
                                    ],
                                    "JobQueue": step_job_queue,
                                    "JobName": step_name,
                                },
                                "End": True,
                            }
                        },
                    }
                )
            # states[f"Level_{level_num}"] = {
            #     "Type": "Parallel",
            #     "Branches": [
            #         {
            #             "StartAt": step,
            #             "States": {
            #                 step: {
            #                     "Type": "Task",
            #                     "Resource": "arn:aws:states:::batch:submitJob.sync",
            #                     "Parameters": {
            #                         "JobDefinition": step_name_to_unique_job_definition_name[
            #                             step
            #                         ],
            #                         "JobQueue": cast(
            #                             AWSBatchStepOperatorSettings,
            #                             self.get_settings(step),
            #                         ).job_queue_name,
            #                         "JobName": step,
            #                     },
            #                     "End": True,
            #                 }
            #             },
            #         }
            #         for step in level
            #     ],
            #     "Next": f"Level_{level_num + 1}"
            #     if level_num < len(dag_levels) - 1
            #     else "Success",
            # }

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
