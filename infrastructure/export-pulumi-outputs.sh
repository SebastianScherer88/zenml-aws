#!/usr/bin/env bash
# Source this before running the zenml CLI commands in the README:
#   source export-pulumi-outputs.sh
#
# Run from the repo root (assumes the pulumi project lives in ./infrastructure).

export ECR_REPOSITORY_URL=$(pulumi stack output ecr_repository_url)
export ECR_REGISTRY_URI="${ECR_REPOSITORY_URL%%/*}"
export ARTIFACT_STORE_S3_BUCKET=$(pulumi stack output artifact_store_s3_bucket)
export SERVER_EXTERNAL_URL=$(pulumi stack output server_external_url)
export BATCH_EXECUTION_ROLE_ARN=$(pulumi stack output batch_execution_role_arn)
export BATCH_JOB_ROLE_ARN=$(pulumi stack output batch_job_role_arn)
export BATCH_DEFAULT_JOB_QUEUE_NAME=$(pulumi stack output batch_default_job_queue_name)
export SFN_EXECUTION_ROLE_ARN=$(pulumi stack output sfn_role_arn)

# Not currently exposed as pulumi outputs - update here if they change,
# or add them as real outputs later.
export LOG_GROUP_NAME=$(pulumi stack output log_group_name)
export LOG_GROUP_ARN=$(pulumi stack output log_group_arn)