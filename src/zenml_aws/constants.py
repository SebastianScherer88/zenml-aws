from enum import StrEnum

AWS_BATCH_STEP_OPERATOR_FLAVOR = "aws_batch"
AWS_STEP_FUNCTIONS_ORCHESTRATOR_FLAVOR = "aws_stepfunctions"
DEFAULT_STATE_MACHINE_TYPE = "STANDARD"

BATCH_DOCKER_IMAGE_KEY = "aws_batch_step_operator"
_ENTRYPOINT_ENV_VARIABLE = "__ZENML_ENTRYPOINT"

AWS_BATCH_JOB_DEFAULT_NAME = "zenml-aws-batch-job-definition"


class AWSBatchTag(StrEnum):
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
    runnable: str = "RUNNABLE"
    starting: str = "STARTING"
    running: str = "RUNNING"
    succeeded: str = "SUCCEEDED"
    failed: str = "FAILED"
