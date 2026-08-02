"""
Provy OTel quickstart — no-code path (#188).

Demonstrates the trace-first onboarding path: emit standard OpenTelemetry agent-trace
spans to Provy's OTLP endpoint and they're auto-normalized into sessions / agents /
reasoning with NO Provy-specific code. The session is your OTel trace id; agents,
step types, reasoning, and tokens are read from the instrumentation convention
(here: OpenInference-style attributes). Provy auto-detects the convention.

This client sends OTLP/HTTP **JSON** (ExportTraceServiceRequest), which the gateway
accepts today. A real OTel SDK exporter (opentelemetry-exporter-otlp-proto-http) sends
protobuf — accepting that wire format directly is tracked separately so you can point
OTEL_EXPORTER_OTLP_ENDPOINT at Provy with zero glue. This script needs no OTel deps.

Run:
    export PROVY_API_KEY=<your fleet ingest key>
    export PROVY_OTLP_ENDPOINT=https://provy.ai/api/otlp/v1/traces  # or your env
    python examples/otel_quickstart.py
Then open Provy → Sessions: one session (this trace) with three agent steps should appear.
"""
import json
import os
import time
import urllib.request

ENDPOINT = os.environ.get("PROVY_OTLP_ENDPOINT", "https://provy.ai/api/otlp/v1/traces")
KEY = os.environ.get("PROVY_API_KEY", "")


def _hex(n: int) -> str:
    return os.urandom(n).hex()


def _attr(key: str, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _span(name, trace_id, kind_attrs, start_ns, end_ns, status_ok=True):
    return {
        "traceId": trace_id,
        "spanId": _hex(8),
        "name": name,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "status": {"code": 1 if status_ok else 2},
        "attributes": [_attr(k, v) for k, v in kind_attrs.items()],
    }


def main():
    if not KEY:
        raise SystemExit("Set PROVY_API_KEY (your fleet ingest key).")

    trace_id = _hex(16)  # one OTel trace == one Provy session (no argus.session_id needed)
    t = time.time_ns()

    # A tiny 3-step agent pipeline, described with standard OpenInference attributes.
    spans = [
        # 1) an agent reasoning step (LLM) — reasoning lands as agent_message for the judge
        _span("planner", trace_id, {
            "openinference.span.kind": "LLM",
            "llm.model_name": "gpt-4o",
            "input.value": "Plan the task for the user request.",
            "output.value": "I will first fetch the data, then summarize the findings for the user.",
            "llm.token_count.prompt": 120,
            "llm.token_count.completion": 38,
        }, t, t + 800_000_000),
        # 2) a tool call — input/output land as tool I/O, not reasoning
        _span("fetch_data", trace_id, {
            "openinference.span.kind": "TOOL",
            "input.value": json.dumps({"query": "latest metrics"}),
            "output.value": json.dumps({"rows": 42}),
        }, t + 800_000_000, t + 1_100_000_000),
        # 3) a final agent decision
        _span("summarizer", trace_id, {
            "openinference.span.kind": "AGENT",
            "output.value": "Summary: 42 rows retrieved; key metric is up 3% week over week.",
            "llm.token_count.prompt": 90,
            "llm.token_count.completion": 25,
        }, t + 1_100_000_000, t + 1_500_000_000),
    ]

    payload = {
        "resourceSpans": [{
            "resource": {"attributes": [_attr("service.name", "otel-quickstart-demo")]},
            "scopeSpans": [{
                "scope": {"name": "openinference.instrumentation.demo"},
                "spans": spans,
            }],
        }],
    }

    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-provy-key": KEY},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        print(f"{resp.status} {resp.read().decode()}")
    print(f"Sent trace {trace_id} (one session, 3 agent steps). Check Provy → Sessions.")


if __name__ == "__main__":
    main()
