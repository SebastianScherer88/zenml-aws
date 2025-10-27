# Overview

This repository implements a collection of AWS integrations for the zenml
platform.

Contains:

- a customized version of [the AWS Batch step operator proposed in this PR in the official zenml repository](https://github.com/zenml-io/zenml/pull/3954) (based on [this original plugin implementation](https://github.com/zenml-io/zenml-plugins/blob/41f9f9bc91e4fa25cf90068bc2db8a8a721b5986/step_operator_batch/step_operator/aws_batch_step_operator.py#L47))
- a placeholder for a future AWS Batch EC2 (and thus GPU) compatible extension of [the great ML Ops Club's step functions orchestrator implementation](https://github.com/mlops-club/zenml-aws-stepfunctions-orchestrator/blob/a9179570d03d44b674031699ac9bbe943bc25fa8/sfn-orchestrator/src/sfn_orchestrator/sfn_orchestrator.py#L175)

# Setup


## Python dependencies

Create a virtual environment with python 3.12:

```bash
uv venv --python=3.12
```

and activate it. Then install all python dependencies:

```bash
uv sync
```

## Infrastructure

Provision the pulumi stack in the AWS cloud, including a publically available
RDS sql server for the remote zenml store. This needs to be run by an AWS
identity that has the required pulumi provisioning permissions. My local setup
achieves this by a designated `PulumiDevRole` holding all the required policies
and/or permissions, and that can be assumed by a 
`pululmi-bootstrap` User. I've configured this setup using the below 
configuration files `~/.aws/config` and `~/.aws/credentials`: 

```conf
[profile pulumi] # ~/.aws/config
role_arn = arn:aws:iam::743582000746:role/PulumiDevRole
source_profile = pulumi-bootstrap
region = eu-west-1
```

```conf
[pulumi-bootstrap] # ~/.aws/credentials
aws_access_key_id = <AWS_ACCESS_KEY_ID>
aws_secret_access_key = <AWS_SECRET_ACCESS_KEY>
```


```bash
export AWS_PROFILE=pulumi # 'set AWS_PROFILE=pulumi' on windows
cd infrastructure
pulumi up -y
```

## Docker image

To authenticate your local docker client with the remote ECR stack you just
provisioned, run:

```bash
aws ecr get-login-password --region eu-west-1 | docker login --username AWS --password-stdin 743582000746.dkr.ecr.eu-west-1.amazonaws.com
```

To build a zenml docker image that can run remotely, run:

```bash
docker build -f infrastructure\Dockerfile . -t 743582000746.dkr.ecr.eu-west-1.amazonaws.com/zenml:latest
docker push 743582000746.dkr.ecr.eu-west-1.amazonaws.com/zenml:latest
```

## Zenml development stack

Create a `zenml` stack using this library's integrations by running the following
commands.

Login with the remote SQL zenml store directly:

```bash
zenml login mysql://zenml:password@zenml-metdata-store2fbf804.c1cyu4q20nag.eu-west-1.rds.amazonaws.com:3306/zenml
```

Register the git repository as a local zenml repository:

```bash
zenml init
```

Register the AWs batch step operator flavour:

```bash
zenml step-operator flavor register src.zenml_aws.step_operator.aws_batch_step_operator_flavor.AWSBatchStepOperatorFlavor
```

Register the step operator component:

```bash
zenml step-operator register aws-batch -f aws_batch --execution_role=arn:aws:iam::743582000746:role/batch-execution-role --job_role=arn:aws:iam::743582000746:role/batch-job-role --default_job_queue_name=zenml-test-ec2-job-queue
```

Register a remote type ECR contaier registry component:

```bash
zenml container-registry register aws-ecr -f aws  --uri=743582000746.dkr.ecr.eu-west-1.amazonaws.com
```

Register a remote type S3 artifact store component:

```bash
zenml artifact-store register aws-s3 -f s3 --path=s3://zenml-artifact-store-e425ed8
```

Register a `zenml-aws-test` zenml stack with the components:

```bash
zenml stack register aws-test -a aws-s3 -o default -s aws-batch -c aws-ecr
```

Set the new stack as the active one:

```bash
zenml stack set aws-test
```

A useful command to update the aws-test zenml stack is:

```bash
zenml stack set default
zenml stack delete aws-test -y
zenml step-operator delete aws-batch
zenml step-operator flavor delete aws_batch
zenml step-operator flavor register zenml_aws.step_operator.aws_batch_step_operator_flavor.AWSBatchStepOperatorFlavor
zenml step-operator register aws-batch -f aws_batch --execution_role=arn:aws:iam::743582000746:role/batch-execution-role --job_role=arn:aws:iam::743582000746:role/batch-job-role --default_job_queue_name=zenml-test-job-queue
zenml stack register aws-test -a aws-s3 -o default -s aws-batch -c aws-ecr
zenml stack set aws-test
```

## Zenml dashboard (optional)

It can be useful to track the state of stacks and pipelines via the zenml
dashboard running independently against the same SQL zenml store:

```bash
cd infrastructure
docker compose up
```

The username is "default", and the password is empty.

# Tests

For local only unit and integration tests, simply run the pytest test suites
in the respective directories:

```bash
pytest tests/unit -vv # unit tests
pytest tests/integration -vv # integration tests
```

For end-to-end tests running on the provisioned AWS infrastructure, run the 
test scripts in the `scripts` directory:

```bash
python scripts/test_run_step_operator.py --backend EC2 --job-queue zenml-test-ec2-job-queue --memory 1000
python scripts/test_run_step_operator.py --backend FARGATE --job-queue zenml-test-fargate-job-queue --memory 1024
```