"""A run says how it went (argus#1686): spans sent and accepted, once, at the end. Silence is never the answer."""
import pytest

pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import provy.client as client_mod
from provy import ProvyExporter
from provy.transport import SpanBuffer


def _spans(n):
    mem = InMemorySpanExporter()
    prov = TracerProvider()
    prov.add_span_processor(SimpleSpanProcessor(mem))
    tr = prov.get_tracer("t")
    for i in range(n):
        with tr.start_as_current_span("a.tool_call", attributes={"provy.session_id": "s", "provy.agent": "a"}):
            pass
    return mem.get_finished_spans()


class _Resp:
    def __init__(self, code=200, text="{}"):
        self.status_code, self.text = code, text


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PROVY_EMIT", "1")
    monkeypatch.delenv("PROVY_QUIET", raising=False)
    client_mod._reset_export_warning()
    yield
    client_mod._reset_export_warning()


def test_exporter_counts_what_was_sent_and_accepted(monkeypatch):
    monkeypatch.setattr(client_mod.requests, "post", lambda url, **kw: _Resp(200))
    exp = ProvyExporter(api_key="k", endpoint="http://x.invalid")
    exp.export(_spans(1200))
    assert exp.delivery_report() == {"sent": 1200, "accepted": 1200, "refused": 0}


def test_exporter_counts_a_refusal(monkeypatch):
    monkeypatch.setattr(client_mod.requests, "post", lambda url, **kw: _Resp(401, "bad key"))
    exp = ProvyExporter(api_key="k", endpoint="http://x.invalid")
    exp.export(_spans(7))
    assert exp.delivery_report() == {"sent": 7, "accepted": 0, "refused": 7}


def test_shutdown_prints_one_line_with_the_numbers(monkeypatch, capsys):
    monkeypatch.setattr(client_mod.requests, "post", lambda url, **kw: _Resp(200))
    exp = ProvyExporter(api_key="k", endpoint="http://x.invalid")
    exp.export(_spans(3))
    exp.shutdown()
    exp.shutdown()
    err = capsys.readouterr().err
    assert err.count("provy:") == 1 and "3 of 3 span(s) accepted" in err


def test_shutdown_says_when_spans_were_refused(monkeypatch, capsys):
    monkeypatch.setattr(client_mod.requests, "post", lambda url, **kw: _Resp(413, "too many"))
    exp = ProvyExporter(api_key="k", endpoint="http://x.invalid")
    exp.export(_spans(4))
    exp.shutdown()
    assert "0 of 4 span(s) accepted, 4 refused" in capsys.readouterr().err


def test_quiet_turns_the_line_off(monkeypatch, capsys):
    monkeypatch.setenv("PROVY_QUIET", "1")
    monkeypatch.setattr(client_mod.requests, "post", lambda url, **kw: _Resp(200))
    exp = ProvyExporter(api_key="k", endpoint="http://x.invalid")
    exp.export(_spans(2))
    exp.shutdown()
    assert capsys.readouterr().err == ""


def test_a_run_that_sent_nothing_prints_nothing(monkeypatch, capsys):
    exp = ProvyExporter(api_key="k", endpoint="http://x.invalid")
    exp.shutdown()
    assert capsys.readouterr().err == ""


def test_buffer_counts_sent_and_summarises_once(monkeypatch, capsys):
    buf = SpanBuffer(sender=lambda batch: True)
    for i in range(5):
        buf.add({"i": i})
    buf.shutdown()
    buf.shutdown()
    assert buf.stats["sent"] == 5
    err = capsys.readouterr().err
    assert err.count("provy:") == 1 and "5 span(s) sent and accepted" in err


def test_buffer_summary_names_loss(monkeypatch, capsys):
    buf = SpanBuffer(sender=lambda batch: False)
    buf.add({"i": 1})
    buf.shutdown()
    assert "0 span(s) sent and accepted, 1 lost" in capsys.readouterr().err
