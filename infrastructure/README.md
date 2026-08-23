# zenml-ecs — ZenML OSS on AWS ECS (Pulumi)

Pulumi (Python) program that stands up a self-hosted [ZenML](https://zenml.io) OSS
deployment on AWS. It's modeled on the networking/config conventions of
[metaflow-aws](https://github.com/SebastianScherer88/metaflow-aws/tree/main/infrastructure)
(VPC layout, `pulumi config set` shape, ALB-restricted-to-dev-IP pattern) and
extends [zenml-aws](https://github.com/SebastianScherer88/zenml-aws/tree/main/infrastructure)
(RDS metadata store, ECR, S3 artifact store, Batch compute) with an actual ECS
Fargate deployment of the ZenML **server** — the piece the reference `zenml-aws`
stack didn't run at all.

## What it deploys

| Component | Resource |
|---|---|
| Networking | VPC, 2 AZs, public + private subnets, 1 NAT gateway |
| ZenML server (API + dashboard) | ECS Fargate service, behind an ALB |
| Metadata store | RDS MySQL, **private**, SG allows only the ECS task |
| Artifact store | S3 bucket (versioned, account-scoped bucket policy) |
| Container registry | ECR repo for your pipeline/step images |
| Batch compute | AWS Batch (Fargate + EC2 compute envs/queues) for the [Batch step operator](https://docs.zenml.io/stacks/step-operators/aws-batch) |
| Secrets | Secrets Manager: DB credentials + precomputed `ZENML_STORE_URL`, server JWT signing key, generated admin password |

## The key difference from `zenml-aws`: no direct RDS login

`zenml-aws`'s RDS instance is `publicly_accessible=True` with a hardcoded
password — a developer (or `zenml` CLI, if pointed at a raw SQL connection
string) could connect straight to the database. This stack removes that path
entirely:

- `rds_sg` only allows inbound `3306` from `ecs_services_sg` (the ZenML server
  task's own security group). There is no rule granting the developer CIDRs,
  or anything else, direct DB access.
- `publicly_accessible=False` on the RDS instance.
- The DB credentials / connection string only ever exist as a Secrets Manager
  secret injected into the ECS task definition (`ZENML_STORE_URL`) — nothing
  a developer types in or configures locally.

Instead, exactly as the [official ZenML deployment docs](https://docs.zenml.io/deploying-zenml)
describe, developers authenticate **against the server**:

```bash
zenml login <server_url>          # pulumi stack output server_url
# username: the "default-user-name" from config (default: "default")
# password: pulumi stack output server_default_user_password_secret_arn
#           -> aws secretsmanager get-secret-value --secret-id <arn> \
#                --query SecretString --output text | jq -r .default_user_password
```

The `server_url` is the ALB's DNS name, which is itself only reachable from
the CIDRs you allow — see below.

## ALB surfacing: restricted to developer IP(s)

Same approach as `metaflow-aws`'s ALB-based dev mode: one ALB in the public
subnets, whose security group only allows ingress on the server port from
`zenml-ecs:security.allowed_cidrs`. The ZenML server ECS task itself sits in
the private subnets with no public IP — the ALB is the only front door, and
the ALB is the only thing gated by your IP.

```yaml
zenml-ecs:security:
  allowed_cidrs: ["<your-ip>/32"]   # e.g. curl -s ifconfig.me
```

Batch job containers reach the server over plain HTTPS via the ALB's public
DNS name (egress-only from `batch_sg`), the same way your local `zenml`
client would — no special networking is needed for pipeline runs to log
metadata back to the server.

## Deploy

```bash
cd infrastructure
pip install -r requirements.txt
pulumi stack init zenml-ecs        # or `pulumi stack select zenml-ecs`
pulumi config set zenml-ecs:security '{"allowed_cidrs": ["<your-ip>/32"]}'
pulumi up
```

Then:

```bash
pulumi stack output server_url
# zenml login <that URL>

# Build & push your pipeline/step images:
aws ecr get-login-password | docker login --username AWS \
  --password-stdin $(pulumi stack output ecr_repository_url | cut -d/ -f1)
docker build -t $(pulumi stack output ecr_repository_url):latest .
docker push $(pulumi stack output ecr_repository_url):latest

# Register the artifact store / container registry / step operator against
# this stack's outputs:
zenml artifact-store register s3-store --flavor=s3 \
  --path=$(pulumi stack output artifact_store_bucket)
zenml container-registry register ecr --flavor=aws \
  --uri=$(pulumi stack output ecr_repository_url)
zenml step-operator register batch --flavor=aws-batch \
  --job_definition_name=<your-batch-job-def> \
  --job_queue=$(pulumi stack output batch_default_job_queue_name) \
  --job_role_arn=$(pulumi stack output batch_job_role_arn)
```

## Notes / things to adjust before production use

- **HTTPS**: the ALB listener is HTTP-only for simplicity. For anything past
  a personal/dev stack, add an ACM cert + HTTPS listener (443) and redirect
  80→443, and restrict the security group to 443 only.
- **`allowed_cidrs`**: the shipped config defaults to `0.0.0.0/0` as a
  placeholder — replace with your actual IP(s)/32 (or VPN egress range)
  before `pulumi up` in any shared/non-throwaway environment.
- **Health check path**: the target group checks `GET /health`. If your
  ZenML server version doesn't expose that route, change
  `server_tg`'s `health_check.path` (`/` with `matcher="200-399,401,404"`
  is a safe fallback since the dashboard/API respond even unauthenticated).
- **Batch job definitions**: this stack provisions the compute environments
  and queues; register the actual Batch job definition(s) via the `zenml
  step-operator register` flow (or a separate `aws.batch.JobDefinition`
  resource) once you know your step images.
- **Multi-AZ / backups**: `skip_final_snapshot=True` and single-AZ RDS are
  dev-friendly defaults — turn on `multi_az` and backups for anything
  production-facing.
