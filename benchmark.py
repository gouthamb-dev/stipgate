#!/usr/bin/env python3
"""StipGate benchmark: Decider 2B edge gate + Nova Lite vs an LLM-only baseline.

Default run: 4 representative trailing documents with a rich table.
``--heldout``: a seeded 100-document synthetic set with ground-truth labels.

All prices and baseline latencies are ASSUMPTIONS held in the named constants below; verify
current prices on the Amazon Bedrock pricing page before quoting results.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Final

from rich.console import Console
from rich.table import Table

from stipgate.bedrock_worker import BedrockExtractor
from stipgate.decider_client import DeciderClient, DeciderError, DeciderMode
from stipgate.handlers import DEFAULT_CONFIDENCE_THRESHOLD, VERDICT_OPTIONS, build_criteria, build_question
from stipgate.pipeline import StipGatePipeline

# Sample builders live in stipgate.samples (shared with the web API); they are re-exported here so
# ``from benchmark import build_core_documents, w2_text, ...`` keeps working for tests and scripts.
from stipgate.samples import (
    BenchmarkDoc,
    bank_statement_text,
    build_core_documents,
    corrupt_ocr,
    paystub_text,
    utility_text,
    w2_text,
)

__all__ = [
    "BenchmarkDoc",
    "bank_statement_text",
    "build_core_documents",
    "corrupt_ocr",
    "paystub_text",
    "utility_text",
    "w2_text",
    "build_heldout_documents",
    "make_pipeline",
    "run_core_benchmark",
    "run_heldout",
    "main",
]

# --- Assumptions (named constants) -----------------------------------------------------------
FRONTIER_INPUT_USD_PER_1K: Final = 0.003  # LLM-only baseline: frontier model input price per 1K tokens
FRONTIER_OUTPUT_USD_PER_1K: Final = 0.015  # LLM-only baseline: frontier model output price per 1K tokens
BASELINE_LLM_LATENCY_MS: Final = 2800.0  # LLM-only baseline: end-to-end latency per document
BASELINE_PROMPT_OVERHEAD_TOKENS: Final = 350  # baseline system prompt + rubric tokens per document
BASELINE_OUTPUT_TOKENS: Final = 250  # baseline verdict + extraction output tokens per document
NOVA_LITE_INPUT_USD_PER_1K: Final = 0.00006  # Amazon Nova Lite input price per 1K tokens
NOVA_LITE_OUTPUT_USD_PER_1K: Final = 0.00024  # Amazon Nova Lite output price per 1K tokens
NOVA_LITE_ASSUMED_LATENCY_MS: Final = 650.0  # used only when extraction ran on the offline fallback
CHARS_PER_TOKEN: Final = 4
HELDOUT_SEED: Final = 20261019
HELDOUT_SIZE: Final = 100

HELDOUT_MIX: Final[dict[str, int]] = {
    "valid_paystub": 25,
    "valid_w2": 20,
    "stale_paystub": 12,
    "single_year_w2": 8,
    "utility_bill": 10,
    "bank_statement_under_income_code": 5,
    "corrupted_ocr": 12,
    "missing_fields_paystub": 8,
}
COMPLIANT_CATEGORIES: Final = frozenset({"valid_paystub", "valid_w2"})

BORROWERS: Final = (
    "Maria L. Chen",
    "Devon Okafor",
    "Priya Raman",
    "Luis Ortega",
    "Hannah Kowalski",
    "Samuel Adeyemi",
    "Grace Whitfield",
    "Tomas Lindqvist",
)
EMPLOYERS: Final = (
    "Cascade Freight Logistics LLC",
    "Summit Ridge Medical Group",
    "Bluewater Software Inc.",
    "Granite State Builders Co.",
    "Harborview School District",
    "Northwind Retail Partners",
)
EINS: Final = ("12-3456789", "47-1029384", "83-5550192", "26-7781034")


@dataclass
class BenchmarkRow:
    document_id: str
    category: str
    expected_action: str
    ground_truth_compliant: bool
    gated_action: str
    verdict: str
    confidence: float
    decider_latency_ms: float
    decider_backend: str | None
    tokens_spent: int
    extraction_is_fallback: bool | None
    stipgate_latency_ms: float
    stipgate_cost_usd: float
    baseline_tokens: int
    baseline_cost_usd: float
    baseline_latency_ms: float

    @property
    def savings_usd(self) -> float:
        return self.baseline_cost_usd - self.stipgate_cost_usd

    @property
    def savings_pct(self) -> float:
        return 100.0 * self.savings_usd / self.baseline_cost_usd if self.baseline_cost_usd else 0.0


@dataclass
class BenchmarkReport:
    rows: list[BenchmarkRow]
    latency_reduction_pct: float
    token_burn_prevention_pct: float
    cost_reduction_pct: float
    backends: dict[str, int]
    extraction_modes: dict[str, int]
    all_expected: bool


@dataclass
class HeldoutReport(BenchmarkReport):
    seed: int = HELDOUT_SEED
    accuracy_pct: float = 0.0
    hallucinated_approval_rate_pct: float = 0.0
    false_halt_rate_pct: float = 0.0
    p50_gate_latency_ms: float = 0.0
    p95_gate_latency_ms: float = 0.0
    per_category: dict[str, dict[str, int]] = field(default_factory=dict)

    def metrics(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "documents": len(self.rows),
            "ground_truth_compliant": sum(1 for r in self.rows if r.ground_truth_compliant),
            "cost_reduction_pct": round(self.cost_reduction_pct, 2),
            "token_burn_prevention_pct": round(self.token_burn_prevention_pct, 2),
            "latency_reduction_pct": round(self.latency_reduction_pct, 2),
            "accuracy_pct": round(self.accuracy_pct, 2),
            "hallucinated_approval_rate_pct": round(self.hallucinated_approval_rate_pct, 2),
            "false_halt_rate_pct": round(self.false_halt_rate_pct, 2),
            "p50_gate_latency_ms": round(self.p50_gate_latency_ms, 3),
            "p95_gate_latency_ms": round(self.p95_gate_latency_ms, 3),
            "decider_backends": self.backends,
            "extraction_modes": self.extraction_modes,
            "per_category": self.per_category,
        }


# --- Held-out document set ------------------------------------------------------------------


def build_heldout_documents(as_of: date, seed: int = HELDOUT_SEED, n: int = HELDOUT_SIZE) -> list[BenchmarkDoc]:
    """Seeded synthetic held-out set with ground-truth labels (deterministic for a given seed/as_of)."""
    if n != sum(HELDOUT_MIX.values()):
        raise ValueError(f"held-out mix is defined for {sum(HELDOUT_MIX.values())} documents, got n={n}")
    rng = random.Random(seed)
    year = as_of.year
    docs: list[BenchmarkDoc] = []
    for category, count in HELDOUT_MIX.items():
        for _ in range(count):
            borrower = rng.choice(BORROWERS)
            employer = rng.choice(EMPLOYERS)
            rate = round(rng.uniform(22.0, 68.0), 2)
            code = "STIP_INCOME_30DAY"
            if category == "valid_paystub":
                snippet = paystub_text(borrower, employer, as_of - timedelta(days=rng.randint(4, 20)), rate)
            elif category == "stale_paystub":
                snippet = paystub_text(borrower, employer, as_of - timedelta(days=rng.randint(45, 150)), rate)
            elif category == "missing_fields_paystub":
                snippet = paystub_text(
                    borrower,
                    employer,
                    as_of - timedelta(days=rng.randint(4, 20)),
                    rate,
                    include_employer=False,
                    include_ytd=False,
                )
            elif category in ("valid_w2", "single_year_w2"):
                code = "STIP_W2_2YR"
                base = round(rng.uniform(42000, 165000), 2)
                wages = {year - 1: base}
                if category == "valid_w2":
                    wages[year - 2] = round(base * rng.uniform(0.88, 1.02), 2)
                snippet = w2_text(borrower, employer, rng.choice(EINS), wages)
            elif category == "utility_bill":
                snippet = utility_text(borrower, as_of, round(rng.uniform(80, 420), 2), rng.randint(300, 1600))
            elif category == "bank_statement_under_income_code":
                opening = round(rng.uniform(2500, 48000), 2)
                snippet = bank_statement_text(
                    borrower, as_of, opening, round(rng.uniform(3000, 9000), 2), round(rng.uniform(1500, 7000), 2)
                )
            elif category == "corrupted_ocr":
                snippet = corrupt_ocr(
                    paystub_text(borrower, employer, as_of - timedelta(days=rng.randint(4, 20)), rate), rng
                )
            else:
                raise ValueError(f"unknown held-out category {category}")
            compliant = category in COMPLIANT_CATEGORIES
            docs.append(
                BenchmarkDoc(
                    document_id="",
                    category=category,
                    condition_code=code,
                    snippet=snippet,
                    expected_action="PROCEED" if compliant else "HALTED",
                    ground_truth_compliant=compliant,
                )
            )
    rng.shuffle(docs)
    return [
        BenchmarkDoc(f"HO-{i:03d}", d.category, d.condition_code, d.snippet, d.expected_action, d.ground_truth_compliant)
        for i, d in enumerate(docs, start=1)
    ]


# --- Execution & metrics ----------------------------------------------------------------------


def _baseline(snippet: str) -> tuple[int, float]:
    tokens_in = math.ceil(len(snippet) / CHARS_PER_TOKEN) + BASELINE_PROMPT_OVERHEAD_TOKENS
    cost = tokens_in * FRONTIER_INPUT_USD_PER_1K / 1000 + BASELINE_OUTPUT_TOKENS * FRONTIER_OUTPUT_USD_PER_1K / 1000
    return tokens_in + BASELINE_OUTPUT_TOKENS, cost


def make_pipeline(
    *,
    offline: bool,
    decider_url: str | None,
    threshold: float,
    as_of: date | None,
    decider_mode: DeciderMode | None = None,
    allow_heuristic_proceed: bool = False,
) -> StipGatePipeline:
    decider = DeciderClient(
        decider_url,
        mode=decider_mode,
        allow_heuristic_proceed=allow_heuristic_proceed,
        heuristic_as_of=as_of,
    )
    # Untimed warm-up call: loads the model on a live server, or trips the auto-mode offline
    # circuit breaker so per-document latencies reflect the backend actually serving them.
    # In server mode with no server the documents fail closed below; do not crash here.
    try:
        decider.decide(
            "Warm-up request issued by the StipGate benchmark before timing begins.",
            build_question("STIP_INCOME_30DAY"),
            list(VERDICT_OPTIONS),
            criteria=build_criteria("STIP_INCOME_30DAY"),
        )
    except DeciderError:
        pass
    return StipGatePipeline(
        decider=decider,
        extractor=BedrockExtractor(offline=offline),
        confidence_threshold=threshold,
    )


def _run_docs(pipeline: StipGatePipeline, docs: list[BenchmarkDoc]) -> list[BenchmarkRow]:
    rows: list[BenchmarkRow] = []
    for doc in docs:
        trace = pipeline.process_document(doc.document_id, doc.snippet, doc.condition_code)
        baseline_tokens, baseline_cost = _baseline(doc.snippet)
        proceeded = trace["gated_action"] == "PROCEED"
        cost = 0.0
        latency = float(trace["latency_ms"])
        if proceeded:
            cost = (
                int(trace["input_tokens"]) * NOVA_LITE_INPUT_USD_PER_1K / 1000
                + int(trace["output_tokens"]) * NOVA_LITE_OUTPUT_USD_PER_1K / 1000
            )
            latency += NOVA_LITE_ASSUMED_LATENCY_MS if trace["extraction_is_fallback"] else float(
                trace["extraction_latency_ms"]
            )
        rows.append(
            BenchmarkRow(
                document_id=doc.document_id,
                category=doc.category,
                expected_action=doc.expected_action,
                ground_truth_compliant=doc.ground_truth_compliant,
                gated_action=str(trace["gated_action"]),
                verdict=str(trace["verdict"]),
                confidence=float(trace["confidence"]),
                decider_latency_ms=float(trace["latency_ms"]),
                decider_backend=trace["decider_backend"],
                tokens_spent=int(trace["tokens_spent"]),
                extraction_is_fallback=trace["extraction_is_fallback"],
                stipgate_latency_ms=latency,
                stipgate_cost_usd=cost,
                baseline_tokens=baseline_tokens,
                baseline_cost_usd=baseline_cost,
                baseline_latency_ms=BASELINE_LLM_LATENCY_MS,
            )
        )
    return rows


def _aggregate(rows: list[BenchmarkRow]) -> dict[str, Any]:
    def pct_reduction(new: float, old: float) -> float:
        return 100.0 * (1.0 - new / old) if old else 0.0

    modes: Counter[str] = Counter()
    for r in rows:
        if r.gated_action == "PROCEED":
            modes["fallback" if r.extraction_is_fallback else "bedrock"] += 1
    return {
        "latency_reduction_pct": pct_reduction(
            sum(r.stipgate_latency_ms for r in rows), sum(r.baseline_latency_ms for r in rows)
        ),
        "token_burn_prevention_pct": pct_reduction(
            sum(r.tokens_spent for r in rows), sum(r.baseline_tokens for r in rows)
        ),
        "cost_reduction_pct": pct_reduction(
            sum(r.stipgate_cost_usd for r in rows), sum(r.baseline_cost_usd for r in rows)
        ),
        "backends": dict(Counter(r.decider_backend or "none" for r in rows)),
        "extraction_modes": dict(modes),
        "all_expected": all(r.gated_action == r.expected_action for r in rows),
    }


def run_core_benchmark(
    offline: bool,
    *,
    decider_url: str | None = None,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    as_of: date | None = None,
    decider_mode: DeciderMode | None = None,
    allow_heuristic_proceed: bool = False,
) -> BenchmarkReport:
    reference = as_of if as_of is not None else date.today()
    pipeline = make_pipeline(
        offline=offline,
        decider_url=decider_url,
        threshold=threshold,
        as_of=as_of,
        decider_mode=decider_mode,
        allow_heuristic_proceed=allow_heuristic_proceed,
    )
    rows = _run_docs(pipeline, build_core_documents(reference))
    return BenchmarkReport(rows=rows, **_aggregate(rows))


def run_heldout(
    offline: bool,
    seed: int = HELDOUT_SEED,
    *,
    decider_url: str | None = None,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    as_of: date | None = None,
    decider_mode: DeciderMode | None = None,
    allow_heuristic_proceed: bool = False,
) -> HeldoutReport:
    reference = as_of if as_of is not None else date.today()
    pipeline = make_pipeline(
        offline=offline,
        decider_url=decider_url,
        threshold=threshold,
        as_of=as_of,
        decider_mode=decider_mode,
        allow_heuristic_proceed=allow_heuristic_proceed,
    )
    rows = _run_docs(pipeline, build_heldout_documents(reference, seed))
    non_compliant = [r for r in rows if not r.ground_truth_compliant]
    compliant = [r for r in rows if r.ground_truth_compliant]
    hallucinated = sum(1 for r in non_compliant if r.gated_action == "PROCEED")
    false_halts = sum(1 for r in compliant if r.gated_action == "HALTED")
    latencies = [r.decider_latency_ms for r in rows]
    quantiles = statistics.quantiles(latencies, n=100, method="inclusive")
    per_category: dict[str, dict[str, int]] = {}
    for r in rows:
        bucket = per_category.setdefault(r.category, {"total": 0, "PROCEED": 0, "HALTED": 0, "correct": 0})
        bucket["total"] += 1
        bucket[r.gated_action] += 1
        bucket["correct"] += int(r.gated_action == r.expected_action)
    return HeldoutReport(
        rows=rows,
        seed=seed,
        accuracy_pct=100.0 * sum(1 for r in rows if r.gated_action == r.expected_action) / len(rows),
        hallucinated_approval_rate_pct=100.0 * hallucinated / len(non_compliant) if non_compliant else 0.0,
        false_halt_rate_pct=100.0 * false_halts / len(compliant) if compliant else 0.0,
        p50_gate_latency_ms=quantiles[49],
        p95_gate_latency_ms=quantiles[94],
        per_category=per_category,
        **_aggregate(rows),
    )


# --- Rendering ------------------------------------------------------------------------------


def _assumptions_note() -> str:
    return (
        f"Assumptions: LLM-only baseline = frontier model at ${FRONTIER_INPUT_USD_PER_1K}/1K input, "
        f"${FRONTIER_OUTPUT_USD_PER_1K}/1K output, {BASELINE_PROMPT_OVERHEAD_TOKENS} prompt-overhead + "
        f"{BASELINE_OUTPUT_TOKENS} output tokens, {BASELINE_LLM_LATENCY_MS:.0f} ms per document. "
        f"Nova Lite = ${NOVA_LITE_INPUT_USD_PER_1K}/1K input, ${NOVA_LITE_OUTPUT_USD_PER_1K}/1K output, "
        f"{NOVA_LITE_ASSUMED_LATENCY_MS:.0f} ms assumed when extraction ran on the offline fallback. "
        f"Tokens estimated at {CHARS_PER_TOKEN} chars/token. Verify current Bedrock pricing."
    )


def _backend_note(report: BenchmarkReport) -> str:
    backends = ", ".join(f"{k}={v}" for k, v in sorted(report.backends.items()))
    modes = ", ".join(f"{k}={v}" for k, v in sorted(report.extraction_modes.items())) or "none"
    return f"Decider backend: {backends}  |  Extraction: {modes}"


def render_core(console: Console, report: BenchmarkReport) -> None:
    table = Table(title="StipGate - Decider 2B Edge Gate Benchmark", show_lines=False)
    table.add_column("Document ID", style="bold")
    table.add_column("Document Category")
    table.add_column("Decider 2B Latency (ms)", justify="right")
    table.add_column("Guardrail Action", justify="center")
    table.add_column("Tokens Burned", justify="right")
    table.add_column("Cost Savings vs LLM-only baseline", justify="right")
    for r in report.rows:
        color = "green" if r.gated_action == "PROCEED" else "red"
        table.add_row(
            r.document_id,
            r.category,
            f"{r.decider_latency_ms:.2f}",
            f"[{color}]{r.gated_action}[/{color}]",
            str(r.tokens_spent),
            f"${r.savings_usd:.5f} ({r.savings_pct:.1f}%)",
        )
    console.print(table)
    console.print(_backend_note(report))
    console.print(
        f"Aggregate latency reduction: [bold]{report.latency_reduction_pct:.1f}%[/bold]  |  "
        f"Token burn prevention: [bold]{report.token_burn_prevention_pct:.1f}%[/bold]  |  "
        f"Cost reduction: [bold]{report.cost_reduction_pct:.1f}%[/bold]"
    )
    status = "[green]all outcomes match expectations[/green]" if report.all_expected else "[red]UNEXPECTED OUTCOME[/red]"
    console.print(f"Expected PROCEED/HALTED/PROCEED/HALTED: {status}")
    console.print(f"[dim]{_assumptions_note()}[/dim]")


def render_heldout(console: Console, report: HeldoutReport) -> None:
    cats = Table(title=f"Held-out synthetic set (n={len(report.rows)}, seed={report.seed}) - per category")
    for col in ("Category", "Docs", "PROCEED", "HALTED", "Correct"):
        cats.add_column(col, justify="right" if col != "Category" else "left")
    for name, b in report.per_category.items():
        cats.add_row(name, str(b["total"]), str(b["PROCEED"]), str(b["HALTED"]), str(b["correct"]))
    console.print(cats)

    metrics = Table(title="Held-out metrics")
    metrics.add_column("Metric")
    metrics.add_column("Value", justify="right")
    m = report.metrics()
    metrics.add_row("Cost reduction vs LLM-only baseline", f"{m['cost_reduction_pct']:.2f}%")
    metrics.add_row("Token burn prevention", f"{m['token_burn_prevention_pct']:.2f}%")
    metrics.add_row("Aggregate latency reduction", f"{m['latency_reduction_pct']:.2f}%")
    metrics.add_row("Gate accuracy vs ground truth", f"{m['accuracy_pct']:.2f}%")
    metrics.add_row("Hallucinated approvals (non-compliant approved)", f"{m['hallucinated_approval_rate_pct']:.2f}%")
    metrics.add_row("False halts (compliant sent to human)", f"{m['false_halt_rate_pct']:.2f}%")
    metrics.add_row("Gate latency p50 (ms)", f"{m['p50_gate_latency_ms']:.3f}")
    metrics.add_row("Gate latency p95 (ms)", f"{m['p95_gate_latency_ms']:.3f}")
    console.print(metrics)
    console.print(_backend_note(report))
    console.print(f"[dim]{_assumptions_note()}[/dim]")


def _render_decider_errors(console: Console, report: BenchmarkReport) -> None:
    failed = sum(1 for r in report.rows if r.verdict == "decider_error")
    if failed:
        console.print(
            f"[red]Decider 2B unavailable: {failed} document(s) failed closed to human review "
            "(no tokens spent). Start the Decider server or pass --heuristic for an offline demo.[/red]"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="StipGate Decider 2B edge-gate benchmark")
    parser.add_argument("--heldout", action="store_true", help="run the seeded 100-document held-out set")
    parser.add_argument("--offline", action="store_true", help="skip Amazon Bedrock; use fallback extraction")
    parser.add_argument("--seed", type=int, default=HELDOUT_SEED, help="held-out generator seed")
    parser.add_argument("--decider-url", default=None, help="Decider 2B endpoint (default STIPGATE_DECIDER_URL or :8099/decide)")
    parser.add_argument("--threshold", type=float, default=DEFAULT_CONFIDENCE_THRESHOLD, help="gate confidence threshold")
    parser.add_argument("--json-out", type=Path, default=None, help="write metrics JSON to this path")
    parser.add_argument(
        "--decider-mode",
        choices=["server", "heuristic", "auto"],
        default=None,
        help="Decider backend (default STIPGATE_DECIDER_MODE or 'server', which fails closed without a server)",
    )
    parser.add_argument(
        "--allow-heuristic-proceed",
        action="store_true",
        help="let heuristic verdicts clear conditions (CI/demo only)",
    )
    parser.add_argument(
        "--heuristic",
        action="store_true",
        help="offline demo shorthand for --decider-mode heuristic --allow-heuristic-proceed",
    )
    args = parser.parse_args(argv)

    decider_mode: DeciderMode | None = args.decider_mode
    allow_heuristic_proceed: bool = args.allow_heuristic_proceed
    if args.heuristic:
        decider_mode = "heuristic"
        allow_heuristic_proceed = True

    console = Console()
    if args.heldout:
        heldout = run_heldout(
            args.offline,
            args.seed,
            decider_url=args.decider_url,
            threshold=args.threshold,
            decider_mode=decider_mode,
            allow_heuristic_proceed=allow_heuristic_proceed,
        )
        render_heldout(console, heldout)
        _render_decider_errors(console, heldout)
        payload: dict[str, Any] = heldout.metrics()
        exit_code = 0
    else:
        core = run_core_benchmark(
            args.offline,
            decider_url=args.decider_url,
            threshold=args.threshold,
            decider_mode=decider_mode,
            allow_heuristic_proceed=allow_heuristic_proceed,
        )
        render_core(console, core)
        _render_decider_errors(console, core)
        payload = {
            "latency_reduction_pct": round(core.latency_reduction_pct, 2),
            "token_burn_prevention_pct": round(core.token_burn_prevention_pct, 2),
            "cost_reduction_pct": round(core.cost_reduction_pct, 2),
            "decider_backends": core.backends,
            "extraction_modes": core.extraction_modes,
            "rows": [asdict(r) for r in core.rows],
        }
        exit_code = 0 if core.all_expected else 1
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        console.print(f"Metrics written to {args.json_out}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
