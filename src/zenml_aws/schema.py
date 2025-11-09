from typing import Literal

from pydantic import BaseModel, ConfigDict


def prettify_alias(field_name: str) -> str:
    # split on underscores, capitalize each word, join with spaces
    return " ".join(word.capitalize() for word in field_name.split("_"))


# class AWSStepFunctionsOrchestratorStepMetadata(BaseModel):
#     step_name: str
#     step_job_definition_arn: str = "arn:aws:batch:eu-west-1:743582000746:job-definition/test_pipeline-report-3107f9b6e827dfea188b908889c4a6a798b8be4dc04a911f0e7bd8c17ddf3bd6:1"
#     step_job_definition_url: str = "https://eu-west-1.console.aws.amazon.com/batch/home?region=eu-west-1#job-definition/fargate/detail/arn:aws:batch:eu-west-1:743582000746:job-definition/test_pipeline-greet-1bad867ae8e979033581cc96d846613cf3c5701098062ff73b994c08812b5670:1"
#     step_job_arn: str = (
#         "arn:aws:batch:eu-west-1:743582000746:job/2a5c5eb6-ac54-48c0-bbe5-d0c0b1f025ce"
#     )
#     step_job_id: str = "2a5c5eb6-ac54-48c0-bbe5-d0c0b1f025ce"
#     step_job_url: str = "https://eu-west-1.console.aws.amazon.com/batch/home?region=eu-west-1#jobs/fargate/detail/2a5c5eb6-ac54-48c0-bbe5-d0c0b1f025ce"  # or 'ec2' instead of 'fargate'
#     step_job_logs_url: str = "https://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1#logsV2:log-groups/log-group/%2Faws%2Fbatch%2Fjob/log-events/test_pipeline-report-3107f9b6e827dfea188b908889c4a6a798b8be4dc04a911f0e7bd8c17ddf3bd6%2Fdefault%2Fb8c540a286e0478297a81bfa923f4c65"

#     model_config = ConfigDict(
#         serialize_by_alias=True, alias_generator=prettify_alias, validate_by_alias=False
#     )


# class AWSStepFunctionsOrchestratorPipelineMetadata(BaseModel):
#     state_machine_name: str = (
#         "test_pipeline-218cb8b3cd6d9f3882f37818295256a751022d22ca579e41791a0f801185dad0"
#     )
#     state_machine_arn: str = "arn:aws:states:eu-west-1:743582000746:stateMachine:test_pipeline-218cb8b3cd6d9f3882f37818295256a751022d22ca579e41791a0f801185dad0"
#     state_machine_url: str = "https://eu-west-1.console.aws.amazon.com/states/home?region=eu-west-1#/statemachines/view/arn%3Aaws%3Astates%3Aeu-west-1%3A743582000746%3AstateMachine%3Atest_pipeline-218cb8b3cd6d9f3882f37818295256a751022d22ca579e41791a0f801185dad0?type=standard"
#     state_machine_execution_arn: str = "arn:aws:states:eu-west-1:743582000746:execution:test_pipeline-218cb8b3cd6d9f3882f37818295256a751022d22ca579e41791a0f801185dad0:zenml-test_pipeline-1762619522"
#     state_machine_execution_url: str = "https://eu-west-1.console.aws.amazon.com/states/home?region=eu-west-1#/v2/executions/details/arn:aws:states:eu-west-1:743582000746:execution:test_pipeline-218cb8b3cd6d9f3882f37818295256a751022d22ca579e41791a0f801185dad0:zenml-test_pipeline-1762619522"

#     model_config = ConfigDict(
#         serialize_by_alias=True, alias_generator=prettify_alias, validate_by_alias=False
#     )


class AWSBatchStepStepMetadata(BaseModel):
    job_definition_arn: str = "arn:aws:batch:{region}:{account_id}:job-definition/{job_definition_name}:{revision_number}"
    job_definition_name: str
    job_definition_url: str = "https://{region}.console.aws.amazon.com/batch/home?region={region}#job-definition/{backend:fargate/ec2}/detail/{job_definition_arn}"
    job_arn: str = "arn:aws:batch:{region}:{account_id}:job/{job_id}"
    job_id: str = "2a5c5eb6-ac54-48c0-bbe5-d0c0b1f025ce"
    job_url: str = "https://{region}.console.aws.amazon.com/batch/home?region={region}#jobs/{backend:fargate/ec2}/detail/{job_id}"  # or 'ec2' instead of 'fargate'. this site includes a Logging tab displaying step logs
    # step_job_logs_url: str = "https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#logsV2:log-groups/log-group/%2Faws%2Fbatch%2Fjob/log-events/test_pipeline-report-3107f9b6e827dfea188b908889c4a6a798b8be4dc04a911f0e7bd8c17ddf3bd6%2Fdefault%2Fb8c540a286e0478297a81bfa923f4c65"

    model_config = ConfigDict(
        serialize_by_alias=True, alias_generator=prettify_alias, validate_by_alias=False
    )

    @classmethod
    def from_arns(
        cls,
        job_definition_arn: str,
        job_backend: Literal["fargate", "ec2"],
        job_arn: str = "",
    ) -> "AWSBatchStepStepMetadata":
        _, _, _, region, account_id, _, job_definition_name, revision = (
            job_definition_arn.split(":")
        )
        job_definition_url = f"https://{region}.console.aws.amazon.com/batch/home?region={region}#job-definition/{job_backend}/detail/{job_definition_arn}"

        if job_arn:
            _, _, _, region, account_id, _, job_id = job_arn.split(":")
            job_url = f"https://{region}.console.aws.amazon.com/batch/home?region={region}#jobs/{job_backend}/detail/{job_id}"
        else:
            job_url = ""

        return cls(
            job_definition_arn=job_definition_arn,
            job_definition_name=job_definition_name,
            job_definition_url=job_definition_url,
            job_arn=job_arn,
            job_id=job_id,
            job_url=job_url,
        )


class AWSStepFunctionPipelineMetadata(BaseModel):
    state_machine_arn: str = (
        "arn:aws:states:{region}:{account_id}:stateMachine:{state_machine_name}"
    )
    state_machine_name: str
    state_machine_url: str = "https://{region}.console.aws.amazon.com/states/home?region={region}#/statemachines/view/arn%3Aaws%3Astates%3A{region}%3A{account_id}%3AstateMachine%3A{state_machine_name}?type=standard"
    state_machine_execution_arn: str = "arn:aws:states:{region}:{account_id}:execution:{state_machine_name}:{state_machine_execution_name}"
    state_machine_execution_name: str
    state_machine_execution_url: str = "https://{region}.console.aws.amazon.com/states/home?region={region}#/v2/executions/details/{state_machine_execution_arn}"
    batch_meta_data: dict[str, AWSBatchStepStepMetadata]

    model_config = ConfigDict(
        serialize_by_alias=True, alias_generator=prettify_alias, validate_by_alias=False
    )

    @classmethod
    def from_arns(
        cls,
        state_machine_arn: str,
        state_machine_execution_arn: str,
        step_meta_data: dict[
            str, dict[Literal["job_definition_arn", "job_backend"], str]
        ],
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
            batch_meta_data={
                step_name: AWSBatchStepStepMetadata.from_arns(**step_meta_data)
                for step_name, step_meta_data in step_meta_data.items()
            },
        )

    # class AWSStepFunctionsOrchestratorRunMetadata(BaseModel):
    # orchestrator_logs_url: str
    # orchestrator_run_id: str
    # orchestrator_url: str
    # pipeline: AWSStepFunctionsOrchestratorPipelineMetadata
    # steps: dict[str, AWSStepFunctionsOrchestratorStepMetadata]
    # state_machine_arn: str
    # state_machine_execution_arn: str
    # step_job_definitions: dict[str, str]

    # @classmethod
    # def from_pipeline(
    #     cls,
    #     state_machine_arn: str,
    #     state_machine_execution_arn: str,
    #     step_job_definitions: dict[str, str],
    # ) -> "AWSStepFunctionsOrchestratorRunMetadata":
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
