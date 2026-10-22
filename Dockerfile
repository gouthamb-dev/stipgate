# StipGate API + test UI (FastAPI / uvicorn via the `stipgate-serve` console script).
#
# Defaults are fail closed: server-mode Decider at http://127.0.0.1:8099/v1/systemone (the sidecar
# on the same host / ECS task). With no Decider reachable every document is HALTED to human review.
#
# SECURITY: the API is unauthenticated unless STIPGATE_API_KEY is set. Set it (from AWS Secrets
# Manager via the ECS `secrets` block in AWS) before exposing the container beyond loopback.
# The server binds 0.0.0.0 inside the container, so publish it on host loopback only:
#   docker run -p 127.0.0.1:8000:8000 stipgate-api
# (`-p 8000:8000` would serve the PII-bearing /v1/* routes on every host interface.)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY stipgate ./stipgate
RUN pip install --no-deps .

RUN useradd --system --uid 10001 --no-create-home stipgate
USER 10001

ENV STIPGATE_DECIDER_MODE=server \
    STIPGATE_DECIDER_URL=http://127.0.0.1:8099/v1/systemone \
    STIPGATE_HOST=0.0.0.0 \
    STIPGATE_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

CMD ["stipgate-serve"]
