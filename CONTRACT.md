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
| `POST /api/ingest/eval` | quality checks | — |
| `POST /api/ingest/outcome` | business outcome | — |
| `POST /api/otlp/v1/traces` | the OTel exporter path | OTLP JSON |

Auth is `x-provy-key` (legacy `x-argus-key` still accepted).

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
