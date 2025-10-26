import click
from zenml import pipeline, step
from zenml.config import DockerSettings, ResourceSettings

from zenml_aws.step_operator.aws_batch_step_operator_flavor import (
    AWSBatchStepOperatorSettings,
)

docker_settings = DockerSettings(
    parent_image="743582000746.dkr.ecr.eu-west-1.amazonaws.com/zenml:latest",
    skip_build=True,
)


@step(name="greet", step_operator=True)
def test_greet(name: str) -> str:
    """A simple step that returns a greeting message."""
    return f"Hello {name}!"


@step(name="report", step_operator=True)
def test_report(message: str) -> str:
    """A simple step that reports on a greeting."""
    return f"The message was '{message}'!"


@pipeline(settings={"docker": docker_settings})
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

    step_settings = {
        "resources": ResourceSettings(
            cpu_count=cpu, memory=f"{memory}MiB"
        ).model_dump(),
        "step_operator.aws_batch": AWSBatchStepOperatorSettings(
            job_queue_name=job_queue,
            backend=backend,
            environment={
                "ZENML_STORE_USERNAME": "zenml",
                "ZENML_STORE_PASSWORD": "password",
            },
        ).model_dump(),
    }

    test_pipeline.with_options(settings=step_settings, enable_cache=False)("Sebastian")


if __name__ == "__main__":
    main()
