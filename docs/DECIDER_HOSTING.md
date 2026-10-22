# Hosting Strands Decider 2B for StipGate

StipGate needs one Decider 2B server it can reach over loopback. The recommended production host is a
**GPU sidecar** (EC2 g6 / g5 class, `--device cuda`) on the same host or ECS task as the StipGate API,
reached at `http://127.0.0.1:<port>/v1/systemone`. CPU works for dev and demos only.

> **Provenance.** Everything here comes from the official docs listed below, summarised in the repo's
> research notes. No Decider model was downloaded and no `strands-decider` command was run while
> building this repo. Items marked **unverified** are inferences or gaps in the docs. Commands under
> "AWS" or "Docker" headings are a guide only and were not executed.

Sources:

- PyPI package: https://pypi.org/project/strands-decider/ (0.2.0 latest; 0.1.0 also published)
- Repo: https://github.com/strands-labs/strands-decider
- Inference doc: https://github.com/strands-labs/strands-decider/blob/main/docs/inference.md
- Deploy overview: https://github.com/strands-labs/strands-decider/blob/main/deploy/README.md
- SageMaker deploy: https://github.com/strands-labs/strands-decider/blob/main/deploy/sagemaker/README.md
- Official serving image: https://github.com/strands-labs/strands-decider/blob/main/deploy/sagemaker/Dockerfile
- Model weights: https://huggingface.co/StrandsAgents/strands-decider-2B-qwen3.5-v1-2610

## Latency reality check

Published latency does **not** support StipGate's "<90 ms" figure. Upstream reports a median of
~115 ms / p95 299 ms per question on an RTX 3090 (WSL2) and a warm median of ~153 ms on an M3 Pro (MPS)
([inference.md](https://github.com/strands-labs/strands-decider/blob/main/docs/inference.md); also
reported by [MarkTechPost](https://www.marktechpost.com/2026/10/01/aws-strands-labs-releases-strands-decider-2b/),
a secondary source). Treat <90 ms as a **target** that needs a GPU and short prompts, and measure it on
your hardware with `python benchmark.py --heldout` against the live server.

## Hosting options compared

| Option | Published latency | Fit for StipGate | Source | Status |
|---|---|---|---|---|
| **GPU sidecar** (EC2 g6.xlarge L4 / g5.xlarge A10G, ECS-on-EC2, same task as the API, loopback) | ~115 ms median / 299 ms p95 per question (RTX 3090; g6/g5 not published) | Real-time gate. **Recommended.** | [inference.md](https://github.com/strands-labs/strands-decider/blob/main/docs/inference.md), [SageMaker README](https://github.com/strands-labs/strands-decider/blob/main/deploy/sagemaker/README.md) (instance types) | verified-from-docs |
| SageMaker real-time GPU endpoint (official CDK stack, default `ml.g6.xlarge`, `/invocations`) | ~1013 ms `latency_ms` in the upstream example | Managed; extra network hop and SigV4. StipGate's client does not sign requests, so it needs a signing adapter (not in this repo). | [SageMaker README](https://github.com/strands-labs/strands-decider/blob/main/deploy/sagemaker/README.md) | verified-from-docs |
| Local Mac, MPS or MLX (`--device mps` / `--device mlx`, `[mlx]` extra) | MPS warm median 153 ms (M3 Pro, <300 tokens); MLX ~1.4-1.6x faster (113 ms at 222 tokens, M4 Pro) | Developer laptops. | [inference.md](https://github.com/strands-labs/strands-decider/blob/main/docs/inference.md) | verified-from-docs |
| CPU container (`--device cpu`, `docker/decider`) | ~1.6 s per ask (Xeon 8375C, 8 threads); ~2.5 s for a 3-question request on `ml.m5.2xlarge` | Demo / CI only. Raise `STIPGATE_DECIDER_TIMEOUT_S` (e.g. 5) or every call times out and fails closed. | [inference.md](https://github.com/strands-labs/strands-decider/blob/main/docs/inference.md), [SageMaker README](https://github.com/strands-labs/strands-decider/blob/main/deploy/sagemaker/README.md) | verified-from-docs |
| AWS Lambda (CPU, 10 GB, 6 vCPU, fp32) | ~1.7 s warm (1 question), 3.5-5.5 s (3 questions), cold start 18-30 s+ | Batch / async only. Not a real-time gate. | [Builder Center community post](https://builder.aws.com/content/3KFNvjfw1TORUmretjxEKgNdyKH) | community numbers, unverified here |
| Amazon Bedrock AgentCore Runtime (int8 CPU, 2 vCPU / 8 GB microVM, IAM SigV4) | ~7.6 s per question warm, ~70 s cold | Async review queue only. The int8 build is a community build, not upstream. | [Builder Center community post](https://builder.aws.com/content/3KNqrNkiEtf7wTUzTufirwAVL7O), [community repo](https://github.com/Vivek0712/strands-decider-agentcore) | community numbers, unverified here |
| Lambda Web Functions | n/a | Not usable: Node.js 24 / arm64 / zip only (2 GB / 2 vCPU). The StipGate API on Lambda needs a container image + Lambda Web Adapter, and the Decider still runs elsewhere. | workflow research notes | unverified here |
| Ollama | n/a | Do not use for the gate. The orchestrator reports an Ollama library entry named `strands-decider`, but the model has no LM head (pointer-head readout); whether that entry keeps the pointer head and calibrated choice probabilities is unknown. | workflow research notes | unverified |
| llama.cpp / vLLM / quantized (int8 / int4 / GGUF) builds | n/a | Not documented upstream. Generation runtimes cannot serve a model without an LM head as-is; the MLX adapter merge refuses quantised base weights. | [inference.md](https://github.com/strands-labs/strands-decider/blob/main/docs/inference.md) | unverified (none found) |

Graviton (arm64), Inferentia and Trainium are **not supported** by the official stack
([SageMaker README](https://github.com/strands-labs/strands-decider/blob/main/deploy/sagemaker/README.md)).
The StipGate API container itself can run on any architecture.

## What the server is

- Package and CLI: `pip install strands-decider`; `strands-decider serve <checkpoint-or-hub-id> --port <P>`
  (also `strands-decider ask ...` for one-off questions). verified-from-docs (PyPI).
- Model: `StrandsAgents/strands-decider-2B-qwen3.5-v1-2610` (1.9B params: Qwen3.5-2B-Base torso + LoRA +
  ~1M-param pointer head, no text generation). The checkpoint holds LoRA + head + tokenizer only; the base
  `Qwen/Qwen3.5-2B-Base` (~4.5 GB) downloads from Hugging Face on first load into `HF_HOME`.
- Memory: ~4.5 GB bf16 on GPU; ~9 GB fp32 on CPU, plus activations.
- Endpoints: `POST /v1/systemone` (alias `POST /invocations`) and `GET /health` (alias `GET /ping`).
  **There is no `/decide` route** on the official server.
- Bind and auth: binds `127.0.0.1` by default and has **no authentication**. Upstream says behaviour
  under concurrent requests is not verified (issue #9), so StipGate serialises calls by default
  (`STIPGATE_DECIDER_MAX_CONCURRENCY=1`).
- Confidence: top probability rescaled so an even split = 0 and certain = 1. Upstream's convention is to
  act without a check at >= 0.9; StipGate's default threshold is 0.88.

### Serve flags (verified-from-docs)

| Flag | Meaning |
|---|---|
| `--port N` | Listen port. StipGate assumes 8099 (the upstream image uses 8080). |
| `--host H` | Bind address. Default `127.0.0.1`; the upstream image uses `0.0.0.0` inside the container. |
| `--device cuda\|mps\|cpu\|mlx` | Inference device. Auto: cuda > mps > cpu. `mlx` needs the `[mlx]` extra. |
| `--max-batch N` | Max batch size (default 32). Throughput knob, not a single-request latency knob. |
| `--max-batch-tokens N` | Token budget per batch. |
| `--strict-window` | Return HTTP 422 for prompts over the context window (StipGate treats any 4xx as a fail-closed `Guide`). |
| `--no-prefix-cache` | Disable the prefix cache. |
| `--vision` | Enable image input (not used by StipGate). |

## Local development

### Native (Linux with NVIDIA GPU, macOS)

```bash
python -m venv .venv-decider && source .venv-decider/bin/activate
pip install strands-decider
strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 --port 8099 --device cuda   # or mps | mlx | cpu
```

CPU-only Linux: install the CPU torch wheel first so pip skips the CUDA wheels:

```bash
pip install torch==2.7.1 --index-url https://download.pytorch.org/whl/cpu
pip install strands-decider
strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 --port 8099 --device cpu
```

Device extras (`[cuda]`, `[cpu]`, `[mps]`, `[mlx]`) were announced for "the next release" after 0.1.0.
Whether `pip install "strands-decider[cpu]==0.2.0"` works is **unverified**; install from a repo clone if
it fails.

Pin the model revision and load it offline (verified-from-docs):

```bash
hf download StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 --revision <YYMMDD> --local-dir v1
strands-decider serve v1 --port 8099
# once the base model is in HF_HOME:
HF_HUB_OFFLINE=1 strands-decider serve v1 --port 8099
```

`<YYMMDD>` is a revision tag on the Hugging Face repo; pick one from the model page.

### Windows

Upstream's GPU numbers were measured under WSL2. Run the Decider inside WSL2 (Ubuntu) with the NVIDIA
WSL driver, and run StipGate on Windows or in WSL2; WSL2 forwards `localhost` ports to Windows by
default. Native Windows serving is **unverified**. Without a GPU, use the Docker CPU sidecar below or the
heuristic demo mode.

### Docker (guide only, not executed)

`docker/decider/Dockerfile` builds a CPU sidecar from PyPI `strands-decider==0.2.0` with the torch /
transformers pins copied from the upstream image. It is **not** the upstream image and the build is
**unverified**. The first start downloads ~4.5 GB of base weights into the `hf-cache` volume, so the
healthcheck allows a 900 s start period.

```bash
docker compose up --build decider stipgate-api        # API at http://127.0.0.1:8000/, Decider on the compose network only
docker compose --profile demo up --build stipgate-demo  # heuristic demo at http://127.0.0.1:8001/, no model
```

Compose points StipGate at `http://decider:8099/v1/systemone` with `STIPGATE_DECIDER_TIMEOUT_S=5` for CPU.
For GPU, build the upstream `deploy/sagemaker/Dockerfile` from a repo clone with `DEVICE=cuda` (see the
GPU comments in `docker-compose.yml`). The upstream image listens on **8080**
(`ENTRYPOINT strands-decider serve "$DECIDER_CHECKPOINT" --host 0.0.0.0 --port 8080 --device "$DECIDER_DEVICE" $DECIDER_ARGS`),
so set `STIPGATE_DECIDER_URL=http://<host>:8080/v1/systemone` with that image.

### CPU vs GPU

| | GPU (L4 / A10G / RTX 3090 class) | CPU (fp32) |
|---|---|---|
| Latency per question | ~115 ms median, 299 ms p95 (RTX 3090) | ~1.6 s (Xeon 8375C, 8 threads); ~2.5 s 3-question on `ml.m5.2xlarge` |
| Memory | ~4.5 GB bf16 | ~9 GB fp32 + activations |
| `STIPGATE_DECIDER_TIMEOUT_S` | 1.5 (default) | 5 |
| Use | Production real-time gate | Dev, demo, CI smoke |

## Verify the contract

Run these against a live server before pointing StipGate at it.

Health:

```bash
curl -s http://127.0.0.1:8099/health
```

```powershell
Invoke-RestMethod http://127.0.0.1:8099/health
```

The StipGate question on the official contract (`POST /v1/systemone`):

```bash
curl -s -X POST http://127.0.0.1:8099/v1/systemone \
  -H "Content-Type: application/json" \
  -d '{"state": "EARNINGS STATEMENT\nEmployee: Maria L. Chen\nEmployer: Cascade Freight Logistics LLC\nPay Period: 05/16/2026 - 05/31/2026\nGross Pay: $3,300.00\nYTD Gross: $36,300.00",
       "questions": {"stipulation_verdict": {"type": "choice",
         "instructions": "Stipulation STIP_INCOME_30DAY: classify this document for the mortgage condition that requires a paystub (earnings statement) from the borrower'"'"'s employer.",
         "criteria": {
           "fully_compliant": "It is a paystub (earnings statement) from the borrower'"'"'s employer and is readable and complete.",
           "invalid_unrelated_doc": "It is not a paystub (earnings statement) from the borrower'"'"'s employer; it is another kind of document.",
           "ambiguous_requires_human_review": "It is garbled, cut off or unreadable."}}}}'
```

Send a one-line definition per option, as above. With `null` criteria (bare option names) the live int8
Decider labelled every sample `invalid_unrelated_doc`; with definitions it got all four samples right and the
valid ones cleared 0.88. StipGate builds these strings with `build_question` / `build_criteria` in
`stipgate/handlers.py`.

```powershell
$body = @{
  state     = "EARNINGS STATEMENT`nEmployee: Maria L. Chen`nEmployer: Cascade Freight Logistics LLC`nPay Period: 05/16/2026 - 05/31/2026`nGross Pay: `$3,300.00`nYTD Gross: `$36,300.00"
  questions = @{
    stipulation_verdict = @{
      type         = "choice"
      instructions = "Stipulation STIP_INCOME_30DAY: classify this document for the mortgage condition that requires a paystub (earnings statement) from the borrower's employer."
      criteria     = @{
        fully_compliant                 = "It is a paystub (earnings statement) from the borrower's employer and is readable and complete."
        invalid_unrelated_doc           = "It is not a paystub (earnings statement) from the borrower's employer; it is another kind of document."
        ambiguous_requires_human_review = "It is garbled, cut off or unreadable."
      }
    }
  }
} | ConvertTo-Json -Depth 6
Invoke-RestMethod http://127.0.0.1:8099/v1/systemone -Method Post -ContentType "application/json" -Body $body
```

Expect `answers.stipulation_verdict.choice` to be one of the three options and
`answers.stipulation_verdict.confidence` to be a number in [0, 1]. StipGate rejects anything else
(string confidence included) and fails closed.

`/decide` (the simple StipGate contract) exists only if you run your own adapter. Against the official
server it returns 404:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8099/decide \
  -H "Content-Type: application/json" \
  -d '{"text": "...", "question": "...", "options": ["fully_compliant", "invalid_unrelated_doc", "ambiguous_requires_human_review"]}'
```

`DeciderClient` handles that 404 by retrying once on `/v1/systemone` (a second 404 fails closed). Set
`STIPGATE_DECIDER_URL=http://127.0.0.1:8099/v1/systemone` to skip the extra round trip.

## Point StipGate at the server

| Variable | Default | Effect |
|---|---|---|
| `STIPGATE_DECIDER_MODE` | `server` | `server`: real Decider only; outage = HALTED + human review. `heuristic`: offline simulator only. `auto`: server first, heuristic on outage (30 s breaker). |
| `STIPGATE_DECIDER_URL` | `http://127.0.0.1:8099/decide` | Endpoint. Use `.../v1/systemone` (or `/invocations`) for the official server. |
| `STIPGATE_DECIDER_TIMEOUT_S` | `1.5` | Per-request timeout. Raise to 5 for CPU. |
| `STIPGATE_DECIDER_MAX_CONCURRENCY` | `1` | Max in-flight requests from one client (upstream concurrency is unverified). |
| `STIPGATE_ALLOW_HEURISTIC_PROCEED` | off | `1` / `true` / `yes` lets heuristic verdicts clear conditions. CI / demo only. |

Production: `STIPGATE_DECIDER_MODE=server`, `STIPGATE_DECIDER_URL=http://127.0.0.1:8099/v1/systemone`, never
set `STIPGATE_ALLOW_HEURISTIC_PROCEED`. Check `GET /healthz`: `decider.reachable` is `true` when the
server's `/health` returns 200.

## Latency tuning

- Run on a GPU (`--device cuda`; `--device mlx` on Apple silicon). CPU is 10x+ slower.
- Keep the Decider on loopback in the same host / task. Every network hop adds to the gate budget.
- Keep snippets short: pre-trim OCR to the pages and fields the condition needs before calling StipGate.
  Upstream latency figures are for short prompts (<300 tokens on Mac).
- One question per request (StipGate already asks exactly one).
- Send a warm-up request after start (the benchmark does this before timing).
- `--max-batch` / `--max-batch-tokens` trade latency for throughput under load; they do not speed up a
  single request.
- Measure p50 / p95 with `python benchmark.py --heldout --decider-mode server` against the live server and
  `GET /v1/metrics` in the running API. <90 ms is a target, not a published result.
