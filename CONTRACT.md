# The ingest contract, and why this repo can drift

This SDK used to live inside the `argus` repo, deliberately. The reason is worth keeping: the client
and the ingest API it talks to could never fall out of step, because a change to both landed in one
commit. That was not theoretical. Per-tenant copies of the judge diverged once and broke scoring
silently.

**Splitting this out gives up that guarantee.** The server now lives somewhere else and can change
without this repo noticing. That trade was made on purpose — the SDK release cycle should not be
attached to a website deploy — but the protection it removed has to be replaced by something, or
the drift comes back and nobody sees it until a customer does.

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
and human rules decay, so the intended replacement is a contract test in CI here that runs the real
client against a deployed pre-prod and asserts both properties above. **It does not exist yet.** Until
it does, this file is the only thing standing between a server change and a silently broken client.

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
