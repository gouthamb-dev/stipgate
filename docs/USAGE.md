# Using StipGate

Install, configure, test, benchmark, run the API + test UI, and embed the gate in your own Strands agent.
For hosting the model see [DECIDER_HOSTING.md](DECIDER_HOSTING.md); for AWS see [DEPLOYMENT.md](DEPLOYMENT.md).

## Install

Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e ".[dev]"        # stipgate-serve console script + mypy + httpx (API tests)
```

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[dev]"
```

## Configuration (environment variables)

| Variable | Default | Effect |
|---|---|---|
| `STIPGATE_DECIDER_URL` | `http://127.0.0.1:8099/decide` | Decider endpoint. Paths ending `/v1/systemone` or `/invocations` use the official contract; `/decide` retries once on `/v1/systemone` after a 404. |
| `STIPGATE_DECIDER_MODE` | `server` | `server`: real Decider only, outage = HALTED + human review. `heuristic`: offline simulator only. `auto`: server first, heuristic on outage (30 s breaker). Invalid value = startup error. |
| `STIPGATE_DECIDER_TIMEOUT_S` | `1.5` | Per-request Decider timeout in seconds (> 0). Use 5 for a CPU Decider. |
| `STIPGATE_DECIDER_MAX_CONCURRENCY` | `1` | Max in-flight Decider requests per client (integer >= 1). |
| `STIPGATE_ALLOW_HEURISTIC_PROCEED` | off | `1` / `true` / `yes`: heuristic verdicts may clear conditions. CI / demo only. Without it a heuristic `fully_compliant` is HALTED. |
| `STIPGATE_BEDROCK_OFFLINE` | off | `1` / `true` / `yes` (API only): never call Bedrock; extraction uses the synthetic fallback and every gated document is `pending_extraction_review`. |
| `STIPGATE_API_KEY` | unset | When set, every `/v1/*` route requires a matching `X-API-Key` header. Unset = **unauthenticated**. |
| `STIPGATE_HOST` | `127.0.0.1` | `stipgate-serve` bind address. A non-loopback host without an API key logs a warning. |
| `STIPGATE_PORT` | `8000` | `stipgate-serve` port (1-65535). |
| `AWS_PROFILE` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` | unset | Standard boto3 credential chain for Nova Lite. No credentials = fallback extraction (`pending_extraction_review`). Under `docker compose` only the three static key variables pass through; `AWS_PROFILE` needs a read-only `~/.aws` mount (see the comments in `docker-compose.yml`), because an empty `AWS_PROFILE=""` makes boto3 raise `ProfileNotFound`. |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | n/a | Ignored for Bedrock: the region is fixed to `us-east-1` in code (`BedrockExtractor(region_name=...)` to change it). |
| `HF_HOME` | Hugging Face default | Decider server only: model cache location (~4.5 GB base model). |

## Tests and type checks

```bash
python -m pytest -q      # offline: urlopen is patched, no network, no AWS
python -m mypy           # strict
```

## Benchmark

```bash
python benchmark.py --offline --heuristic        # offline demo: PROCEED / HALTED / PROCEED / HALTED, exit 0
python benchmark.py --decider-mode server        # live Decider at STIPGATE_DECIDER_URL (+ Nova Lite if AWS credentials exist)
python benchmark.py --heldout --offline --heuristic --json-out heldout.json   # 100-document held-out set
python benchmark.py --heldout --decider-mode server --offline                 # held-out against the live Decider
```

`--heuristic` is shorthand for `--decider-mode heuristic --allow-heuristic-proceed`. Other flags:
`--decider-url`, `--threshold`, `--seed`, `--json-out`.

Without a Decider server and without `--heuristic`, the default `server` mode fails closed. Every
document is HALTED with verdict `decider_error` and 0 tokens, the backend line reads `Decider backend:
none=4`, the run prints

```
Decider 2B unavailable: 4 document(s) failed closed to human review (no tokens spent). Start the Decider server or pass --heuristic for an offline demo.
```

and the 4-document run exits 1 (outcomes differ from the expected PROCEED/HALTED/PROCEED/HALTED). The
held-out run exits 0 and reports 100% false halts. Each refused connection waits for the timeout, so
expect ~1.5 s per document.

## Run the API and test UI

```powershell
$env:STIPGATE_DECIDER_MODE = "heuristic"; $env:STIPGATE_ALLOW_HEURISTIC_PROCEED = "1"; $env:STIPGATE_BEDROCK_OFFLINE = "1"; stipgate-serve
```

```bash
STIPGATE_DECIDER_MODE=heuristic STIPGATE_ALLOW_HEURISTIC_PROCEED=1 STIPGATE_BEDROCK_OFFLINE=1 stipgate-serve
```

That is the offline demo (simulated verdicts, synthetic extraction). With a real Decider, set
`STIPGATE_DECIDER_URL=http://127.0.0.1:8099/v1/systemone` and leave the mode at `server`.

Equivalent entry points: `python -m stipgate.api` and
`uvicorn stipgate.api:create_app --factory --host 127.0.0.1 --port 8000`.
`stipgate-serve --help` lists `--host`, `--port`, `--log-level`.

Open http://127.0.0.1:8000/. The page shows a health strip (Decider mode, reachability, auth), a yellow
banner whenever verdicts are simulated, an optional API-key field (kept in memory, sent as `X-API-Key`),
four sample buttons, a document form (Ctrl+Enter submits), the result (PROCEED / HALTED badge with icon
and word, verdict, confidence, latency, tokens, clearance status, extracted payload, synthetic-field
note), and the review queue table.

> **Security.** Without `STIPGATE_API_KEY` the API is unauthenticated, and responses include borrower
> data. Keep it on `127.0.0.1`, or set a key and put it behind TLS. Never expose it publicly without one.

### Endpoints

| Method + path | Auth | Purpose |
|---|---|---|
| `GET /healthz` | open | Liveness (always 200) + `decider.reachable` readiness, mode, Bedrock config, warnings. |
| `POST /v1/documents/evaluate` | key | Gate one document. Body `{document_id, condition_code, snippet}`; returns the pipeline trace + `heuristic_warning`. |
| `POST /v1/documents/upload?document_id=...&condition_code=...` | key | Raw file body (PDF, PNG, JPEG, GIF, WebP or UTF-8 text, max 4 MB). Extracts the text, then gates it like `/evaluate`. Adds `ocr` (method, pages, chars, tokens) and `extracted_text` to the trace. |
| `GET /v1/review-queue` | key | Tickets for loan officers (`count`, `items`). |
| `GET /v1/samples` | key | The four synthetic benchmark documents. |
| `GET /v1/metrics` | key | In-process counters and gate latency p50 / p95. |
| `GET /` | open | Test UI. |

Request rules: `document_id` 1-64 chars of `[A-Za-z0-9._:-]`; `condition_code` one of `STIP_INCOME_30DAY`,
`STIP_W2_2YR`, `STIP_ASSET_2MO`; `snippet` 1-50,000 chars; no extra fields. Violations return 422.

### Uploading a document

The UI's "Upload a file" field handles two cases:

- `.txt`: read in the browser and placed in the OCR text box. Nothing is sent until you choose Evaluate document.
- PDF or image: "Upload and evaluate file" sends the raw bytes to `/v1/documents/upload`. The extracted text
  is shown in the OCR text box afterwards so you can see exactly what the gate read.

How the server gets the text (file type comes from magic bytes, not the Content-Type or file name):

| Input | Method (`ocr.method`) | Leaves the host? | Tokens |
|---|---|---|---|
| UTF-8 text | `plain_text` | no | 0 |
| PDF with a text layer (>= 40 visible chars) | `pdf_text_layer` (pypdf) | no | 0 |
| Scanned PDF, or PNG / JPEG / GIF / WebP | `nova_lite_ocr` (Converse `document` / `image` block) | yes, to Bedrock | reported in `ocr.total_tokens` |

Nova Lite OCR runs **before** the gate, so it spends tokens even when the document is then HALTED. Those
tokens are not in `tokens_spent`; `/v1/metrics` tracks them as `ocr_tokens_spent`. It uses the same Bedrock
client and IAM permission (`bedrock:InvokeModel` on Nova Lite) as extraction.

Errors: 413 over 4 MB; 422 for unsupported, empty, encrypted, over-20-page or textless files, or extracted
text over 50,000 chars; 503 when OCR is needed but Bedrock is offline / has no credentials. Uploads are
processed in memory and not stored; audit lines record the OCR method and token counts, never the text.

```bash
curl -s -X POST "http://127.0.0.1:8000/v1/documents/upload?document_id=DOC-78&condition_code=STIP_INCOME_30DAY" \
  -H "X-API-Key: $STIPGATE_API_KEY" -H "Content-Type: application/pdf" --data-binary @paystub.pdf
```

```powershell
Invoke-RestMethod "$base/v1/documents/upload?document_id=DOC-78&condition_code=STIP_INCOME_30DAY" `
  -Method Post -Headers $headers -ContentType "application/pdf" -InFile .\paystub.pdf
```

### PowerShell examples

```powershell
$base = "http://127.0.0.1:8000"
$headers = @{}
if ($env:STIPGATE_API_KEY) { $headers["X-API-Key"] = $env:STIPGATE_API_KEY }

Invoke-RestMethod "$base/healthz" | ConvertTo-Json -Depth 5

$samples = (Invoke-RestMethod "$base/v1/samples" -Headers $headers).samples
foreach ($s in $samples) {
  $body = @{ document_id = $s.document_id; condition_code = $s.condition_code; snippet = $s.snippet } | ConvertTo-Json
  $r = Invoke-RestMethod "$base/v1/documents/evaluate" -Method Post -ContentType "application/json" -Headers $headers -Body $body
  "{0} {1} {2} {3} {4} tokens={5}" -f $r.document_id, $r.gated_action, $r.verdict, $r.confidence, $r.clearance_status, $r.tokens_spent
}

$queue = Invoke-RestMethod "$base/v1/review-queue" -Headers $headers
"review queue: $($queue.count)"
$queue.items | Select-Object document_id, verdict, confidence, reason

Invoke-RestMethod "$base/v1/metrics" -Headers $headers | ConvertTo-Json -Depth 5

$page = Invoke-WebRequest "$base/" -UseBasicParsing
"$($page.StatusCode) $($page.Headers['Content-Type']) aria-live=$($page.Content.Contains('aria-live'))"
```

### curl examples

On Windows use `curl.exe`. Drop the `X-API-Key` header when auth is disabled (an empty value is ignored).

```bash
curl -s http://127.0.0.1:8000/healthz
curl -s -H "X-API-Key: $STIPGATE_API_KEY" http://127.0.0.1:8000/v1/samples
curl -s -X POST http://127.0.0.1:8000/v1/documents/evaluate \
  -H "Content-Type: application/json" -H "X-API-Key: $STIPGATE_API_KEY" \
  -d '{"document_id": "DOC-77", "condition_code": "STIP_INCOME_30DAY", "snippet": "UTILITY BILL\nAccount Holder: Maria L. Chen\nAmount Due: $142.18"}'
curl -s -H "X-API-Key: $STIPGATE_API_KEY" http://127.0.0.1:8000/v1/review-queue
curl -s -H "X-API-Key: $STIPGATE_API_KEY" http://127.0.0.1:8000/v1/metrics
```

### Reading a result

- `gated_action`: `PROCEED` (Decider passed it) or `HALTED` (human review, `tokens_spent` 0).
- `clearance_status`: `cleared` (real Nova Lite extraction), `pending_extraction_review` (gate passed but
  extraction fell back; a loan officer must verify the figures), or `halted`.
- `review_ticket_id` (gate halt) and `extraction_review_ticket_id` (extraction review) point into the queue.
- `decider_backend`: `server`, `heuristic`, or `null` when the Decider failed. `heuristic_warning: true`
  means the verdict was simulated.
- `extraction_synthetic_fields`: payload fields filled with synthetic defaults (e.g.
  `SYNTHETIC FALLBACK EMPLOYER`).

## Embed `StipulationGuardrailHandler` in your agent

```python
from strands import Agent
from strands.models.bedrock import BedrockModel

from stipgate import (
    BedrockExtractor,
    DeciderClient,
    InMemoryReviewQueue,
    StipulationGuardrailHandler,
    make_clearance_tool,
)

decider = DeciderClient(
    "http://127.0.0.1:8099/v1/systemone",
    timeout_s=1.5,
    mode="server",                    # fail closed on outage; never "heuristic" in production
)
queue = InMemoryReviewQueue()         # or any object with enqueue(ticket: ReviewTicket) -> None (SQS, LOS task API)
handler = StipulationGuardrailHandler(decider, confidence_threshold=0.80, check_threshold=0.85, review_queue=queue)
extractor = BedrockExtractor()        # Nova Lite, us-east-1

agent = Agent(
    model=BedrockModel(model_id="amazon.nova-lite-v1:0", region_name="us-east-1"),
    tools=[make_clearance_tool(extractor, queue)],
    interventions=[handler],
    callback_handler=None,
)
agent("Borrower uploaded doc DOC-77 for STIP_INCOME_30DAY: <ocr text>")
```

Shortcut: `build_agent(StipGatePipeline(decider=decider))` does the same wiring and adds a system prompt
that tells the model not to call a `pending_extraction_review` result "cleared".

The clearance tool returns `status: "cleared"` only for a real Nova Lite extraction. A fallback returns
`status: "pending_extraction_review"` and opens a ticket on the queue you pass to `make_clearance_tool`
(that ticket records confidence 0.0 because the gate score is not visible inside the tool).

Gate a call without an agent loop, safely from many threads:

```python
from strands.interventions import Proceed

from stipgate import CLEAR_CONDITION_TOOL

action, decision = handler.evaluate_with_decision(
    CLEAR_CONDITION_TOOL,
    {"document_id": "DOC-77", "condition_code": "STIP_INCOME_30DAY", "document_snippet": ocr_text},
    tool_use_id="upload-123",
)
if isinstance(action, Proceed):
    ...  # extract
else:
    print(decision.verdict, decision.confidence, decision.reason, decision.ticket_id)  # tokens_spent == 0
```

Use the returned `GateDecision` per call. `handler.last_decision` is a last-writer convenience and is not
safe for concurrent callers.

Batch / webhook path:

```python
from stipgate import StipGatePipeline

trace = StipGatePipeline(decider=decider).process_document("DOC-77", ocr_text, "STIP_INCOME_30DAY")
# trace["gated_action"], trace["clearance_status"], trace["extracted_payload"], trace["review_ticket_id"], ...
```
