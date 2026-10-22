"""StipGatePipeline - wires DeciderClient, StipulationGuardrailHandler and BedrockExtractor.

Two entry points share the exact same decision path
(``StipulationGuardrailHandler.evaluate_with_decision``):

* :meth:`StipGatePipeline.process_document` - batch/LOS-webhook style, returns an execution trace.
* :func:`build_agent` - a real Strands ``Agent`` exposing the ``clear_underwriting_condition`` tool
  with the handler attached via ``Agent(interventions=[...])``.

Clearance status: ``cleared`` only when the gate passed AND Nova Lite produced the payload. When the
gate passed but extraction fell back to the regex/synthetic payload, the status is
``pending_extraction_review`` and a loan-officer review ticket is opened; ``halted`` when the gate
stopped the document.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Final, Literal

from strands import Agent, tool
from strands.interventions import Proceed
from strands.models import Model
from strands.models.bedrock import BedrockModel
from strands.tools.decorator import DecoratedFunctionTool

from stipgate.bedrock_worker import BEDROCK_REGION, NOVA_LITE_MODEL_ID, BedrockExtractor
from stipgate.decider_client import DeciderClient
from stipgate.handlers import (
    CLEAR_CONDITION_TOOL,
    DEFAULT_CONFIDENCE_THRESHOLD,
    HumanReviewQueue,
    InMemoryReviewQueue,
    ReviewTicket,
    StipulationGuardrailHandler,
)

__all__ = [
    "StipGatePipeline",
    "make_clearance_tool",
    "build_agent",
    "AGENT_SYSTEM_PROMPT",
    "ClearanceStatus",
    "PENDING_EXTRACTION_REVIEW",
]

logger = logging.getLogger("stipgate.pipeline")

ClearanceStatus = Literal["cleared", "pending_extraction_review", "halted"]
PENDING_EXTRACTION_REVIEW: Final = "pending_extraction_review"
_PREVIEW_CHARS: Final = 240

AGENT_SYSTEM_PROMPT: Final = (
    "You are a mortgage loan-operations assistant. When a borrower uploads a trailing document for an "
    "underwriting stipulation, call clear_underwriting_condition with the document_id, the condition_code "
    "(for example STIP_INCOME_30DAY or STIP_W2_2YR) and the verbatim document_snippet. Never claim a "
    "condition is cleared unless the tool returns status 'cleared'. If the tool returns status "
    "'pending_extraction_review', the document passed the automated check but a loan officer must verify "
    "the extracted figures: do not say the condition is cleared; tell the borrower it is pending loan "
    "officer review. If the tool returns StipGate guidance, follow it: do not retry, and tell the borrower "
    "a loan officer will review the document."
)


def _open_extraction_review(
    queue: HumanReviewQueue,
    *,
    document_id: str | None,
    tool_use_id: str | None,
    condition_code: str | None,
    confidence: float,
    snippet: str,
    fallback_reason: str | None,
) -> str | None:
    """Open a loan-officer ticket for a fallback extraction; return its id, or None if enqueue failed."""
    ticket = ReviewTicket(
        ticket_id=uuid.uuid4().hex,
        document_id=document_id,
        tool_use_id=tool_use_id,
        condition_code=condition_code,
        verdict=PENDING_EXTRACTION_REVIEW,
        confidence=confidence,
        reason=(
            f"Nova Lite extraction unavailable ({fallback_reason}); regex/synthetic payload requires "
            "loan officer verification"
        ),
        snippet_preview=snippet[:_PREVIEW_CHARS],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        queue.enqueue(ticket)
    except Exception:
        logger.exception(
            "Failed to enqueue extraction review ticket %s for doc=%s; status remains %s",
            ticket.ticket_id,
            document_id,
            PENDING_EXTRACTION_REVIEW,
        )
        return None
    logger.info(
        "PENDING_EXTRACTION_REVIEW doc=%s condition=%s reason=%s ticket=%s",
        document_id,
        condition_code,
        fallback_reason,
        ticket.ticket_id,
    )
    return ticket.ticket_id


class StipGatePipeline:
    """Edge-gated trailing-document clearance pipeline."""

    def __init__(
        self,
        decider: DeciderClient | None = None,
        extractor: BedrockExtractor | None = None,
        *,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        review_queue: HumanReviewQueue | None = None,
        offline_extraction: bool = False,
    ) -> None:
        self.decider: DeciderClient = decider if decider is not None else DeciderClient()
        self.extractor: BedrockExtractor = (
            extractor if extractor is not None else BedrockExtractor(offline=offline_extraction)
        )
        self.review_queue: HumanReviewQueue = review_queue if review_queue is not None else InMemoryReviewQueue()
        self.handler: StipulationGuardrailHandler = StipulationGuardrailHandler(
            self.decider,
            confidence_threshold=confidence_threshold,
            review_queue=self.review_queue,
        )

    def process_document(self, document_id: str, snippet: str, condition_code: str) -> dict[str, Any]:
        """Gate one document through Decider 2B and extract with Nova Lite only if it passes.

        Every trace field comes from this call's own ``GateDecision`` (safe for concurrent callers).
        """
        started = time.perf_counter()
        tool_use_id = f"pipeline-{document_id}"
        action, decision = self.handler.evaluate_with_decision(
            CLEAR_CONDITION_TOOL,
            {"document_id": document_id, "condition_code": condition_code, "document_snippet": snippet},
            tool_use_id=tool_use_id,
        )
        latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        proceeded = isinstance(action, Proceed) and decision is not None and decision.action == "PROCEED"

        trace: dict[str, Any] = {
            "document_id": document_id,
            "latency_ms": latency_ms,
            "verdict": decision.verdict if decision is not None else "decider_error",
            "confidence": round(decision.confidence, 4) if decision is not None else 0.0,
            "gated_action": "PROCEED" if proceeded else "HALTED",
            "tokens_spent": 0,
            "extracted_payload": None,
            "clearance_status": "halted",
            "condition_code": decision.condition_code if decision is not None else condition_code,
            "decider_backend": decision.backend if decision is not None else None,
            "decider_latency_ms": round(decision.decider_latency_ms, 3) if decision is not None else 0.0,
            "decider_model": decision.model if decision is not None else None,
            "llm_model": None,
            "total_latency_ms": latency_ms,
            "review_ticket_id": decision.ticket_id if decision is not None else None,
            "guardrail_feedback": getattr(action, "feedback", None),
            "extraction_is_fallback": None,
            "extraction_latency_ms": 0.0,
            "extraction_review_ticket_id": None,
            "extraction_fallback_reason": None,
            "extraction_synthetic_fields": [],
            "input_tokens": 0,
            "output_tokens": 0,
        }
        if not proceeded or decision is None:
            return trace

        gated_code = decision.condition_code or condition_code
        result = self.extractor.extract(snippet, gated_code)
        status: ClearanceStatus = "cleared"
        extraction_ticket: str | None = None
        if result.is_fallback:
            status = PENDING_EXTRACTION_REVIEW
            extraction_ticket = _open_extraction_review(
                self.review_queue,
                document_id=document_id,
                tool_use_id=tool_use_id,
                condition_code=gated_code,
                confidence=decision.confidence,
                snippet=snippet,
                fallback_reason=result.fallback_reason,
            )
        trace.update(
            {
                "tokens_spent": result.total_tokens,
                "extracted_payload": result.payload.model_dump(),
                "clearance_status": status,
                "extraction_is_fallback": result.is_fallback,
                "extraction_latency_ms": round(result.latency_ms, 3),
                "extraction_review_ticket_id": extraction_ticket,
                "extraction_fallback_reason": result.fallback_reason,
                "extraction_synthetic_fields": list(result.synthetic_fields),
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                # Fallback extractions never reached the LLM, so no model is reported for them.
                "llm_model": None if result.is_fallback else self.extractor.model_id,
                "total_latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            }
        )
        return trace


def make_clearance_tool(
    extractor: BedrockExtractor, review_queue: HumanReviewQueue | None = None
) -> DecoratedFunctionTool[..., dict[str, Any]]:
    """Build the ``clear_underwriting_condition`` Strands tool bound to ``extractor``.

    A fallback extraction returns status ``pending_extraction_review`` (never ``cleared``) and opens
    a ticket on ``review_queue`` when one is given. The gate's confidence is not visible inside the
    tool, so that ticket records confidence 0.0; the gate's own ``GateDecision`` holds the score.
    """

    @tool(name=CLEAR_CONDITION_TOOL)
    def clear_underwriting_condition(document_id: str, condition_code: str, document_snippet: str) -> dict[str, Any]:
        """Clear a mortgage underwriting stipulation using a borrower's trailing document.

        Args:
            document_id: LOS identifier of the uploaded trailing document.
            condition_code: Underwriting stipulation code, e.g. STIP_INCOME_30DAY or STIP_W2_2YR.
            document_snippet: Verbatim OCR text of the uploaded document.
        """
        result = extractor.extract(document_snippet, condition_code)
        status: ClearanceStatus = "cleared"
        ticket_id: str | None = None
        if result.is_fallback:
            status = PENDING_EXTRACTION_REVIEW
            if review_queue is not None:
                ticket_id = _open_extraction_review(
                    review_queue,
                    document_id=document_id,
                    tool_use_id=None,
                    condition_code=condition_code,
                    confidence=0.0,
                    snippet=document_snippet,
                    fallback_reason=result.fallback_reason,
                )
        return {
            "status": status,
            "document_id": document_id,
            "condition_code": condition_code,
            **result.payload.model_dump(),
            "tokens_spent": result.total_tokens,
            "review_ticket_id": ticket_id,
            "extraction_is_fallback": result.is_fallback,
            "synthetic_fields": list(result.synthetic_fields),
        }

    return clear_underwriting_condition


def build_agent(pipeline: StipGatePipeline, model: Model | str | None = None) -> Agent:
    """Create a Strands Agent whose clearance tool is gated by ``pipeline.handler``."""
    return Agent(
        model=model if model is not None else BedrockModel(model_id=NOVA_LITE_MODEL_ID, region_name=BEDROCK_REGION),
        tools=[make_clearance_tool(pipeline.extractor, pipeline.review_queue)],
        interventions=[pipeline.handler],
        system_prompt=AGENT_SYSTEM_PROMPT,
        callback_handler=None,
    )
