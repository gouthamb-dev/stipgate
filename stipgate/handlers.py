"""StipulationGuardrailHandler - the reusable Strands intervention that gates condition clearance.

Strands API note: the task spec references ``strands.handlers.InterventionHandler``. That path does
not exist in strands-agents 1.61.1 (``strands.handlers`` only holds callback handlers). The real
intervention API lives in ``strands.interventions`` and is used here directly - no compatibility
shim. Attach the handler with ``Agent(..., interventions=[handler])``; Strands registers the
``BeforeToolCallEvent`` hook and turns a returned ``Guide`` into ``event.cancel_tool`` so the tool
body never runs and the model receives the guidance as an error tool result.

Fail-closed guarantees:

1. Every step of the Decider check (argument extraction, the Decider call, response validation)
   runs inside ``try/except Exception`` and any failure returns ``Guide`` (human escalation).
2. ``on_error`` is ``"deny"`` so even a defect outside the try block blocks the tool call.
3. A ``fully_compliant`` verdict from the heuristic simulator is halted unless the Decider client
   was explicitly configured with ``allow_heuristic_proceed`` (CI/demo only).

Concurrency: :meth:`StipulationGuardrailHandler.evaluate_with_decision` returns the per-call
:class:`GateDecision`. ``last_decision`` is a last-writer convenience and must not be read by
concurrent callers.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Literal, Protocol

from strands.hooks import BeforeToolCallEvent
from strands.interventions import Guide, InterventionHandler, OnError, Proceed

from stipgate.decider_client import DeciderClient

__all__ = [
    "CLEAR_CONDITION_TOOL",
    "VERDICT_OPTIONS",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "QUESTION_TEMPLATE",
    "CONDITION_DOCUMENTS",
    "required_document",
    "build_question",
    "build_criteria",
    "SUPPORTED_CONDITION_CODES",
    "HEURISTIC_NOT_AUTHORISED_REASON",
    "GateAction",
    "ReviewTicket",
    "HumanReviewQueue",
    "InMemoryReviewQueue",
    "GateDecision",
    "StipulationGuardrailHandler",
]

logger = logging.getLogger("stipgate.guardrail")

CLEAR_CONDITION_TOOL: Final = "clear_underwriting_condition"
VERDICT_OPTIONS: Final[tuple[str, ...]] = (
    "fully_compliant",
    "invalid_unrelated_doc",
    "ambiguous_requires_human_review",
)
DEFAULT_CONFIDENCE_THRESHOLD: Final = 0.88
# What each stipulation requires, in plain words. Decider 2B reads these through the question and the
# per-option criteria; with bare option names (criteria = null) it labelled every sample
# invalid_unrelated_doc on the live int8 runtime.
CONDITION_DOCUMENTS: Final[dict[str, str]] = {
    "STIP_INCOME_30DAY": "a paystub (earnings statement) from the borrower's employer",
    "STIP_W2_2YR": "IRS Form W-2 wage statements for the borrower covering two tax years",
    "STIP_ASSET_2MO": "two monthly bank statements for the borrower",
}
QUESTION_TEMPLATE: Final = (
    "Stipulation {condition_code}: classify this document for the mortgage condition that requires {document}."
)


def required_document(condition_code: str) -> str:
    """Plain-language description of the document a stipulation requires."""
    return CONDITION_DOCUMENTS.get(condition_code, f"the document required by stipulation {condition_code}")


def build_question(condition_code: str) -> str:
    """Decider question for one stipulation (keeps the STIP_* code for the heuristic simulator)."""
    return QUESTION_TEMPLATE.format(document=required_document(condition_code), condition_code=condition_code)


def build_criteria(condition_code: str) -> dict[str, str]:
    """One-line definition per verdict option, sent as the Decider ``criteria``."""
    document = required_document(condition_code)
    return {
        "fully_compliant": f"It is {document} and is readable and complete.",
        "invalid_unrelated_doc": f"It is not {document}; it is another kind of document.",
        "ambiguous_requires_human_review": "It is garbled, cut off or unreadable.",
    }
# Condition codes with explicit rules (matches the heuristic's _CONDITION_RULES). The handler still
# accepts any STIP_* code (unknown codes fail closed via the Decider); the web API enforces this set.
SUPPORTED_CONDITION_CODES: Final = ("STIP_INCOME_30DAY", "STIP_W2_2YR", "STIP_ASSET_2MO")
HEURISTIC_NOT_AUTHORISED_REASON: Final = (
    "heuristic backend is not authorised to clear conditions "
    "(set allow_heuristic_proceed=True only for CI/demo)"
)
CONDITION_CODE_RE: Final = re.compile(r"^STIP_[A-Z0-9_]{2,64}$")
_PREVIEW_CHARS: Final = 240

GateAction = Literal["PROCEED", "HALTED"]


@dataclass(frozen=True)
class ReviewTicket:
    """A document routed to the human loan-officer review queue."""

    ticket_id: str
    document_id: str | None
    tool_use_id: str | None
    condition_code: str | None
    verdict: str
    confidence: float
    reason: str
    snippet_preview: str
    created_at: str


class HumanReviewQueue(Protocol):
    """Destination for halted documents (LOS task queue, SQS, ticketing system, ...)."""

    def enqueue(self, ticket: ReviewTicket) -> None: ...


class InMemoryReviewQueue:
    """Thread-safe in-memory review queue used by default and in tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[ReviewTicket] = []

    def enqueue(self, ticket: ReviewTicket) -> None:
        with self._lock:
            self._items.append(ticket)

    def pending(self) -> list[ReviewTicket]:
        with self._lock:
            return list(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


@dataclass(frozen=True)
class GateDecision:
    """Audit record of a single gate evaluation."""

    tool_use_id: str | None
    document_id: str | None
    condition_code: str | None
    verdict: str
    confidence: float
    decider_latency_ms: float
    action: GateAction
    reason: str
    tokens_spent: int = 0
    backend: str | None = None
    ticket_id: str | None = None
    model: str | None = None  # Decider model id reported by the server, when available


class StipulationGuardrailHandler(InterventionHandler):
    """Gate ``clear_underwriting_condition`` tool calls behind a local Decider 2B verdict."""

    def __init__(
        self,
        decider: DeciderClient,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        review_queue: HumanReviewQueue | None = None,
        *,
        target_tool: str = CLEAR_CONDITION_TOOL,
    ) -> None:
        if isinstance(confidence_threshold, bool) or not isinstance(confidence_threshold, (int, float)):
            raise ValueError("confidence_threshold must be a number")
        if not math.isfinite(confidence_threshold) or not 0.0 < confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be in (0, 1]")
        if not target_tool:
            raise ValueError("target_tool must be a non-empty tool name")
        self.decider: DeciderClient = decider
        self.confidence_threshold: float = float(confidence_threshold)
        self.review_queue: HumanReviewQueue = review_queue if review_queue is not None else InMemoryReviewQueue()
        self.target_tool: str = target_tool
        # Last-writer convenience for single-threaded callers and tests; concurrent callers must
        # use the decision returned by evaluate_with_decision().
        self.last_decision: GateDecision | None = None
        self.decisions: list[GateDecision] = []
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "stipgate-stipulation-guardrail"

    @property
    def on_error(self) -> OnError:
        return "deny"

    # -- Strands lifecycle hook ---------------------------------------------------------------

    def before_tool_call(self, event: BeforeToolCallEvent, **kwargs: Any) -> Proceed | Guide:
        """Strands ``BeforeToolCallEvent`` entry point; delegates to :meth:`evaluate`."""
        tool_name = ""
        try:
            tool_use = event.tool_use
            tool_name = str(tool_use.get("name", ""))
            raw_input = tool_use.get("input")
            tool_input: Mapping[str, object] = raw_input if isinstance(raw_input, Mapping) else {}
            tool_use_id = tool_use.get("toolUseId")
            return self.evaluate(tool_name, tool_input, tool_use_id=str(tool_use_id) if tool_use_id else None)
        except Exception as exc:  # fail closed for the gated tool or an unidentifiable call
            if tool_name and tool_name != self.target_tool:
                return Proceed(reason="not gated")
            return self._halt(
                tool_use_id=None,
                document_id=None,
                condition_code=None,
                snippet="",
                verdict="decider_error",
                confidence=0.0,
                latency_ms=0.0,
                backend=None,
                reason=f"fail-closed: {type(exc).__name__}: {exc}",
            )[0]

    # -- framework-free decision path ---------------------------------------------------------

    def evaluate(
        self, tool_name: str, tool_input: Mapping[str, object], tool_use_id: str | None = None
    ) -> Proceed | Guide:
        """Decide whether a tool call may proceed. Non-target tools bypass the Decider."""
        return self.evaluate_with_decision(tool_name, tool_input, tool_use_id)[0]

    def evaluate_with_decision(
        self, tool_name: str, tool_input: Mapping[str, object], tool_use_id: str | None = None
    ) -> tuple[Proceed | Guide, GateDecision | None]:
        """Like :meth:`evaluate` but also return this call's :class:`GateDecision`.

        The decision is ``None`` only for non-target tools (which bypass the Decider). Concurrent
        callers must use this method rather than reading :attr:`last_decision`, which is a
        last-writer convenience shared across calls.
        """
        if tool_name != self.target_tool:
            return Proceed(reason="not gated"), None

        document_id: str | None = None
        condition_code: str | None = None
        snippet = ""
        latency_ms = 0.0
        try:
            raw_doc_id = tool_input.get("document_id")
            document_id = str(raw_doc_id) if raw_doc_id is not None else None
            raw_snippet = tool_input.get("document_snippet")
            if not isinstance(raw_snippet, str) or not raw_snippet.strip():
                raise ValueError("document_snippet is missing or empty")
            snippet = raw_snippet
            raw_code = tool_input.get("condition_code")
            if not isinstance(raw_code, str):
                raise ValueError("condition_code is missing")
            normalized = raw_code.strip().upper()
            if not CONDITION_CODE_RE.match(normalized):
                raise ValueError(f"condition_code {raw_code!r} is not a valid stipulation code")
            condition_code = normalized

            question = build_question(condition_code)
            options = list(VERDICT_OPTIONS)
            result = self.decider.decide_detailed(snippet, question, options, criteria=build_criteria(condition_code))
            verdict = result.verdict
            confidence: object = result.confidence
            latency_ms = float(result.latency_ms)
            backend: str | None = result.backend
            reported_model = getattr(result, "model", None)
            model = reported_model if isinstance(reported_model, str) else None

            if not isinstance(verdict, str) or verdict not in VERDICT_OPTIONS:
                raise ValueError(f"Decider verdict {verdict!r} outside typed options")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise ValueError(f"Decider confidence {confidence!r} is not numeric")
            score = float(confidence)
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise ValueError(f"Decider confidence {score!r} outside [0, 1]")

            if verdict == "fully_compliant" and score >= self.confidence_threshold:
                if result.backend == "heuristic" and not result.heuristic_proceed_allowed:
                    return self._halt(
                        tool_use_id=tool_use_id,
                        document_id=document_id,
                        condition_code=condition_code,
                        snippet=snippet,
                        verdict=verdict,
                        confidence=score,
                        latency_ms=latency_ms,
                        backend=backend,
                        reason=HEURISTIC_NOT_AUTHORISED_REASON,
                        model=model,
                    )
                reason = (
                    f"Decider 2B verified {condition_code}: verdict={verdict} "
                    f"confidence={score:.3f} >= {self.confidence_threshold:.2f}"
                )
                decision = GateDecision(
                    tool_use_id=tool_use_id,
                    document_id=document_id,
                    condition_code=condition_code,
                    verdict=verdict,
                    confidence=score,
                    decider_latency_ms=latency_ms,
                    action="PROCEED",
                    reason=reason,
                    backend=backend,
                    model=model,
                )
                self._record(decision)
                logger.info("PROCEED %s doc=%s %s", self.target_tool, document_id, reason)
                return Proceed(reason=reason), decision

            return self._halt(
                tool_use_id=tool_use_id,
                document_id=document_id,
                condition_code=condition_code,
                snippet=snippet,
                verdict=verdict,
                confidence=score,
                latency_ms=latency_ms,
                backend=backend,
                reason=(
                    f"verdict={verdict} confidence={score:.3f} "
                    f"below gate (threshold {self.confidence_threshold:.2f})"
                    if verdict == "fully_compliant"
                    else f"verdict={verdict}"
                ),
                model=model,
            )
        except Exception as exc:
            return self._halt(
                tool_use_id=tool_use_id,
                document_id=document_id,
                condition_code=condition_code,
                snippet=snippet,
                verdict="decider_error",
                confidence=0.0,
                latency_ms=latency_ms,
                backend=None,
                reason=f"fail-closed: {type(exc).__name__}: {exc}",
            )

    # -- internals ----------------------------------------------------------------------------

    def _record(self, decision: GateDecision) -> None:
        with self._lock:
            self.last_decision = decision
            self.decisions.append(decision)

    def _halt(
        self,
        *,
        tool_use_id: str | None,
        document_id: str | None,
        condition_code: str | None,
        snippet: str,
        verdict: str,
        confidence: float,
        latency_ms: float,
        backend: str | None,
        reason: str,
        model: str | None = None,
    ) -> tuple[Guide, GateDecision]:
        ticket = ReviewTicket(
            ticket_id=uuid.uuid4().hex,
            document_id=document_id,
            tool_use_id=tool_use_id,
            condition_code=condition_code,
            verdict=verdict,
            confidence=confidence,
            reason=reason,
            snippet_preview=snippet[:_PREVIEW_CHARS],
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            self.review_queue.enqueue(ticket)
        except Exception:
            logger.exception("Failed to enqueue review ticket %s; halting tool call regardless", ticket.ticket_id)
        decision = GateDecision(
            tool_use_id=tool_use_id,
            document_id=document_id,
            condition_code=condition_code,
            verdict=verdict,
            confidence=confidence,
            decider_latency_ms=latency_ms,
            action="HALTED",
            reason=reason,
            tokens_spent=0,
            backend=backend,
            ticket_id=ticket.ticket_id,
            model=model,
        )
        self._record(decision)
        logger.info(
            "HALTED %s doc=%s condition=%s %s tokens_spent=0 ticket=%s",
            self.target_tool,
            document_id,
            condition_code,
            reason,
            ticket.ticket_id,
        )
        feedback = (
            f"StipGate HALTED {self.target_tool} for {condition_code or 'UNKNOWN'}: "
            f"Decider 2B verdict={verdict} confidence={confidence:.3f} "
            f"(threshold {self.confidence_threshold:.2f}). Zero LLM tokens spent. "
            f"Routed to human loan officer review queue (ticket {ticket.ticket_id}). "
            "Do not retry clearing this condition; tell the borrower a loan officer will review the document."
        )
        return Guide(feedback=feedback, reason=verdict), decision
