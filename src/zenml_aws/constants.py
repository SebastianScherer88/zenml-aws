from enum import StrEnum

from zenml.enums import ExecutionStatus

AWS_BATCH_STEP_OPERATOR_FLAVOR = "aws_batch"
AWS_STEP_FUNCTIONS_ORCHESTRATOR_FLAVOR = "aws_stepfunctions"
DEFAULT_STATE_MACHINE_TYPE = "STANDARD"

BATCH_DOCKER_IMAGE_KEY = "aws_batch_step_operator"

ENV_ZENML_STEP_FUNCTIONS_RUN_ID = "AWS_STEPFUNCTIONS_RUN_ID"
ENV_ZENML_STEP_FUNCTIONS_STEP_NAME = "AWS_STEPFUNCTIONS_STEP_NAME"
ENV_ZENML_STEP_FUNCTIONS_JOB_ID = "AWS_BATCH_JOB_ID"
ENV_ZENML_STEP_FUNCTIONS_REGION = "AWS_REGION"
_ENTRYPOINT_ENV_VARIABLE = "__ZENML_ENTRYPOINT"


class AWSBatchTag(StrEnum):
    stack_id: str = "ZENML_STACK_ID"
    stack_name: str = "ZENML_STACK_NAME"

    component_id: str = "ZENML_AWS_COMPONENT_ID"
    component_name: str = "ZENML_AWS_COMPONENT_NAME"

    orchestrator_run_id: str = ENV_ZENML_STEP_FUNCTIONS_RUN_ID

    pipeline_name: str = "ZENML_AWS_BATCH_PIPELINE_NAME"
    pipeline_run_id: str = "ZENML_AWS_BATCH_PIPELINE_RUN_ID"
    pipeline_run_name: str = "ZENML_AWS_BATCH_PIPELINE_RUN_NAME"

    step_name: str = "ZENML_AWS_BATCH_STEP_NAME"
    step_run_id: str = "ZENML_AWS_BATCH_STEP_RUN_ID"
    step_run_name: str = "ZENML_AWS_BATCH_STEP_RUN_NAME"

    @classmethod
    def list(cls) -> list[str]:
        return [field.value for field in cls]


class AWSBatchJobStatus(StrEnum):
    submitted: str = "SUBMITTED"
    pending: str = "PENDING"
    runnable: str = "RUNNABLE"
    starting: str = "STARTING"
    running: str = "RUNNING"
    succeeded: str = "SUCCEEDED"
    failed: str = "FAILED"


BATCH_JOB_TO_ZENML_EXECUTION_STATUS: dict[AWSBatchJobStatus, ExecutionStatus] = {
    AWSBatchJobStatus.submitted: ExecutionStatus.PROVISIONING,
    AWSBatchJobStatus.pending: ExecutionStatus.PROVISIONING,
    AWSBatchJobStatus.runnable: ExecutionStatus.PROVISIONING,
    AWSBatchJobStatus.starting: ExecutionStatus.INITIALIZING,
    AWSBatchJobStatus.running: ExecutionStatus.RUNNING,
    AWSBatchJobStatus.succeeded: ExecutionStatus.COMPLETED,
    AWSBatchJobStatus.failed: ExecutionStatus.FAILED,
}


class AWSStateMachineExecutionStatus(StrEnum):
    running: str = "RUNNING"
    succeeded: str = "SUCCEEDED"
    failed: str = "FAILED"
    timed_out: str = "TIMED_OUT"
    aborted: str = "ABORTED"


STATE_MACHINE_EXECUTION_TO_ZENML_EXECUTION_STATUS: dict[
    AWSStateMachineExecutionStatus, ExecutionStatus
] = {
    AWSStateMachineExecutionStatus.running: ExecutionStatus.RUNNING,
    AWSStateMachineExecutionStatus.succeeded: ExecutionStatus.COMPLETED,
    AWSStateMachineExecutionStatus.failed: ExecutionStatus.FAILED,
    AWSStateMachineExecutionStatus.timed_out: ExecutionStatus.STOPPED,
    AWSStateMachineExecutionStatus.aborted: ExecutionStatus.STOPPED,
}
