"""Client for a locally served Strands Decider 2B model.

The client speaks two wire protocols, selected by the endpoint path:

* ``.../decide`` (default ``http://127.0.0.1:8099/decide``) - the StipGate simple contract::

      request : {"text": "<snippet>", "question": "<question>", "options": ["a", "b", ...]}
      response: {"verdict": "a", "confidence": 0.93}

* ``.../v1/systemone`` or ``.../invocations`` - the official ``strands-decider serve`` contract::

      request : {"state": "<snippet>",
                 "questions": {"stipulation_verdict": {"type": "choice",
                                                       "instructions": "<question>",
                                                       "criteria": {"a": "<definition>", "b": null}}}}
      response: {"answers": {"stipulation_verdict": {"type": "choice", "choice": "a",
                                                     "confidence": 0.93, ...}}, ...}

If a ``/decide`` endpoint answers HTTP 404 the client retries once against
``<scheme://host:port>/v1/systemone`` so the official server works with the default URL.

Backend modes (constructor ``mode=`` or env ``STIPGATE_DECIDER_MODE``; default ``server``):

* ``server`` (fail-closed production default) - only the Decider server is consulted. When it is
  unreachable (connection refused, timeout, HTTP 5xx) :class:`DeciderUnavailableError` is raised
  and the guardrail handler escalates the document to human review. There is no circuit breaker,
  so recovery is immediate once the server is back.
* ``heuristic`` - never calls the server; :class:`HeuristicDecider`, a deterministic rule-based
  simulator for offline CI, unit tests and demos, answers every question.
* ``auto`` - server first, heuristic when the server is unreachable, with a circuit breaker that
  skips the server for ``offline_retry_s`` seconds after a failure.

Heuristic answers are tagged ``backend="heuristic"`` in :class:`DeciderResult`; the guardrail
handler refuses to clear a condition on a heuristic verdict unless ``allow_heuristic_proceed`` is
enabled (constructor or env ``STIPGATE_ALLOW_HEURISTIC_PROCEED``), which is meant for CI/demo only.

In every mode an HTTP 4xx (other than the single 404 retry described above) raises
:class:`DeciderError`, and a reachable server that returns an invalid answer (verdict outside the
options, non-numeric or out-of-range confidence, non-JSON) raises :class:`DeciderResponseError`,
so the caller fails closed instead of silently trusting a heuristic.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal

__all__ = [
    "DEFAULT_DECIDER_URL",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_DECIDER_MODE",
    "DEFAULT_MAX_CONCURRENCY",
    "SYSTEMONE_QUESTION_KEY",
    "ENV_DECIDER_URL",
    "ENV_DECIDER_MODE",
    "ENV_ALLOW_HEURISTIC_PROCEED",
    "ENV_DECIDER_TIMEOUT_S",
    "ENV_DECIDER_MAX_CONCURRENCY",
    "Backend",
    "DeciderMode",
    "DeciderError",
    "DeciderResponseError",
    "DeciderUnavailableError",
    "DeciderResult",
    "YesNoCheck",
    "latest_document_date",
    "HeuristicDecider",
    "DeciderClient",
]

logger = logging.getLogger("stipgate.decider")

DEFAULT_DECIDER_URL: Final = "http://127.0.0.1:8099/decide"
DEFAULT_TIMEOUT_S: Final = 1.5
DEFAULT_DECIDER_MODE: Final = "server"
DEFAULT_MAX_CONCURRENCY: Final = 1
SYSTEMONE_QUESTION_KEY: Final = "stipulation_verdict"
ENV_DECIDER_URL: Final = "STIPGATE_DECIDER_URL"
ENV_DECIDER_MODE: Final = "STIPGATE_DECIDER_MODE"
ENV_ALLOW_HEURISTIC_PROCEED: Final = "STIPGATE_ALLOW_HEURISTIC_PROCEED"
ENV_DECIDER_TIMEOUT_S: Final = "STIPGATE_DECIDER_TIMEOUT_S"
ENV_DECIDER_MAX_CONCURRENCY: Final = "STIPGATE_DECIDER_MAX_CONCURRENCY"

Backend = Literal["server", "heuristic"]
DeciderMode = Literal["server", "heuristic", "auto"]
Outcome = Literal["COMPLIANT", "UNRELATED", "AMBIGUOUS"]
WireProtocol = Literal["simple", "systemone"]

_DECIDER_MODES: Final[tuple[DeciderMode, ...]] = ("server", "heuristic", "auto")
_TRUTHY: Final = frozenset({"1", "true", "yes"})


class DeciderError(Exception):
    """Raised when the Decider cannot produce a trustworthy answer."""


class DeciderResponseError(DeciderError):
    """Raised when a reachable Decider server returns a malformed or out-of-contract answer."""


class DeciderUnavailableError(DeciderError):
    """Raised in ``server`` mode when the Decider server is unreachable (refused, timeout, 5xx)."""


@dataclass(frozen=True)
class DeciderResult:
    """One Decider answer with its provenance (safe to use from concurrent callers)."""

    verdict: str
    confidence: float
    backend: Backend
    rationale: str
    latency_ms: float
    heuristic_proceed_allowed: bool
    model: str | None = None  # model id reported by the server (e.g. "strands-decider-2B-...@216d1cc")
    # P(true) per requested yes/no check, answered in the same server call. None when the backend
    # cannot answer checks (heuristic simulator, simple /decide contract): the caller decides.
    checks: Mapping[str, float] | None = None


@dataclass(frozen=True)
class YesNoCheck:
    """A yes/no requirement sent as a systemone ``noul`` question alongside the main choice."""

    question: str
    true_means: str
    false_means: str


# --------------------------------------------------------------------------------------------
# Heuristic fallback simulator
# --------------------------------------------------------------------------------------------

_ALLOWED_CHARS: Final = frozenset(string.ascii_letters + string.digits + string.whitespace + ".,:;$/-()%&'#\"+")
_GARBAGE_RATIO_LIMIT: Final = 0.04
_MIXED_RATIO_LIMIT: Final = 0.12
_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9]+")
_MIXED_TOKEN_RE: Final = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z\d]+$")
_HAS_ALPHA_RE: Final = re.compile(r"[A-Za-z]")
_TRUNCATION_RE: Final = re.compile(
    r"\[(?:ocr\s+)?truncated\]|<eof>|\[page\s+\d+\s+(?:of\s+\d+\s+)?missing\]|continued on next page|\.\.\.\s*$",
    re.IGNORECASE,
)
_CONDITION_RE: Final = re.compile(r"\bSTIP_[A-Z0-9_]+\b")


def _patterns(*raw: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(rf"\b(?:{p})\b", re.IGNORECASE) for p in raw)


_TAXONOMY: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    "paystub": _patterns(
        r"earnings statement",
        r"pay ?stub",
        r"pay period",
        r"pay date",
        r"net pay",
        r"gross pay",
        r"ytd|year[- ]to[- ]date",
        r"federal withholding|fed(?:eral)? tax",
        r"hours",
        r"deductions",
    ),
    "w2": _patterns(
        r"form w-2",
        r"wage and tax statement",
        r"wages, tips",
        r"federal income tax withheld",
        r"social security wages",
        r"medicare wages",
        r"employer identification number|ein",
        r"omb no",
    ),
    "utility": _patterns(
        r"utility|utilities",
        r"electric|electricity",
        r"kwh",
        r"natural gas|gas service",
        r"water|sewer",
        r"meter",
        r"service address",
        r"amount due|total due",
        r"due date",
    ),
    "bank_statement": _patterns(
        r"bank statement|account statement",
        r"beginning balance|opening balance",
        r"ending balance|closing balance",
        r"deposits",
        r"withdrawals",
        r"routing",
        r"available balance",
    ),
}


@dataclass(frozen=True)
class _ConditionRule:
    category: str
    window_days: int | None


_CONDITION_RULES: Final[dict[str, _ConditionRule]] = {
    "STIP_INCOME_30DAY": _ConditionRule("paystub", 30),
    "STIP_W2_2YR": _ConditionRule("w2", None),
    "STIP_ASSET_2MO": _ConditionRule("bank_statement", 60),
}

_MONTHS: Final[dict[str, int]] = {
    name: idx
    for idx, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}
_MONTH_ALT: Final = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_PATTERN: Final = rf"(\d{{1,2}}/\d{{1,2}}/\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}}|(?:{_MONTH_ALT})\.?\s+\d{{1,2}},\s*\d{{4}})"
_PERIOD_DATE_RE: Final = re.compile(
    r"(pay\s+period\s+end(?:ing)?|period\s+end(?:ing)?|pay\s+date|check\s+date|"
    r"statement\s+(?:period\s+)?end(?:ing)?|statement\s+date)\s*[:\-]?\s*" + _DATE_PATTERN,
    re.IGNORECASE,
)
_W2_YEAR_RES: Final = (
    re.compile(r"(?:tax\s+year|form\s+w-2)\D{0,6}(20\d{2})", re.IGNORECASE),
    re.compile(r"(20\d{2})\s+form\s+w-2", re.IGNORECASE),
)
_EMPLOYEE_RE: Final = re.compile(r"(employee(?:\s+name)?|borrower)\s*:\s*\S", re.IGNORECASE)
_EMPLOYER_RE: Final = re.compile(r"(employer|company)(?:\s+name)?\s*:\s*\S", re.IGNORECASE)
_GROSS_RE: Final = re.compile(r"gross\s+pay[^\d$]{0,20}\$?\s*[\d,]+\.\d{2}", re.IGNORECASE)
_YTD_RE: Final = re.compile(r"(ytd|year[- ]to[- ]date)[^\d$]{0,25}\$?\s*[\d,]+\.\d{2}", re.IGNORECASE)
_W2_WAGES_RE: Final = re.compile(r"wages, tips[^\d$]{0,30}\$?\s*[\d,]+\.\d{2}", re.IGNORECASE)
_EIN_RE: Final = re.compile(r"\b\d{2}-\d{7}\b")
_HOLDER_RE: Final = re.compile(r"(account\s+holder|name)\s*:\s*\S", re.IGNORECASE)
_ENDING_BALANCE_RE: Final = re.compile(
    r"(ending|closing)\s+balance[^\d$]{0,20}\$?\s*[\d,]+\.\d{2}", re.IGNORECASE
)


def parse_date(raw: str) -> date | None:
    """Parse ``MM/DD/YYYY``, ``YYYY-MM-DD`` or ``Month DD, YYYY``; return None if invalid."""
    value = raw.strip()
    try:
        if "/" in value:
            month_s, day_s, year_s = value.split("/")
            return date(int(year_s), int(month_s), int(day_s))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        match = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4})", value)
        if match:
            month = _MONTHS.get(match.group(1).lower())
            if month is None:
                return None
            return date(int(match.group(3)), month, int(match.group(2)))
    except ValueError:
        return None
    return None


def latest_document_date(text: str) -> date | None:
    """Latest pay-period-end / pay / check / statement date found in ``text`` (None if none parse)."""
    dates = [d for d in (parse_date(m.group(2)) for m in _PERIOD_DATE_RE.finditer(text)) if d is not None]
    return max(dates) if dates else None


class HeuristicDecider:
    """Deterministic rule-based stand-in for Decider 2B (offline CI / unit tests).

    Evaluates document taxonomy, statutory recency windows and completeness/corruption
    signals, and returns one of the caller's options with a calibrated confidence.
    """

    def __init__(self) -> None:
        self.last_rationale: str = ""

    def decide(
        self, text: str, question: str, options: list[str], as_of: date | None = None
    ) -> tuple[str, float]:
        """Classify ``text`` against ``question`` and return ``(option, confidence)``."""
        verdict, confidence, rationale = self.decide_with_rationale(text, question, options, as_of)
        self.last_rationale = rationale
        return verdict, confidence

    def decide_with_rationale(
        self, text: str, question: str, options: list[str], as_of: date | None = None
    ) -> tuple[str, float, str]:
        """Like :meth:`decide` but returns ``(option, confidence, rationale)`` without shared state."""
        if not options:
            raise ValueError("options must be non-empty")
        reference = as_of if as_of is not None else date.today()
        outcome, confidence, rationale = self._classify(text, question, reference)
        return self._map_outcome(outcome, options), round(confidence, 4), rationale

    @staticmethod
    def _map_outcome(outcome: Outcome, options: list[str]) -> str:
        preferred = {
            "COMPLIANT": "fully_compliant",
            "UNRELATED": "invalid_unrelated_doc",
            "AMBIGUOUS": "ambiguous_requires_human_review",
        }[outcome]
        if preferred in options:
            return preferred
        for option in options:
            lowered = option.lower()
            if "review" in lowered or "ambiguous" in lowered:
                return option
        return options[-1]

    def _classify(self, text: str, question: str, as_of: date) -> tuple[Outcome, float, str]:
        stripped = text.strip()
        if len(stripped) < 60:
            return "AMBIGUOUS", 0.95, "insufficient text"

        signals = self._corruption_signals(text)
        if len(signals) >= 2:
            return "AMBIGUOUS", 0.94, "corrupted scan: " + ", ".join(signals)
        if len(signals) == 1:
            return "AMBIGUOUS", 0.86, "possible corruption: " + signals[0]

        hits = {category: sum(1 for p in pats if p.search(text)) for category, pats in _TAXONOMY.items()}
        if all(count == 0 for count in hits.values()):
            return "UNRELATED", 0.82, "no recognised document taxonomy"

        code_match = _CONDITION_RE.search(question)
        rule = _CONDITION_RULES.get(code_match.group(0)) if code_match else None
        if rule is None:
            return "AMBIGUOUS", 0.60, "unknown or missing stipulation condition code"

        expected = rule.category
        best = max(hits, key=lambda c: hits[c])
        if best != expected and hits[best] >= 2 and hits[expected] < 2:
            return "UNRELATED", min(0.97, 0.88 + 0.02 * hits[best]), f"document looks like {best}, expected {expected}"
        if hits[expected] < 2:
            return "AMBIGUOUS", 0.65, f"weak {expected} taxonomy signal"

        missing = 0
        if expected == "w2":
            required, accepted_sets = self._w2_required_years(as_of)
            years = self._w2_years(text)
            covered = any(s <= years for s in accepted_sets)
            if not covered:
                recent = years & set().union(*accepted_sets)
                if recent:
                    return "AMBIGUOUS", 0.84, "incomplete two-year W-2 history"
                return "AMBIGUOUS", 0.86, "stale tax years"
            missing += self._missing_w2_fields(text, len(required))
        else:
            ref = self._latest_period_date(text)
            if ref is None:
                missing += 1
            else:
                age = (as_of - ref).days
                window = rule.window_days if rule.window_days is not None else 30
                if age < -3:
                    return "AMBIGUOUS", 0.80, "future-dated document"
                if age > window:
                    return "AMBIGUOUS", 0.86, f"stale: outside {window}-day window ({age} days old)"
            missing += self._missing_period_fields(text, expected)

        if missing >= 1:
            return "AMBIGUOUS", max(0.55, 0.85 - 0.08 * missing), f"{missing} required field(s) missing"

        confidence = min(0.97, 0.90 + 0.01 * min(7, max(0, hits[expected] - 3)))
        return "COMPLIANT", confidence, f"{expected} verified within statutory window"

    @staticmethod
    def _corruption_signals(text: str) -> list[str]:
        signals: list[str] = []
        garbage = sum(1 for ch in text if ch not in _ALLOWED_CHARS)
        if garbage / max(1, len(text)) > _GARBAGE_RATIO_LIMIT:
            signals.append("garbage OCR characters")
        alpha_tokens = [t for t in _TOKEN_RE.findall(text) if _HAS_ALPHA_RE.search(t)]
        if alpha_tokens:
            mixed = sum(1 for t in alpha_tokens if _MIXED_TOKEN_RE.match(t))
            if mixed / len(alpha_tokens) > _MIXED_RATIO_LIMIT:
                signals.append("mixed letter/digit OCR substitutions")
        if _TRUNCATION_RE.search(text):
            signals.append("truncated document")
        return signals

    @staticmethod
    def _w2_required_years(as_of: date) -> tuple[set[int], list[set[int]]]:
        year = as_of.year
        required = {year - 1, year - 2}
        accepted = [required]
        if as_of.month == 1:
            accepted.append({year - 2, year - 3})
        return required, accepted

    @staticmethod
    def _w2_years(text: str) -> set[int]:
        years: set[int] = set()
        for pattern in _W2_YEAR_RES:
            years.update(int(m) for m in pattern.findall(text))
        return years

    @staticmethod
    def _latest_period_date(text: str) -> date | None:
        dates = [d for d in (parse_date(m.group(2)) for m in _PERIOD_DATE_RE.finditer(text)) if d is not None]
        return max(dates) if dates else None

    @staticmethod
    def _missing_w2_fields(text: str, year_count: int) -> int:
        missing = 0
        if not _EMPLOYEE_RE.search(text):
            missing += 1
        if not _EMPLOYER_RE.search(text):
            missing += 1
        if len(_W2_WAGES_RE.findall(text)) < year_count:
            missing += 1
        if not _EIN_RE.search(text):
            missing += 1
        return missing

    @staticmethod
    def _missing_period_fields(text: str, category: str) -> int:
        if category == "bank_statement":
            checks = [_HOLDER_RE, _ENDING_BALANCE_RE]
        else:
            checks = [_EMPLOYEE_RE, _EMPLOYER_RE, _GROSS_RE, _YTD_RE]
        return sum(1 for pattern in checks if not pattern.search(text))


# --------------------------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------------------------


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _resolve_mode(mode: DeciderMode | None) -> DeciderMode:
    if mode is not None:
        if mode not in _DECIDER_MODES:
            raise ValueError(f"mode must be one of {list(_DECIDER_MODES)}, got {mode!r}")
        return mode
    raw = os.environ.get(ENV_DECIDER_MODE, "").strip().lower()
    if not raw:
        return DEFAULT_DECIDER_MODE
    for candidate in _DECIDER_MODES:
        if raw == candidate:
            return candidate
    raise ValueError(f"{ENV_DECIDER_MODE} must be one of {list(_DECIDER_MODES)}, got {raw!r}")


def _resolve_timeout(timeout_s: float | None) -> float:
    source = "timeout_s"
    value: object = timeout_s
    if timeout_s is None:
        raw = os.environ.get(ENV_DECIDER_TIMEOUT_S, "").strip()
        if not raw:
            return DEFAULT_TIMEOUT_S
        source = ENV_DECIDER_TIMEOUT_S
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"{ENV_DECIDER_TIMEOUT_S} must be a positive number, got {raw!r}") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{source} must be a positive finite number")
    resolved = float(value)
    if not (math.isfinite(resolved) and resolved > 0):
        raise ValueError(f"{source} must be a positive finite number")
    return resolved


def _resolve_concurrency(max_concurrency: int | None) -> int:
    source = "max_concurrency"
    value: object = max_concurrency
    if max_concurrency is None:
        raw = os.environ.get(ENV_DECIDER_MAX_CONCURRENCY, "").strip()
        if not raw:
            return DEFAULT_MAX_CONCURRENCY
        source = ENV_DECIDER_MAX_CONCURRENCY
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{ENV_DECIDER_MAX_CONCURRENCY} must be an integer >= 1, got {raw!r}") from exc
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{source} must be an integer >= 1")
    return value


class DeciderClient:
    """Calls a local Decider 2B server; see the module docstring for the three backend modes."""

    def __init__(
        self,
        endpoint: str | None = None,
        timeout_s: float | None = None,
        *,
        mode: DeciderMode | None = None,
        allow_heuristic_proceed: bool | None = None,
        max_concurrency: int | None = None,
        heuristic: HeuristicDecider | None = None,
        heuristic_as_of: date | None = None,
        offline_retry_s: float = 30.0,
    ) -> None:
        resolved = endpoint or os.environ.get(ENV_DECIDER_URL) or DEFAULT_DECIDER_URL
        parsed = urllib.parse.urlparse(resolved)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Decider endpoint must be an http(s) URL, got {resolved!r}")
        self.endpoint: str = resolved
        self.mode: DeciderMode = _resolve_mode(mode)
        self.timeout_s: float = _resolve_timeout(timeout_s)
        self.max_concurrency: int = _resolve_concurrency(max_concurrency)
        self.allow_heuristic_proceed: bool = (
            bool(allow_heuristic_proceed)
            if allow_heuristic_proceed is not None
            else _env_flag(ENV_ALLOW_HEURISTIC_PROCEED)
        )
        self.heuristic: HeuristicDecider = heuristic if heuristic is not None else HeuristicDecider()
        self.heuristic_as_of: date | None = heuristic_as_of
        # Last-writer diagnostics only: not safe for concurrent reads. Use the DeciderResult
        # returned by decide_detailed() for per-call provenance.
        self.last_backend: Backend | None = None
        self.last_rationale: str = ""
        self.last_latency_ms: float = 0.0
        # Bounds in-flight requests to the server (upstream concurrency support is unverified).
        self._server_slots = threading.BoundedSemaphore(self.max_concurrency)
        # Circuit breaker (auto mode only): after the server is found unreachable, skip it for
        # offline_retry_s seconds instead of paying the connect timeout on every document.
        self.offline_retry_s: float = max(0.0, offline_retry_s)
        self._offline_until: float = 0.0
        self._warned_offline: bool = False

    # -- public API ---------------------------------------------------------------------------

    def decide(
        self,
        text: str,
        question: str,
        options: list[str],
        *,
        criteria: Mapping[str, str | None] | None = None,
        checks: Mapping[str, YesNoCheck] | None = None,
    ) -> tuple[str, float]:
        """Return ``(verdict, confidence)`` where ``verdict`` is one of ``options``.

        Thin wrapper over :meth:`decide_detailed`; raises the same exceptions.
        """
        result = self.decide_detailed(text, question, options, criteria=criteria, checks=checks)
        return result.verdict, result.confidence

    def decide_detailed(
        self,
        text: str,
        question: str,
        options: list[str],
        *,
        criteria: Mapping[str, str | None] | None = None,
        checks: Mapping[str, YesNoCheck] | None = None,
    ) -> DeciderResult:
        """Answer ``question`` about ``text`` and return a per-call :class:`DeciderResult`.

        ``criteria`` optionally defines each option in words (sent as the systemone ``criteria`` values;
        options without an entry are sent as ``null``). ``checks`` adds yes/no ``noul`` questions to the
        same systemone request; their P(true) values come back in ``DeciderResult.checks`` and a missing
        or invalid check answer raises :class:`DeciderResponseError`. The simple ``/decide`` contract and
        the heuristic simulator ignore both (``checks`` is then ``None``).

        Raises:
            ValueError: invalid arguments.
            DeciderUnavailableError: ``server`` mode and the server is unreachable.
            DeciderResponseError: the server answered with an invalid payload.
            DeciderError: the server answered with an HTTP 4xx (any mode).
        """
        self._validate_inputs(text, question, options)
        started = time.perf_counter()
        model: str | None = None
        check_answers: dict[str, float] | None = None
        try:
            if self.mode == "heuristic" or (self.mode == "auto" and time.monotonic() < self._offline_until):
                verdict, confidence, rationale = self._run_heuristic(text, question, options)
                backend: Backend = "heuristic"
            else:
                try:
                    with self._server_slots:
                        verdict, confidence, model, check_answers = self._ask_server(
                            text, question, options, criteria, checks
                        )
                    rationale = "decider-2b server"
                    backend = "server"
                except urllib.error.HTTPError as exc:
                    if exc.code < 500:
                        raise DeciderError(f"Decider server returned HTTP {exc.code}") from exc
                    verdict, confidence, rationale = self._on_unreachable(exc, text, question, options)
                    backend = "heuristic"
                except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                    verdict, confidence, rationale = self._on_unreachable(exc, text, question, options)
                    backend = "heuristic"
            result = DeciderResult(
                verdict=verdict,
                confidence=confidence,
                backend=backend,
                rationale=rationale,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                heuristic_proceed_allowed=self.allow_heuristic_proceed,
                model=model if backend == "server" else None,
                checks=check_answers if backend == "server" else None,
            )
        finally:
            self.last_latency_ms = (time.perf_counter() - started) * 1000.0
        self.last_backend = result.backend
        self.last_rationale = result.rationale
        self.last_latency_ms = result.latency_ms
        return result

    def probe(self, timeout_s: float = 0.5) -> bool | None:
        """Health-check the server: None in heuristic mode, else True iff ``GET /health`` is 200.

        Servers without ``/health`` (404, e.g. AgentCore-style runtimes) are probed on ``GET /ping``.
        Never raises.
        """
        if self.mode == "heuristic":
            return None
        parsed = urllib.parse.urlparse(self.endpoint)
        base = f"{parsed.scheme}://{parsed.netloc}"
        for path in ("/health", "/ping"):
            try:
                request = urllib.request.Request(f"{base}{path}", method="GET")
                with urllib.request.urlopen(request, timeout=timeout_s) as response:
                    return bool(getattr(response, "status", None) == 200)
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    return False
            except Exception:
                return False
        return False

    # -- internals ----------------------------------------------------------------------------

    @staticmethod
    def _validate_inputs(text: str, question: str, options: list[str]) -> None:
        if not isinstance(text, str) or not isinstance(question, str):
            raise ValueError("text and question must be strings")
        if not isinstance(options, list) or not options:
            raise ValueError("options must be a non-empty list of strings")
        if not all(isinstance(o, str) and o for o in options):
            raise ValueError("options must be non-empty strings")
        if len(set(options)) != len(options):
            raise ValueError("options must be unique")

    @staticmethod
    def _protocol_for(url: str) -> WireProtocol:
        path = urllib.parse.urlparse(url).path.rstrip("/")
        if path.endswith("/v1/systemone") or path.endswith("/invocations"):
            return "systemone"
        return "simple"

    @staticmethod
    def _systemone_url(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}/v1/systemone"

    def _ask_server(
        self,
        text: str,
        question: str,
        options: list[str],
        criteria: Mapping[str, str | None] | None = None,
        checks: Mapping[str, YesNoCheck] | None = None,
    ) -> tuple[str, float, str | None, dict[str, float] | None]:
        """Call the configured endpoint; on a simple-protocol 404 retry once against /v1/systemone."""
        endpoint = self.endpoint
        try:
            return self._call_server(endpoint, text, question, options, criteria, checks)
        except urllib.error.HTTPError as exc:
            if exc.code != 404 or self._protocol_for(endpoint) != "simple":
                raise
        fallback_url = self._systemone_url(endpoint)
        answer = self._call_server(fallback_url, text, question, options, criteria, checks)
        self.endpoint = fallback_url
        return answer

    def _call_server(
        self,
        url: str,
        text: str,
        question: str,
        options: list[str],
        criteria: Mapping[str, str | None] | None = None,
        checks: Mapping[str, YesNoCheck] | None = None,
    ) -> tuple[str, float, str | None, dict[str, float] | None]:
        protocol = self._protocol_for(url)
        body: dict[str, Any]
        if protocol == "systemone":
            definitions = criteria or {}
            questions: dict[str, Any] = {
                SYSTEMONE_QUESTION_KEY: {
                    "type": "choice",
                    "instructions": question,
                    "criteria": {option: definitions.get(option) for option in options},
                }
            }
            for name, check in (checks or {}).items():
                if name == SYSTEMONE_QUESTION_KEY:
                    raise ValueError(f"check name {name!r} collides with the main question")
                questions[name] = {
                    "type": "noul",
                    "instructions": check.question,
                    "criteria": {"true": check.true_means, "false": check.false_means},
                }
            body = {"state": text, "questions": questions}
        else:
            body = {"text": text, "question": question, "options": list(options)}
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            raw = response.read()
        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise DeciderResponseError("Decider server returned non-JSON payload") from exc
        if not isinstance(payload, dict):
            raise DeciderResponseError("Decider server returned a non-object JSON payload")
        reported = payload.get("model")
        model = reported[:200] if isinstance(reported, str) and reported else None
        if protocol == "systemone":
            verdict, confidence = self._parse_systemone(payload, options)
            return verdict, confidence, model, self._parse_checks(payload, checks) if checks else None
        return (*self._parse_simple(payload, options), model, None)

    @staticmethod
    def _parse_checks(payload: dict[str, Any], checks: Mapping[str, YesNoCheck]) -> dict[str, float]:
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            raise DeciderResponseError("systemone response missing 'answers'")
        result: dict[str, float] = {}
        for name in checks:
            answer = answers.get(name)
            value = answer.get("noul") if isinstance(answer, dict) else None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise DeciderResponseError(f"check {name!r}: P(true) {value!r} is not numeric")
            probability = float(value)
            if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
                raise DeciderResponseError(f"check {name!r}: P(true) {probability!r} outside [0, 1]")
            result[name] = probability
        return result

    def _parse_simple(self, payload: dict[str, Any], options: list[str]) -> tuple[str, float]:
        verdict = payload.get("verdict", payload.get("answer"))
        return self._validated(verdict, payload.get("confidence"), options)

    def _parse_systemone(self, payload: dict[str, Any], options: list[str]) -> tuple[str, float]:
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            raise DeciderResponseError("systemone response missing 'answers'")
        answer = answers.get(SYSTEMONE_QUESTION_KEY)
        if not isinstance(answer, dict):
            raise DeciderResponseError(f"systemone response missing answer '{SYSTEMONE_QUESTION_KEY}'")
        return self._validated(answer.get("choice"), answer.get("confidence"), options)

    @staticmethod
    def _validated(verdict: object, confidence: object, options: list[str]) -> tuple[str, float]:
        if not isinstance(verdict, str) or verdict not in options:
            raise DeciderResponseError(f"Decider verdict {verdict!r} is not one of {options}")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise DeciderResponseError(f"Decider confidence {confidence!r} is not numeric")
        value = float(confidence)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise DeciderResponseError(f"Decider confidence {value!r} outside [0, 1]")
        return verdict, value

    def _on_unreachable(
        self, exc: BaseException, text: str, question: str, options: list[str]
    ) -> tuple[str, float, str]:
        if self.mode != "auto":
            raise DeciderUnavailableError(f"Decider server unreachable at {self.endpoint}: {exc}") from exc
        if not self._warned_offline:
            logger.warning(
                "Decider 2B server unreachable at %s (%s); auto mode using heuristic fallback simulator",
                self.endpoint,
                exc,
            )
            self._warned_offline = True
        self._offline_until = time.monotonic() + self.offline_retry_s
        return self._run_heuristic(text, question, options)

    def _run_heuristic(self, text: str, question: str, options: list[str]) -> tuple[str, float, str]:
        return self.heuristic.decide_with_rationale(text, question, options, as_of=self.heuristic_as_of)
