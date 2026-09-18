# Changelog

Versions follow semver. A new argument on a public call is a MINOR bump, because a caller written
against the new surface will not run on the old package.

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
