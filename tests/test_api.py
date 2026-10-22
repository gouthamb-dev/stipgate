"""StipGate web API tests. Offline: no Decider server, no AWS; heuristic backend opted in explicitly."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stipgate.api import MAX_SNIPPET_CHARS, ConditionCode, create_app
from stipgate.bedrock_worker import BedrockExtractor
from stipgate.decider_client import DeciderClient
from stipgate.handlers import SUPPORTED_CONDITION_CODES
from stipgate.pipeline import StipGatePipeline

EXPECTED_ACTIONS = ["PROCEED", "HALTED", "PROCEED", "HALTED"]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch a live Decider server; clear every StipGate env var."""

    def offline(*_args: Any, **_kwargs: Any) -> Any:
        raise urllib.error.URLError("offline test")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    for name in (
        "STIPGATE_DECIDER_URL",
        "STIPGATE_DECIDER_MODE",
        "STIPGATE_ALLOW_HEURISTIC_PROCEED",
        "STIPGATE_DECIDER_TIMEOUT_S",
        "STIPGATE_DECIDER_MAX_CONCURRENCY",
        "STIPGATE_API_KEY",
        "STIPGATE_BEDROCK_OFFLINE",
        "STIPGATE_HOST",
        "STIPGATE_PORT",
    ):
        monkeypatch.delenv(name, raising=False)


def heuristic_pipeline() -> StipGatePipeline:
    return StipGatePipeline(
        decider=DeciderClient(mode="heuristic", allow_heuristic_proceed=True, heuristic_as_of=None),
        extractor=BedrockExtractor(offline=True),
    )


def make_client(app: FastAPI | None = None) -> TestClient:
    return TestClient(app if app is not None else create_app(heuristic_pipeline()))


def post_samples(client: TestClient, headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
    samples = client.get("/v1/samples", headers=headers).json()["samples"]
    traces: list[dict[str, Any]] = []
    for sample in samples:
        response = client.post(
            "/v1/documents/evaluate",
            json={
                "document_id": sample["document_id"],
                "snippet": sample["snippet"],
                "condition_code": sample["condition_code"],
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        traces.append(response.json())
    return traces


def valid_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"document_id": "DOC-1", "snippet": "EARNINGS STATEMENT", "condition_code": "STIP_W2_2YR"}
    body.update(overrides)
    return body


def test_healthz_reports_heuristic_mode() -> None:
    response = make_client().get("/healthz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["decider"]["mode"] == "heuristic"
    assert data["decider"]["reachable"] is None
    assert data["decider"]["allow_heuristic_proceed"] is True
    assert data["auth_enabled"] is False
    assert data["bedrock"]["offline"] is True
    assert data["bedrock"]["client_configured"] is False
    assert "heuristic backend active: verdicts are simulated" in data["warnings"]
    assert "authentication disabled" in data["warnings"]


def test_samples_evaluate_end_to_end() -> None:
    client = make_client()
    samples = client.get("/v1/samples").json()["samples"]
    assert len(samples) == 4
    assert [s["expected_action"] for s in samples] == EXPECTED_ACTIONS

    traces = post_samples(client)
    assert [t["gated_action"] for t in traces] == EXPECTED_ACTIONS
    for trace in traces:
        assert trace["heuristic_warning"] is True
        assert trace["decider_backend"] == "heuristic"
        if trace["gated_action"] == "PROCEED":
            assert trace["clearance_status"] == "pending_extraction_review"
            assert trace["extracted_payload"] is not None
            assert trace["extraction_review_ticket_id"]
            assert trace["tokens_spent"] > 0
        else:
            assert trace["clearance_status"] == "halted"
            assert trace["tokens_spent"] == 0
            assert trace["extracted_payload"] is None
            assert trace["review_ticket_id"]


def test_review_queue_holds_halted_and_extraction_tickets() -> None:
    client = make_client()
    post_samples(client)
    data = client.get("/v1/review-queue").json()
    assert data["count"] == 4
    verdicts = sorted(item["verdict"] for item in data["items"])
    assert verdicts.count("pending_extraction_review") == 2
    assert all(len(item["snippet_preview"]) <= 240 for item in data["items"])


def test_metrics_after_samples() -> None:
    client = make_client()
    traces = post_samples(client)
    metrics = client.get("/v1/metrics").json()
    assert metrics["requests"] == 4
    assert metrics["halted"] == 2
    assert metrics["proceeded"] == 2
    assert metrics["cleared"] == 0
    assert metrics["pending_extraction_review"] == 2
    assert metrics["tokens_spent"] == sum(t["tokens_spent"] for t in traces)
    assert metrics["by_backend"] == {"heuristic": 4}
    latency = metrics["gate_latency_ms"]
    assert latency["samples"] == 4
    assert 0.0 <= latency["p50"] <= latency["p95"]


def test_audit_log_has_no_snippet_or_payload(caplog: pytest.LogCaptureFixture) -> None:
    client = make_client()
    caplog.set_level(logging.INFO, logger="stipgate.audit")
    snippet = "EARNINGS STATEMENT SECRETBORROWERNAME"
    response = client.post("/v1/documents/evaluate", json=valid_body(snippet=snippet))
    assert response.status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.name == "stipgate.audit"]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["document_id"] == "DOC-1"
    assert record["gated_action"] == "HALTED"
    assert len(record["review_ticket_ids"]) == 1
    assert "SECRETBORROWERNAME" not in lines[0]
    assert "snippet" not in record
    assert "extracted_payload" not in record


@pytest.mark.parametrize(
    "body",
    [
        valid_body(condition_code="STIP_UNKNOWN"),
        valid_body(snippet="x" * (MAX_SNIPPET_CHARS + 1)),
        valid_body(snippet=""),
        valid_body(document_id="bad id/<script>"),
        valid_body(document_id=""),
        valid_body(extra_field="nope"),
    ],
    ids=["unknown-code", "snippet-too-long", "empty-snippet", "bad-document-id", "empty-document-id", "extra-field"],
)
def test_evaluate_validation_errors(body: dict[str, Any]) -> None:
    client = make_client()
    response = client.post("/v1/documents/evaluate", json=body)
    assert response.status_code == 422
    assert client.get("/v1/metrics").json()["requests"] == 0


def test_api_key_required_on_v1_routes() -> None:
    client = make_client(create_app(heuristic_pipeline(), api_key="s3cret"))
    assert client.get("/v1/samples").status_code == 401
    wrong = client.get("/v1/samples", headers={"X-API-Key": "wrong"})
    assert wrong.status_code == 401
    assert wrong.json() == {"detail": "missing or invalid API key"}
    assert client.post("/v1/documents/evaluate", json=valid_body()).status_code == 401
    assert client.get("/v1/review-queue").status_code == 401
    assert client.get("/v1/metrics").status_code == 401
    assert client.get("/v1/samples", headers={"X-API-Key": "s3cret"}).status_code == 200
    traces = post_samples(client, headers={"X-API-Key": "s3cret"})
    assert [t["gated_action"] for t in traces] == EXPECTED_ACTIONS
    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").json()["auth_enabled"] is True
    assert client.get("/").status_code == 200


def test_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STIPGATE_API_KEY", "env-key")
    client = make_client()
    assert client.get("/v1/samples").status_code == 401
    assert client.get("/v1/samples", headers={"X-API-Key": "env-key"}).status_code == 200


def test_default_server_mode_fails_closed_through_api() -> None:
    pipeline = StipGatePipeline(decider=DeciderClient(), extractor=BedrockExtractor(offline=True))
    client = make_client(create_app(pipeline))
    response = client.post(
        "/v1/documents/evaluate",
        json=valid_body(snippet="EARNINGS STATEMENT pay period ending", condition_code="STIP_INCOME_30DAY"),
    )
    assert response.status_code == 200
    trace = response.json()
    assert trace["gated_action"] == "HALTED"
    assert trace["verdict"] == "decider_error"
    assert trace["tokens_spent"] == 0
    assert trace["extracted_payload"] is None
    assert trace["review_ticket_id"]
    assert trace["heuristic_warning"] is False

    health = client.get("/healthz").json()
    assert health["decider"]["mode"] == "server"
    assert health["decider"]["reachable"] is False
    assert "Decider server unreachable: every document will fail closed" in health["warnings"]
    assert "heuristic backend active: verdicts are simulated" not in health["warnings"]


def test_index_serves_self_contained_accessible_ui() -> None:
    response = make_client().get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    body = response.text
    assert '<html lang="en">' in body
    assert 'aria-live="polite"' in body
    assert 'role="alert"' in body
    assert "<caption" in body
    assert 'scope="col"' in body
    assert 'src="http' not in body
    assert 'href="http' not in body
    assert "innerHTML" not in body
    for code in SUPPORTED_CONDITION_CODES:
        assert f'value="{code}"' in body


def test_condition_code_literal_matches_supported_codes() -> None:
    assert get_args(ConditionCode) == SUPPORTED_CONDITION_CODES


class ExplodingPipeline(StipGatePipeline):
    def process_document(self, document_id: str, snippet: str, condition_code: str) -> dict[str, Any]:
        raise RuntimeError("database password=hunter2 leaked in message")


def test_pipeline_exception_returns_generic_500() -> None:
    pipeline = ExplodingPipeline(
        decider=DeciderClient(mode="heuristic", allow_heuristic_proceed=True),
        extractor=BedrockExtractor(offline=True),
    )
    client = make_client(create_app(pipeline))
    response = client.post("/v1/documents/evaluate", json=valid_body())
    assert response.status_code == 500
    assert response.json() == {"detail": "internal error; document not cleared"}
    assert "hunter2" not in response.text
    assert client.get("/v1/metrics").json()["requests"] == 0


def test_review_queue_without_listing_returns_501() -> None:
    class WriteOnlyQueue:
        def __init__(self) -> None:
            self.tickets: list[Any] = []

        def enqueue(self, ticket: Any) -> None:
            self.tickets.append(ticket)

    pipeline = StipGatePipeline(
        decider=DeciderClient(mode="heuristic", allow_heuristic_proceed=True),
        extractor=BedrockExtractor(offline=True),
        review_queue=WriteOnlyQueue(),
    )
    response = make_client(create_app(pipeline)).get("/v1/review-queue")
    assert response.status_code == 501
