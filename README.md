# provy-sdk

Python SDK for [Provy](https://provy.ai): prove your AI agents actually worked.

Send every session, agent step and evaluation with an ingest key. No database credentials and no
Provy-side configuration — close a session and it appears on your dashboard.

> **A note on names.** You may see `argus` inside: environment variables such as `ARGUS_INGEST_KEY`
> and `ARGUS_URL` still work, and OpenTelemetry attributes are still `argus.*`. Argus is the original
> codename, and those names are kept for wire compatibility so existing integrations keep working.
> Everything you actually type is `provy`.

## Reliability

Telemetry that silently disappears is worse than none, so this client is built not to lose spans:

- **Retries** transient failures with backoff, honouring `Retry-After`. A timeout or a 503 delays
  your data rather than destroying it.
- **Buffers and batches** spans, flushing on a background thread and again at process exit, so a
  short script cannot end with telemetry still in memory.
- **Never silent.** Anything dropped is counted and logged, and the counts are readable at
  `client.buffer_stats`. Telemetry being switched off is announced too, once, on stderr.
- **Never duplicates.** Every span carries an id, so the server can recognise a retried write as
  the same span rather than a second one.
- **Never raises into your agent.** A failed send is our problem, not a crash in your pipeline.

---

## Install

```bash
pip install provy-sdk
```

The base install is the ingest client only (just `requests`). Optional extras:

| Extra | Adds | For |
|---|---|---|
| `provy-sdk[otel]` | OpenTelemetry SDK | streaming existing OTel spans via `ProvyExporter` |
| `provy-sdk[judge]` | `anthropic` | running the LLM-as-judge in your own pipeline (circuit breakers) |
| `provy-sdk[engine]` | `supabase` | the legacy direct-to-database path (prefer the ingest API instead) |

---

## Connect

Get an ingest key from Provy: **Agent Fleets → your fleet → Reveal key**. Set it in your environment:

```bash
export PROVY_API_KEY=provy_...
export PROVY_EMIT=1              # required, see below
# optional, defaults to the hosted app:
export PROVY_URL=https://provy.ai
```

That key authenticates your fleet. It is the only credential you need. Keys issued before mid-2026
begin with `argus_` and still work.

### `PROVY_EMIT`: read this before your first run

**Nothing is sent unless `PROVY_EMIT=1` is set** (or you pass `enabled=True` to the client). With it
unset the client is a deliberate no-op: `open_session()` returns a local id, everything else does
nothing, and your code runs unchanged.

This exists so a laptop run holding production credentials cannot write into your production Provy.
Set it in the environments that should report (production, staging, CI) and leave it off on your
machine.

The client logs one warning to stderr the first time it suppresses anything, so a run that reports
nothing tells you why. Before 0.5.1 it did not, and the only clue was an empty dashboard.

---

## Quickstart — direct ingest

```python
from provy import ProvyClient

provy = ProvyClient()  # reads PROVY_API_KEY from the environment

session_id = provy.open_session("premarket")

provy.trace(
    session_id = session_id,
    agent      = "research",
    step_type  = "agent_message",       # tool_call | agent_message | decision | error | skip
    outcome    = "Generated AAPL thesis",
    latency_ms = 1240,
    tokens_in  = 800,
    tokens_out = 150,
)

provy.close_session(session_id, result_summary="Trade plan ready")
```

Open **Sessions** in Provy — your run appears within seconds.

### `step_type` is a closed set, and a wrong value is silent

`tool_call` · `agent_message` · `decision` · `error` · `skip`

Provy reasons about these five and nothing outside them. A step typed anything else is accepted,
stored and shown in the UI, and is then **invisible to attribution, the judge, pattern detection and
embeddings**. Nothing errors; you simply never get a cause named on that step.

This README advertised `agent_step` and `llm_call` until 0.9.0. Neither is real. `agent_step` was
also the worked example and the `trace_fn` default, so following this document produced a fleet whose
steps Provy could not reason about. The client now logs a warning naming the value and the likely
replacement. There is no step type meaning "a model call": use `agent_message` and set `model`.

The decorator form auto-traces a function:

```python
@provy.trace_fn(agent="research", step_type="agent_message")
def run_research(ticker):
    ...

run_research("AAPL", session_id=session_id)
```

---

## Already on OpenTelemetry?

If your pipeline emits OTel spans (LangChain, CrewAI, AutoGen, LlamaIndex, or raw OTel), attach the exporter and stream them — no per-step calls:

```bash
pip install "provy-sdk[otel]"
```

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from provy import ProvyExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(ProvyExporter(api_key="provy_...")))
```

Provy auto-detects the convention (OpenInference, OpenLLMetry/Traceloop, Langfuse, OTel GenAI) and builds the session from your spans.

---

## Keeping sensitive values out of Provy

Pass a `mask` and it runs on every payload before anything leaves your process. Provy also masks
credentials and common identifiers on its own way out to a model provider, and that cannot be switched
off, but by then it already holds your data. Masking here means the raw value never arrives.

```python
from provy import ProvyClient, tokenizing_masker

client = ProvyClient(
    ingest_key="provy_...",
    mask=tokenizing_masker(secret=os.environ["MY_MASK_SECRET"]),
)
```

That masks bearer tokens, API keys, JWTs, email addresses, US SSNs, Luhn-valid card numbers and
separator-formatted phone numbers. **Keep the secret yourself and the tokens are irreversible to Provy.**

### Why tokens and not `[REDACTED]`

Replacing every email with one flat label makes two very different runs identical:

```text
looked up [REDACTED_EMAIL] ... refunded [REDACTED_EMAIL]     # correct
looked up [REDACTED_EMAIL] ... refunded [REDACTED_EMAIL]     # refunded the WRONG person
```

Nothing downstream can tell those apart. `tokenizing_masker` gives each distinct value a stable
pseudonym instead, so the same person is the same token everywhere and a different person visibly is
not:

```text
looked up [EMAIL_a91c4e2f7b03] ... refunded [EMAIL_a91c4e2f7b03]   # correct
looked up [EMAIL_a91c4e2f7b03] ... refunded [EMAIL_5d7e08b1cc42]   # the defect, still visible
```

Measured on Provy's own judge: the unmasked judge separates those two cases ten times out of ten, flat
labels separate them zero times out of ten, and stable tokens are back to ten out of ten.

### What is not masked, and why

Join keys pass through untouched: `session_id`, `span_id`, `parent_span_id`, `entity_id`, `agent`,
`step_type`, `tool_name`. `entity_id` is what ties a trace to the outcome you later report for the same
work item, and `agent` is what the fleet view groups by, so masking them would break reconciliation
without protecting anything. **If a ticket or order id is itself sensitive to you, pass one that is
already pseudonymous.** You choose what goes in that field.

Numbers, booleans and dictionary keys are never touched either, so a contract condition graded on a
number cannot be broken by masking.

Names and postal addresses are **not** masked. No regular expression can find a name, so catching that
class needs a named-entity pass (Microsoft Presidio is what most of the ecosystem uses) or a rule of
your own:

```python
from provy import Rule, tokenizing_masker
import re

masker = tokenizing_masker(
    secret=os.environ["MY_MASK_SECRET"],
    rules=[*DEFAULT_RULES, Rule("account", re.compile(r"\bACC-\d{6}\b"))],
)
```

### If your masker raises

The payload is dropped and the failure is logged. This is the one place the SDK fails closed: sending
data you believed was masked is worse than not sending it. Watch for `mask() raised` in your logs.

---

## Quality scoring

By default Provy runs the LLM-as-judge **server-side** on the traces you send — no SDK code, no key of yours. Configure criteria in **Eval Manager** and scores appear on the Quality page.

Run the judge **in your own pipeline** only when you want the verdict before an output is used (circuit breakers):

```bash
pip install "provy-sdk[judge]"   # adds anthropic
```

```python
from provy import evaluate_session_outputs

evaluate_session_outputs(session_id, {"research": research_output_text})
```

Needs `ANTHROPIC_API_KEY` in your environment. Same judge core as the server side.

---

## Business outcomes

For metrics you compute yourself (no LLM), write them with `write_eval()` — they land in **Outcomes**:

```python
from provy import write_eval

write_eval(
    session_id = session_id,
    eval_name  = "approval_rate",
    agent      = "risk",
    score      = 0.6,
    passed     = True,
    threshold  = 0.2,
    reasoning  = "3 of 5 proposals approved",
)
```

---

## API reference

### `ProvyClient(ingest_key=None, base_url=None, enabled=None, buffered=True, mask=None)`
Reads `PROVY_API_KEY` / `PROVY_URL` from the environment when arguments are omitted (legacy `ARGUS_INGEST_KEY` / `ARGUS_URL` still work). `mask` runs on every payload before it is sent; see "Keeping sensitive values out of Provy".

| Method | When to call |
|---|---|
| `open_session(session_type, external_id=None, metadata=None)` | start of a run; returns `session_id` |
| `trace(...)` | each step; returns the span id. Full signature below |
| `close_session(session_id, status="completed", result_summary=None, terminal_reason=None)` | end of the run |
| `report_outcome(entity_id, label=None, value=None, signals=None, session_id=None, source="confirmed", occurred_at=None, business_date=None)` | what actually happened, from your system of record |
| `trace_fn(agent, step_type="agent_message")` | decorator that auto-traces a function |

#### `report_outcome` is how anything gets reconciled

It was missing from this table until 0.9.0, and the "Business outcomes" section below showed
`write_eval()` instead, which is a different thing: an eval row, not a settled result. Until an
outcome arrives the Ledger stays empty, nothing can diverge and no cause can be named, so a reader
who trusted the old table built an integration that could never reconcile.

`signals` is a dict of the named values your system of record settles on, and a contract condition
binds to one of those names. `business_date` is the day the work ran, not the day you report it.

#### `trace` in full

```python
trace(session_id, agent, step_type, outcome,
      tool_name=None, latency_ms=None, tokens_in=None, tokens_out=None, cost_usd=None,
      error=None, output_json=None, claim=None, parent_trace_id=None, entity_id=None,
      inputs=None, model=None, prompt_version=None,
      cache_read_tokens=None, cache_write_tokens=None) -> str
```

This table used to read `trace(session_id, agent, step_type, outcome, ...)` with the rest a literal
ellipsis, which hid the four arguments that decide whether a fleet can be reconciled or attributed
at all:

| argument | why it matters |
|---|---|
| `entity_id` | the work item this step is about. Without it a step cannot be joined to the outcome you later report, so nothing reconciles per item |
| `claim` | what the agent says will hold, `{"signal": ..., "value": ...}`. This is what lets Provy put a broken condition against an agent by name. Claims cost nothing and buy attribution |
| `inputs` | span ids whose output this step consumed. This is what tells "caused by" from "came after". **Omit it to say nothing; pass `[]` to say this step read nothing.** Those are different claims and the server stores them differently |
| `output_json` | the step's own reported values. Contract conditions bind to these names on the trace side |

`model`, `prompt_version`, `cache_read_tokens` and `cache_write_tokens` are each stored in their own
column and were **not expressible from this client until 0.9.0**. They must never be buried inside
`output_json`: anything in there is dropped once the trace body moves to object storage, which is how
model names were lost.

`outcome` is **required** here, positionally, even though the help docs list it as optional. Pass a
short verdict word.

Every step is given a `span_id` whether or not the `otel` extra is installed, and the server dedupes
on `(tenant, session, span_id)`, so a retry after a lost response replaces the step instead of
recording it twice. You do not pass one and do not need to.

### `ProvyExporter(api_key, endpoint=None, enabled=None, mask=None)`
OTel `SpanExporter`. Attach to any `TracerProvider`. Needs the `otel` extra. Takes the same `mask` as the client, because span attributes carry your content too.

### `tokenizing_masker(secret, rules=None)` / `redacting_masker(rules=None)`
Maskers for the `mask` argument. Prefer the tokenizing one; see the section above for why.

### `evaluate_session_outputs(session_id, agent_outputs)`
Client-side LLM-as-judge. Needs the `judge` extra and `ANTHROPIC_API_KEY`.

### `write_eval(session_id, eval_name, agent, score, passed, threshold, reasoning, layer=5)`
Writes one business-outcome eval row.

> **Legacy:** `TraceLogger` (direct database writes via the `engine` extra) predates the ingest API. New pipelines should use `ProvyClient`. `TraceLogger` remains for existing internal pipelines.

---

## Examples

- `examples/otel_quickstart.py` — stream OTel spans to Provy
- `examples/github-actions-otel.yml` — run a pipeline in GitHub Actions and stream to Provy

---

## Support

Open an issue at [github.com/amitgarg73/provy-sdk](https://github.com/amitgarg73/provy-sdk/issues).
