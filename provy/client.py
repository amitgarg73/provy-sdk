"""
Provy SDK — Python ingest client.

The canonical way to send data to Provy. Authenticates with an ingest key and
POSTs to the ingest API — no database credentials, works for any tenant. Two
shapes, same key:

  PATH 1 — OTel exporter (LangChain, CrewAI, AutoGen, LlamaIndex, any OTel pipeline)
  ----------------------------------------------------------------------------------
  pip install "provy-sdk[otel]"

  from opentelemetry.sdk.trace import TracerProvider
  from opentelemetry.sdk.trace.export import BatchSpanProcessor
  from provy import ProvyExporter

  provider = TracerProvider()
  provider.add_span_processor(BatchSpanProcessor(ProvyExporter(api_key="provy_...")))
  # That's it — your OTel spans stream to Provy automatically.

  PATH 2 — Direct ingest API (custom pipelines)
  ---------------------------------------------
  pip install provy-sdk

  from provy import ProvyClient

  client = ProvyClient(ingest_key="provy_...")
  session_id = client.open_session("premarket")
  client.trace(session_id=session_id, agent="research", step_type="agent_step", outcome="Done")
  client.close_session(session_id, result_summary="Trade plan ready")

  # OTel span IDs are populated automatically when opentelemetry-sdk is installed.
  # Pass parent_trace_id manually if you manage span relationships yourself:
  client.trace(..., parent_trace_id="<hex-span-id>")
"""

from __future__ import annotations

import os
import time
import uuid
import functools
import threading
import logging
import requests
from .transport import SpanBuffer, post_with_retry

PROVY_BASE_URL = os.environ.get("PROVY_URL") or os.environ.get("ARGUS_URL", "https://provy.ai")


def _emit_enabled(override: "bool | None" = None) -> bool:
    """Whether the SDK may send telemetry to Provy.

    Off by default, so a local or dev run holding production credentials does not
    write into your production Provy. Turn it on in your production (or CI)
    environment with PROVY_EMIT=1, or pass enabled=True to the client. An explicit
    override always wins. When off, the client is a no-op: open_session returns a
    local id and trace/close do nothing, so your code runs unchanged.
    """
    if override is not None:
        return override
    return os.environ.get("PROVY_EMIT", "").strip().lower() in ("1", "true", "yes", "on")

# Optional OTel — imported at runtime so the SDK works without it installed
try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider as OtelTracerProvider
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    from opentelemetry.trace import StatusCode
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False
    SpanExporter = object          # type: ignore[assignment,misc]
    SpanExportResult = None        # type: ignore[assignment]


# ---------------------------------------------------------------------------
# ProvyExporter — OTel SpanExporter that streams spans to Provy OTLP gateway
# ---------------------------------------------------------------------------

log = logging.getLogger("provy.sdk")


class ProvyExporter(SpanExporter):  # type: ignore[misc]
    """
    OTel SpanExporter. Attach to any TracerProvider; spans stream to Provy.

    Usage:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from provy import ProvyExporter

        provider = TracerProvider()
        provider.add_span_processor(BatchSpanProcessor(ProvyExporter(api_key="provy_...")))
    """

    def __init__(self, api_key: str, endpoint: str | None = None, enabled: "bool | None" = None):
        if not _OTEL_AVAILABLE:
            raise ImportError("opentelemetry-sdk and opentelemetry-api are required for ProvyExporter")
        self.api_key  = api_key
        self.endpoint = (endpoint or PROVY_BASE_URL).rstrip("/") + "/api/otlp/v1/traces"
        self._headers = {"x-provy-key": api_key, "Content-Type": "application/json"}
        self._enabled = enabled

    def export(self, spans) -> "SpanExportResult":  # type: ignore[override]
        if not _emit_enabled(self._enabled):
            return SpanExportResult.SUCCESS  # type: ignore[attr-defined]  # emission off: drop silently
        otlp_spans = []
        for span in spans:
            ctx = span.get_span_context()
            parent_ctx = span.parent

            attrs = []
            for k, v in (span.attributes or {}).items():
                if isinstance(v, bool):   attrs.append({"key": k, "value": {"boolValue":   v}})
                elif isinstance(v, int):  attrs.append({"key": k, "value": {"intValue":    v}})
                elif isinstance(v, float):attrs.append({"key": k, "value": {"doubleValue": v}})
                else:                     attrs.append({"key": k, "value": {"stringValue": str(v)}})

            events = []
            for ev in (span.events or []):
                ev_attrs = []
                for k, v in (ev.attributes or {}).items():
                    ev_attrs.append({"key": k, "value": {"stringValue": str(v)}})
                events.append({"name": ev.name, "attributes": ev_attrs})

            otlp_spans.append({
                "spanId":              format(ctx.span_id,  "016x") if ctx else None,
                "parentSpanId":        format(parent_ctx.span_id, "016x") if parent_ctx else None,
                "traceId":             format(ctx.trace_id, "032x") if ctx else None,
                "name":                span.name,
                "startTimeUnixNano":   str(span.start_time),
                "endTimeUnixNano":     str(span.end_time),
                "status":              {"code": span.status.status_code.value if span.status else 0},
                "attributes":          attrs,
                "events":              events,
            })

        payload = {"resourceSpans": [{"scopeSpans": [{"spans": otlp_spans}]}]}
        try:
            r = requests.post(self.endpoint, json=payload, headers=self._headers, timeout=10)
            r.raise_for_status()
            return SpanExportResult.SUCCESS  # type: ignore[attr-defined]
        except Exception:
            return SpanExportResult.FAILURE  # type: ignore[attr-defined]

    def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# ProvyClient — direct ingest API (Path 2)
# ---------------------------------------------------------------------------

class ProvyClient:
    """
    Direct ingest API client for custom pipelines.

    OTel span IDs (span_id, parent_span_id) are generated automatically when
    opentelemetry-sdk is installed. Pass parent_trace_id manually to control
    the call graph when you are not using OTel.
    """

    def __init__(self, ingest_key: str | None = None, base_url: str | None = None, enabled: "bool | None" = None, buffered: bool = True):
        self.key  = ingest_key or os.environ.get("PROVY_API_KEY") or os.environ.get("ARGUS_INGEST_KEY", "")
        self.base = (base_url or PROVY_BASE_URL).rstrip("/")
        self._enabled = enabled
        self._headers = {
            "x-provy-key":  self.key,
            "Content-Type": "application/json",
        }
        # OTel state (populated when opentelemetry-sdk is installed)
        self._tracer         = None
        self._session_span   = None
        self._session_span_id: str | None = None
        self._agent_spans: dict[str, object] = {}

        if _OTEL_AVAILABLE:
            _provider = OtelTracerProvider()
            self._tracer = _provider.get_tracer("provy-sdk")

        # Spans are buffered and flushed in batches (#484). Buffering is what turns a transient
        # outage into a delay rather than data loss, and batching is free once they are queued: the
        # ingest API takes an array, so one flush is one request instead of forty.
        #
        # Set buffered=False for a strictly synchronous client. Only do that if you genuinely need
        # the write to have landed before the next line runs, and accept that a blip loses the span.
        self._buffered = buffered
        self._buffer = SpanBuffer(self._send_spans) if buffered else None

    # ---- Session lifecycle ------------------------------------------------

    def open_session(
        self,
        session_type: str,
        external_id:  str | None = None,
        metadata:     dict | None = None,
    ) -> str:
        if not _emit_enabled(self._enabled):
            return str(uuid.uuid4())  # emission off: local id so caller code keeps working
        # Synchronous on purpose: the caller needs the id back. Retried, because losing a session
        # open loses every span that would have hung off it.
        r = post_with_retry(
            f"{self.base}/api/ingest/session/open",
            {"session_type": session_type, "external_id": external_id, "metadata": metadata},
            self._headers,
        )
        if r is None or r.status_code >= 400:
            raise RuntimeError(
                "provy: could not open session after retries. "
                "Check PROVY_API_KEY and connectivity."
            )
        session_id = r.json()["session_id"]

        if self._tracer and _OTEL_AVAILABLE:
            self._session_span = self._tracer.start_span(f"session:{session_type}")  # type: ignore[union-attr]
            span_ctx = self._session_span.get_span_context()  # type: ignore[union-attr]
            self._session_span_id = format(span_ctx.span_id, "016x")

        return session_id

    def trace(
        self,
        session_id:     str,
        agent:          str,
        step_type:      str,
        outcome:        str,
        tool_name:      str | None = None,
        latency_ms:     int | None = None,
        tokens_in:      int | None = None,
        tokens_out:     int | None = None,
        cost_usd:       float | None = None,
        error:          str | None = None,
        output_json:    dict | None = None,
        parent_trace_id: str | None = None,
        entity_id:      str | None = None,
    ) -> str:
        """Log a trace step. Returns the span_id for this step (use as parent_trace_id for children).

        Pass entity_id (the work-item key: trade/order id, ticket id) to join this trace to the
        outcome you later report for the same item, so per-item quality and reconciliation link up.
        """
        if not _emit_enabled(self._enabled):
            return ""  # emission off: no-op

        span_id        = None
        parent_span_id = parent_trace_id  # caller override

        if self._tracer and _OTEL_AVAILABLE:
            # Determine OTel parent context
            import opentelemetry.context as otel_ctx_api  # type: ignore[import]
            from opentelemetry.trace import NonRecordingSpan  # type: ignore[import]

            if agent in self._agent_spans:
                parent_span = self._agent_spans[agent]
            elif self._session_span:
                parent_span = self._session_span
            else:
                parent_span = None

            ctx = otel_trace.set_span_in_context(parent_span) if parent_span else otel_ctx_api.context.Context()  # type: ignore[attr-defined]
            span = self._tracer.start_span(f"{agent}:{step_type}", context=ctx)  # type: ignore[union-attr]

            sc = span.get_span_context()
            span_id        = format(sc.span_id, "016x")
            parent_sc      = parent_span.get_span_context() if parent_span else None  # type: ignore[union-attr]
            parent_span_id = parent_span_id or (format(parent_sc.span_id, "016x") if parent_sc else None)

            # Store as the current agent span so nested tool calls can reference it
            self._agent_spans[agent] = span
            span.end()

        body: dict = {
            "session_id":    session_id,
            "agent":         agent,
            "step_type":     step_type,
            "outcome":       outcome,
            "tool_name":     tool_name,
            "latency_ms":    latency_ms,
            "tokens_input":  tokens_in,
            "tokens_output": tokens_out,
            "cost_usd":      cost_usd,
            "error":         error,
            "output_json":   output_json,
            "entity_id":     entity_id,
        }
        if span_id:        body["span_id"]        = span_id
        if parent_span_id: body["parent_span_id"] = parent_span_id

        # Buffered by default. Returns the locally generated span id, so the caller's call graph is
        # correct whether or not the span has reached the server yet.
        if self._buffer is not None:
            self._buffer.add(body)
        else:
            post_with_retry(f"{self.base}/api/ingest/trace", body, self._headers)
        return span_id or ""

    # ---- transport ---------------------------------------------------------

    def _send_spans(self, batch: list[dict]) -> bool:
        """Deliver one batch of spans. Returns False when they are lost, so the buffer can count."""
        r = post_with_retry(f"{self.base}/api/ingest/trace", batch, self._headers)
        return r is not None and r.status_code < 400

    def flush(self) -> None:
        """Send anything still buffered, synchronously. Safe to call at any time."""
        if self._buffer is not None:
            self._buffer.flush()

    @property
    def buffer_stats(self) -> dict:
        """Pending, dropped and failed span counts. Loss is visible, never silent."""
        return self._buffer.stats if self._buffer is not None else {"pending": 0, "dropped": 0, "failed": 0}

    def close_session(
        self,
        session_id:      str,
        status:          str = "completed",
        result_summary:  str | None = None,
        terminal_reason: str | None = None,
    ) -> None:
        if not _emit_enabled(self._enabled):
            return  # emission off: no-op
        if self._session_span and _OTEL_AVAILABLE:
            self._session_span.end()  # type: ignore[union-attr]
            self._session_span    = None
            self._session_span_id = None
            self._agent_spans     = {}

        # ⛔ FLUSH BEFORE CLOSING. Buffered spans must land before the session closes, or the server
        # computes a verdict over a run whose steps have not arrived. This ordering is what makes
        # buffering safe rather than a race.
        self.flush()
        r = post_with_retry(
            f"{self.base}/api/ingest/session/close",
            {"session_id": session_id, "result_summary": result_summary, "terminal_reason": terminal_reason},
            self._headers,
        )
        if r is None or r.status_code >= 400:
            log.error("provy: could not close session %s after retries", session_id)

    # ---- Quality checks ---------------------------------------------------

    def eval(
        self,
        session_id: str,
        eval_name:  str,
        agent:      str,
        score:      float,
        passed:     bool,
        layer:      int = 4,
        entity_id:  str | None = None,
        detail:     dict | None = None,
        threshold:  float | None = None,
    ) -> None:
        """Write one eval result (a quality check) for a session.

        Call once per criterion per session. score is 0..1; passed is score >= threshold.
        layer defaults to 4 (LLM-as-judge / output quality). Pass entity_id to score a
        specific work item when a session evaluates more than one. No-op when emission is off.
        """
        if not _emit_enabled(self._enabled):
            return  # emission off: no-op
        body: dict = {
            "session_id": session_id,
            "eval_name":  eval_name,
            "agent":      agent,
            "score":      score,
            "passed":     passed,
            "layer":      layer,
        }
        if entity_id is not None: body["entity_id"] = entity_id
        if detail    is not None: body["detail"]    = detail
        if threshold is not None: body["threshold"] = threshold

        r = post_with_retry(f"{self.base}/api/ingest/eval", body, self._headers)
        if r is None or r.status_code >= 400:
            log.error("provy: eval ingest failed after retries")

    # ---- Outcomes ---------------------------------------------------------

    def report_outcome(
        self,
        entity_id:   str,
        label:       str | None = None,
        value:       float | None = None,
        signals:     dict | None = None,
        session_id:  str | None = None,
        source:      str = "confirmed",
        occurred_at: str | None = None,
    ) -> None:
        """Report a real business outcome for a work item and reconcile it against the prediction.

        Keyed on entity_id (the same work-item key you tagged the traces/evals with). Send a
        label ('success' | 'fail') or a numeric value (its sign reconciles the prediction) to
        reconcile the overall prediction, and optionally a signals bag (name -> number | bool | str)
        to grade the contract's conditions (Estimated vs Real). Numeric strings coerce to numbers;
        non-numeric strings grade eq/in conditions. One call does both. Usually
        posted later by a downstream job when the outcome lands. No-op when emission is off.
        """
        if not _emit_enabled(self._enabled):
            return  # emission off: no-op
        body: dict = {
            "entity_id": entity_id,
            "source":    source,
        }
        if label       is not None: body["label"]       = label
        if value       is not None: body["value"]       = value
        if signals     is not None: body["signals"]     = signals
        if session_id  is not None: body["session_id"]  = session_id
        if occurred_at is not None: body["occurred_at"] = occurred_at

        r = post_with_retry(f"{self.base}/api/ingest/outcome", body, self._headers)
        if r is None or r.status_code >= 400:
            log.error("provy: outcome ingest failed after retries")

    # ---- Decorator --------------------------------------------------------

    def trace_fn(self, agent: str, step_type: str = "agent_step"):
        """Decorator that auto-traces a function call."""
        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(*args, session_id: str | None = None, **kwargs):
                start  = time.time()
                error  = None
                result = None
                try:
                    result = fn(*args, **kwargs)
                    return result
                except Exception as exc:
                    error = str(exc)
                    raise
                finally:
                    if session_id:
                        latency = int((time.time() - start) * 1000)
                        try:
                            self.trace(
                                session_id = session_id,
                                agent      = agent,
                                step_type  = step_type,
                                outcome    = str(result or error or ""),
                                latency_ms = latency,
                                error      = error,
                            )
                        except Exception as exc:  # noqa: BLE001
                            # ⛔ STILL DOES NOT RAISE, AND THAT IS DELIBERATE. This decorator wraps
                            # the caller's own function; breaking their agent because telemetry
                            # failed would be worse than the span being late.
                            #
                            # ⛔ BUT IT IS NO LONGER SILENT. This used to be `except Exception: pass`,
                            # so auto-instrumented spans vanished without trace on any failure. For a
                            # product built on the premise that an agent's own account of itself
                            # cannot be trusted, an SDK that quietly discarded the evidence was the
                            # wrong failure to have.
                            #
                            # In practice trace() now buffers, so this path is close to unreachable;
                            # it catches programming errors rather than network ones.
                            log.error(
                                "provy: could not record span for agent=%s step_type=%s: %s",
                                agent, step_type, exc,
                            )
            return wrapper
        return decorator
