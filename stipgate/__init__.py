"""StipGate: Edge-Gated Mortgage Trailing Document Clearance.

A Strands Decider 2B compliance circuit breaker that gates underwriting-condition clearance
before any Amazon Bedrock (Nova Lite) tokens are spent.
"""

from stipgate.bedrock_worker import BedrockExtractor, ExtractionResult, IncomeClearancePayload
from stipgate.decider_client import (
    DeciderClient,
    DeciderError,
    DeciderMode,
    DeciderResponseError,
    DeciderResult,
    DeciderUnavailableError,
    HeuristicDecider,
)
from stipgate.handlers import (
    CLEAR_CONDITION_TOOL,
    CONDITION_DOCUMENTS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    SUPPORTED_CONDITION_CODES,
    VERDICT_OPTIONS,
    GateDecision,
    HumanReviewQueue,
    InMemoryReviewQueue,
    ReviewTicket,
    StipulationGuardrailHandler,
    build_criteria,
    build_question,
)
from stipgate.ocr import DocumentTextExtractor, OcrError, OcrResult, OcrUnavailableError
from stipgate.pipeline import StipGatePipeline, build_agent, make_clearance_tool

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "BedrockExtractor",
    "ExtractionResult",
    "IncomeClearancePayload",
    "DeciderClient",
    "DeciderError",
    "DeciderMode",
    "DeciderResponseError",
    "DeciderResult",
    "DeciderUnavailableError",
    "HeuristicDecider",
    "CLEAR_CONDITION_TOOL",
    "CONDITION_DOCUMENTS",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "SUPPORTED_CONDITION_CODES",
    "VERDICT_OPTIONS",
    "GateDecision",
    "HumanReviewQueue",
    "InMemoryReviewQueue",
    "ReviewTicket",
    "StipulationGuardrailHandler",
    "build_criteria",
    "build_question",
    "DocumentTextExtractor",
    "OcrError",
    "OcrResult",
    "OcrUnavailableError",
    "StipGatePipeline",
    "build_agent",
    "make_clearance_tool",
]
