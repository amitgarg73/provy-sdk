"""The emit gate must be safe AND audible, and every span must carry an id.

Both defects these cover shipped in 0.5.0 and both were invisible from inside the repo. They were
found by installing the published package from PyPI and pointing it at a live endpoint, which is
the only test that exercises what a customer actually gets.
"""
import logging
import pytest

from provy import ProvyClient
import provy.client as client_mod


@pytest.fixture(autouse=True)
def _clean_gate(monkeypatch):
    # The warn-once flag is module state and would leak between tests, making whichever test ran
    # second pass for the wrong reason.
    client_mod._reset_emit_warning()
    monkeypatch.delenv("PROVY_EMIT", raising=False)
    yield
    client_mod._reset_emit_warning()


class TestEmitOffIsAudible:
    """The regression: telemetry off used to be indistinguishable from telemetry working."""

    def test_warns_when_emit_is_off(self, caplog):
        with caplog.at_level(logging.WARNING, logger="provy.sdk"):
            ProvyClient(ingest_key="k", base_url="http://example.invalid")
        assert any("telemetry is OFF" in r.message for r in caplog.records), \
            "constructing a client with emission off must say so"
        # The message has to be actionable, not just present.
        joined = " ".join(r.message for r in caplog.records)
        assert "PROVY_EMIT=1" in joined
        assert "enabled=True" in joined

    def test_warns_once_per_process_not_once_per_call(self, caplog):
        with caplog.at_level(logging.WARNING, logger="provy.sdk"):
            c = ProvyClient(ingest_key="k", base_url="http://example.invalid")
            for _ in range(5):
                c.trace(session_id="s", agent="a", step_type="llm", outcome="success")
            c.close_session(session_id="s")
        warnings = [r for r in caplog.records if "telemetry is OFF" in r.message]
        assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"

    def test_silent_when_emission_is_on(self, caplog, monkeypatch):
        monkeypatch.setenv("PROVY_EMIT", "1")
        with caplog.at_level(logging.WARNING, logger="provy.sdk"):
            ProvyClient(ingest_key="k", base_url="http://example.invalid")
        assert not [r for r in caplog.records if "telemetry is OFF" in r.message], \
            "a correctly configured client must not nag"

    def test_explicit_enabled_true_beats_the_env(self, caplog):
        with caplog.at_level(logging.WARNING, logger="provy.sdk"):
            ProvyClient(ingest_key="k", base_url="http://example.invalid", enabled=True)
        assert not [r for r in caplog.records if "telemetry is OFF" in r.message]

    def test_explicit_enabled_false_still_warns(self, caplog):
        # Opting out on purpose is still worth one line: it is the same missing data either way,
        # and someone else usually has to debug it.
        with caplog.at_level(logging.WARNING, logger="provy.sdk"):
            ProvyClient(ingest_key="k", base_url="http://example.invalid", enabled=False)
        assert [r for r in caplog.records if "telemetry is OFF" in r.message]


class TestEverySpanCarriesAnId:
    """Retries dedupe on (tenant_id, session_id, span_id). No id means no dedupe means duplicates.

    Measured against a live endpoint before the fix: the same body sent twice wrote 2 rows without
    a span_id and 1 row with one. OpenTelemetry is an optional extra, so the DEFAULT install was
    the one duplicating.
    """

    def _capture(self, monkeypatch):
        sent = []
        monkeypatch.setattr(client_mod, "post_with_retry",
                            lambda url, payload, headers, **kw: sent.append(payload) or _Ok())
        return sent

    def test_span_id_is_always_sent(self, monkeypatch):
        monkeypatch.setenv("PROVY_EMIT", "1")
        sent = self._capture(monkeypatch)
        c = ProvyClient(ingest_key="k", base_url="http://example.invalid", buffered=False)
        c.trace(session_id="s", agent="a", step_type="llm", outcome="success")
        assert sent, "nothing was sent"
        assert sent[0].get("span_id"), "a span with no id cannot be deduped on retry"

    def test_trace_returns_the_id_it_sent(self, monkeypatch):
        monkeypatch.setenv("PROVY_EMIT", "1")
        sent = self._capture(monkeypatch)
        c = ProvyClient(ingest_key="k", base_url="http://example.invalid", buffered=False)
        returned = c.trace(session_id="s", agent="a", step_type="llm", outcome="success")
        # The docstring promises this value is usable as parent_trace_id. It used to be "".
        assert returned
        assert returned == sent[0]["span_id"]

    def test_ids_are_distinct_per_span(self, monkeypatch):
        monkeypatch.setenv("PROVY_EMIT", "1")
        sent = self._capture(monkeypatch)
        c = ProvyClient(ingest_key="k", base_url="http://example.invalid", buffered=False)
        for _ in range(20):
            c.trace(session_id="s", agent="a", step_type="llm", outcome="success")
        ids = [b["span_id"] for b in sent]
        assert len(set(ids)) == 20, "reusing an id would make the server collapse distinct steps"

    def test_caller_supplied_parent_is_preserved(self, monkeypatch):
        monkeypatch.setenv("PROVY_EMIT", "1")
        sent = self._capture(monkeypatch)
        c = ProvyClient(ingest_key="k", base_url="http://example.invalid", buffered=False)
        c.trace(session_id="s", agent="a", step_type="llm", outcome="success",
                parent_trace_id="parent-1")
        assert sent[0]["parent_span_id"] == "parent-1"

    def test_returns_a_usable_id_even_when_emission_is_off(self):
        # The call graph the caller builds must not change shape based on whether telemetry is on.
        c = ProvyClient(ingest_key="k", base_url="http://example.invalid")
        assert c.trace(session_id="s", agent="a", step_type="llm", outcome="success")


class _Ok:
    status_code = 200
    def json(self): return {}
