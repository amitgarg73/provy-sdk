# The ingest contract, and why this repo can drift

This SDK used to live inside the `argus` repo, deliberately. The reason is worth keeping: the client
and the ingest API it talks to could never fall out of step, because a change to both landed in one
commit. That was not theoretical. Per-tenant copies of the judge diverged once and broke scoring
silently.

**Splitting this out gives up that guarantee.** The server now lives somewhere else and can change
without this repo noticing. That trade was made on purpose — the SDK release cycle should not be
attached to a website deploy — but the protection it removed has to be replaced by something, or
the drift comes back and nobody sees it until a customer does.

## Pointing the client somewhere

`PROVY_URL` sets the host. **`PROVY_BASE_URL` is accepted too, since 0.6.1**, because the module
constant is named `PROVY_BASE_URL` and setting what the file appears to be called was silently
ignored: the client kept the default, `https://provy.ai`, and a pre-prod key sent there gets a
correct 401 while the customer's telemetry goes nowhere they would think to look. `PROVY_URL` wins
when both are set.

## What this SDK depends on

| Endpoint | Used for | Shape it assumes |
|---|---|---|
| `POST /api/ingest/session/open` | starting a run | returns `{ session_id }` |
| `POST /api/ingest/trace` | spans | accepts a single object **or an array** (server #479) |
| `POST /api/ingest/session/close` | ending a run | accepts `session_id`, `result_summary`, `terminal_reason` |
| `POST /api/ingest/eval` | quality checks | accepts `session_id`, `eval_name`, `agent`, `score`, `passed`, `layer`, `entity_id`, `detail`, `threshold` |
| `POST /api/ingest/outcome` | business outcome | accepts `entity_id` (required), `business_date`, `label` \| `value`, `signals`, `session_id`, `source`, `occurred_at` |
| `POST /api/otlp/v1/traces` | the OTel exporter path | OTLP JSON |

Auth is `x-provy-key` (legacy `x-argus-key` still accepted).

## The forward claim (#747)

⛔ **AVAILABLE ON BOTH PATHS SINCE 0.6.1, AND ON ONLY ONE IN 0.6.0.** `TraceLogger.log_agent_message`
and `log_decision` take `claim=`, and so does `ProvyClient.trace(...)`, which is the path this
document tells new pipelines to prefer. In 0.6.0 the ingest path had no named argument: a claim was
reachable only by hand as `output_json={"provy_claim": {...}}`. Found by the contract test on the day
0.6.0 was published, which is what that test is for.

`log_agent_message(..., claim=)` and `log_decision(..., claim=)` put a claim into the span payload
under the reserved key **`provy_claim`**, as `{signal, value, confidence?, entity_id?}` or a list of
those. The server lifts it into its own column at ingest.

⛔ **It is a separate key on purpose, and putting a claim in `payload` does not work.** The server
keeps the LAST value it sees for a signal across a session. That is correct for a READING, because a
close step supersedes earlier partials and grading depends on it. It destroys a CLAIM. Measured on
the reference fleet: an agent reported `realized_pnl: 0` during the run, a later step reported the
settled figure under the same key, and the server stored the settled figure as what the agent had
claimed. Every non-zero claimed value on that fleet is a byte-for-byte copy of what settled.

⛔ **`signal` must name the key the OUTCOME settles under.** A claim filed under a name the contract
does not grade never meets the outcome it is meant to be compared against, so it buys nothing. If
the contract grades `refund_posted`, claim `refund_posted`, not `expected_refund`.

⛔ **A claim never arrives on its own, from any transport.** No auto-instrumentation emits one,
because none exists in the OpenTelemetry semantic conventions: GenAI covers model, tokens and tool
names, all of which a library can observe by watching. "I expect this refund to post" is not
observable. Only whoever wrote the agent knows what it is trying to achieve. This is a semantics
gap, not a transport gap, which is why there is no `POST /api/ingest/claim` and should not be: on
the trace, the agent, the act and the span id come free and anchor the claim to the moment it was
made. Behind a door you get a claim with no trace behind it.

**On the OTel path** the same claim rides as the attribute `provy.claim`, JSON-encoded. Structured
values ride as `provy.payload.<key>`. ⛔ That prefix was silently dropped by the gateway until
argus#755: measured on the reference fleet, `target_price`, `estimated_profit`, `entry_price` and
`realized_pnl` were each present on **0** of the 3,556 spans carrying a payload. If you are pinning
a server version, this is one to pin above.

## The payload shapes this client sends

⛔ **These two rows used to be a dash, and #733 lived in exactly that gap.** A shape the document
does not state is a shape nobody can check.

**`POST /api/ingest/outcome`**

```json
{
  "entity_id":   "AAPL",           // REQUIRED. The work item.
  "business_date": "2026-09-05",   // the day the WORK RAN. Absent lands on the server fallback.
  "label":       "success",        // "success" | "fail", or omit and send `value`
  "value":       -29.05,           // numeric result, if the outcome carries one
  "signals":     { "win_rate": 0.4 },  // extra readings, optional
  "session_id":  "…",              // optional; the ledger keys on entity + date, not session
  "source":      "broker",         // who settled it
  "occurred_at": "2026-09-05T21:00:00Z"
}
```

**`POST /api/ingest/eval`**

```json
{
  "session_id": "…",               // REQUIRED
  "eval_name":  "no_hallucination",
  "agent":      "research",
  "score":      0.93,              // 0-1
  "passed":     true,
  "layer":      4,                 // 4 is the LLM judge; only layer 4 feeds the quality score
  "entity_id":  "AAPL",            // optional; without it the eval describes the whole session
  "threshold":  0.7,
  "detail":     { }
}
```

## The work-item address (#733)

A ledger row is keyed by `(tenant_id, workflow_id, entity_id, business_date)` and that tuple is
unique. Tenant and workflow come from the ingest key, so **the caller supplies the other two.**

`business_date` is the day the WORK RAN, not the day it is reported. Until 0.5.4 the SDK could not
send it at all, so every SDK caller landed on the server's fallback: the most recent open prediction
for that entity wins, with a warning logged server-side and nothing visible to the caller. That
reconciles against the wrong attempt whenever an entity is worked more than once.

⛔ **Do not default it client-side.** A guessed date addresses a row that is wrong with confidence,
which is worse than a missing one landing on a documented fallback. Absent means absent.

Note that the ServiceNow business rule in `argus` posts raw HTTP and always sent this field. The
integration that reconciled correctly was the one that bypassed this SDK, which is the shape of
evidence that says a contract test is missing (see Enforcement below).

## The two properties this client relies on

**Batched writes.** `SpanBuffer` posts arrays. If the server ever stopped accepting an array, every
buffered flush would fail at once rather than degrading.

**Idempotent writes.** Retries are automatic here, and the server upserts on
`(tenant_id, session_id, span_id)`. If that conflict target were removed, this SDK's retries would
start duplicating rows, and the client would become the cause of the corruption it exists to avoid.

Half of that property is ours to hold up, and 0.5.0 did not. The server deliberately does **not**
collapse spans that arrive with no `span_id`, since a step that did not identify itself cannot be
deduped, and `trace()` only set one when OpenTelemetry happened to be installed. OTel is an optional
extra, so the default install duplicated on every retry. Measured: the same body sent twice wrote 2
rows without an id and 1 row with one. Since 0.5.1 every span carries an id whether or not OTel is
present. **Do not make `span_id` optional again on either side of this contract.**

## What to do about it

Anyone changing the ingest contract in the `argus` repo must check this list. That is a human rule
and human rules decay, so the replacement is a contract test in CI that runs the real client against
a deployed pre-prod and asserts both properties above.

✅ **It exists as of 6 Sep 2026: `argus/harness/sdk-contract.py`, run nightly by
`certify-nightly.yml`.** It installs the PUBLISHED wheel from PyPI, provisions a throwaway synthetic
tenant, drives `ProvyClient` as a customer would, verifies what landed with `psql`, and destroys the
tenant in a `finally`. Seven assertions, including that a re-sent span writes one row and that every
span carries a `span_id`.

⛔ **IT LIVES IN THE ARGUS REPO, NOT HERE, AND THAT IS DELIBERATE.** Verifying idempotency needs a
read of `ag_traces`, so it needs a database URL. That credential exists in one repository and adding
it to a second is the exposure `/api/admin/tenants` was built to remove. The test still tests THIS
client, by version, from PyPI.

⛔ **AND IT TESTS THE WHEEL, NOT THE REPO**, because that is where 0.5.0's two defects were.

## Agent identity and span parentage (#668)

⛔ **A sub-agent's parent span is looked up by BASE name, not exact name.** An agent that fans out
per entity emits step names like `research_GILD`, and only `research` has a span. Looking the exact
name up misses, falls through to the session root, and the trace arrives flat instead of nested.

The rule lives in `provy/identity.py` and mirrors `lib/agent-identity.ts` on the server: strip a
final `_SEGMENT` **only** when that segment is 1-5 uppercase letters.

⛔ **`agent.split("_")[0]` is not this rule.** It folds `market_shadow` into `market` and
`insights_agent` into `insights`, merging two different agents into one. Both names are real.

Applies to **both** transports, which each carried the bug independently:

- OTel — `client.py`, choosing the parent span for the OTel context.
- REST — `session.py`, setting `parent_span_id` on the payload.

⛔ **A span must still be ended when its agent finishes**, not swept at session close. A span held
open until the session ends reports the session's remaining time rather than its own work: measured
at 82–377s claimed against 9–23s of real work on the reference fleet. The SDK ends each span
immediately; a caller building its own tracer must do the same.

## Tenant-side masking, and the one thing it duplicates

`provy/redact.py` gives a caller a `mask` hook that runs before anything leaves their process. It is
additive: Provy masks credentials and common identifiers again on its own egress to a model provider,
unconditionally, so a gap in a caller's masker is less early protection and never an open door.

⛔ **`DEFAULT_RULES` DUPLICATES THE SERVER'S PATTERN LIST, AND THE SERVER IS THE SOURCE OF TRUTH.** The
server list lives in `argus/web/lib/redact.ts` `DEFAULT_PATTERNS`. The copy is deliberate: the entire
point of masking here is to run before anything reaches the server, and a client that has to ask the
server what to mask has already sent the data it was trying to protect.

That makes this a drift risk of the kind this document exists to record. What holds it:

- `tests/test_redact.py` pins the rule names, so a change on this side is visible in a diff.
- Nothing pins the server side. **If you add, remove or retune a pattern in `lib/redact.ts`, update
  `provy/redact.py` in the same change** or write down here why you could not.
- A drift is a degradation, not a break. Either the client masks something the server would not have
  (harmless, the value never arrives) or it misses something the server still catches on egress.

### Two behaviours a caller depends on, so do not change them quietly

**Join keys are never masked.** `session_id`, `span_id`, `parent_span_id`, `entity_id`, `agent`,
`step_type`, `tool_name`. `entity_id` joins a trace to the outcome reported for the same work item, and
the server groups by `agent`, so masking either would break reconciliation on the server while looking
like a privacy improvement on the client. `PROTECTED_KEYS` is pinned by a test.

**The mask fails closed.** A masker that raises means the payload is dropped, counted and logged, not
sent unmasked. Every other failure in this SDK degrades toward sending, because telemetry must not
break a caller's agent. This one is the exception on purpose: a caller who configured a mask believes
their data is protected, and sending it anyway is the one outcome worse than losing it.

### Tokens over flat labels

`tokenizing_masker` is the one to recommend and `redacting_masker` is the fallback. A flat label
collapses every distinct value to one string, which makes "the agent acted for the right person"
unanswerable from the trace. Measured against Provy's own judge on ten ITSM work items: unmasked
separates a correct run from a wrong-person run 10 of 10 times, flat labels 0 of 10 (the two inputs are
byte-identical, so this is structural and not a sampling result), stable tokens 10 of 10.

⛔ **AND MASKING IS NOT FREE ON THE SERVER'S SIDE OF THE FENCE.** In the same measurement, masking names
moved the server's `response_coherence` score by about seven times the judge's own run-to-run noise, and
flipped 2 of 10 checks across their threshold. Tokenising does not help with that; its win is
detectability. A tenant who turns masking on needs their affected thresholds re-baselined, which is a
server-side conversation and not something this SDK can solve. Full numbers:
`argus/docs/compliance/pii-plan.md` section 3a.
