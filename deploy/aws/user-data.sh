#!/bin/bash
# EC2 bootstrap for StipGate (Amazon Linux 2023, x86_64, free-tier m7i-flex.large: 2 vCPU / 8 GB).
#
# Two containers on a private docker network:
#   decider       Strands Decider 2B (StrandsAgents/strands-decider-2B-qwen3.5-v1-2610, pinned), CPU int8,
#                 built from the community AgentCore runtime (fits 8 GB: ~4.2 GB resident, 6.6 GB peak).
#                 Port 8080 is NOT published on the host (the runtime has no auth).
#   stipgate-api  StipGate API + UI on :8000, X-API-Key required, Nova Lite via the instance role.
#
# The official `strands-decider serve` upcasts to fp32 on CPU (~9 GB) and does not fit 8 GB, which is why
# the community int8 runtime is used: https://github.com/Vivek0712/strands-decider-agentcore
# __BUCKET__ / __REGION__ are substituted by deploy.ps1. Log: /var/log/stipgate-bootstrap.log
set -euxo pipefail
exec > >(tee -a /var/log/stipgate-bootstrap.log) 2>&1

REGION="__REGION__"
BUCKET="__BUCKET__"
RUNTIME_REPO="https://github.com/Vivek0712/strands-decider-agentcore"
RUNTIME_REF="f9691ee9b2891ecb72308377947e4d655f07bde5"
STRANDS_DECIDER_REF="d942e5515db750f80a51032629ec312455a87976"
DECIDER_MODEL="StrandsAgents/strands-decider-2B-qwen3.5-v1-2610"
DECIDER_REVISION="216d1ccb46a9e9b036eb9187b248db98480f77d4"

# 4 GB swap as headroom for the 6.6 GB load peak on an 8 GB host.
if [ ! -f /swapfile ]; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo "/swapfile swap swap defaults 0 0" >> /etc/fstab
fi

dnf install -y docker git
systemctl enable --now docker

mkdir -p /opt/stipgate
cd /opt/stipgate
aws s3 cp "s3://${BUCKET}/stipgate-src.tgz" src.tgz --region "$REGION"
tar -xzf src.tgz
rm -f src.tgz

if [ ! -d /opt/decider-runtime ]; then
  git clone "$RUNTIME_REPO" /opt/decider-runtime
fi
git -C /opt/decider-runtime checkout "$RUNTIME_REF"

docker build -t stipgate-decider:local \
  --build-arg STRANDS_DECIDER_REF="$STRANDS_DECIDER_REF" /opt/decider-runtime/runtime
docker build -t stipgate-api:local .

# Never trace commands that handle the API key (it would land in the log and the EC2 console output).
set +x
API_KEY="$(aws ssm get-parameter --name /stipgate/api-key --with-decryption \
  --query Parameter.Value --output text --region "$REGION")"

docker network inspect stipgate >/dev/null 2>&1 || docker network create stipgate
docker volume inspect hf-cache >/dev/null 2>&1 || docker volume create hf-cache
docker rm -f decider stipgate-api >/dev/null 2>&1 || true

# First start downloads ~4.6 GB from Hugging Face, merges LoRA and quantizes to int8 (~1-2 min after download).
docker run -d --name decider --restart unless-stopped --network stipgate \
  -v hf-cache:/tmp/hf \
  -e DECIDER_MODEL="$DECIDER_MODEL" \
  -e DECIDER_REVISION="$DECIDER_REVISION" \
  -e DECIDER_QUANT=int8 \
  -e DECIDER_QENGINE=onednn \
  -e DECIDER_MAX_TOKENS=2048 \
  stipgate-decider:local

# CPU int8 on 2 vCPU takes seconds per ask, so the gate timeout is raised from the 1.5 s GPU default.
docker run -d --name stipgate-api --restart unless-stopped --network stipgate \
  -p 8000:8000 \
  -e STIPGATE_DECIDER_MODE=server \
  -e STIPGATE_DECIDER_URL=http://decider:8080/invocations \
  -e STIPGATE_DECIDER_TIMEOUT_S=30 \
  -e STIPGATE_API_KEY="$API_KEY" \
  -e AWS_REGION="$REGION" \
  -e AWS_DEFAULT_REGION="$REGION" \
  stipgate-api:local

unset API_KEY
set -x
echo "STIPGATE_BOOTSTRAP_DONE"
