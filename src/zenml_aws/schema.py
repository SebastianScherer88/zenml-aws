from typing import Literal

from pydantic import BaseModel, ConfigDict


def prettify_alias(field_name: str) -> str:
    # split on underscores, capitalize each word, join with spaces
    return " ".join(word.capitalize() for word in field_name.split("_"))


class AWSBatchStepStepMetadata(BaseModel):
    job_definition_arn: str
    job_definition_name: str
    job_definition_revision: int
    job_definition_url: str
    job_queue_name: str
    job_backend: str
    job_arn: str = ""  # "arn:aws:batch:{region}:{account_id}:job/{job_id}"
    job_id: str = ""
    job_url: str = ""  # "https://{region}.console.aws.amazon.com/batch/home?region={region}#jobs/{backend:fargate/ec2}/detail/{job_id}"  # or 'ec2' instead of 'fargate'. this site includes a Logging tab displaying step logs
    # step_job_logs_url: str = "https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#logsV2:log-groups/log-group/%2Faws%2Fbatch%2Fjob/log-events/test_pipeline-report-3107f9b6e827dfea188b908889c4a6a798b8be4dc04a911f0e7bd8c17ddf3bd6%2Fdefault%2Fb8c540a286e0478297a81bfa923f4c65"

    model_config = ConfigDict(
        serialize_by_alias=True, alias_generator=prettify_alias, validate_by_alias=False
    )

    @classmethod
    def from_arns(
        cls,
        job_definition_arn: str,
        job_backend: Literal["fargate", "ec2"],
        job_queue_name: str,
        job_arn: str = "",
    ) -> "AWSBatchStepStepMetadata":
        _, _, _, region, account_id, prefixed_job_definition_name, revision = (
            job_definition_arn.split(":")
        )
        _, job_definition_name = prefixed_job_definition_name.split("/")
        job_definition_url = f"https://{region}.console.aws.amazon.com/batch/home?region={region}#job-definition/{job_backend}/detail/{job_definition_arn}"

        if job_arn:
            _, _, _, region, account_id, prefixed_job_id = job_arn.split(":")
            _, job_id = prefixed_job_id.split("/")
            job_url = f"https://{region}.console.aws.amazon.com/batch/home?region={region}#jobs/{job_backend}/detail/{job_id}"
        else:
            job_url = job_id = ""

        return cls(
            job_definition_arn=job_definition_arn,
            job_definition_name=job_definition_name,
            job_definition_revision=int(revision),
            job_definition_url=job_definition_url,
            job_queue_name=job_queue_name,
            job_backend=job_backend,
            job_arn=job_arn,
            job_id=job_id,
            job_url=job_url,
        )


class AWSBatchStepStepMetadataServer(AWSBatchStepStepMetadata):
    model_config = ConfigDict(
        serialize_by_alias=True, alias_generator=prettify_alias, validate_by_alias=True
    )


class AWSStepFunctionPipelineMetadata(BaseModel):
    state_machine_arn: str
    state_machine_name: str
    state_machine_url: str
    state_machine_execution_arn: str
    state_machine_execution_name: str
    state_machine_execution_url: str

    model_config = ConfigDict(
        serialize_by_alias=True, alias_generator=prettify_alias, validate_by_alias=False
    )

    @classmethod
    def from_arns(
        cls,
        state_machine_arn: str,
        state_machine_execution_arn: str,
    ) -> "AWSStepFunctionPipelineMetadata":
        _, _, _, region, account_id, _, state_machine_name = state_machine_arn.split(
            ":"
        )
        state_machine_url = f"https://{region}.console.aws.amazon.com/states/home?region={region}#/statemachines/view/arn%3Aaws%3Astates%3A{region}%3A{account_id}%3AstateMachine%3A{state_machine_name}?type=standard"

        (
            _,
            _,
            _,
            region,
            account_id,
            _,
            state_machine_name,
            state_machine_execution_name,
        ) = state_machine_execution_arn.split(":")
        state_machine_execution_url = f"https://{region}.console.aws.amazon.com/states/home?region={region}#/v2/executions/details/{state_machine_execution_arn}"

        return cls(
            state_machine_arn=state_machine_arn,
            state_machine_name=state_machine_name,
            state_machine_url=state_machine_url,
            state_machine_execution_arn=state_machine_execution_arn,
            state_machine_execution_name=state_machine_execution_name,
            state_machine_execution_url=state_machine_execution_url,
        )


class AWSStepFunctionPipelineMetadataServer(AWSStepFunctionPipelineMetadata):
    model_config = ConfigDict(
        serialize_by_alias=True, alias_generator=prettify_alias, validate_by_alias=True
    )


# class AWSStepfunctionOrchestratorMetaData(BaseModel):
#     aws_stepfunctions: AWSStepFunctionPipelineMetadata
#     aws_batch: dict[str, AWSBatchStepStepMetadata]

#     model_config = ConfigDict(
#         serialize_by_alias=True, alias_generator=prettify_alias, validate_by_alias=False,extra="ignore"
#     )

#     @classmethod
#     def from_arns(
#         cls,
#         state_machine_arn: str,
#         state_machine_execution_arn: str,
#         step_meta_data: dict[
#             str, dict[Literal["job_definition_arn", "job_backend"], str]
#         ],
#     ) -> "AWSStepfunctionOrchestratorMetaData":
#         return cls(
#             aws_stepfunctions=AWSStepFunctionPipelineMetadata.from_arns(
#                 state_machine_arn, state_machine_execution_arn
#             ),
#             aws_batch={
#                 step_name: AWSBatchStepStepMetadata.from_arns(
#                     **step_meta_data[step_name]
#                 )
#                 for step_name in step_meta_data
#             },
#         )

#     region = state_machine_execution_arn.split(":")[3]

#     return cls(
#         orchestrator_run_id=state_machine_execution_arn,
#         orchestrator_url=(
#             f"https://{region}.console.aws.amazon.com/states/home"
#             f"?region={region}#/executions/details/{state_machine_execution_arn}"
#         ),
#         orchestrator_logs_url=(
#             f"https://{region}.console.aws.amazon.com/cloudwatch/home"
#             f"?region={region}#logsV2:log-groups/log-group/$252Faws$252F"
#             "batch$252Fjob"
#         ),
#         state_machine_arn=state_machine_arn,
#         state_machine_execution_arn=state_machine_execution_arn,
#         step_job_definitions=step_job_definitions,
#         pipeline=AWSStepFunctionsOrchestratorPipelineMetadata(),
#         steps={
#             step_name: AWSStepFunctionsOrchestratorStepMetadata(step_name=step_name)
#             for step_name in step_job_definitions.keys()
#         },
#     )
