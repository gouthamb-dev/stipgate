"""StipGate test suite. No network, no AWS credentials required."""

from __future__ import annotations

import email.message
import io
import json
import math
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from pydantic import ValidationError
from strands.hooks import BeforeToolCallEvent
from strands.interventions import Guide, Proceed

from benchmark import (
    build_core_documents,
    build_heldout_documents,
    paystub_text,
    run_core_benchmark,
    run_heldout,
    w2_text,
)
from stipgate import (
    CLEAR_CONDITION_TOOL,
    VERDICT_OPTIONS,
    BedrockExtractor,
    DeciderClient,
    DeciderError,
    DeciderResponseError,
    ExtractionResult,
    HeuristicDecider,
    IncomeClearancePayload,
    InMemoryReviewQueue,
    ReviewTicket,
    StipGatePipeline,
    StipulationGuardrailHandler,
    build_agent,
)
from stipgate.decider_client import DeciderMode, DeciderResult, DeciderUnavailableError, YesNoCheck
from stipgate.handlers import DEFAULT_CONFIDENCE_THRESHOLD, build_question

AS_OF = date(2026, 6, 15)
OPTIONS = list(VERDICT_OPTIONS)
CORE = {doc.document_id: doc for doc in build_core_documents(AS_OF)}
PAYSTUB = CORE["SG-001"].snippet
UTILITY = CORE["SG-002"].snippet
W2 = CORE["SG-003"].snippet
CORRUPTED = CORE["SG-004"].snippet


def question(code: str) -> str:
    return build_question(code)


# --------------------------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch a live Decider server, even if one is running on :8099.

    ``stipgate.decider_client`` calls ``urllib.request.urlopen`` through the module attribute,
    so patching ``urllib.request`` intercepts every Decider request.
    """

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
    ):
        monkeypatch.delenv(name, raising=False)


class FakeDecider(DeciderClient):
    """Returns a fixed server verdict and records calls."""

    def __init__(self, verdict: str = "fully_compliant", confidence: float = 0.95) -> None:
        super().__init__("http://127.0.0.1:8099/decide", heuristic_as_of=AS_OF)
        self.verdict = verdict
        self.confidence = confidence
        self.calls: list[tuple[str, str, list[str]]] = []
        self.criteria: list[Mapping[str, str | None] | None] = []

    def decide_detailed(
        self,
        text: str,
        question: str,
        options: list[str],
        *,
        criteria: Mapping[str, str | None] | None = None,
        checks: Mapping[str, YesNoCheck] | None = None,
    ) -> DeciderResult:
        self.calls.append((text, question, options))
        self.criteria.append(criteria)
        self.last_backend = "server"
        return DeciderResult(self.verdict, self.confidence, "server", "fake", 0.1, False)


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)
        self.status = 200

    def read(self) -> bytes:
        return self._body.read()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class Recorder:
    """Scripted urlopen replacement recording requests and timeouts."""

    def __init__(self, responses: list[bytes | BaseException]) -> None:
        self.responses = responses
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeResponse(item)

    def body(self, index: int = 0) -> dict[str, Any]:
        data = self.requests[index].data
        assert isinstance(data, bytes)
        parsed: dict[str, Any] = json.loads(data)
        return parsed


def heuristic_decider() -> DeciderClient:
    """Heuristic backend explicitly authorised to clear conditions (CI/demo opt-in)."""
    return DeciderClient(mode="heuristic", allow_heuristic_proceed=True, heuristic_as_of=AS_OF)


def install(monkeypatch: pytest.MonkeyPatch, responses: list[bytes | BaseException]) -> Recorder:
    recorder = Recorder(responses)
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    return recorder


def tool_input(snippet: str = PAYSTUB, code: str = "STIP_INCOME_30DAY") -> dict[str, object]:
    return {"document_id": "DOC-1", "condition_code": code, "document_snippet": snippet}


def make_event(name: str, inputs: Mapping[str, object]) -> BeforeToolCallEvent:
    return BeforeToolCallEvent(
        agent=MagicMock(),
        selected_tool=None,
        tool_use={"name": name, "input": dict(inputs), "toolUseId": "tu-1"},
        invocation_state={},
    )


class StubConverseClient:
    def __init__(self, response: Mapping[str, Any] | None = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def converse_response(text: str, usage: Mapping[str, int] | None = None) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": dict(usage) if usage is not None else {"inputTokens": 412, "outputTokens": 58, "totalTokens": 470},
        "stopReason": "end_turn",
    }


VALID_JSON = json.dumps(
    {
        "borrower_name": "Maria L. Chen",
        "employer_name": "Cascade Freight Logistics LLC",
        "gross_pay": 3300.0,
        "ytd_gross": 39600.0,
        "pay_period_end": "2026-06-10",
    }
)


class SpyExtractor(BedrockExtractor):
    def __init__(self) -> None:
        super().__init__(offline=True)
        self.calls = 0

    def extract(self, snippet: str, condition_code: str) -> ExtractionResult:
        self.calls += 1
        return super().extract(snippet, condition_code)


# --------------------------------------------------------------------------------------------
# 7a Decider client
# --------------------------------------------------------------------------------------------


def test_decider_unreachable_uses_heuristic() -> None:
    client = DeciderClient(mode="auto", heuristic_as_of=AS_OF)
    verdict, confidence = client.decide(PAYSTUB, question("STIP_INCOME_30DAY"), OPTIONS)
    assert client.last_backend == "heuristic"
    assert verdict in OPTIONS
    assert 0.0 <= confidence <= 1.0
    assert client.endpoint == "http://127.0.0.1:8099/decide"


def test_decider_default_server_mode_unreachable_raises() -> None:
    client = DeciderClient()
    assert client.mode == "server"
    with pytest.raises(DeciderUnavailableError):
        client.decide(PAYSTUB, question("STIP_INCOME_30DAY"), OPTIONS)


def test_decider_simple_protocol_server_response(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = install(monkeypatch, [json.dumps({"verdict": "fully_compliant", "confidence": 0.93}).encode()])
    client = DeciderClient()
    assert client.decide("doc text", "q?", OPTIONS) == ("fully_compliant", 0.93)
    body = rec.body()
    assert body == {"text": "doc text", "question": "q?", "options": OPTIONS}
    assert rec.timeouts == [1.5]
    assert rec.requests[0].get_method() == "POST"
    assert rec.requests[0].full_url == "http://127.0.0.1:8099/decide"
    assert client.last_backend == "server"


def test_decider_systemone_protocol_request_and_response(monkeypatch: pytest.MonkeyPatch) -> None:
    response = {
        "model": "strands-decider-2B",
        "answers": {
            "stipulation_verdict": {
                "type": "choice",
                "choice": "invalid_unrelated_doc",
                "probabilities": {"invalid_unrelated_doc": 0.97},
                "confidence": 0.955,
            }
        },
    }
    rec = install(monkeypatch, [json.dumps(response).encode()])
    client = DeciderClient("http://127.0.0.1:8099/v1/systemone")
    assert client.decide("state text", "q?", OPTIONS) == ("invalid_unrelated_doc", 0.955)
    body = rec.body()
    assert body["state"] == "state text"
    spec = body["questions"]["stipulation_verdict"]
    assert spec["type"] == "choice"
    assert spec["instructions"] == "q?"
    assert list(spec["criteria"]) == OPTIONS
    assert all(value is None for value in spec["criteria"].values())

    rec = install(monkeypatch, [json.dumps(response).encode()])
    client.decide("state text", "q?", OPTIONS, criteria={"fully_compliant": "It is a paystub."})
    sent = rec.body()["questions"]["stipulation_verdict"]["criteria"]
    assert sent == {"fully_compliant": "It is a paystub.", "invalid_unrelated_doc": None,
                    "ambiguous_requires_human_review": None}


def test_decider_reports_server_model_and_pipeline_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server's model id flows DeciderResult -> GateDecision -> trace; heuristic reports none."""
    response = {
        "model": "strands-decider-2B-qwen3.5-v1-2610@216d1cc",
        "answers": {
            "stipulation_verdict": {"type": "choice", "choice": "invalid_unrelated_doc", "confidence": 0.95},
            "has_employer_and_ytd": {"type": "noul", "noul": 0.2},
        },
    }
    install(monkeypatch, [json.dumps(response).encode()])
    pipeline = StipGatePipeline(
        decider=DeciderClient("http://127.0.0.1:8080/invocations"), extractor=SpyExtractor()
    )
    trace = pipeline.process_document("SG-002", UTILITY, "STIP_INCOME_30DAY")
    assert trace["decider_model"] == "strands-decider-2B-qwen3.5-v1-2610@216d1cc"
    assert trace["decider_backend"] == "server"
    assert trace["llm_model"] is None
    assert trace["total_latency_ms"] >= trace["decider_latency_ms"]

    heuristic = heuristic_decider().decide_detailed(UTILITY, "q?", OPTIONS)
    assert heuristic.model is None


def test_pipeline_real_extraction_reports_llm_model_and_total_latency() -> None:
    extractor = BedrockExtractor(client=StubConverseClient(converse_response(VALID_JSON)))
    pipeline = StipGatePipeline(decider=heuristic_decider(), extractor=extractor)
    trace = pipeline.process_document("SG-001", PAYSTUB, "STIP_INCOME_30DAY")
    assert trace["clearance_status"] == "cleared"
    assert trace["llm_model"] == "amazon.nova-lite-v1:0"
    assert trace["total_latency_ms"] >= trace["latency_ms"]


def test_decider_404_retries_systemone(monkeypatch: pytest.MonkeyPatch) -> None:
    not_found = urllib.error.HTTPError(
        "http://127.0.0.1:8099/decide", 404, "Not Found", email.message.Message(), None
    )
    ok = {"answers": {"stipulation_verdict": {"choice": "fully_compliant", "confidence": 0.91}}}
    rec = install(monkeypatch, [not_found, json.dumps(ok).encode()])
    client = DeciderClient()
    assert client.decide("t", "q", OPTIONS) == ("fully_compliant", 0.91)
    assert rec.requests[1].full_url == "http://127.0.0.1:8099/v1/systemone"
    assert client.endpoint == "http://127.0.0.1:8099/v1/systemone"


def http_error(code: int, url: str = "http://127.0.0.1:8099/decide") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", email.message.Message(), None)


def test_decider_5xx_falls_back_to_heuristic(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, [http_error(503)])
    client = DeciderClient(mode="auto", heuristic_as_of=AS_OF)
    verdict, _ = client.decide(UTILITY, question("STIP_INCOME_30DAY"), OPTIONS)
    assert client.last_backend == "heuristic"
    assert verdict == "invalid_unrelated_doc"


def test_decider_5xx_server_mode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, [http_error(503)])
    with pytest.raises(DeciderUnavailableError):
        DeciderClient(heuristic_as_of=AS_OF).decide(UTILITY, question("STIP_INCOME_30DAY"), OPTIONS)


@pytest.mark.parametrize("mode", ["server", "auto"])
def test_decider_http_400_raises_decider_error(monkeypatch: pytest.MonkeyPatch, mode: DeciderMode) -> None:
    install(monkeypatch, [http_error(400)])
    client = DeciderClient(mode=mode, heuristic_as_of=AS_OF)
    with pytest.raises(DeciderError) as info:
        client.decide(PAYSTUB, question("STIP_INCOME_30DAY"), OPTIONS)
    assert not isinstance(info.value, DeciderUnavailableError)
    assert client.last_backend is None  # never answered by the heuristic


def test_decider_404_on_systemone_retry_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = install(monkeypatch, [http_error(404), http_error(404, "http://127.0.0.1:8099/v1/systemone")])
    client = DeciderClient(mode="auto", heuristic_as_of=AS_OF)
    with pytest.raises(DeciderError) as info:
        client.decide(PAYSTUB, question("STIP_INCOME_30DAY"), OPTIONS)
    assert not isinstance(info.value, DeciderUnavailableError)
    assert len(rec.requests) == 2
    assert rec.requests[1].full_url == "http://127.0.0.1:8099/v1/systemone"
    assert client.last_backend is None


def test_decider_mode_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = install(monkeypatch, [])
    monkeypatch.setenv("STIPGATE_DECIDER_MODE", "heuristic")
    client = DeciderClient(heuristic_as_of=AS_OF)
    assert client.mode == "heuristic"
    result = client.decide_detailed(PAYSTUB, question("STIP_INCOME_30DAY"), OPTIONS)
    assert result.backend == "heuristic"
    assert rec.requests == []


def test_decider_invalid_mode_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        DeciderClient(mode="bogus")  # type: ignore[arg-type]
    monkeypatch.setenv("STIPGATE_DECIDER_MODE", "bogus")
    with pytest.raises(ValueError, match="STIPGATE_DECIDER_MODE"):
        DeciderClient()


def test_decider_timeout_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = install(monkeypatch, [json.dumps({"verdict": "fully_compliant", "confidence": 0.93}).encode()])
    monkeypatch.setenv("STIPGATE_DECIDER_TIMEOUT_S", "4.5")
    client = DeciderClient()
    assert client.timeout_s == 4.5
    client.decide("t", "q", OPTIONS)
    assert rec.timeouts == [4.5]
    assert DeciderClient(timeout_s=2.0).timeout_s == 2.0
    for bad in ("abc", "0", "-1", "inf"):
        monkeypatch.setenv("STIPGATE_DECIDER_TIMEOUT_S", bad)
        with pytest.raises(ValueError):
            DeciderClient()


def test_decider_max_concurrency_config(monkeypatch: pytest.MonkeyPatch) -> None:
    assert DeciderClient().max_concurrency == 1
    monkeypatch.setenv("STIPGATE_DECIDER_MAX_CONCURRENCY", "4")
    assert DeciderClient().max_concurrency == 4
    assert DeciderClient(max_concurrency=2).max_concurrency == 2
    for bad in ("0", "two"):
        monkeypatch.setenv("STIPGATE_DECIDER_MAX_CONCURRENCY", bad)
        with pytest.raises(ValueError, match="STIPGATE_DECIDER_MAX_CONCURRENCY"):
            DeciderClient()


def test_decider_allow_heuristic_proceed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert DeciderClient().allow_heuristic_proceed is False
    for truthy in ("1", "TRUE", "yes"):
        monkeypatch.setenv("STIPGATE_ALLOW_HEURISTIC_PROCEED", truthy)
        assert DeciderClient().allow_heuristic_proceed is True
    assert DeciderClient(allow_heuristic_proceed=False).allow_heuristic_proceed is False
    monkeypatch.setenv("STIPGATE_ALLOW_HEURISTIC_PROCEED", "0")
    assert DeciderClient().allow_heuristic_proceed is False


def test_decide_detailed_returns_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    heuristic = DeciderClient(mode="heuristic", allow_heuristic_proceed=True, heuristic_as_of=AS_OF)
    result = heuristic.decide_detailed(PAYSTUB, question("STIP_INCOME_30DAY"), OPTIONS)
    assert result.backend == "heuristic"
    assert result.verdict == "fully_compliant"
    assert result.heuristic_proceed_allowed is True
    assert result.rationale
    assert result.latency_ms >= 0.0

    install(monkeypatch, [json.dumps({"verdict": "invalid_unrelated_doc", "confidence": 0.9}).encode()])
    server = DeciderClient().decide_detailed("t", "q", OPTIONS)
    assert (server.verdict, server.confidence, server.backend) == ("invalid_unrelated_doc", 0.9, "server")
    assert server.heuristic_proceed_allowed is False


def test_probe_returns_none_in_heuristic_mode_and_false_when_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    assert DeciderClient(mode="heuristic").probe() is None
    assert DeciderClient().probe() is False
    rec = install(monkeypatch, [b"{}"])
    assert DeciderClient("http://127.0.0.1:8099/v1/systemone").probe(timeout_s=0.2) is True
    assert rec.requests[0].full_url == "http://127.0.0.1:8099/health"
    assert rec.requests[0].get_method() == "GET"
    assert rec.timeouts == [0.2]


def test_decider_invalid_server_verdict_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, [json.dumps({"verdict": "yes", "confidence": 0.99}).encode()])
    with pytest.raises(DeciderResponseError):
        DeciderClient().decide("t", "q", OPTIONS)


@pytest.mark.parametrize("confidence", [1.7, "NaN", -0.1, True, None, "0.93"])
def test_decider_invalid_confidence_raises(monkeypatch: pytest.MonkeyPatch, confidence: object) -> None:
    raw = json.dumps({"verdict": "fully_compliant", "confidence": confidence}).replace('"NaN"', "NaN")
    install(monkeypatch, [raw.encode()])
    with pytest.raises(DeciderResponseError):
        DeciderClient().decide("t", "q", OPTIONS)


def test_decider_non_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, [b"<html>oops</html>"])
    with pytest.raises(DeciderResponseError):
        DeciderClient().decide("t", "q", OPTIONS)


@pytest.mark.parametrize("options", [[], ["a", "a"], ["a", ""]])
def test_decider_rejects_bad_options(options: list[str]) -> None:
    with pytest.raises(ValueError):
        DeciderClient().decide("t", "q", options)


def test_decider_offline_circuit_breaker_skips_server(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = install(monkeypatch, [urllib.error.URLError("refused")])
    client = DeciderClient(mode="auto", heuristic_as_of=AS_OF)
    client.decide(PAYSTUB, question("STIP_INCOME_30DAY"), OPTIONS)
    client.decide(PAYSTUB, question("STIP_INCOME_30DAY"), OPTIONS)
    assert len(rec.requests) == 1
    assert client.last_backend == "heuristic"


def test_heuristic_valid_paystub_compliant() -> None:
    verdict, confidence = HeuristicDecider().decide(PAYSTUB, question("STIP_INCOME_30DAY"), OPTIONS, AS_OF)
    assert verdict == "fully_compliant"
    assert confidence >= 0.88


def test_heuristic_valid_w2_compliant() -> None:
    verdict, confidence = HeuristicDecider().decide(W2, question("STIP_W2_2YR"), OPTIONS, AS_OF)
    assert verdict == "fully_compliant"
    assert confidence >= 0.88


def test_heuristic_stale_paystub_ambiguous() -> None:
    stale = build_core_documents(AS_OF - timedelta(days=90))[0].snippet
    heuristic = HeuristicDecider()
    verdict, _ = heuristic.decide(stale, question("STIP_INCOME_30DAY"), OPTIONS, AS_OF)
    assert verdict == "ambiguous_requires_human_review"
    assert "stale" in heuristic.last_rationale


def test_heuristic_single_year_w2_ambiguous() -> None:
    single = w2_text("Maria L. Chen", "Cascade Freight Logistics LLC", "12-3456789", {AS_OF.year - 1: 81240.0})
    verdict, confidence = HeuristicDecider().decide(single, question("STIP_W2_2YR"), OPTIONS, AS_OF)
    assert verdict == "ambiguous_requires_human_review"
    assert confidence < 0.88


def test_heuristic_utility_bill_unrelated() -> None:
    verdict, confidence = HeuristicDecider().decide(UTILITY, question("STIP_INCOME_30DAY"), OPTIONS, AS_OF)
    assert verdict == "invalid_unrelated_doc"
    assert confidence >= 0.9


def test_heuristic_corrupted_scan_ambiguous() -> None:
    verdict, _ = HeuristicDecider().decide(CORRUPTED, question("STIP_INCOME_30DAY"), OPTIONS, AS_OF)
    assert verdict == "ambiguous_requires_human_review"


def test_heuristic_unknown_condition_ambiguous() -> None:
    verdict, confidence = HeuristicDecider().decide(PAYSTUB, question("STIP_FLOOD_CERT"), OPTIONS, AS_OF)
    assert verdict == "ambiguous_requires_human_review"
    assert confidence < 0.88


# --------------------------------------------------------------------------------------------
# 7b Handler
# --------------------------------------------------------------------------------------------


def make_handler(decider: DeciderClient, threshold: float = 0.88) -> tuple[StipulationGuardrailHandler, InMemoryReviewQueue]:
    queue = InMemoryReviewQueue()
    return StipulationGuardrailHandler(decider, confidence_threshold=threshold, review_queue=queue), queue


def test_threshold_087_returns_guide() -> None:
    handler, queue = make_handler(FakeDecider("fully_compliant", 0.87))
    result = handler.evaluate(CLEAR_CONDITION_TOOL, tool_input())
    assert isinstance(result, Guide)
    assert "Zero LLM tokens spent" in result.feedback
    assert len(queue) == 1
    assert handler.last_decision is not None
    assert handler.last_decision.action == "HALTED"
    assert handler.last_decision.tokens_spent == 0


def test_threshold_089_returns_proceed() -> None:
    handler, queue = make_handler(FakeDecider("fully_compliant", 0.89))
    assert isinstance(handler.evaluate(CLEAR_CONDITION_TOOL, tool_input()), Proceed)
    assert len(queue) == 0
    assert handler.last_decision is not None and handler.last_decision.action == "PROCEED"


def test_threshold_exactly_088_returns_proceed() -> None:
    handler, _ = make_handler(FakeDecider("fully_compliant", 0.88))
    assert isinstance(handler.evaluate(CLEAR_CONDITION_TOOL, tool_input()), Proceed)


def test_handler_sends_typed_question_and_options() -> None:
    fake = FakeDecider("fully_compliant", 0.95)
    handler, _ = make_handler(fake)
    handler.evaluate(CLEAR_CONDITION_TOOL, tool_input(code="stip_w2_2yr"))
    text, asked, options = fake.calls[0]
    assert text == PAYSTUB
    assert asked == (
        "Stipulation STIP_W2_2YR: classify this document for the mortgage condition that requires "
        "IRS Form W-2 wage statements for the borrower covering two tax years."
    )
    assert options == ["fully_compliant", "invalid_unrelated_doc", "ambiguous_requires_human_review"]
    sent = fake.criteria[0]
    assert sent is not None and set(sent) == set(options)
    assert "IRS Form W-2" in str(sent["fully_compliant"])


def test_custom_threshold_respected() -> None:
    handler, _ = make_handler(FakeDecider("fully_compliant", 0.93), threshold=0.95)
    assert isinstance(handler.evaluate(CLEAR_CONDITION_TOOL, tool_input()), Guide)


@pytest.mark.parametrize("threshold", [0.0, -0.1, 1.5, math.nan])
def test_invalid_threshold_rejected(threshold: float) -> None:
    with pytest.raises(ValueError):
        StipulationGuardrailHandler(FakeDecider(), confidence_threshold=threshold)


@pytest.mark.parametrize("verdict", ["invalid_unrelated_doc", "ambiguous_requires_human_review"])
def test_non_compliant_verdict_high_confidence_guides(verdict: str) -> None:
    handler, _ = make_handler(FakeDecider(verdict, 0.99))
    assert isinstance(handler.evaluate(CLEAR_CONDITION_TOOL, tool_input()), Guide)


def test_utility_bill_returns_guide_via_heuristic() -> None:
    decider = DeciderClient(mode="auto", heuristic_as_of=AS_OF)
    handler, queue = make_handler(decider)
    assert isinstance(handler.evaluate(CLEAR_CONDITION_TOOL, tool_input(UTILITY)), Guide)
    assert decider.last_backend == "heuristic"
    assert handler.last_decision is not None
    assert handler.last_decision.verdict == "invalid_unrelated_doc"
    assert queue.pending()[0].verdict == "invalid_unrelated_doc"


def test_corrupted_scan_returns_guide_via_heuristic() -> None:
    handler, _ = make_handler(DeciderClient(mode="auto", heuristic_as_of=AS_OF))
    assert isinstance(handler.evaluate(CLEAR_CONDITION_TOOL, tool_input(CORRUPTED)), Guide)
    assert handler.last_decision is not None
    assert handler.last_decision.verdict == "ambiguous_requires_human_review"


def test_non_target_tool_bypasses_decider() -> None:
    decider = MagicMock(spec=DeciderClient)
    handler = StipulationGuardrailHandler(decider)
    assert isinstance(handler.evaluate("get_loan_status", {"loan_id": "L-1"}), Proceed)
    assert isinstance(handler.before_tool_call(make_event("get_loan_status", {"loan_id": "L-1"})), Proceed)
    action, decision = handler.evaluate_with_decision("get_loan_status", {"loan_id": "L-1"})
    assert isinstance(action, Proceed) and decision is None
    decider.decide_detailed.assert_not_called()
    decider.decide.assert_not_called()
    assert handler.last_decision is None


def test_decider_exception_fails_closed() -> None:
    decider = MagicMock(spec=DeciderClient)
    decider.decide_detailed.side_effect = RuntimeError("decider exploded")
    decider.last_backend = None
    handler, queue = make_handler(decider)
    result = handler.evaluate(CLEAR_CONDITION_TOOL, tool_input())
    assert isinstance(result, Guide)
    assert len(queue) == 1
    assert handler.last_decision is not None
    assert handler.last_decision.verdict == "decider_error"
    assert handler.last_decision.tokens_spent == 0


def test_server_unreachable_default_mode_handler_guides() -> None:
    handler, queue = make_handler(DeciderClient())
    action, decision = handler.evaluate_with_decision(CLEAR_CONDITION_TOOL, tool_input())
    assert isinstance(action, Guide)
    assert decision is not None
    assert decision.verdict == "decider_error"
    assert decision.action == "HALTED"
    assert decision.tokens_spent == 0
    assert "DeciderUnavailableError" in decision.reason
    assert len(queue) == 1 and queue.pending()[0].ticket_id == decision.ticket_id


def test_heuristic_compliant_without_opt_in_guides() -> None:
    handler, queue = make_handler(DeciderClient(mode="heuristic", heuristic_as_of=AS_OF))
    action, decision = handler.evaluate_with_decision(CLEAR_CONDITION_TOOL, tool_input(PAYSTUB))
    assert isinstance(action, Guide)
    assert decision is not None
    assert decision.verdict == "fully_compliant"
    assert decision.action == "HALTED"
    assert decision.backend == "heuristic"
    assert "not authorised" in decision.reason
    assert len(queue) == 1


def test_auto_mode_outage_never_proceeds() -> None:
    handler, _ = make_handler(DeciderClient(mode="auto", heuristic_as_of=AS_OF))
    for snippet, code in ((PAYSTUB, "STIP_INCOME_30DAY"), (W2, "STIP_W2_2YR")):
        action, decision = handler.evaluate_with_decision(CLEAR_CONDITION_TOOL, tool_input(snippet, code))
        assert isinstance(action, Guide)
        assert decision is not None and decision.action == "HALTED" and decision.backend == "heuristic"


def test_heuristic_with_opt_in_proceeds() -> None:
    handler, queue = make_handler(heuristic_decider())
    action, decision = handler.evaluate_with_decision(CLEAR_CONDITION_TOOL, tool_input(PAYSTUB))
    assert isinstance(action, Proceed)
    assert decision is not None and decision.action == "PROCEED" and decision.backend == "heuristic"
    assert len(queue) == 0


def test_handler_http_400_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, [http_error(400)])
    handler, queue = make_handler(DeciderClient(mode="auto", heuristic_as_of=AS_OF))
    action, decision = handler.evaluate_with_decision(CLEAR_CONDITION_TOOL, tool_input())
    assert isinstance(action, Guide)
    assert decision is not None and decision.verdict == "decider_error"
    assert "HTTP 400" in decision.reason
    assert len(queue) == 1


def test_handler_404_on_systemone_retry_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = install(monkeypatch, [http_error(404), http_error(404, "http://127.0.0.1:8099/v1/systemone")])
    handler, _ = make_handler(DeciderClient(mode="auto", heuristic_as_of=AS_OF))
    action, decision = handler.evaluate_with_decision(CLEAR_CONDITION_TOOL, tool_input())
    assert isinstance(action, Guide)
    assert decision is not None and decision.verdict == "decider_error"
    assert len(rec.requests) == 2


def test_server_string_confidence_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, [json.dumps({"verdict": "fully_compliant", "confidence": "0.99"}).encode()])
    handler, _ = make_handler(DeciderClient())
    action, decision = handler.evaluate_with_decision(CLEAR_CONDITION_TOOL, tool_input())
    assert isinstance(action, Guide)
    assert decision is not None and decision.verdict == "decider_error"
    assert "DeciderResponseError" in decision.reason


def test_evaluate_with_decision_returns_per_call_decision() -> None:
    handler, _ = make_handler(FakeDecider("fully_compliant", 0.95))
    first_action, first = handler.evaluate_with_decision(CLEAR_CONDITION_TOOL, tool_input(), tool_use_id="tu-a")
    handler.decider = FakeDecider("invalid_unrelated_doc", 0.99)
    second_action, second = handler.evaluate_with_decision(
        CLEAR_CONDITION_TOOL, {**tool_input(UTILITY), "document_id": "DOC-2"}, tool_use_id="tu-b"
    )
    assert isinstance(first_action, Proceed) and isinstance(second_action, Guide)
    assert first is not None and second is not None
    assert (first.tool_use_id, first.document_id, first.action, first.backend) == ("tu-a", "DOC-1", "PROCEED", "server")
    assert (second.tool_use_id, second.document_id, second.action) == ("tu-b", "DOC-2", "HALTED")
    assert first.decider_latency_ms == 0.1
    assert handler.last_decision is second
    assert handler.decisions == [first, second]


def test_missing_snippet_fails_closed() -> None:
    fake = FakeDecider()
    handler, _ = make_handler(fake)
    assert isinstance(handler.evaluate(CLEAR_CONDITION_TOOL, {"condition_code": "STIP_INCOME_30DAY"}), Guide)
    assert fake.calls == []


@pytest.mark.parametrize("code", ["", "INCOME", "STIP_", 42])
def test_bad_condition_code_fails_closed(code: object) -> None:
    fake = FakeDecider()
    handler, _ = make_handler(fake)
    result = handler.evaluate(CLEAR_CONDITION_TOOL, {"condition_code": code, "document_snippet": PAYSTUB})
    assert isinstance(result, Guide)
    assert fake.calls == []


@pytest.mark.parametrize(("verdict", "confidence"), [("approve", 0.99), ("fully_compliant", 1.2), ("fully_compliant", math.nan)])
def test_out_of_options_verdict_fails_closed(verdict: str, confidence: float) -> None:
    handler, _ = make_handler(FakeDecider(verdict, confidence))
    assert isinstance(handler.evaluate(CLEAR_CONDITION_TOOL, tool_input()), Guide)
    assert handler.last_decision is not None and handler.last_decision.verdict == "decider_error"


def test_queue_failure_still_guides() -> None:
    class BrokenQueue:
        def enqueue(self, ticket: ReviewTicket) -> None:
            raise OSError("queue down")

    handler = StipulationGuardrailHandler(FakeDecider("invalid_unrelated_doc", 0.99), review_queue=BrokenQueue())
    assert isinstance(handler.evaluate(CLEAR_CONDITION_TOOL, tool_input()), Guide)


def test_before_tool_call_event_returns_guide() -> None:
    handler, queue = make_handler(FakeDecider("ambiguous_requires_human_review", 0.9))
    result = handler.before_tool_call(make_event(CLEAR_CONDITION_TOOL, tool_input()))
    assert isinstance(result, Guide)
    assert queue.pending()[0].tool_use_id == "tu-1"


def test_before_tool_call_non_mapping_input_fails_closed() -> None:
    handler, _ = make_handler(FakeDecider())
    event = BeforeToolCallEvent(
        agent=MagicMock(),
        selected_tool=None,
        tool_use={"name": CLEAR_CONDITION_TOOL, "input": "not a dict", "toolUseId": "tu-2"},
        invocation_state={},
    )
    assert isinstance(handler.before_tool_call(event), Guide)


def test_handler_on_error_is_deny_and_name() -> None:
    handler = StipulationGuardrailHandler(FakeDecider())
    assert handler.on_error == "deny"
    assert handler.name == "stipgate-stipulation-guardrail"


# --------------------------------------------------------------------------------------------
# 7c Bedrock worker
# --------------------------------------------------------------------------------------------


PAYLOAD_FIELDS: dict[str, Any] = {
    "borrower_name": "Maria L. Chen",
    "employer_name": "Cascade Freight Logistics LLC",
    "gross_pay": 3300.0,
    "ytd_gross": 39600.0,
    "pay_period_end": "2026-06-10",
}


def test_payload_valid_parses() -> None:
    payload = IncomeClearancePayload.model_validate(PAYLOAD_FIELDS)
    assert payload.gross_pay == 3300.0
    assert payload.pay_period_end == "2026-06-10"


def test_payload_us_date_normalized() -> None:
    payload = IncomeClearancePayload.model_validate({**PAYLOAD_FIELDS, "pay_period_end": "6/10/2026"})
    assert payload.pay_period_end == "2026-06-10"


@pytest.mark.parametrize(
    "overrides",
    [
        {"gross_pay": -1.0},
        {"ytd_gross": 100.0},
        {"pay_period_end": "2026-02-30"},
        {"pay_period_end": "June 10th"},
        {"borrower_name": "12345"},
        {"gross_pay": "lots"},
        {"extra_field": "x"},
    ],
)
def test_payload_invalid_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        IncomeClearancePayload.model_validate({**PAYLOAD_FIELDS, **overrides})


def test_payload_missing_field_rejected() -> None:
    fields = dict(PAYLOAD_FIELDS)
    del fields["employer_name"]
    with pytest.raises(ValidationError):
        IncomeClearancePayload.model_validate(fields)


def test_extractor_parses_converse_response() -> None:
    stub = StubConverseClient(converse_response(f"```json\n{VALID_JSON}\n```"))
    result = BedrockExtractor(client=stub).extract(PAYSTUB, "STIP_INCOME_30DAY")
    assert result.is_fallback is False
    assert result.payload.borrower_name == "Maria L. Chen"
    assert (result.input_tokens, result.output_tokens, result.total_tokens) == (412, 58, 470)
    call = stub.calls[0]
    assert call["modelId"] == "amazon.nova-lite-v1:0"
    assert call["messages"][0]["role"] == "user"
    assert PAYSTUB in call["messages"][0]["content"][0]["text"]
    assert call["inferenceConfig"]["temperature"] == 0.0


def test_extractor_client_error_falls_back() -> None:
    error = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "Converse")
    result = BedrockExtractor(client=StubConverseClient(error=error)).extract(PAYSTUB, "STIP_INCOME_30DAY")
    assert result.is_fallback is True
    assert result.fallback_reason is not None and "ClientError" in result.fallback_reason
    assert result.payload.borrower_name == "Maria L. Chen"
    assert result.payload.employer_name == "Cascade Freight Logistics LLC"
    assert result.payload.gross_pay == 3300.0
    assert result.synthetic_fields == ()


def test_extractor_offline_fallback_marks_synthetic_fields() -> None:
    no_employer = "\n".join(line for line in PAYSTUB.splitlines() if not line.startswith("Employer:"))
    result = BedrockExtractor(offline=True).extract(no_employer, "STIP_INCOME_30DAY")
    assert result.is_fallback is True
    assert "employer_name" in result.synthetic_fields
    assert result.payload.employer_name == "SYNTHETIC FALLBACK EMPLOYER"
    assert "borrower_name" not in result.synthetic_fields

    bare = BedrockExtractor(offline=True).extract("EARNINGS STATEMENT with no recognisable fields", "STIP_INCOME_30DAY")
    assert bare.synthetic_fields == ("borrower_name", "employer_name", "gross_pay", "ytd_gross", "pay_period_end")
    assert bare.payload.borrower_name == "SYNTHETIC FALLBACK BORROWER"


def test_extractor_no_credentials_falls_back() -> None:
    extractor = BedrockExtractor(client=StubConverseClient(error=NoCredentialsError()))
    result = extractor.extract(W2, "STIP_W2_2YR")
    assert result.is_fallback is True
    assert result.payload.gross_pay == 81240.0
    assert result.payload.pay_period_end == f"{AS_OF.year - 1}-12-31"
    assert extractor.client is None


def test_extractor_malformed_json_falls_back_keeps_usage() -> None:
    stub = StubConverseClient(converse_response("Sure! The borrower is Maria.", {"inputTokens": 300, "outputTokens": 9}))
    result = BedrockExtractor(client=stub).extract(PAYSTUB, "STIP_INCOME_30DAY")
    assert result.is_fallback is True
    assert (result.input_tokens, result.output_tokens, result.total_tokens) == (300, 9, 309)


def test_extractor_offline_fallback_estimates_tokens() -> None:
    result = BedrockExtractor(offline=True).extract(PAYSTUB, "STIP_INCOME_30DAY")
    assert result.is_fallback is True
    assert result.fallback_reason == "offline"
    assert result.total_tokens == result.input_tokens + result.output_tokens > 0
    assert result.payload.pay_period_end == (AS_OF - timedelta(days=5)).isoformat()


# --------------------------------------------------------------------------------------------
# 7d Pipeline / Agent
# --------------------------------------------------------------------------------------------

REQUIRED_TRACE_KEYS = {
    "document_id",
    "latency_ms",
    "verdict",
    "confidence",
    "gated_action",
    "tokens_spent",
    "extracted_payload",
}


def test_pipeline_halted_trace_zero_tokens() -> None:
    spy = SpyExtractor()
    pipeline = StipGatePipeline(decider=DeciderClient(mode="auto", heuristic_as_of=AS_OF), extractor=spy)
    trace = pipeline.process_document("SG-002", UTILITY, "STIP_INCOME_30DAY")
    assert REQUIRED_TRACE_KEYS <= trace.keys()
    assert trace["gated_action"] == "HALTED"
    assert trace["tokens_spent"] == 0
    assert trace["extracted_payload"] is None
    assert trace["verdict"] == "invalid_unrelated_doc"
    assert trace["clearance_status"] == "halted"
    assert trace["extraction_review_ticket_id"] is None
    assert spy.calls == 0


def test_pipeline_proceed_trace_has_payload() -> None:
    spy = SpyExtractor()
    queue = InMemoryReviewQueue()
    pipeline = StipGatePipeline(decider=heuristic_decider(), extractor=spy, review_queue=queue)
    trace = pipeline.process_document("SG-001", PAYSTUB, "STIP_INCOME_30DAY")
    assert REQUIRED_TRACE_KEYS <= trace.keys()
    assert trace["gated_action"] == "PROCEED"
    assert trace["verdict"] == "fully_compliant"
    assert trace["tokens_spent"] > 0
    assert trace["latency_ms"] >= 0
    assert trace["extracted_payload"]["borrower_name"] == "Maria L. Chen"
    assert spy.calls == 1
    # Offline fallback extraction never clears the condition.
    assert trace["clearance_status"] == "pending_extraction_review"
    assert trace["extraction_fallback_reason"] == "offline"
    assert trace["extraction_synthetic_fields"] == []
    tickets = queue.pending()
    assert len(tickets) == 1
    assert trace["extraction_review_ticket_id"] == tickets[0].ticket_id
    assert tickets[0].verdict == "pending_extraction_review"
    assert tickets[0].document_id == "SG-001"
    assert tickets[0].confidence == trace["confidence"]
    assert "offline" in tickets[0].reason


def test_pipeline_real_extraction_cleared() -> None:
    queue = InMemoryReviewQueue()
    extractor = BedrockExtractor(client=StubConverseClient(converse_response(VALID_JSON)))
    pipeline = StipGatePipeline(decider=heuristic_decider(), extractor=extractor, review_queue=queue)
    trace = pipeline.process_document("SG-001", PAYSTUB, "STIP_INCOME_30DAY")
    assert trace["gated_action"] == "PROCEED"
    assert trace["clearance_status"] == "cleared"
    assert trace["extraction_is_fallback"] is False
    assert trace["extraction_review_ticket_id"] is None
    assert trace["tokens_spent"] == 470
    assert len(queue) == 0


def test_pipeline_extraction_review_enqueue_failure_still_pending() -> None:
    class BrokenQueue:
        def enqueue(self, ticket: ReviewTicket) -> None:
            raise OSError("queue down")

    pipeline = StipGatePipeline(decider=heuristic_decider(), extractor=SpyExtractor(), review_queue=BrokenQueue())
    trace = pipeline.process_document("SG-001", PAYSTUB, "STIP_INCOME_30DAY")
    assert trace["clearance_status"] == "pending_extraction_review"
    assert trace["extraction_review_ticket_id"] is None


class MappingDecider(DeciderClient):
    """Maps each snippet to a fixed server verdict, sleeping randomly to force thread interleaving."""

    def __init__(self, mapping: dict[str, tuple[str, float]]) -> None:
        super().__init__("http://127.0.0.1:8099/decide", heuristic_as_of=AS_OF)
        self.mapping = mapping

    def decide_detailed(
        self,
        text: str,
        question: str,
        options: list[str],
        *,
        criteria: Mapping[str, str | None] | None = None,
        checks: Mapping[str, YesNoCheck] | None = None,
    ) -> DeciderResult:
        verdict, confidence = self.mapping[text]
        time.sleep(random.uniform(0, 0.003))
        return DeciderResult(verdict, confidence, "server", "mapping", 1.0, False)


def test_pipeline_concurrent_traces_match_documents() -> None:
    expected = {
        "SG-001": ("fully_compliant", 0.95),
        "SG-002": ("invalid_unrelated_doc", 0.97),
        "SG-003": ("fully_compliant", 0.75),
        "SG-004": ("ambiguous_requires_human_review", 0.93),
    }
    decider = MappingDecider({CORE[doc_id].snippet: answer for doc_id, answer in expected.items()})
    pipeline = StipGatePipeline(decider=decider, extractor=BedrockExtractor(offline=True))
    doc_ids = list(expected)
    jobs = [(f"{doc_ids[i % 4]}-{i}", doc_ids[i % 4]) for i in range(48)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(
                pipeline.process_document, job_id, CORE[doc_id].snippet, CORE[doc_id].condition_code
            ): (job_id, doc_id)
            for job_id, doc_id in jobs
        }
        results = [(futures[f], f.result()) for f in futures]

    halted_tickets: list[str] = []
    for (job_id, doc_id), trace in results:
        verdict, confidence = expected[doc_id]
        assert trace["document_id"] == job_id
        assert trace["verdict"] == verdict
        assert trace["confidence"] == confidence
        should_proceed = verdict == "fully_compliant" and confidence >= DEFAULT_CONFIDENCE_THRESHOLD
        assert trace["gated_action"] == ("PROCEED" if should_proceed else "HALTED")
        if should_proceed:
            assert trace["extracted_payload"] is not None
            assert trace["review_ticket_id"] is None
        else:
            assert trace["tokens_spent"] == 0
            assert trace["extracted_payload"] is None
            halted_tickets.append(trace["review_ticket_id"])
    assert len(halted_tickets) == 36
    assert len(set(halted_tickets)) == len(halted_tickets)


def test_pipeline_fail_closed_trace() -> None:
    decider = MagicMock(spec=DeciderClient)
    decider.decide_detailed.side_effect = TimeoutError("slow")
    decider.last_backend = None
    pipeline = StipGatePipeline(decider=decider, extractor=SpyExtractor())
    trace = pipeline.process_document("X", PAYSTUB, "STIP_INCOME_30DAY")
    assert trace["gated_action"] == "HALTED"
    assert trace["verdict"] == "decider_error"
    assert trace["tokens_spent"] == 0


def _direct_call(pipeline: StipGatePipeline) -> Any:
    agent = build_agent(pipeline)
    return agent.tool.clear_underwriting_condition(
        document_id="SG-001",
        condition_code="STIP_INCOME_30DAY",
        document_snippet=PAYSTUB,
        record_direct_tool_call=False,
    )


def test_agent_direct_tool_call_halted_by_intervention() -> None:
    spy = SpyExtractor()
    pipeline = StipGatePipeline(decider=FakeDecider("fully_compliant", 0.5), extractor=spy)
    result = _direct_call(pipeline)
    assert result["status"] == "error"
    assert result["content"][0]["text"].startswith("GUIDANCE:")
    assert spy.calls == 0
    assert len(pipeline.review_queue.pending()) == 1  # type: ignore[attr-defined]


def test_agent_direct_tool_call_proceeds() -> None:
    spy = SpyExtractor()
    queue = InMemoryReviewQueue()
    pipeline = StipGatePipeline(decider=FakeDecider("fully_compliant", 0.95), extractor=spy, review_queue=queue)
    result = _direct_call(pipeline)
    assert result["status"] == "success"
    assert spy.calls == 1
    content = str(result["content"][0])
    assert "pending_extraction_review" in content
    assert "'cleared'" not in content and '"cleared"' not in content
    tickets = queue.pending()
    assert len(tickets) == 1 and tickets[0].verdict == "pending_extraction_review"


def test_agent_direct_tool_call_real_extraction_cleared() -> None:
    queue = InMemoryReviewQueue()
    extractor = BedrockExtractor(client=StubConverseClient(converse_response(VALID_JSON)))
    pipeline = StipGatePipeline(decider=FakeDecider("fully_compliant", 0.95), extractor=extractor, review_queue=queue)
    result = _direct_call(pipeline)
    assert result["status"] == "success"
    content = str(result["content"][0])
    assert "cleared" in content and "pending_extraction_review" not in content
    assert len(queue) == 0


# --------------------------------------------------------------------------------------------
# 7e Benchmark
# --------------------------------------------------------------------------------------------


def test_core_benchmark_actions() -> None:
    report = run_core_benchmark(offline=True, as_of=AS_OF, decider_mode="heuristic", allow_heuristic_proceed=True)
    assert [r.gated_action for r in report.rows] == ["PROCEED", "HALTED", "PROCEED", "HALTED"]
    assert all(r.tokens_spent == 0 for r in report.rows if r.gated_action == "HALTED")
    assert all(r.tokens_spent > 0 for r in report.rows if r.gated_action == "PROCEED")
    assert report.all_expected
    assert report.backends == {"heuristic": 4}


def test_heldout_benchmark_metrics() -> None:
    docs = build_heldout_documents(AS_OF)
    assert len(docs) == 100
    assert sum(d.ground_truth_compliant for d in docs) == 45
    assert [d.snippet for d in docs] == [d.snippet for d in build_heldout_documents(AS_OF)]
    report = run_heldout(offline=True, as_of=AS_OF, decider_mode="heuristic", allow_heuristic_proceed=True)
    metrics = report.metrics()
    for key in (
        "cost_reduction_pct",
        "hallucinated_approval_rate_pct",
        "false_halt_rate_pct",
        "p50_gate_latency_ms",
        "p95_gate_latency_ms",
        "decider_backends",
    ):
        assert key in metrics
    assert metrics["documents"] == 100
    assert all(r.tokens_spent == 0 for r in report.rows if r.gated_action == "HALTED")


def test_core_benchmark_without_opt_in_fails_closed() -> None:
    report = run_core_benchmark(offline=True, as_of=AS_OF)
    assert [r.gated_action for r in report.rows] == ["HALTED"] * 4
    assert all(r.verdict == "decider_error" for r in report.rows)
    assert all(r.tokens_spent == 0 for r in report.rows)
    assert not report.all_expected


def test_no_network_fixture_blocks_urlopen() -> None:
    blocked: Callable[..., Any] = urllib.request.urlopen
    with pytest.raises(urllib.error.URLError):
        blocked("http://127.0.0.1:8099/decide", timeout=0.01)


# --------------------------------------------------------------------------------------------
# Requirement checks (Decider noul questions) and the deterministic date rule
# --------------------------------------------------------------------------------------------


def _server_answer(choice: str, confidence: float, **checks: float) -> bytes:
    answers: dict[str, Any] = {"stipulation_verdict": {"type": "choice", "choice": choice, "confidence": confidence}}
    answers.update({name: {"type": "noul", "noul": p} for name, p in checks.items()})
    return json.dumps({"answers": answers}).encode()


def _server_handler() -> tuple[StipulationGuardrailHandler, InMemoryReviewQueue]:
    return make_handler(DeciderClient("http://127.0.0.1:8080/invocations", heuristic_as_of=AS_OF))


def test_requirement_checks_sent_in_same_request(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = install(monkeypatch, [_server_answer("fully_compliant", 0.95, covers_two_tax_years=0.97)])
    handler, _ = _server_handler()
    w2 = CORE["SG-003"].snippet
    assert isinstance(handler.evaluate(CLEAR_CONDITION_TOOL, tool_input(w2, "STIP_W2_2YR")), Proceed)
    questions = rec.body()["questions"]
    assert questions["covers_two_tax_years"]["type"] == "noul"
    assert f"tax year {AS_OF.year - 1}" in questions["covers_two_tax_years"]["instructions"]
    assert handler.last_decision is not None and handler.last_decision.checks == {"covers_two_tax_years": 0.97}


def test_failed_requirement_check_halts(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, [_server_answer("fully_compliant", 0.95, covers_two_tax_years=0.30)])
    handler, queue = _server_handler()
    action = handler.evaluate(CLEAR_CONDITION_TOOL, tool_input(CORE["SG-003"].snippet, "STIP_W2_2YR"))
    assert isinstance(action, Guide)
    assert handler.last_decision is not None and handler.last_decision.verdict == "failed_requirement_check"
    assert len(queue) == 1


def test_missing_check_answer_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, [_server_answer("fully_compliant", 0.95)])
    handler, _ = _server_handler()
    action = handler.evaluate(CLEAR_CONDITION_TOOL, tool_input(CORE["SG-003"].snippet, "STIP_W2_2YR"))
    assert isinstance(action, Guide)
    assert handler.last_decision is not None and handler.last_decision.verdict == "decider_error"


def test_stale_paystub_halted_by_date_rule_after_decider_approves(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, [_server_answer("fully_compliant", 0.97, has_employer_and_ytd=0.95)])
    handler, _ = _server_handler()
    stale = paystub_text("Maria L. Chen", "Cascade Freight Logistics LLC", AS_OF - timedelta(days=90), 30.0)
    action = handler.evaluate(CLEAR_CONDITION_TOOL, tool_input(stale))
    assert isinstance(action, Guide)
    decision = handler.last_decision
    assert decision is not None and decision.verdict == "failed_date_rule" and "days old" in decision.reason


def test_unrelated_verdict_kept_when_date_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, [_server_answer("invalid_unrelated_doc", 0.92, has_employer_and_ytd=0.05)])
    handler, _ = _server_handler()
    handler.evaluate(CLEAR_CONDITION_TOOL, tool_input(UTILITY))
    assert handler.last_decision is not None and handler.last_decision.verdict == "invalid_unrelated_doc"
