# Strands Decider 2B - CPU sidecar for StipGate (dev / demo only).
#
# Built from the official PyPI package strands-decider==0.2.0 (https://pypi.org/project/strands-decider/).
# This is NOT the upstream image and the build is UNVERIFIED: it was never built or run while
# writing this repo. Version pins (torch 2.7.1, transformers 5.19.0) are copied from the upstream
# serving image: https://github.com/strands-labs/strands-decider/blob/main/deploy/sagemaker/Dockerfile
#
# For GPU (the real-time path) build the upstream deploy/sagemaker/Dockerfile from a repo clone with
# DEVICE=cuda instead. CPU inference is ~1.6-2.5 s per ask (upstream docs), so StipGate must raise
# STIPGATE_DECIDER_TIMEOUT_S (docker-compose.yml sets 5) or every call times out and fails closed.
# linux/amd64 only: the official stack does not support Graviton (arm64).
#
# First start downloads the base model Qwen/Qwen3.5-2B-Base (~4.5 GB) into HF_HOME; mount a volume
# there (docker-compose.yml: hf-cache) so it is downloaded once. Set HF_HUB_OFFLINE=1 once cached.
# The Decider server has NO authentication: never publish its port beyond a private network.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# CPU-only torch wheel (skips the CUDA wheels on Linux), then the Decider package.
RUN pip install torch==2.7.1 --index-url https://download.pytorch.org/whl/cpu \
    && pip install strands-decider==0.2.0 transformers==5.19.0

RUN useradd --system --uid 10002 --no-create-home decider

ENV HF_HOME=/opt/hf \
    DECIDER_MODEL=StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 \
    DECIDER_DEVICE=cpu \
    DECIDER_ARGS=""

RUN mkdir -p /opt/hf && chown 10002 /opt/hf

USER 10002

EXPOSE 8099

# Long start period: the first start downloads ~4.5 GB of base weights before /health answers.
HEALTHCHECK --interval=30s --timeout=5s --start-period=900s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["sh", "-c", "exec strands-decider serve \"$DECIDER_MODEL\" --host 0.0.0.0 --port 8099 --device \"$DECIDER_DEVICE\" $DECIDER_ARGS"]
