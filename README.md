# StipGate

**A Strands Decider 2B guardrail that checks a borrower's mortgage document before any LLM token is spent on it.**

[![CI](https://github.com/gouthamb-dev/stipgate/actions/workflows/ci.yml/badge.svg)](https://github.com/gouthamb-dev/stipgate/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

`#strands-decider-2b` `#guardrails` · Built for the AWS Builder Center *Agent Experience Hackathon: Strands Decider*

A Strands agent wants to call `clear_underwriting_condition` on an uploaded document. Before the tool runs,
a Strands `InterventionHandler` asks Decider 2B one typed question: *is this the document the condition
requires?* Only a confident `fully_compliant` lets Amazon Bedrock Nova Lite extract the figures. Everything
else is halted with **zero LLM tokens** and sent to a human loan officer. Every error fails closed.

| | |
|---|---|
| **Decision pattern** | Guardrails: verify a tool call's arguments (the document) before the tool runs |
| **Decider 2B question** | one `choice` question, three typed options, a one-line definition per option |
| **Bedrock** | Nova Lite (`amazon.nova-lite-v1:0`) for extraction, and for OCR of scanned PDFs / images |
| **Reusable asset** | `StipulationGuardrailHandler` + `build_question` / `build_criteria` ([recipe below](#the-reusable-decision-pattern)) |
| **Runs on** | laptop (heuristic demo or local Decider), Docker Compose, or AWS (EC2 + CloudFront scripts included) |

---

## Results on the live AWS deployment

Real Decider 2B (int8, CPU) and real Nova Lite, measured through CloudFront in October 2026:

| Document | Expected | Decider 2B verdict | Confidence | Gate | Nova Lite |
|---|---|---|---|---|---|
| SG-001 valid 30-day paystub | PROCEED | `fully_compliant` | 0.941 | PROCEED, **cleared** | 450 tokens, ~0.5 s |
| SG-002 utility bill sent as income proof | HALTED | `invalid_unrelated_doc` | 0.816 | HALTED | **0 tokens** |
| SG-003 two years of W-2s | PROCEED | `fully_compliant` | 0.910 | PROCEED, **cleared** | 584-639 tokens, ~0.4 s |
| SG-004 truncated / corrupted OCR scan | HALTED | `ambiguous_requires_human_review` | 0.174 | HALTED | **0 tokens** |
| PNG photo of SG-001 (upload) | PROCEED | `fully_compliant` | 0.948 | PROCEED, **cleared** | OCR ~2,000 tokens + 437 extraction |

The gate itself takes 2.6-3.9 s per document on this 2-vCPU instance. Upstream publishes ~115 ms median on
an RTX 3090 ([inference.md](https://github.com/strands-labs/strands-decider/blob/main/docs/inference.md)),
so a GPU host is the way to a real-time gate.

---

## Architecture

```mermaid
flowchart TB
    subgraph Clients["Clients"]
        UI["Test UI (GET /)<br/>paste text · upload .txt / PDF / image"]
        LOS["LOS / POS webhook<br/>or any HTTP client"]
        AG["Strands Agent<br/>build_agent()"]
    end

    subgraph API["StipGate API · FastAPI · X-API-Key on /v1/*"]
        EV["POST /v1/documents/evaluate<br/>(OCR text)"]
        UP["POST /v1/documents/upload<br/>(raw file, max 4 MB)"]
        OCR["DocumentTextExtractor<br/>.txt → decode · text PDF → pypdf (local)<br/>scanned PDF / image → Nova Lite OCR"]
        PIPE["StipGatePipeline"]
    end

    subgraph Gate["Decision: StipulationGuardrailHandler (strands.interventions)"]
        Q["build_question(code) + build_criteria(code)<br/>options: fully_compliant · invalid_unrelated_doc ·<br/>ambiguous_requires_human_review"]
        DEC{{"Strands Decider 2B<br/>local sidecar, loopback / private network"}}
        RULE{"fully_compliant<br/>AND confidence ≥ 0.88?"}
    end

    subgraph Gen["Generation: Amazon Bedrock (us-east-1)"]
        NOVA["Nova Lite Converse<br/>→ IncomeClearancePayload (Pydantic)"]
    end

    subgraph Out["Outcomes"]
        CLR["cleared<br/>payload returned"]
        PER["pending_extraction_review<br/>(extraction fell back)"]
        HALT["HALTED · 0 tokens<br/>Guide(feedback) to the agent"]
        RQ[("Human review queue<br/>ReviewTicket")]
    end

    UI --> EV & UP
    LOS --> EV & UP
    UP --> OCR --> PIPE
    EV --> PIPE
    AG -- "before_tool_call:<br/>clear_underwriting_condition" --> Q
    PIPE --> Q --> DEC --> RULE
    RULE -- "yes: Proceed" --> NOVA
    RULE -- "no / timeout / error" --> HALT --> RQ
    NOVA -- valid JSON --> CLR
    NOVA -- "no creds / bad JSON" --> PER --> RQ
    PIPE -. "stipgate.audit JSON line<br/>(no document text)" .-> LOG[("logs / CloudWatch")]
```

AWS deployment used by the live demo (scripts in [`deploy/aws/`](deploy/aws/README.md)):

```mermaid
flowchart LR
    B["Browser / client"] -- "HTTPS" --> CF["CloudFront"]
    CF -- ":8000, CloudFront prefix list only" --> EC2
    subgraph EC2["EC2 m7i-flex.large (AL2023, IMDSv2)"]
        A["stipgate-api"] -- "private docker network" --> D["decider (int8 CPU)"]
    end
    A -- "instance role: InvokeModel on Nova Lite only" --> BR["Bedrock Nova Lite"]
    EC2 -. boot .-> S3["S3 source tarball"]
    EC2 -. key .-> SSM["SSM /stipgate/api-key"]
```

Sequence diagram, trust boundaries, PII flow and the fail-closed matrix: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## The reusable decision pattern

The asset other builders can drop into their own agent: **a typed question, an option set with a definition
per option, and the wiring that puts it in front of a tool.**

### 1. The question and options

```python
# stipgate/handlers.py
VERDICT_OPTIONS = ("fully_compliant", "invalid_unrelated_doc", "ambiguous_requires_human_review")

CONDITION_DOCUMENTS = {
    "STIP_INCOME_30DAY": "a paystub (earnings statement) from the borrower's employer",
    "STIP_W2_2YR": "IRS Form W-2 wage statements for the borrower covering two tax years",
    "STIP_ASSET_2MO": "two monthly bank statements for the borrower",
}

QUESTION_TEMPLATE = (
    "Stipulation {condition_code}: classify this document for the mortgage condition that requires {document}."
)

def build_criteria(condition_code: str) -> dict[str, str]:
    document = required_document(condition_code)
    return {
        "fully_compliant": f"It is {document} and is readable and complete.",
        "invalid_unrelated_doc": f"It is not {document}; it is another kind of document.",
        "ambiguous_requires_human_review": "It is garbled, cut off or unreadable.",
    }
```

What goes over the wire (official `strands-decider serve` contract, `POST /v1/systemone` or `/invocations`):

```json
{"state": "<document text>",
 "questions": {"stipulation_verdict": {
   "type": "choice",
   "instructions": "Stipulation STIP_INCOME_30DAY: classify this document for the mortgage condition that requires a paystub (earnings statement) from the borrower's employer.",
   "criteria": {
     "fully_compliant": "It is a paystub (earnings statement) from the borrower's employer and is readable and complete.",
     "invalid_unrelated_doc": "It is not a paystub (earnings statement) from the borrower's employer; it is another kind of document.",
     "ambiguous_requires_human_review": "It is garbled, cut off or unreadable."}}}}
```

> **Lesson learned.** The first version sent bare option names (`"criteria": {"fully_compliant": null, ...}`)
> and a vague question. On the live model every sample came back `invalid_unrelated_doc`, including valid
> paystubs. Giving each option a one-line definition and naming the required document in the question fixed
> all four verdicts and lifted valid documents from 0.33-0.59 to 0.91-0.94 confidence, with the 0.88
> threshold unchanged.

### 2. The wiring: a Strands intervention in front of the tool

```python
from strands import Agent
from strands.models.bedrock import BedrockModel
from stipgate import (BedrockExtractor, DeciderClient, InMemoryReviewQueue,
                      StipulationGuardrailHandler, make_clearance_tool)

decider = DeciderClient("http://127.0.0.1:8099/v1/systemone", timeout_s=1.5, mode="server")
queue = InMemoryReviewQueue()            # or anything with enqueue(ticket) -> None: SQS, an LOS task API
handler = StipulationGuardrailHandler(
    decider,
    confidence_threshold=0.88,           # Proceed only on fully_compliant AND confidence >= 0.88
    review_queue=queue,
    target_tool="clear_underwriting_condition",
)

agent = Agent(
    model=BedrockModel(model_id="amazon.nova-lite-v1:0", region_name="us-east-1"),
    tools=[make_clearance_tool(BedrockExtractor(), queue)],
    interventions=[handler],             # before_tool_call -> Proceed() or Guide(feedback)
    callback_handler=None,
)
agent("Borrower uploaded doc DOC-77 for STIP_INCOME_30DAY: <ocr text>")
```

On a halt the handler returns `Guide(feedback=...)`; Strands cancels the tool, the model receives the
guidance as the tool result, and a `ReviewTicket` is queued. `build_agent(StipGatePipeline())` does the same
wiring. Without an agent loop: `StipGatePipeline(decider=decider).process_document(doc_id, text, code)`.

> The spec this was built from referenced `strands.handlers.InterventionHandler`. That path does not exist in
> strands-agents 1.61.1; the real API is `strands.interventions`, which StipGate subclasses directly.

### 3. Adapting it to your own agent

Swap the domain, keep the shape:

```python
from stipgate import DeciderClient

result = DeciderClient("http://127.0.0.1:8099/v1/systemone").decide_detailed(
    text=user_supplied_document,
    question="Classify this document for the expense claim that requires an itemised hotel receipt.",
    options=["valid", "wrong_document", "needs_human_review"],
    criteria={
        "valid": "It is an itemised hotel receipt and is readable and complete.",
        "wrong_document": "It is not an itemised hotel receipt; it is another kind of document.",
        "needs_human_review": "It is garbled, cut off or unreadable.",
    },
)
if result.verdict == "valid" and result.confidence >= 0.88:
    ...  # call the expensive tool / LLM
else:
    ...  # halt, ask the user, or route to a person
```

Rules that made it work: name the required thing in the question, define every option in one sentence,
include an explicit "send to a human" option, act only above a threshold, and treat every error as a halt.

---

## Quick start

Python 3.11+.

```bash
git clone https://github.com/gouthamb-dev/stipgate.git
cd stipgate
python -m venv .venv && source .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e ".[dev]"
python -m pytest -q                                      # offline, no AWS or model needed
```

**1. Offline demo (no model, no AWS).** Verdicts come from a built-in heuristic and the UI shows a yellow
"simulated" banner.

```bash
STIPGATE_DECIDER_MODE=heuristic STIPGATE_ALLOW_HEURISTIC_PROCEED=1 STIPGATE_BEDROCK_OFFLINE=1 stipgate-serve
# PowerShell: $env:STIPGATE_DECIDER_MODE="heuristic"; $env:STIPGATE_ALLOW_HEURISTIC_PROCEED="1"; $env:STIPGATE_BEDROCK_OFFLINE="1"; stipgate-serve
```

Open http://127.0.0.1:8000/, load a sample, click **Evaluate document**.

**2. Real Decider 2B + Nova Lite.** Serve the model (GPU strongly recommended; see
[docs/DECIDER_HOSTING.md](docs/DECIDER_HOSTING.md)), have AWS credentials with Nova Lite access in us-east-1,
then:

```bash
pip install strands-decider
strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 --port 8099 --device cuda   # or mps | mlx | cpu
export STIPGATE_DECIDER_URL=http://127.0.0.1:8099/v1/systemone
stipgate-serve
```

**3. Docker Compose.**

```bash
docker compose up --build decider stipgate-api            # API + CPU Decider sidecar on http://127.0.0.1:8000/
docker compose --profile demo up --build stipgate-demo    # heuristic demo on http://127.0.0.1:8001/
```

**4. AWS.** One EC2 instance behind CloudFront, deployed and updated with PowerShell + AWS CLI:

```powershell
pwsh deploy/aws/deploy.ps1          # S3, SSM API key, IAM role, security group, EC2 + bootstrap
pwsh deploy/aws/cloudfront.ps1      # HTTPS URL, locks the instance to CloudFront
pwsh deploy/aws/smoke-test.ps1 -BaseUrl https://<id>.cloudfront.net
pwsh deploy/aws/update-api.ps1      # ship code changes later
pwsh deploy/aws/teardown.ps1        # delete everything
```

Full guide, IAM, costs and security notes: [deploy/aws/README.md](deploy/aws/README.md). A production
design (ECS on EC2 GPU sidecar, internal ALB, PrivateLink, Secrets Manager): [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## API

| Method + path | Auth | Purpose |
|---|---|---|
| `GET /` | open | Self-contained test UI (no external assets, strict CSP) |
| `GET /healthz` | open | Liveness + Decider reachability, mode, Bedrock config, warnings |
| `POST /v1/documents/evaluate` | key | Gate OCR text. Body `{document_id, condition_code, snippet}` |
| `POST /v1/documents/upload?document_id=&condition_code=` | key | Raw file body (PDF, PNG, JPEG, GIF, WebP, UTF-8 text; max 4 MB). Extracts text, then gates it |
| `GET /v1/review-queue` | key | Tickets for loan officers |
| `GET /v1/samples` | key | The four benchmark documents |
| `GET /v1/metrics` | key | Counters, OCR tokens, gate latency p50 / p95 |

```bash
curl -s -X POST https://<host>/v1/documents/evaluate \
  -H "Content-Type: application/json" -H "X-API-Key: $STIPGATE_API_KEY" \
  -d '{"document_id":"DOC-77","condition_code":"STIP_INCOME_30DAY","snippet":"EARNINGS STATEMENT\nEmployer: ..."}'

curl -s -X POST "https://<host>/v1/documents/upload?document_id=DOC-78&condition_code=STIP_W2_2YR" \
  -H "X-API-Key: $STIPGATE_API_KEY" -H "Content-Type: application/pdf" --data-binary @w2s.pdf
```

Key fields in the response: `gated_action` (`PROCEED` / `HALTED`), `verdict`, `confidence`,
`clearance_status` (`cleared` / `pending_extraction_review` / `halted`), `tokens_spent`, `extracted_payload`,
`review_ticket_id`. Uploads add `ocr` (method, pages, tokens) and `extracted_text`.

How uploads are read: `.txt` is decoded and a PDF with a text layer is read with `pypdf`, both locally with 0
tokens. A scanned PDF or an image is transcribed by Nova Lite **before** the gate, so it spends OCR tokens even
when the document is then halted (reported separately as `ocr.total_tokens` / `ocr_tokens_spent`).

Every environment variable, PowerShell examples and response details: [docs/USAGE.md](docs/USAGE.md).

---

## Fail closed

The gate halts whenever anything is uncertain. Each row below is covered by a test
([full matrix](docs/ARCHITECTURE.md#fail-closed-matrix)).

- Decider unreachable, timeout, HTTP 4xx/5xx, non-JSON, a verdict outside the options, or a confidence that is
  not a number in [0, 1] → HALTED, verdict `decider_error`, 0 tokens, review ticket.
- `fully_compliant` below 0.88, or any other verdict → HALTED.
- The heuristic simulator runs only on explicit opt-in and can never clear a condition unless
  `STIPGATE_ALLOW_HEURISTIC_PROCEED=1` (CI / demo only).
- Bedrock failure after the gate passed → `pending_extraction_review`, never `cleared`; synthetic fields are listed.
- For W-2s, `ytd_gross` is forced to equal the latest year's `gross_pay` if the model sums several years.
- `on_error="deny"`: a defect outside the handler still blocks the tool.

---

## Benchmark

```bash
python benchmark.py --offline --heuristic                  # 4 documents, expects PROCEED/HALTED/PROCEED/HALTED
python benchmark.py --decider-mode server                  # against a live Decider (+ Nova Lite with AWS creds)
python benchmark.py --heldout --decider-mode server        # seeded 100-document synthetic held-out set
```

The held-out set (seed `20261019`) has 45 compliant documents (paystubs, two-year W-2s) and 55 non-compliant
ones (stale paystubs, single-year W-2s, utility bills, bank statements under an income code, corrupted scans,
missing fields). It reports cost reduction, hallucinated approvals, false halts, accuracy and gate p50 / p95.

| Run | Backend | Hallucinated approvals | False halts | Accuracy |
|---|---|---|---|---|
| `--heldout --offline --heuristic` | heuristic simulator (opted in) | 0% (0/55) | 0% (0/45) | 100% |
| `--heldout --offline`, no Decider server | none: every document failed closed | 0% (0/55) | 100% (45/45) | 55% |

These held-out numbers come from the heuristic, which was written alongside the synthetic set, so they are
optimistic and are **not** Decider 2B results. The live Decider 2B numbers are the table at the top. Pricing
constants used for cost estimates are named in `benchmark.py`; verify them against current Bedrock pricing.

---

## Project layout

```
stipgate/
├── stipgate/
│   ├── handlers.py          # StipulationGuardrailHandler, question/criteria builders, review queue
│   ├── decider_client.py    # DeciderClient (server | heuristic | auto), wire contracts, HeuristicDecider
│   ├── bedrock_worker.py    # BedrockExtractor (Nova Lite Converse) + IncomeClearancePayload
│   ├── ocr.py               # DocumentTextExtractor: .txt / pypdf / Nova Lite OCR for uploads
│   ├── pipeline.py          # StipGatePipeline, make_clearance_tool, build_agent
│   ├── api.py               # FastAPI service, auth, metrics, audit log, stipgate-serve
│   ├── samples.py           # synthetic benchmark / UI documents
│   └── static/index.html    # accessible single-file test UI
├── tests/                   # 120 offline tests (pytest), strict mypy
├── benchmark.py             # 4-document run + 100-document held-out set
├── deploy/aws/              # deploy, CloudFront, update, rotate key, smoke test, teardown
├── docker/decider/          # CPU Decider sidecar image (PyPI strands-decider)
├── docs/                    # ARCHITECTURE, DECIDER_HOSTING, DEPLOYMENT, USAGE
├── Dockerfile               # API image (non-root, healthcheck)
├── docker-compose.yml
└── .github/workflows/ci.yml # tests + mypy + benchmark + image build
```

## Docs

| Doc | Covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, request sequence, trust boundaries, PII flow, fail-closed matrix, observability |
| [docs/DECIDER_HOSTING.md](docs/DECIDER_HOSTING.md) | Running Decider 2B: hosting options, serve flags, contract checks, latency tuning |
| [deploy/aws/README.md](deploy/aws/README.md) | The AWS deployment this demo runs on, step by step |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Production reference design on AWS (guide only) |
| [docs/USAGE.md](docs/USAGE.md) | Every env var, API examples, uploads, embedding the handler |

## Security

- Set `STIPGATE_API_KEY` before exposing the API beyond loopback. Without it `/v1/*` is unauthenticated and
  responses carry borrower data. The key is shared, not per user.
- The Decider server has no authentication. Keep it on loopback or a private network.
- Audit log lines carry no document text and no extracted payload. Review tickets hold a 240-character preview.
- All sample documents and names in this repo are synthetic.
- Decider 2B is an experimental model. StipGate is a hackathon project and not a production underwriting system.

## License

[MIT](LICENSE)
