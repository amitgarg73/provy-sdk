# Changelog

Versions follow semver. A new argument on a public call is a MINOR bump, because a caller written
against the new surface will not run on the old package.

## 0.9.0

### Added — telemetry that arrives late (argus#1072)

- `trace(occurred_at=)` and `open_session(started_at=)`: when the work actually ran, ISO 8601.

  Without them the server stamps arrival, so a collector catching up after an outage is recorded as a
  burst of work at the moment it drained, and everything read from that clock describes Provy's
  ingestion rather than your agents. It is most wrong right after an upstream incident.

  ⛔ The OTel door has honoured a span's own `startTimeUnixNano` all along, so the door we recommend
  was the door that lost the time. A time in the future is refused server-side, with five minutes of
  clock skew allowed.


Everything here was found by an outside integration that had never seen Provy, building against this
README alone (argus#1057, #1071). None of it was caught by this package's own tests, because every
one of them asserts what the code does and the defects were in what the code and the document
disagreed about.

### Added

- `trace(model=, prompt_version=, cache_read_tokens=, cache_write_tokens=)`. Each is stored in its
  own column server-side and the other two integration doors are warned never to lose them. This
  client had no parameter for any of the four, so the one door that could not express them was the
  one whose customers were never told. The integration that found this put `model` and
  `prompt_version` inside `output_json` under `_model` and `_prompt_version`, wrote down that it
  expected them to be dropped, and was right: anything in the payload goes when the trace body moves
  to object storage.
- `STEP_TYPES` and `STEP_TYPE_LOOKALIKES`, exported. The closed set of step types Provy reasons
  about, which this package had never stated anywhere.
- `trace()` now logs a warning when `step_type` is outside that set, naming the value and the likely
  replacement. It warns rather than raises: an existing caller sending `agent_step` today would break
  on upgrade, and breaking someone's agent over telemetry is the wrong trade.

### Fixed

- **`trace_fn` defaulted to `step_type="agent_step"`, which Provy does not reason about.** A step
  typed outside the closed set is accepted, stored, shown, and then invisible to attribution, the
  judge, pattern detection and embeddings. The decorator whose entire purpose is effortless
  instrumentation defaulted to the one value that makes the instrumentation inert. Now
  `agent_message`. The integration that found this read the TypeScript SDK's type union, concluded
  the decorator was unsafe, and did not use it.

### Documentation, all of it wrong in ways that changed what people built

- The quickstart advertised `step_type` as `llm_call | tool_call | agent_step | decision | error`.
  Three of those five are not real and two real ones were missing. `agent_step` was also the worked
  example. A Python customer following this document produced a fleet Provy could not reason about,
  silently.
- `report_outcome` was absent from the API reference, from the quickstart, and from the "Business
  outcomes" section, which showed `write_eval()` instead. Reporting the outcome is the whole product:
  until one arrives the Ledger stays empty, nothing diverges and no cause can be named. The README
  described a client that could not reconcile anything.
- The `trace` row read `trace(session_id, agent, step_type, outcome, ...)` with the rest a literal
  ellipsis, hiding `entity_id`, `claim`, `inputs` and `output_json`. Those four decide whether a
  fleet can be reconciled or attributed at all. The full signature is now in the README with a line
  on why each matters.
- `inputs`: omitted and `[]` are different claims and the server stores them differently. Only the
  TypeScript SDK said so.
- `outcome` is required and positional here, and the help docs call it optional. Said plainly now.
- Every span carries an id and retries dedupe on it. True all along, and a customer had to take it
  on trust because nothing in this README said it.

## 0.8.0

⛔ **This release was nearly published as 0.7.1.** `pyproject.toml` carried 0.7.1 and the working
tree carried one further commit that added `inputs=` to `trace()`. Shipping new public surface under
a patch number is not recoverable once it is on PyPI, so the version was raised instead.

### Added
- **`trace(..., inputs=[span_id, ...])`** declares which spans' output a step consumed (argus#1009).
  It is a DATA edge and deliberately not `parent_trace_id`, which is the CALL TREE: a step can run
  inside one span and consume the output of another. The key is omitted entirely when `inputs` is
  not passed, and `[]` is sent only when the caller means "consumed nothing".

### Changed (from the unreleased 0.7.1 work)
- An **untimed step is sent as not timed, never as 0 ms** (#4). A duration of zero and the absence
  of a duration are different facts, and the wire now tells them apart.
- **`time_step()`** times a block, so the common case does not need a manual stopwatch.

## 0.7.0
- The masking hook refuses a missing secret when you build it, rather than on the first span.

## Earlier
See the git history; releases before 0.7.0 predate this file.
