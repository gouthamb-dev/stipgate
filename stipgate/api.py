"""StipGate web API + test UI (FastAPI).

Endpoints:

* ``GET  /healthz``                  - liveness (always 200) + readiness detail (``decider.reachable``).
* ``POST /v1/documents/evaluate``    - gate one trailing document; returns the pipeline execution trace.
* ``POST /v1/documents/upload``      - raw file body (PDF / image / text, max 4 MB) with ``document_id`` and
  ``condition_code`` query parameters; text is extracted (see :mod:`stipgate.ocr`) and then gated exactly
  like ``/evaluate``. The response adds ``ocr`` (method, tokens) and ``extracted_text``.
* ``GET  /v1/review-queue``          - tickets routed to the human loan-officer queue.
* ``GET  /v1/samples``               - the four synthetic benchmark documents (for the UI).
* ``GET  /v1/metrics``               - in-process gate counters and latency percentiles.
* ``GET  /``                         - self-contained accessible test UI (``stipgate/static/index.html``).

Authentication: when ``STIPGATE_API_KEY`` (or ``create_app(api_key=...)``) is set, every ``/v1/*``
route requires a matching ``X-API-Key`` header (constant-time comparison). ``/healthz`` and ``/``
stay open; neither returns document data. Without a key the service is UNAUTHENTICATED: keep it on
loopback (``stipgate-serve`` warns when bound elsewhere without a key).

Configuration is environment-driven (see :func:`build_default_pipeline`): the Decider client reads
``STIPGATE_DECIDER_URL`` / ``STIPGATE_DECIDER_MODE`` / ``STIPGATE_DECIDER_TIMEOUT_S`` /
``STIPGATE_DECIDER_MAX_CONCURRENCY`` / ``STIPGATE_ALLOW_HEURISTIC_PROCEED``; ``STIPGATE_BEDROCK_OFFLINE``
forces the synthetic extraction fallback (never ``cleared``).

Every evaluation emits one JSON line on logger ``stipgate.audit`` carrying no snippet text and no
extracted payload (no borrower PII).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import secrets
import statistics
import threading
from collections import Counter, deque
from dataclasses import asdict
from importlib import resources
from typing import Annotated, Any, Final, Literal

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from stipgate import __version__
from stipgate.bedrock_worker import BedrockExtractor
from stipgate.decider_client import DeciderClient
from stipgate.handlers import InMemoryReviewQueue
from stipgate.ocr import MAX_UPLOAD_BYTES, DocumentTextExtractor, OcrError, OcrResult, OcrUnavailableError
from stipgate.pipeline import StipGatePipeline
from stipgate.samples import core_samples

__all__ = [
    "ConditionCode",
    "MAX_SNIPPET_CHARS",
    "ENV_API_KEY",
    "ENV_BEDROCK_OFFLINE",
    "ENV_HOST",
    "ENV_PORT",
    "UI_SECURITY_HEADERS",
    "EvaluateRequest",
    "GateMetrics",
    "build_default_pipeline",
    "create_app",
    "main",
]

logger = logging.getLogger("stipgate.api")
audit_logger = logging.getLogger("stipgate.audit")

ConditionCode = Literal["STIP_INCOME_30DAY", "STIP_W2_2YR", "STIP_ASSET_2MO"]
MAX_SNIPPET_CHARS: Final = 50_000
ENV_API_KEY: Final = "STIPGATE_API_KEY"
ENV_BEDROCK_OFFLINE: Final = "STIPGATE_BEDROCK_OFFLINE"
ENV_HOST: Final = "STIPGATE_HOST"
ENV_PORT: Final = "STIPGATE_PORT"
DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8000
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_LATENCY_WINDOW: Final = 1000
_INTERNAL_ERROR_DETAIL: Final = "internal error; document not cleared"
_AUTH_ERROR_DETAIL: Final = "missing or invalid API key"
_DOCUMENT_ID_PATTERN: Final = r"^[A-Za-z0-9._:-]+$"
_UPLOAD_TOO_LARGE_DETAIL: Final = f"file exceeds {MAX_UPLOAD_BYTES} bytes"

UI_SECURITY_HEADERS: Final[dict[str, str]] = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


class EvaluateRequest(BaseModel):
    """Body of ``POST /v1/documents/evaluate``."""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=64, pattern=_DOCUMENT_ID_PATTERN)
    snippet: str = Field(min_length=1, max_length=MAX_SNIPPET_CHARS)
    condition_code: ConditionCode


def _percentile(sorted_samples: list[float], quantiles: list[float], index: int) -> float:
    if not sorted_samples:
        return 0.0
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    return quantiles[index]


class GateMetrics:
    """Thread-safe in-process counters for gate evaluations (exposed at ``GET /v1/metrics``)."""

    def __init__(self, window: int = _LATENCY_WINDOW) -> None:
        self._lock = threading.Lock()
        self.requests: int = 0
        self.proceeded: int = 0
        self.halted: int = 0
        self.cleared: int = 0
        self.pending_extraction_review: int = 0
        self.tokens_spent: int = 0
        self.by_backend: Counter[str] = Counter()
        self.uploads: int = 0
        self.ocr_tokens_spent: int = 0
        self.by_ocr_method: Counter[str] = Counter()
        self._latencies: deque[float] = deque(maxlen=window)

    def record_ocr(self, result: OcrResult) -> None:
        """Count one upload's text extraction (OCR tokens are tracked apart from gate ``tokens_spent``)."""
        with self._lock:
            self.uploads += 1
            self.ocr_tokens_spent += result.total_tokens
            self.by_ocr_method[result.method] += 1

    def record(self, trace: dict[str, Any]) -> None:
        latency = float(trace.get("latency_ms", 0.0))
        with self._lock:
            self.requests += 1
            if trace.get("gated_action") == "PROCEED":
                self.proceeded += 1
            else:
                self.halted += 1
            status = trace.get("clearance_status")
            if status == "cleared":
                self.cleared += 1
            elif status == "pending_extraction_review":
                self.pending_extraction_review += 1
            self.tokens_spent += int(trace.get("tokens_spent", 0))
            self.by_backend[str(trace.get("decider_backend") or "none")] += 1
            if math.isfinite(latency):
                self._latencies.append(latency)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            samples = sorted(self._latencies)
            data: dict[str, Any] = {
                "requests": self.requests,
                "proceeded": self.proceeded,
                "halted": self.halted,
                "cleared": self.cleared,
                "pending_extraction_review": self.pending_extraction_review,
                "tokens_spent": self.tokens_spent,
                "by_backend": dict(self.by_backend),
                "uploads": self.uploads,
                "ocr_tokens_spent": self.ocr_tokens_spent,
                "by_ocr_method": dict(self.by_ocr_method),
            }
        cuts = statistics.quantiles(samples, n=100, method="inclusive") if len(samples) >= 2 else []
        data["gate_latency_ms"] = {
            "samples": len(samples),
            "p50": round(_percentile(samples, cuts, 49), 3),
            "p95": round(_percentile(samples, cuts, 94), 3),
        }
        return data


def build_default_pipeline() -> StipGatePipeline:
    """Pipeline configured entirely from the environment (server-mode Decider by default)."""
    return StipGatePipeline(
        decider=DeciderClient(),
        extractor=BedrockExtractor(offline=_env_flag(ENV_BEDROCK_OFFLINE)),
        review_queue=InMemoryReviewQueue(),
    )


def _audit(trace: dict[str, Any], ocr: dict[str, Any] | None = None) -> None:
    """One JSON line per evaluation; deliberately excludes snippet text and extracted payload."""
    ticket_ids = [
        ticket
        for ticket in (trace.get("review_ticket_id"), trace.get("extraction_review_ticket_id"))
        if ticket is not None
    ]
    record = {
        "event": "stipgate.evaluate",
        "document_id": trace.get("document_id"),
        "condition_code": trace.get("condition_code"),
        "gated_action": trace.get("gated_action"),
        "clearance_status": trace.get("clearance_status"),
        "verdict": trace.get("verdict"),
        "confidence": trace.get("confidence"),
        "decider_backend": trace.get("decider_backend"),
        "decider_latency_ms": trace.get("decider_latency_ms"),
        "latency_ms": trace.get("latency_ms"),
        "tokens_spent": trace.get("tokens_spent"),
        "review_ticket_ids": ticket_ids,
    }
    if ocr is not None:
        record["ocr"] = ocr  # method / tokens / sizes only, never the text
    audit_logger.info(json.dumps(record, separators=(",", ":")))


def _load_ui() -> str:
    return resources.files("stipgate").joinpath("static/index.html").read_text("utf-8")


async def _read_body_limited(request: Request, limit: int) -> bytes:
    """Read the raw request body, rejecting anything over ``limit`` bytes without buffering it all."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from None
        if declared_size > limit:
            raise HTTPException(status_code=413, detail=_UPLOAD_TOO_LARGE_DETAIL)
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise HTTPException(status_code=413, detail=_UPLOAD_TOO_LARGE_DETAIL)
    return bytes(buffer)


def create_app(
    pipeline: StipGatePipeline | None = None,
    *,
    api_key: str | None = None,
    ocr: DocumentTextExtractor | None = None,
) -> FastAPI:
    """Build the FastAPI app. ``api_key`` overrides ``STIPGATE_API_KEY``; an empty key disables auth.

    ``ocr`` defaults to a :class:`DocumentTextExtractor` sharing the pipeline extractor's Bedrock client,
    so uploads need no extra IAM permission beyond ``bedrock:InvokeModel`` on Nova Lite.
    """
    gate = pipeline if pipeline is not None else build_default_pipeline()
    ocr_engine = (
        ocr
        if ocr is not None
        else DocumentTextExtractor(lambda: gate.extractor.client, model_id=gate.extractor.model_id)
    )
    key = api_key if api_key is not None else os.environ.get(ENV_API_KEY, "")
    auth_enabled = bool(key)
    metrics = GateMetrics()
    ui_html = _load_ui()

    def require_api_key(x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None) -> None:
        if not auth_enabled:
            return
        supplied = (x_api_key or "").encode("utf-8")
        if not secrets.compare_digest(supplied, key.encode("utf-8")):
            raise HTTPException(status_code=401, detail=_AUTH_ERROR_DETAIL)

    app = FastAPI(
        title="StipGate",
        version=__version__,
        description="Edge-gated mortgage trailing document clearance (Strands Decider 2B + Bedrock Nova Lite).",
    )
    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        decider = gate.decider
        extractor = gate.extractor
        reachable = decider.probe()
        warnings: list[str] = []
        if decider.mode in ("heuristic", "auto"):
            warnings.append("heuristic backend active: verdicts are simulated")
        if reachable is False and decider.mode == "server":
            warnings.append("Decider server unreachable: every document will fail closed")
        if not auth_enabled:
            warnings.append("authentication disabled")
        return {
            "status": "ok",
            "version": __version__,
            "auth_enabled": auth_enabled,
            "decider": {
                "mode": decider.mode,
                "endpoint": decider.endpoint,
                "timeout_s": decider.timeout_s,
                "max_concurrency": decider.max_concurrency,
                "allow_heuristic_proceed": decider.allow_heuristic_proceed,
                "reachable": reachable,
            },
            "bedrock": {
                "model_id": extractor.model_id,
                "region": extractor.region_name,
                "offline": extractor.offline,
                "client_configured": extractor.client is not None,
            },
            "warnings": warnings,
        }

    def run_gate(
        document_id: str, snippet: str, condition_code: str, ocr_result: OcrResult | None = None
    ) -> dict[str, Any]:
        try:
            trace = gate.process_document(document_id, snippet, condition_code)
        except Exception:
            logger.exception("Evaluation failed for doc=%s; document not cleared", document_id)
            raise HTTPException(status_code=500, detail=_INTERNAL_ERROR_DETAIL) from None
        response = dict(trace)
        response["heuristic_warning"] = trace.get("decider_backend") == "heuristic"
        metrics.record(trace)
        _audit(trace, ocr_result.summary() if ocr_result is not None else None)
        return response

    @v1.post("/documents/evaluate")
    def evaluate(request: EvaluateRequest) -> dict[str, Any]:
        return run_gate(request.document_id, request.snippet, request.condition_code)

    @v1.post("/documents/upload")
    async def upload(
        request: Request,
        document_id: Annotated[str, Query(min_length=1, max_length=64, pattern=_DOCUMENT_ID_PATTERN)],
        condition_code: Annotated[ConditionCode, Query()],
    ) -> dict[str, Any]:
        data = await _read_body_limited(request, MAX_UPLOAD_BYTES)
        try:
            result = await run_in_threadpool(ocr_engine.extract, data)
        except OcrUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except OcrError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception:
            logger.exception("Text extraction failed for doc=%s; document not evaluated", document_id)
            raise HTTPException(status_code=500, detail="internal error; document not evaluated") from None
        metrics.record_ocr(result)
        if not result.text:
            raise HTTPException(status_code=422, detail="no readable text found in the document")
        if len(result.text) > MAX_SNIPPET_CHARS:
            raise HTTPException(
                status_code=422,
                detail=f"extracted text is {len(result.text)} characters; the limit is {MAX_SNIPPET_CHARS}",
            )
        response = await run_in_threadpool(run_gate, document_id, result.text, condition_code, result)
        response["ocr"] = result.summary()
        response["extracted_text"] = result.text
        return response

    @v1.get("/review-queue")
    def review_queue() -> dict[str, Any]:
        pending = getattr(gate.review_queue, "pending", None)
        if not callable(pending):
            raise HTTPException(status_code=501, detail="review queue does not support listing")
        items = [asdict(ticket) for ticket in pending()]
        return {"count": len(items), "items": items}

    @v1.get("/samples")
    def samples() -> dict[str, Any]:
        return {"samples": core_samples()}

    @v1.get("/metrics")
    def get_metrics() -> dict[str, Any]:
        return metrics.snapshot()

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(ui_html, headers=UI_SECURITY_HEADERS)

    app.include_router(v1)
    return app


def _default_port() -> int:
    raw = os.environ.get(ENV_PORT, "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{ENV_PORT} must be an integer port, got {raw!r}") from exc
    if not 0 < port < 65536:
        raise ValueError(f"{ENV_PORT} must be between 1 and 65535, got {port}")
    return port


def main(argv: list[str] | None = None) -> int:
    """Console entry point ``stipgate-serve``."""
    parser = argparse.ArgumentParser(prog="stipgate-serve", description="Serve the StipGate API and test UI.")
    parser.add_argument("--host", default=os.environ.get(ENV_HOST) or DEFAULT_HOST, help="bind address")
    parser.add_argument("--port", type=int, default=_default_port(), help="bind port")
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug"],
        help="log level",
    )
    args = parser.parse_args(argv)
    host: str = args.host
    port: int = args.port
    log_level: str = args.log_level

    logging.basicConfig(level=log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if host not in _LOOPBACK_HOSTS and not os.environ.get(ENV_API_KEY):
        logger.warning(
            "Binding to non-loopback host %s without %s: the API is unauthenticated. Set %s before exposing it.",
            host,
            ENV_API_KEY,
            ENV_API_KEY,
        )
    uvicorn.run(create_app(), host=host, port=port, log_level=log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
