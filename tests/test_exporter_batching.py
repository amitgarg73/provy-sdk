"""The OTLP exporter must never lose a full batch silently (argus#1683).

A customer on the OpenTelemetry door sent 330 sessions and exited 0; 8 arrived. OpenTelemetry's
BatchSpanProcessor hands an exporter up to 512 spans, the gateway refuses more than 500 in one request, and the
exporter used to swallow the refusal. These tests pin both halves: chunking, and an audible failure.
"""
import logging
import pytest

pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.export import SpanExportResult

import provy.client as client_mod
from provy import ProvyExporter


def _spans(n):
    mem = InMemorySpanExporter()
    prov = TracerProvider()
    prov.add_span_processor(SimpleSpanProcessor(mem))
    tr = prov.get_tracer("t")
    for i in range(n):
        with tr.start_as_current_span(f"a.tool_call", attributes={"provy.session_id": f"s{i % 7}", "provy.agent": "a"}):
            pass
    return mem.get_finished_spans()


class _Resp:
    def __init__(self, code=200, text="{}"):
        self.status_code, self.text = code, text


@pytest.fixture(autouse=True)
def _gate(monkeypatch):
    monkeypatch.setenv("PROVY_EMIT", "1")
    client_mod._reset_export_warning()
    yield
    client_mod._reset_export_warning()


def _count(call):
    return len(call["json"]["resourceSpans"][0]["scopeSpans"][0]["spans"])


def test_a_default_batch_of_512_is_split_under_the_gateway_limit(monkeypatch):
    sent = []
    monkeypatch.setattr(client_mod.requests, "post", lambda url, **kw: sent.append(kw) or _Resp(200))
    result = ProvyExporter(api_key="k", endpoint="http://x.invalid").export(_spans(512))
    assert result == SpanExportResult.SUCCESS
    assert [_count(c) for c in sent] == [500, 12]


def test_many_spans_arrive_in_full_and_no_request_exceeds_500(monkeypatch):
    sent = []
    monkeypatch.setattr(client_mod.requests, "post", lambda url, **kw: sent.append(kw) or _Resp(200))
    ProvyExporter(api_key="k", endpoint="http://x.invalid").export(_spans(1200))
    counts = [_count(c) for c in sent]
    assert sum(counts) == 1200 and max(counts) <= 500 and len(counts) == 3


def test_a_refusal_is_reported_once_with_the_status(monkeypatch, caplog):
    monkeypatch.setattr(client_mod.requests, "post", lambda url, **kw: _Resp(413, '{"error":"too many spans"}'))
    exp = ProvyExporter(api_key="k", endpoint="http://x.invalid")
    with caplog.at_level(logging.WARNING, logger="provy.sdk"):
        r1 = exp.export(_spans(3))
        r2 = exp.export(_spans(3))
    assert r1 == SpanExportResult.FAILURE and r2 == SpanExportResult.FAILURE
    msgs = [r.message for r in caplog.records if "span export" in r.message]
    assert len(msgs) == 1, "a refused export must be audible, and only once"
    assert "413" in msgs[0] and "x.invalid" in msgs[0]


def test_no_response_is_reported_too(monkeypatch, caplog):
    def boom(url, **kw):
        raise OSError("connection refused")
    monkeypatch.setattr(client_mod.requests, "post", boom)
    with caplog.at_level(logging.WARNING, logger="provy.sdk"):
        r = ProvyExporter(api_key="k", endpoint="http://x.invalid").export(_spans(2))
    assert r == SpanExportResult.FAILURE
    assert any("no response" in m.message and "connection refused" in m.message for m in caplog.records)


def test_a_successful_export_is_silent(monkeypatch, caplog):
    monkeypatch.setattr(client_mod.requests, "post", lambda url, **kw: _Resp(200))
    with caplog.at_level(logging.WARNING, logger="provy.sdk"):
        r = ProvyExporter(api_key="k", endpoint="http://x.invalid").export(_spans(4))
    assert r == SpanExportResult.SUCCESS
    assert not [m for m in caplog.records if "span export" in m.message]
