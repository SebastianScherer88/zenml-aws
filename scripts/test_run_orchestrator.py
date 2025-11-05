import click
from zenml import pipeline, step
from zenml.config import DockerSettings, ResourceSettings

from zenml_aws.orchestrator.aws_stepfunctions_batch_orchestrator_flavor import (
    AWSStepFunctionsOrchestratorSettings,
)
from zenml_aws.step_operator.aws_batch_step_operator_flavor import (
    AWSBatchStepOperatorSettings,
)

docker_settings = DockerSettings(
    parent_image="743582000746.dkr.ecr.eu-west-1.amazonaws.com/zenml:latest",
    skip_build=True,
)


@step(name="greet", step_operator=True, environment={"test-a": "A"})
def test_greet(name: str) -> str:
    """A simple step that returns a greeting message."""
    return f"Hello {name}!"


@step(name="report", step_operator=True, environment={"test-b": "B"})
def test_report(message: str) -> str:
    """A simple step that reports on a greeting."""
    return f"The message was '{message}'!"


@pipeline(settings={"docker": docker_settings}, environment={"test-c": "C"})
def test_pipeline(name: str):
    """A simple pipeline with just one step."""
    greeting = test_greet(name)
    report = test_report(greeting)

    return report


@click.command()
@click.option("--backend", type=click.Choice(["FARGATE", "EC2"]), default="EC2")
@click.option("--cpu", type=click.IntRange(1, 5), default=1)
@click.option("--memory", type=click.IntRange(100, 5000), default=1000)
@click.option(
    "--job-queue",
    type=click.Choice(["zenml-test-ec2-job-queue", "zenml-test-fargate-job-queue"]),
    default="zenml-test-ec2-job-queue",
)
def main(backend: str, cpu: str, memory: str, job_queue: str):
    click.echo(f"{backend}, {cpu}, {memory}, {job_queue}")

    pipeline_settings = settings = {
        "resources": ResourceSettings(
            cpu_count=cpu, memory=f"{memory}MiB"
        ).model_dump(),
        "orchestrator": AWSStepFunctionsOrchestratorSettings(tags={"test-a": "A"}),
    }
    pipeline_environment = {
        "ZENML_STORE_USERNAME": "zenml",
        "ZENML_STORE_PASSWORD": "password",
    }
    test_pipeline.configure(
        settings=pipeline_settings, environment=pipeline_environment
    )

    step_configurations = {
        "greet": {
            "settings": {
                "step_operator": AWSBatchStepOperatorSettings(
                    job_queue_name=job_queue,
                    backend=backend,
                ).model_dump(),
            },
        },
        "report": {
            "settings": {
                "resources": ResourceSettings(
                    cpu_count=2 * cpu, memory=f"{2*memory}MiB"
                ),
                "step_operator": AWSBatchStepOperatorSettings(
                    job_queue_name=job_queue,
                    backend=backend,
                ).model_dump(),
            },
            "environment": {"test-d": "D"},
        },
    }
    test_pipeline.with_options(
        settings=settings, step_configurations=step_configurations, enable_cache=False
    )("Sebastian")


if __name__ == "__main__":
    main()
