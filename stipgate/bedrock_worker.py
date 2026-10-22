"""Amazon Bedrock Nova Lite extraction of income-verification data (Converse API).

Only documents that already passed the Decider 2B gate reach this worker. The model is asked for
strict JSON which is validated into :class:`IncomeClearancePayload`. Any Bedrock/credential/
network/parse failure degrades to a regex-derived fallback payload (flagged ``is_fallback``) with
an estimated token count, so local runs and CI work without AWS credentials.

Fallback data is regex-derived from the snippet, and any field the regexes cannot find is filled
with a clearly synthetic default listed in ``ExtractionResult.synthetic_fields``. A fallback
extraction never clears a condition: the pipeline marks it ``pending_extraction_review`` and
opens a loan-officer review ticket.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from stipgate.decider_client import parse_date

__all__ = [
    "NOVA_LITE_MODEL_ID",
    "BEDROCK_REGION",
    "SYSTEM_PROMPT",
    "IncomeClearancePayload",
    "ExtractionResult",
    "ConverseClient",
    "BedrockExtractor",
]

logger = logging.getLogger("stipgate.bedrock")

NOVA_LITE_MODEL_ID: Final = "amazon.nova-lite-v1:0"
BEDROCK_REGION: Final = "us-east-1"
CHARS_PER_TOKEN: Final = 4
SYSTEM_PROMPT: Final = (
    "You are a mortgage underwriting data extractor. Read the verified trailing document and "
    "respond with ONLY one JSON object, no prose and no markdown, with exactly these keys: "
    '"borrower_name" (string), "employer_name" (string), "gross_pay" (number), '
    '"ytd_gross" (number), "pay_period_end" (string, ISO date YYYY-MM-DD). '
    "Numbers must not contain currency symbols or thousands separators. "
    "For a paystub: gross_pay is the current-period gross pay, ytd_gross is year-to-date gross, "
    "pay_period_end is the pay period end date. For W-2 forms: gross_pay and ytd_gross are both the "
    "most recent tax year's Box 1 wages, tips, other compensation, and pay_period_end is December 31 "
    "of that tax year."
)

W2_CONDITION_CODE: Final = "STIP_W2_2YR"
_W2_PROMPT_HINT: Final = (
    "This is a set of W-2 forms. Use ONLY the most recent tax year's Box 1 amount for BOTH gross_pay and "
    "ytd_gross. Never add years together. pay_period_end is December 31 of that most recent tax year.\n"
)
_FALLBACK_BORROWER: Final = "SYNTHETIC FALLBACK BORROWER"
_FALLBACK_EMPLOYER: Final = "SYNTHETIC FALLBACK EMPLOYER"
_FALLBACK_GROSS: Final = 4250.00
_ISO_DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_US_DATE_RE: Final = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_AMOUNT: Final = r"\$?\s*([\d,]+\.\d{2})"
_EMPLOYEE_VALUE_RE: Final = re.compile(r"(?:employee(?:\s+name)?|borrower)\s*:\s*([^\n]+)", re.IGNORECASE)
_EMPLOYER_VALUE_RE: Final = re.compile(r"(?:employer|company)(?:\s+name)?\s*:\s*([^\n]+)", re.IGNORECASE)
_GROSS_VALUE_RE: Final = re.compile(r"gross\s+pay[^\d$\n]{0,20}" + _AMOUNT, re.IGNORECASE)
_YTD_VALUE_RE: Final = re.compile(r"(?:ytd|year[- ]to[- ]date)[^\d$\n]{0,25}" + _AMOUNT, re.IGNORECASE)
_W2_BLOCK_YEAR_RE: Final = re.compile(r"(?:tax\s+year|form\s+w-2)\D{0,6}(20\d{2})|(20\d{2})\s+form\s+w-2", re.IGNORECASE)
_W2_WAGES_VALUE_RE: Final = re.compile(r"wages, tips[^\d$\n]{0,30}" + _AMOUNT, re.IGNORECASE)
_PERIOD_END_VALUE_RE: Final = re.compile(
    r"(?:pay\s+period\s+end(?:ing)?|period\s+end(?:ing)?)\s*[:\-]?\s*"
    r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\.?\s+\d{1,2},\s*\d{4})",
    re.IGNORECASE,
)
_NAME_FIELD_SPLIT_RE: Final = re.compile(r"\s{2,}|\s+(?:SSN|EIN|ID|Employee ID)\b", re.IGNORECASE)


class IncomeClearancePayload(BaseModel):
    """Structured income-verification data used to clear an underwriting stipulation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True, allow_inf_nan=False)

    borrower_name: str = Field(min_length=2, max_length=120)
    employer_name: str = Field(min_length=2, max_length=160)
    gross_pay: float = Field(ge=0)
    ytd_gross: float = Field(ge=0)
    pay_period_end: str

    @field_validator("pay_period_end", mode="before")
    @classmethod
    def _normalize_date(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("pay_period_end must be a date string")
        text = value.strip()
        us = _US_DATE_RE.match(text)
        if us:
            text = f"{int(us.group(3)):04d}-{int(us.group(1)):02d}-{int(us.group(2)):02d}"
        if not _ISO_DATE_RE.match(text):
            raise ValueError("pay_period_end must be YYYY-MM-DD (or MM/DD/YYYY)")
        try:
            date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"pay_period_end {text!r} is not a valid calendar date") from exc
        return text

    @field_validator("borrower_name", "employer_name")
    @classmethod
    def _require_letters(cls, value: str) -> str:
        if not re.search(r"[A-Za-z]", value):
            raise ValueError("name must contain letters")
        return value

    @model_validator(mode="after")
    def _ytd_covers_period(self) -> IncomeClearancePayload:
        if self.ytd_gross + 0.005 < self.gross_pay:
            raise ValueError("ytd_gross must be greater than or equal to gross_pay")
        return self


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome of one extraction, including token accounting."""

    payload: IncomeClearancePayload
    input_tokens: int
    output_tokens: int
    total_tokens: int
    is_fallback: bool
    fallback_reason: str | None
    latency_ms: float
    model_id: str
    synthetic_fields: tuple[str, ...] = ()


class ConverseClient(Protocol):
    """Subset of the boto3 ``bedrock-runtime`` client used by the extractor."""

    def converse(self, **kwargs: Any) -> Mapping[str, Any]: ...


def _estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


def _to_amount(raw: str) -> float:
    return float(raw.replace(",", ""))


class BedrockExtractor:
    """Extract :class:`IncomeClearancePayload` from a gated document with Nova Lite."""

    def __init__(
        self,
        client: ConverseClient | None = None,
        *,
        model_id: str = NOVA_LITE_MODEL_ID,
        region_name: str = BEDROCK_REGION,
        max_tokens: int = 512,
        temperature: float = 0.0,
        offline: bool = False,
    ) -> None:
        self.model_id: str = model_id
        self.region_name: str = region_name
        self.max_tokens: int = max_tokens
        self.temperature: float = temperature
        self.offline: bool = offline
        self.client: ConverseClient | None = client
        if client is None and not offline:
            try:
                self.client = boto3.client(
                    "bedrock-runtime",
                    region_name=region_name,
                    config=Config(
                        connect_timeout=3,
                        read_timeout=20,
                        retries={"max_attempts": 2, "mode": "standard"},
                    ),
                )
            except (BotoCoreError, ValueError) as exc:
                logger.warning("Could not create bedrock-runtime client (%s); using fallback extraction", exc)
                self.client = None

    def build_prompt(self, snippet: str, condition_code: str) -> str:
        """User prompt sent alongside :data:`SYSTEM_PROMPT`."""
        hint = _W2_PROMPT_HINT if condition_code == W2_CONDITION_CODE else ""
        return (
            f"Stipulation condition: {condition_code}\n"
            f"{hint}"
            "Extract the income clearance JSON from this verified document:\n"
            "<document>\n"
            f"{snippet}\n"
            "</document>"
        )

    def extract(self, snippet: str, condition_code: str) -> ExtractionResult:
        """Run Converse extraction; never raises for AWS, network, or parse failures."""
        prompt = self.build_prompt(snippet, condition_code)
        started = time.perf_counter()
        if self.client is None:
            return self._fallback(snippet, prompt, "offline" if self.offline else "no bedrock client", started, None)

        usage: tuple[int, int, int] | None = None
        try:
            response = self.client.converse(
                modelId=self.model_id,
                system=[{"text": SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": self.max_tokens, "temperature": self.temperature},
            )
            input_tokens, output_tokens, total_tokens = self._read_usage(response)
            usage = (input_tokens, output_tokens, total_tokens)
            text = "".join(
                str(block["text"])
                for block in response["output"]["message"]["content"]
                if isinstance(block, Mapping) and "text" in block
            )
            payload = IncomeClearancePayload.model_validate(self._parse_json(text))
            if condition_code == W2_CONDITION_CODE:
                payload = self._normalize_w2(payload)
        except (
            ClientError,
            NoCredentialsError,
            EndpointConnectionError,
            BotoCoreError,
            json.JSONDecodeError,
            ValidationError,
            KeyError,
            TypeError,
            ValueError,
            IndexError,
        ) as exc:
            logger.warning("Nova Lite extraction failed (%s: %s); using fallback payload", type(exc).__name__, exc)
            if isinstance(exc, (NoCredentialsError, EndpointConnectionError)):
                # Environment-level failure: skip repeated credential/endpoint probing on later calls.
                self.client = None
            return self._fallback(snippet, prompt, f"{type(exc).__name__}: {exc}", started, usage)

        return ExtractionResult(
            payload=payload,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            is_fallback=False,
            fallback_reason=None,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            model_id=self.model_id,
        )

    @staticmethod
    def _normalize_w2(payload: IncomeClearancePayload) -> IncomeClearancePayload:
        """W-2 contract: gross_pay and ytd_gross are both the latest year's Box 1 (never a multi-year sum)."""
        if abs(payload.ytd_gross - payload.gross_pay) <= 0.005:
            return payload
        logger.warning(
            "Nova Lite returned W-2 ytd_gross != gross_pay (likely a multi-year sum); using gross_pay for both"
        )
        return payload.model_copy(update={"ytd_gross": payload.gross_pay})

    @staticmethod
    def _read_usage(response: Mapping[str, Any]) -> tuple[int, int, int]:
        usage = response.get("usage") or {}
        if not isinstance(usage, Mapping):
            raise TypeError("Converse usage is not an object")
        input_tokens = int(usage.get("inputTokens", 0))
        output_tokens = int(usage.get("outputTokens", 0))
        total_tokens = int(usage.get("totalTokens", input_tokens + output_tokens))
        return input_tokens, output_tokens, total_tokens

    @staticmethod
    def _parse_json(text: str) -> Any:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", cleaned)
            cleaned = re.sub(r"\s*```\s*$", "", cleaned)
        start = cleaned.find("{")
        if start < 0:
            raise json.JSONDecodeError("no JSON object in model output", cleaned, 0)
        obj, _ = json.JSONDecoder().raw_decode(cleaned, start)
        if not isinstance(obj, dict):
            raise ValueError("model output JSON is not an object")
        return obj

    def _fallback(
        self,
        snippet: str,
        prompt: str,
        reason: str,
        started: float,
        usage: tuple[int, int, int] | None,
    ) -> ExtractionResult:
        payload, synthetic_fields = self.derive_payload_with_provenance(snippet)
        if usage is not None and usage[2] > 0:
            input_tokens, output_tokens, total_tokens = usage
        else:
            input_tokens = _estimate_tokens(SYSTEM_PROMPT + prompt)
            output_tokens = _estimate_tokens(payload.model_dump_json())
            total_tokens = input_tokens + output_tokens
        return ExtractionResult(
            payload=payload,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            is_fallback=True,
            fallback_reason=reason,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            model_id=self.model_id,
            synthetic_fields=synthetic_fields,
        )

    @staticmethod
    def _clean_name(raw: str) -> str:
        return _NAME_FIELD_SPLIT_RE.split(raw.strip(), maxsplit=1)[0].strip(" ,;")

    @classmethod
    def derive_payload(cls, snippet: str) -> IncomeClearancePayload:
        """Regex-derive a payload from the snippet (fallback path)."""
        return cls.derive_payload_with_provenance(snippet)[0]

    @classmethod
    def derive_payload_with_provenance(cls, snippet: str) -> tuple[IncomeClearancePayload, tuple[str, ...]]:
        """Regex-derive a payload and list the fields filled from synthetic defaults (model-field order)."""
        synthetic: set[str] = set()
        borrower = _FALLBACK_BORROWER
        employee_match = _EMPLOYEE_VALUE_RE.search(snippet)
        if employee_match:
            candidate = cls._clean_name(employee_match.group(1))
            if len(candidate) >= 2 and re.search(r"[A-Za-z]", candidate):
                borrower = candidate[:120]

        employer = _FALLBACK_EMPLOYER
        employer_match = _EMPLOYER_VALUE_RE.search(snippet)
        if employer_match:
            candidate = cls._clean_name(employer_match.group(1))
            if len(candidate) >= 2 and re.search(r"[A-Za-z]", candidate):
                employer = candidate[:160]

        gross: float | None = None
        ytd: float | None = None
        period_end: str | None = None

        # Each W-2 block may state its year twice ("2025 Form W-2", "Tax Year 2025"); keep first occurrences.
        w2_years = list(dict.fromkeys(int(a or b) for a, b in _W2_BLOCK_YEAR_RE.findall(snippet)))
        w2_wages = [_to_amount(m) for m in _W2_WAGES_VALUE_RE.findall(snippet)]
        if w2_years and w2_wages:
            # Pair each W-2 year with its wages in document order and pick the latest tax year.
            pairs = list(zip(w2_years, w2_wages))
            latest_year, latest_wages = max(pairs, key=lambda p: p[0])
            gross = ytd = latest_wages
            period_end = f"{latest_year:04d}-12-31"
        else:
            gross_match = _GROSS_VALUE_RE.search(snippet)
            if gross_match:
                gross = _to_amount(gross_match.group(1))
            ytd_match = _YTD_VALUE_RE.search(snippet)
            if ytd_match:
                ytd = _to_amount(ytd_match.group(1))
            period_match = _PERIOD_END_VALUE_RE.search(snippet)
            if period_match:
                parsed = parse_date(period_match.group(1))
                if parsed is not None:
                    period_end = parsed.isoformat()

        if borrower == _FALLBACK_BORROWER:
            synthetic.add("borrower_name")
        if employer == _FALLBACK_EMPLOYER:
            synthetic.add("employer_name")
        if gross is None:
            synthetic.add("gross_pay")
        gross_value = gross if gross is not None else _FALLBACK_GROSS
        if ytd is None or ytd < gross_value:
            synthetic.add("ytd_gross")
        ytd_value = max(ytd if ytd is not None else gross_value, gross_value)
        if period_end is None:
            synthetic.add("pay_period_end")
        payload = IncomeClearancePayload(
            borrower_name=borrower,
            employer_name=employer,
            gross_pay=round(gross_value, 2),
            ytd_gross=round(ytd_value, 2),
            pay_period_end=period_end or date.today().isoformat(),
        )
        return payload, tuple(name for name in IncomeClearancePayload.model_fields if name in synthetic)
