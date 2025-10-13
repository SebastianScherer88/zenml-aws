# Overview

This repository implements a collection of AWS integrations for the zenml
platform.

Contains:

- a personal pseudo fork of [the AWS Batch step operator proposed in this PR in the official zenml repository](https://github.com/zenml-io/zenml/pull/3954) (based on [this original plugin implementation](https://github.com/zenml-io/zenml-plugins/blob/41f9f9bc91e4fa25cf90068bc2db8a8a721b5986/step_operator_batch/step_operator/aws_batch_step_operator.py#L47))
- a placeholder for a future AWS Batch EC2 (and thus GPU) compatible extension of [the great ML Ops Club's step functions orchestrator implementation](https://github.com/mlops-club/zenml-aws-stepfunctions-orchestrator/blob/a9179570d03d44b674031699ac9bbe943bc25fa8/sfn-orchestrator/src/sfn_orchestrator/sfn_orchestrator.py#L175)