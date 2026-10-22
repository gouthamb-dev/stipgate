# Deploying StipGate

> **Looking for the deployment that actually runs the demo?** See [deploy/aws/README.md](../deploy/aws/README.md)
> (one EC2 instance + CloudFront, deployed and tested). This document is the production reference design.
>
> **Guide only.** Nothing in this document was deployed or executed while building the repo: no ECS
> resources were created and the GPU image was not built or run. AWS CLI commands, IAM JSON and the task
> definition are starting points to adapt and review. Decider facts come from the official docs listed in
> [DECIDER_HOSTING.md](DECIDER_HOSTING.md); items marked **unverified** are gaps.

Recommended shape: the StipGate API and a GPU Decider 2B sidecar in **one** ECS-on-EC2 task (g6.xlarge or
g5.xlarge), talking over `localhost`, behind an **internal** ALB, with Bedrock reached through a VPC
interface endpoint. See [ARCHITECTURE.md](ARCHITECTURE.md) for components and trust boundaries.

## Latency expectation

Published Decider 2B latency is ~115 ms median / 299 ms p95 per question on an RTX 3090 and ~153 ms warm
median on an M3 Pro ([inference.md](https://github.com/strands-labs/strands-decider/blob/main/docs/inference.md)).
That does **not** support "<90 ms". It is a target that needs a GPU, loopback networking and short
prompts, and you must measure it on the deployed hardware (`GET /v1/metrics`, `benchmark.py --heldout`).

## Hosting options compared

| Option | Published latency | Fit for StipGate | Source | Status |
|---|---|---|---|---|
| **GPU sidecar** (EC2 g6.xlarge L4 / g5.xlarge A10G, ECS-on-EC2, same task as the API, loopback) | ~115 ms median / 299 ms p95 per question (RTX 3090; g6/g5 not published) | Real-time gate. **Recommended.** | [inference.md](https://github.com/strands-labs/strands-decider/blob/main/docs/inference.md), [SageMaker README](https://github.com/strands-labs/strands-decider/blob/main/deploy/sagemaker/README.md) (instance types) | verified-from-docs |
| SageMaker real-time GPU endpoint (official CDK stack, default `ml.g6.xlarge`, `/invocations`) | ~1013 ms `latency_ms` in the upstream example | Managed; extra network hop and SigV4. StipGate's client does not sign requests, so it needs a signing adapter (not in this repo). | [SageMaker README](https://github.com/strands-labs/strands-decider/blob/main/deploy/sagemaker/README.md) | verified-from-docs |
| Local Mac, MPS or MLX | MPS warm median 153 ms (M3 Pro); MLX 113 ms at 222 tokens (M4 Pro) | Developer laptops. | [inference.md](https://github.com/strands-labs/strands-decider/blob/main/docs/inference.md) | verified-from-docs |
| CPU container (`docker/decider`) | ~1.6 s per ask (Xeon 8375C); ~2.5 s 3-question on `ml.m5.2xlarge` | Demo / CI. Raise `STIPGATE_DECIDER_TIMEOUT_S` to 5. | [inference.md](https://github.com/strands-labs/strands-decider/blob/main/docs/inference.md), [SageMaker README](https://github.com/strands-labs/strands-decider/blob/main/deploy/sagemaker/README.md) | verified-from-docs |
| AWS Lambda (CPU, 10 GB, 6 vCPU, fp32) | ~1.7 s warm (1 question), 3.5-5.5 s (3 questions), cold 18-30 s+ | Batch / async only. | [Builder Center community post](https://builder.aws.com/content/3KFNvjfw1TORUmretjxEKgNdyKH) | community numbers, unverified here |
| Amazon Bedrock AgentCore Runtime (int8 CPU, 2 vCPU / 8 GB, IAM SigV4) | ~7.6 s warm, ~70 s cold | Async review queue only; int8 is a community build. | [Builder Center community post](https://builder.aws.com/content/3KNqrNkiEtf7wTUzTufirwAVL7O), [community repo](https://github.com/Vivek0712/strands-decider-agentcore) | community numbers, unverified here |
| Lambda Web Functions | n/a | Not usable (Node.js 24 / arm64 / zip only). StipGate API on Lambda = container image + Lambda Web Adapter; the Decider still runs elsewhere. | workflow research notes | unverified here |
| Ollama | n/a | Do not use for the gate: listed in the Ollama library per the orchestrator, but pointer-head calibration is unknown. | workflow research notes | unverified |

Graviton (arm64), Inferentia and Trainium are not supported for the Decider by the official stack. The
StipGate API image can be multi-arch.

## Edge / on-prem topology

```
 LOS / POS (lender network)
      │ HTTPS + X-API-Key
      ▼
 ┌──────────────────────── edge host (GPU) ─────────────────────────┐
 │ reverse proxy / TLS  ──►  StipGate API :8000 (stipgate-serve)    │
 │                              │ http://127.0.0.1:8099/v1/systemone │
 │                              ▼                                    │
 │                         Decider 2B sidecar (loopback only)        │
 └──────────────────────────────┬────────────────────────────────────┘
                                │ HTTPS (egress proxy or AWS PrivateLink via VPN / Direct Connect)
                                ▼
                    bedrock-runtime.us-east-1 (Nova Lite, gated documents only)
```

- Run `stipgate-serve --host 0.0.0.0` only behind the TLS proxy, and always with `STIPGATE_API_KEY` set.
- The Decider binds `127.0.0.1` and is never reachable from the LOS network.
- OCR text for halted documents never leaves the host. Exception: a scanned PDF or image sent to
  `POST /v1/documents/upload` is transcribed by Nova Lite before the gate runs (text-based PDFs and `.txt`
  are read locally). Clients that must keep halted documents on the host should send pre-extracted text to
  `/v1/documents/evaluate` instead.

## AWS: ECS on EC2 with a GPU sidecar

### Topology

```
 LOS (VPC peer / VPN) ──► internal ALB :443 ──► target group (IP) ──► stipgate-api :8000  ┐
                                                                                          │ same ECS task,
                                                       decider :8080 (upstream image) ◄───┘ awsvpc, localhost
                                                                │
 private subnets ── VPC interface endpoint com.amazonaws.us-east-1.bedrock-runtime (private DNS)
```

- Capacity: an ECS capacity provider on an Auto Scaling group of `g6.xlarge` (L4 24 GB, upstream default)
  or `g5.xlarge` (A10G) with the ECS GPU-optimized AMI. amd64 only for the Decider.
- Fargate has no GPUs. Fargate fits only the CPU variant (`docker/decider`, ~9 GB fp32 + activations, so
  size the task generously) with `STIPGATE_DECIDER_TIMEOUT_S` raised to 5; that is a demo, not a
  real-time gate.
- Both containers in one task with `networkMode: awsvpc`, so they share `localhost`. The Decider gets the
  GPU; the API does not need one.
- The internal ALB health check targets `GET /healthz` on 8000. `/healthz` is liveness and always returns
  200; readiness is the `decider.reachable` field. The task definition below makes the API container wait
  for a HEALTHY Decider, and a Decider outage after start makes every document fail closed (HALTED, human
  review, 0 tokens), so alarm on `decider_error` (see [ARCHITECTURE.md](ARCHITECTURE.md#observability)).
- Run one Decider per task; scale tasks, not in-task concurrency (upstream concurrency is unverified).

### Images

- StipGate API: the repo `Dockerfile` (python:3.11-slim, non-root uid 10001).
- Decider (GPU): build the upstream `deploy/sagemaker/Dockerfile` from a clone of
  https://github.com/strands-labs/strands-decider with `DEVICE=cuda`. It starts with
  `strands-decider serve "$DECIDER_CHECKPOINT" --host 0.0.0.0 --port 8080 --device "$DECIDER_DEVICE" $DECIDER_ARGS`,
  so StipGate must use `STIPGATE_DECIDER_URL=http://127.0.0.1:8080/v1/systemone`. The exact build
  invocation (build args, context directory) is in the upstream SageMaker README; the command below is
  **unverified**.
- Decider (CPU): `docker/decider/Dockerfile` (PyPI `strands-decider==0.2.0`, port 8099, unverified build).
- Weights: avoid a 4.5 GB download on every task start. Either bake the checkpoint and base model into the
  image, or pre-stage them (`hf download ... --local-dir`) to S3 / EFS, mount at `HF_HOME`, and set
  `HF_HUB_OFFLINE=1` so the server never reaches Hugging Face at runtime.

ECR push (guide only):

```bash
AWS_ACCOUNT=111122223333
REGION=us-east-1
REGISTRY=$AWS_ACCOUNT.dkr.ecr.$REGION.amazonaws.com

aws ecr create-repository --repository-name stipgate-api --region $REGION
aws ecr create-repository --repository-name strands-decider-gpu --region $REGION
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REGISTRY

# StipGate API (repo root)
docker build --platform linux/amd64 -t stipgate-api:1.0.0 .
docker tag stipgate-api:1.0.0 $REGISTRY/stipgate-api:1.0.0
docker push $REGISTRY/stipgate-api:1.0.0

# Decider GPU image from the upstream repo (build args / context: check deploy/sagemaker/README.md; unverified)
git clone https://github.com/strands-labs/strands-decider
docker build --platform linux/amd64 --build-arg DEVICE=cuda -f strands-decider/deploy/sagemaker/Dockerfile -t strands-decider-gpu:v1 strands-decider
docker tag strands-decider-gpu:v1 $REGISTRY/strands-decider-gpu:v1
docker push $REGISTRY/strands-decider-gpu:v1
```

### Task definition (excerpt)

Replace account ids, ARNs and image tags. `DECIDER_CHECKPOINT` / `DECIDER_DEVICE` are the upstream image's
variables. ECS caps `healthCheck.startPeriod` at 300 s, which is why the weights should be pre-staged.

```json
{
  "family": "stipgate",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["EC2"],
  "cpu": "4096",
  "memory": "14336",
  "taskRoleArn": "arn:aws:iam::111122223333:role/stipgate-task-role",
  "executionRoleArn": "arn:aws:iam::111122223333:role/stipgate-task-execution-role",
  "containerDefinitions": [
    {
      "name": "decider",
      "image": "111122223333.dkr.ecr.us-east-1.amazonaws.com/strands-decider-gpu:v1",
      "essential": true,
      "resourceRequirements": [{ "type": "GPU", "value": "1" }],
      "environment": [
        { "name": "DECIDER_CHECKPOINT", "value": "/opt/hf/v1" },
        { "name": "DECIDER_DEVICE", "value": "cuda" },
        { "name": "HF_HOME", "value": "/opt/hf" },
        { "name": "HF_HUB_OFFLINE", "value": "1" }
      ],
      "healthCheck": {
        "command": ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"],
        "interval": 30,
        "timeout": 5,
        "retries": 3,
        "startPeriod": 300
      },
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/stipgate",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "decider"
        }
      }
    },
    {
      "name": "stipgate-api",
      "image": "111122223333.dkr.ecr.us-east-1.amazonaws.com/stipgate-api:1.0.0",
      "essential": true,
      "portMappings": [{ "containerPort": 8000, "protocol": "tcp" }],
      "environment": [
        { "name": "STIPGATE_DECIDER_MODE", "value": "server" },
        { "name": "STIPGATE_DECIDER_URL", "value": "http://127.0.0.1:8080/v1/systemone" },
        { "name": "STIPGATE_DECIDER_TIMEOUT_S", "value": "1.5" },
        { "name": "STIPGATE_DECIDER_MAX_CONCURRENCY", "value": "1" },
        { "name": "STIPGATE_HOST", "value": "0.0.0.0" },
        { "name": "STIPGATE_PORT", "value": "8000" }
      ],
      "secrets": [
        {
          "name": "STIPGATE_API_KEY",
          "valueFrom": "arn:aws:secretsmanager:us-east-1:111122223333:secret:stipgate/api-key-AbCdEf"
        }
      ],
      "dependsOn": [{ "containerName": "decider", "condition": "HEALTHY" }],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/stipgate",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "api"
        }
      }
    }
  ]
}
```

Never set `STIPGATE_DECIDER_MODE=heuristic`/`auto` or `STIPGATE_ALLOW_HEURISTIC_PROCEED` in production.
Leave `STIPGATE_BEDROCK_OFFLINE` unset so Nova Lite is called (otherwise every gated document becomes
`pending_extraction_review`).

### Bedrock over PrivateLink

Create an interface endpoint for `com.amazonaws.us-east-1.bedrock-runtime` in the task subnets with
private DNS enabled, so `bedrock-runtime.us-east-1.amazonaws.com` resolves to private IPs. Without a NAT
gateway you also need endpoints for ECR (`ecr.api`, `ecr.dkr`, S3 gateway), CloudWatch Logs and Secrets
Manager.

```bash
aws ec2 create-vpc-endpoint --region us-east-1 --vpc-id vpc-0example \
  --vpc-endpoint-type Interface --service-name com.amazonaws.us-east-1.bedrock-runtime \
  --subnet-ids subnet-0example1 subnet-0example2 --security-group-ids sg-0example --private-dns-enabled
```

The endpoint security group should allow 443 only from the task security group.

### IAM: task role (least privilege)

The Converse API is authorised by `bedrock:InvokeModel`. StipGate calls only Nova Lite in us-east-1:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeNovaLiteOnly",
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0",
      "Condition": {
        "StringEquals": { "aws:RequestedRegion": "us-east-1" }
      }
    }
  ]
}
```

If you later switch to a cross-region inference profile (for example `us.amazon.nova-lite-v1:0`), the policy
must also allow the inference-profile ARN and the foundation-model ARN in each destination region, and the
region condition must change. The code pins `amazon.nova-lite-v1:0` in us-east-1 today.

Trust policy for both roles:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "ecs-tasks.amazonaws.com" },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": { "aws:SourceAccount": "111122223333" }
      }
    }
  ]
}
```

### IAM: task execution role

Image pull, log delivery and the single API-key secret:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcrAuth",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "EcrPull",
      "Effect": "Allow",
      "Action": ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"],
      "Resource": [
        "arn:aws:ecr:us-east-1:111122223333:repository/stipgate-api",
        "arn:aws:ecr:us-east-1:111122223333:repository/strands-decider-gpu"
      ]
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-east-1:111122223333:log-group:/ecs/stipgate:*"
    },
    {
      "Sid": "ApiKeySecret",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:us-east-1:111122223333:secret:stipgate/api-key-AbCdEf"
    }
  ]
}
```

If the secret uses a customer-managed KMS key, add `kms:Decrypt` on that key ARN.

### API key in Secrets Manager

```bash
aws secretsmanager create-secret --region us-east-1 --name stipgate/api-key \
  --secret-string "$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

ECS injects it through the task definition `secrets` block as `STIPGATE_API_KEY`. Clients send it in the
`X-API-Key` header on every `/v1/*` call. Rotating the secret requires a new task deployment (the key is
read at start).

### CloudWatch

- Container logs go to `/ecs/stipgate` via the `awslogs` driver.
- Create metric filters on the `stipgate.audit` lines (gate latency, halts, `decider_error`, heuristic
  backend, tokens) as listed in [ARCHITECTURE.md](ARCHITECTURE.md#observability), and alarm on any
  `decider_error` burst and any heuristic verdict.
- `GET /v1/metrics` gives per-task counters for spot checks; it is not aggregated across tasks.

### Bedrock Guardrails (optional, not implemented in code)

Amazon Bedrock Guardrails can be a second layer on the Nova Lite call: create a guardrail (for example
sensitive-information filters for SSNs) and pass `guardrailConfig={"guardrailIdentifier": ..., "guardrailVersion": ...}`
to `converse()` in `BedrockExtractor.extract`. StipGate does **not** do this today. Adding it needs a code
change and an IAM permission for the guardrail (check the Bedrock Guardrails docs for the exact action
and resource ARN). Decider 2B remains the gate; Guardrails only filter what Nova Lite sees and returns.

### Nova Lite model access

Confirm Amazon Nova Lite (`amazon.nova-lite-v1:0`) is available to the account in **us-east-1** in the
Amazon Bedrock console (model access / model catalog) before deploying. Depending on account settings,
access may already be enabled. Without access, Converse returns `AccessDeniedException`, StipGate falls
back to synthetic extraction and every gated document becomes `pending_extraction_review` (never
`cleared`).

### Alternative: SageMaker real-time endpoint for the Decider

Upstream ships a CDK stack (`deploy/sagemaker`, `npx cdk deploy`) that creates a real-time endpoint
(default `ml.g6.xlarge`) serving the `/invocations` contract; callers need `sagemaker:InvokeEndpoint`
([SageMaker README](https://github.com/strands-labs/strands-decider/blob/main/deploy/sagemaker/README.md)).

Why the sidecar is preferred for the gate:

- Loopback vs a network hop plus SigV4 signing; the upstream example shows ~1013 ms `latency_ms`.
- `DeciderClient` sends unsigned HTTP. Calling SageMaker needs a SigV4-signing adapter (for example a small
  proxy on loopback that calls `InvokeEndpoint`), which this repo does not include.
- One more managed endpoint to secure and pay for.

SageMaker is a reasonable choice when several services must share GPU capacity, or when you need managed
autoscaling of the model separately from the API.

## Cost notes

- GPU instance hours dominate. One g6.xlarge / g5.xlarge per task runs whether documents arrive or not;
  check current EC2 / SageMaker pricing for your region.
- Nova Lite tokens are spent only on gated (PROCEED) documents. Halted documents cost zero tokens.
- The prices in `benchmark.py` (`NOVA_LITE_*_USD_PER_1K`, `FRONTIER_*_USD_PER_1K`) are assumptions; verify
  them on the Amazon Bedrock pricing page before quoting savings.
- CPU / Lambda / AgentCore hosting is cheaper per hour but seconds per question; use it only for async
  review, not the real-time gate.
