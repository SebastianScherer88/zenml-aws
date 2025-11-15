"""Improved AWS Step Functions Orchestrator with Parallel Execution and Safety Checks"""

import hashlib
import json
import os
import time
import uuid
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
from zenml.client import Client
from zenml.config.base_settings import BaseSettings
from zenml.constants import (
    METADATA_ORCHESTRATOR_RUN_ID,
)
from zenml.enums import ExecutionStatus, StackComponentType
from zenml.logger import get_logger
from zenml.models import PipelineRunResponse, PipelineSnapshotResponse
from zenml.orchestrators import ContainerizedOrchestrator, SubmissionResult
from zenml.stack import Stack, StackValidator

from zenml_aws.aws_batch_job_definition import (
    AWSBatchJobDefinition,
    check_existing_batch_job_definition,
    register_new_batch_job_definition,
    sanitize_name,
)
from zenml_aws.constants import (
    AWS_BATCH_STEP_OPERATOR_FLAVOR,
    BATCH_JOB_TO_ZENML_EXECUTION_STATUS,
    ENV_ZENML_STEP_FUNCTIONS_RUN_ID,
    STATE_MACHINE_EXECUTION_TO_ZENML_EXECUTION_STATUS,
    AWSBatchJobStatus,
    AWSBatchTag,
    AWSStateMachineExecutionStatus,
)
from zenml_aws.flavors.aws_batch_step_operator_flavor import (
    AWSBatchStepOperatorSettings,
)

# Custom imports
from zenml_aws.flavors.aws_stepfunctions_batch_orchestrator_flavor import (
    AWSStepFunctionsOrchestratorConfig,
    AWSStepFunctionsOrchestratorSettings,
)
from zenml_aws.schema import (
    AWSBatchStepStepMetadata,
    AWSBatchStepStepMetadataServer,
    AWSStepFunctionPipelineMetadata,
    AWSStepFunctionPipelineMetadataServer,
)

logger = get_logger(__name__)

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

        Runs on the zenml step, i.e. as a zenml hook inside the AWS Batch job.

        Returns:
            The orchestrator run id.
        """

        logger.warning("Running `get_orchestrator_run_id` method.")

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

    def get_pipeline_run_metadata(self, run_id: uuid.UUID) -> Dict[str, dict[str, str]]:
        """Get general component-specific metadata for a pipeline run.

        Runs on the zenml step, i.e. as a zenml hook inside the AWS Batch job.

        Args:
            run_id: The ID of the pipeline run.

        Returns:
            A dictionary of metadata.
        """

        logger.warning("Running `get_pipeline_run_metadata` method.")

        orchestrator_run_id = os.environ[ENV_ZENML_STEP_FUNCTIONS_RUN_ID]

        return {METADATA_ORCHESTRATOR_RUN_ID: orchestrator_run_id}

    def fetch_status(
        self, run: "PipelineRunResponse", include_steps: bool = False
    ) -> Tuple[Optional[ExecutionStatus], Optional[Dict[str, ExecutionStatus]]]:
        """Uses orchestrator run id tag attached to the state machine to
        retrieve the state machine execution's state. Does not support step
        level status. Runs on the zenml server.

        Args:
            run (PipelineRunResponse): _description_
            include_steps (bool, optional): _description_. Defaults to False.

        Returns:
            Tuple[ ExecutionStatus, None ]: The mapped AWS Stepfunctions state
            machine execution status.
        """

        # Make sure that the stack exists and is accessible
        if run.stack is None:
            raise ValueError(
                "The stack that the run was executed on is not available " "anymore."
            )

        # ensure pipeline run has advanced enough in its life cycle to avoid
        # phantom pipeline runs on the server and ui
        if (METADATA_ORCHESTRATOR_RUN_ID not in run.run_metadata) or (
            run.orchestrator_run_id is None
        ):
            raise ValueError(
                "Can not find the orchestrator run ID, thus can not fetch "
                "the status."
            )

        # Make sure that the run belongs to this orchestrator
        assert self.id == run.stack.components[StackComponentType.ORCHESTRATOR][0].id

        boto_session = self._get_aws_session()
        stepfunction_client = boto_session.client("stepfunctions")

        logger.info(f"The fetch_status metadata: {run.run_metadata}")

        # check for the orchestrator run id in the metadata, or the run's
        # attribute
        if "AWS Stepfunctions" not in run.run_metadata:
            raise ValueError(
                "Can not find the AWS Stepfunctions metadata, thus can not fetch "
                "the status."
            )

        # Fetch the status of the pipeline, using the static meta data's
        # state machine execution arn as reference
        stepfunctions_metadata = AWSStepFunctionPipelineMetadataServer(
            **run.run_metadata["AWS Stepfunctions"]
        )
        state_machine_execution_status = stepfunction_client.describe_execution(
            executionArn=stepfunctions_metadata.state_machine_execution_arn
        )["status"]
        pipeline_status = STATE_MACHINE_EXECUTION_TO_ZENML_EXECUTION_STATUS[
            state_machine_execution_status
        ]

        # Fetch the status of the steps, using the static meta data's
        # batch job definition arns as reference; unfortunately, the AWS
        # tagging does not currently support AWS Batch for tag based querying
        if include_steps:
            if "AWS Batch" not in run.run_metadata:
                raise ValueError(
                    "Can not find the AWS Stepfunctions metadata, thus can not fetch "
                    "the status."
                )
            batch_metadata = {
                step_name: AWSBatchStepStepMetadataServer(**step_meta_data)
                for step_name, step_meta_data in run.run_metadata["AWS Batch"].items()
            }
            # get batch job statuses
            batch_client = boto_session.client("batch")

            jobs = {step: [] for step in batch_metadata}
            for step_name, step_meta in batch_metadata.items():
                paginator = batch_client.get_paginator("list_jobs")
                for page in paginator.paginate(jobQueue=step_meta.job_queue_name):
                    page_jobs = page["jobSummaryList"]
                    jobs[step_name].extend(page_jobs)

            job_statuses = {
                step: AWSBatchJobStatus.submitted for step in batch_metadata
            }
            for step_name, step_jobs in jobs.items():
                if not step_jobs:
                    continue
                step_job_descriptions = batch_client.describe_jobs(
                    jobs=[job["jobId"] for job in step_jobs]
                )["jobs"]
                for job_description in step_job_descriptions:
                    if (
                        job_description["jobDefinition"]
                        == batch_metadata[step_name].job_definition_arn
                    ):
                        job_statuses[step_name] = job_description["status"]
                        break

            step_statuses = {
                step_name: BATCH_JOB_TO_ZENML_EXECUTION_STATUS[job_statuses[step_name]]
                for step_name in job_statuses
            }
        else:
            step_statuses = None

        return pipeline_status, step_statuses

    def _stop_run(self, run: "PipelineRunResponse", graceful: bool = False) -> None:
        stepfunctions_metadata = AWSStepFunctionPipelineMetadataServer(
            **run.run_metadata["AWS Stepfunctions"]
        )
        boto_session = self._get_aws_session()

        # get stepfunction state machine execution status
        stepfunction_client = boto_session.client("stepfunctions")
        _ = stepfunction_client.stop_execution(
            executionArn=stepfunctions_metadata.state_machine_execution_arn,
            cause="Cancelling execution via script",
        )

    def get_step_settings(
        self, step_name: str, snapshot: PipelineSnapshotResponse
    ) -> AWSBatchStepOperatorSettings | AWSStepFunctionsOrchestratorSettings:
        """Utility to retrieve the step's AWSBatchStepOperatorSettings, if
        the AWSBatchStepOperator component has been registered in the stack and
        used for this step. If not, returns the AWSStepfunctionOrchestrator
        settings of the pipeline.

        Args:
            snapshot (PipelineSnapshotResponse): _description_

        Returns:
            None | AWSBatchStepOperatorSettings: _description_
        """

        step = snapshot.step_configurations[step_name]

        step_settings: AWSBatchStepOperatorSettings | None = step.config.settings.get(
            f"step_operator.{AWS_BATCH_STEP_OPERATOR_FLAVOR}", None
        )

        if step_settings is None:
            step_settings: AWSStepFunctionsOrchestratorSettings = self.get_settings(
                snapshot
            )

        return step_settings

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

        step_name_to_job_definition: dict[str, AWSBatchJobDefinition] = {}
        step_name_to_unique_job_definition_name: dict[str, str] = {}
        step_name_to_arn_and_revision: dict[str, str] = {}

        orchestrator_run_id = uuid.uuid4()

        for step_name, step in snapshot.step_configurations.items():
            step_environment = {
                **snapshot.pipeline_configuration.environment,
                **step_environments[step_name],
                # each step's environment must include the a priori
                # orchestrator run id
                ENV_ZENML_STEP_FUNCTIONS_RUN_ID: str(orchestrator_run_id),
            }

            step_aws_batch_job_definition = AWSBatchJobDefinition.from_orchestrator(
                orchestrator=self,
                orchestrator_run_id=str(orchestrator_run_id),
                step=step,
                placeholder_run=placeholder_run,
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
            step_name_to_job_definition[step_name] = step_aws_batch_job_definition

            existing_job_definition = check_existing_batch_job_definition(
                batch_client, unique_batch_job_definition_name
            )
            if not existing_job_definition:
                logger.info(
                    f"AWS Batch job definition {unique_batch_job_definition_name} doesnt exist yet. Registering..."
                )
                step_name_to_arn_and_revision[step_name] = (
                    register_new_batch_job_definition(
                        batch_client,
                        step_aws_batch_job_definition,
                    )
                )

        # assemble and run as stepfunctions state machine
        state_machine_definition = self.create_state_machine_definition(
            snapshot,
            step_name_to_job_definition,
        )

        state_machine_definition_json = json.dumps(
            state_machine_definition, sort_keys=True
        ).encode()
        state_machine_definition_hash = hashlib.sha256(
            state_machine_definition_json
        ).hexdigest()[:20]

        # Create and execute state machine using helper functions
        stepfunction_client = boto_session.client("stepfunctions")
        state_machine_arn = self.create_state_machine_from_definition(
            placeholder_run=placeholder_run,
            stepfunction_client=stepfunction_client,
            name=f"{sanitize_name(snapshot.pipeline.name,30)}-{state_machine_definition_hash}",
            definition=state_machine_definition,
            orchestrator_run_id=str(orchestrator_run_id),
        )

        state_machine_execution_arn = self.start_state_machine_execution(
            stepfunction_client=stepfunction_client,
            state_machine_arn=state_machine_arn,
            pipeline_name=snapshot.pipeline_configuration.name,
        )

        if self.config.wait_for_completion:

            def wait_for_completion():
                while True:
                    now = datetime.now()
                    response = stepfunction_client.describe_execution(
                        executionArn=state_machine_execution_arn
                    )
                    state_machine_execution_status: AWSStateMachineExecutionStatus = (
                        response["status"]
                    )
                    pipeline_status = STATE_MACHINE_EXECUTION_TO_ZENML_EXECUTION_STATUS[
                        state_machine_execution_status
                    ]
                    logger.info(
                        f"Pipeline status: [{pipeline_status}].  State machine execution status {state_machine_execution_arn}: {state_machine_execution_status}] @{now}."
                    )

                    if (
                        state_machine_execution_status
                        == AWSStateMachineExecutionStatus.running
                    ):
                        time.sleep(self.config.poll_interval_seconds)
                    elif state_machine_execution_status in (
                        AWSStateMachineExecutionStatus.succeeded,
                        AWSStateMachineExecutionStatus.failed,
                        AWSStateMachineExecutionStatus.aborted,
                        AWSStateMachineExecutionStatus.timed_out,
                    ):
                        break

                if (
                    state_machine_execution_status
                    in self.config.delete_stepfunctions_resource_on
                ):
                    # clean up state machine
                    try:
                        stepfunction_client.delete_state_machine(
                            stateMachineArn=state_machine_arn
                        )
                        logger.info(
                            f"Successfully deleted state machine {state_machine_arn} @ {now}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to delete state machine {state_machine_arn}: {e}"
                        )

                if state_machine_execution_status in (
                    AWSStateMachineExecutionStatus.failed,
                    AWSStateMachineExecutionStatus.aborted,
                    AWSStateMachineExecutionStatus.timed_out,
                ):
                    raise RuntimeError(
                        f"Pipeline failed: [{pipeline_status}]. State machine execution {state_machine_execution_arn} of state machine {state_machine_arn} failed: [{state_machine_execution_status}]: {response} @{now}"
                    )
                elif (
                    state_machine_execution_status
                    == AWSStateMachineExecutionStatus.succeeded
                ):
                    logger.info(f"Pipeline succeeded: [{pipeline_status}] @{now}")
        else:
            wait_for_completion = None

        step_settings = self.get_step_settings(step_name, snapshot)

        step_meta_data = {
            step_name: {
                "job_backend": step_settings.backend.lower(),
                "job_definition_arn": step_name_to_arn_and_revision[step_name],
                "job_queue_name": step_settings.job_queue_name,
            }
            for step_name in step_name_to_job_definition
        }

        return SubmissionResult(
            wait_for_completion=wait_for_completion,
            metadata={
                "AWS Stepfunctions": AWSStepFunctionPipelineMetadata.from_arns(
                    state_machine_arn, state_machine_execution_arn
                ).model_dump(),
                "AWS Batch": {
                    step_name: AWSBatchStepStepMetadata.from_arns(
                        **step_meta_data[step_name]
                    ).model_dump()
                    for step_name in step_meta_data
                },
            },
        )

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

    def create_state_machine_definition(
        self,
        snapshot: PipelineSnapshotResponse,
        step_name_to_job_definition: dict[str, AWSBatchJobDefinition],
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
                step_settings = self.get_step_settings(step_name, snapshot)

                states[state_name]["Branches"].append(
                    {
                        "StartAt": step_name,
                        "States": {
                            step_name: {
                                "Type": "Task",
                                "Resource": "arn:aws:states:::batch:submitJob.sync",
                                "Parameters": {
                                    "JobDefinition": step_name_to_job_definition[
                                        step_name
                                    ].jobDefinitionName,
                                    "JobQueue": step_settings.job_queue_name,
                                    "JobName": step_name,
                                    # each batch job inherits tags from the definition
                                    "Tags": step_name_to_job_definition[step_name].tags,
                                },
                                "End": True,
                            }
                        },
                    }
                )

        # 🔹 Add Success state
        states.update({"Success": {"Type": "Succeed"}})

        # 🔹 Define the full state machine JSON
        definition = {
            "Comment": f"ZenML Pipeline: {snapshot.pipeline_configuration.name}",
            "StartAt": "Start",
            "States": states,
            "TimeoutSeconds": self.config.timeout_seconds,
        }

        return definition

    def create_state_machine_from_definition(
        self,
        placeholder_run: PipelineRunResponse,
        stepfunction_client: boto3.client,
        name: str,
        definition: Dict[str, Any],
        orchestrator_run_id: str,
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

        pipeline_settings: AWSStepFunctionsOrchestratorSettings = self.get_settings(
            placeholder_run.snapshot
        )
        client = Client()

        tags = [
            {"key": AWSBatchTag.stack_id, "value": str(client.active_stack.id)},
            {"key": AWSBatchTag.stack_name, "value": client.active_stack.name},
            {"key": AWSBatchTag.component_id, "value": str(self.id)},
            {"key": AWSBatchTag.component_name, "value": self.name},
            {"key": AWSBatchTag.pipeline_name, "value": placeholder_run.pipeline.name},
            {"key": AWSBatchTag.pipeline_run_id, "value": str(placeholder_run.id)},
            {"key": AWSBatchTag.pipeline_run_name, "value": placeholder_run.name},
            {"key": AWSBatchTag.orchestrator_run_id, "value": orchestrator_run_id},
        ]
        tags.extend(self.map_tags(pipeline_settings.tags))

        response = stepfunction_client.create_state_machine(
            name=name,
            definition=json.dumps(definition),
            roleArn=self.config.stepfunctions_execution_role,
            loggingConfiguration={
                "level": "ALL",
                "includeExecutionData": False,
                "destinations": [
                    {
                        "cloudWatchLogsLogGroup": {
                            "logGroupArn": self.config.stepfunctions_log_group_arn
                        }
                    }
                ],
            },
            type="STANDARD",
            tags=tags,
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

    @staticmethod
    def start_state_machine_execution(
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
