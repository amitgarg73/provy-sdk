"""Tests for TraceLogger — focuses on log_skip and the skip trace payload."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, patch
import pytest
from provy.session import TraceLogger, _emit_enabled


class TestEmitGate:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("PROVY_EMIT", raising=False)
        assert _emit_enabled() is False

    def test_on_when_flag_set(self, monkeypatch):
        for v in ("1", "true", "YES", "on"):
            monkeypatch.setenv("PROVY_EMIT", v)
            assert _emit_enabled() is True

    def test_disabled_tracer_writes_nothing_and_makes_no_stub(self, monkeypatch):
        monkeypatch.delenv("PROVY_EMIT", raising=False)
        monkeypatch.setenv("TENANT_ID", "t1")
        client = MagicMock()
        with patch("provy.db.get_client", return_value=client):
            tracer = TraceLogger("sess-off", workflow_id="w1")  # __init__ -> stub skipped
            tracer.log_agent_message("research", "reasoning", "ok")
            tracer.close_session("completed")
        client.table.assert_not_called()  # no ag_sessions / ag_traces writes at all


def _make_tracer() -> tuple[TraceLogger, list[dict]]:
    """Return a TraceLogger wired to a mock Supabase client + captured rows."""
    captured: list[dict] = []

    mock_result = MagicMock()
    mock_result.data = [{"id": "span-1"}]
    mock_client = MagicMock()
    mock_client.table.return_value.insert.return_value.execute.return_value = mock_result
    mock_client.table.return_value.upsert.return_value.execute.return_value = mock_result
    mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = mock_result

    def capture_insert(row):
        captured.append(row)
        return mock_client.table.return_value.insert.return_value

    mock_client.table.return_value.insert.side_effect = capture_insert

    with patch("provy.db.get_client", return_value=mock_client), \
         patch.dict(os.environ, {"TENANT_ID": "t1", "WORKFLOW_ID": "w1", "SUPABASE_URL": "x", "SUPABASE_KEY": "y"}):
        tracer = TraceLogger.__new__(TraceLogger)
        tracer.session_id    = "sess-test"
        tracer._tenant_id    = "t1"
        tracer._workflow_id  = "w1"
        tracer._session_type = None
        tracer._default_model = "claude-haiku-4-5-20251001"
        tracer._sequence     = 0
        tracer._agent_spans  = {}
        tracer._tokens       = {}
        tracer._enabled      = True  # these tests exercise the emitting path
        from datetime import datetime
        tracer._started_at   = datetime.utcnow()
        tracer._db           = mock_client

    return tracer, captured


class TestLogSkip:
    def test_design_skip_writes_trace_row(self):
        tracer, captured = _make_tracer()
        with patch("provy.db.get_client", return_value=tracer._db):
            tracer.log_skip("news", reason="no_candidates", skip_type="design")
        row = captured[-1]
        assert row["step_type"] == "skip"
        assert row["agent"] == "news"
        assert row["outcome"] == "skipped"
        assert row["payload"]["reason"] == "no_candidates"
        assert row["payload"]["skip_type"] == "design"

    def test_error_skip_sets_skip_type_error(self):
        tracer, captured = _make_tracer()
        with patch("provy.db.get_client", return_value=tracer._db):
            tracer.log_skip("risk", reason="upstream_data_missing", skip_type="error")
        row = captured[-1]
        assert row["payload"]["skip_type"] == "error"
        assert row["payload"]["reason"] == "upstream_data_missing"

    def test_default_skip_type_is_design(self):
        tracer, captured = _make_tracer()
        with patch("provy.db.get_client", return_value=tracer._db):
            tracer.log_skip("news", reason="no_candidates")
        row = captured[-1]
        assert row["payload"]["skip_type"] == "design"

    def test_log_skip_returns_span_id(self):
        tracer, _ = _make_tracer()
        with patch("provy.db.get_client", return_value=tracer._db):
            result = tracer.log_skip("news", reason="no_candidates")
        assert isinstance(result, str)
        assert len(result) > 0


class TestForwardClaim:
    """The agent's own forward claim, carried as a field (argus#747).

    ⛔ WHY A SEPARATE FIELD AND NOT A PAYLOAD KEY. Provy keeps the LAST value it sees for a signal
    across a session. That is right for a READING (a close step supersedes earlier partials, and
    grading depends on it) and it destroys a CLAIM: on the reference fleet an agent reported
    `realized_pnl: 0` during the run, a later step reported the settled figure under the same key,
    and Provy stored the settled figure as what the agent had claimed. Every non-zero claimed value
    on that fleet is a byte-for-byte copy of what actually settled (argus#751).
    """

    @staticmethod
    def _emit(fn_name, *args, **kwargs):
        tracer, captured = _make_tracer()
        with patch("provy.db.get_client", return_value=tracer._db):
            getattr(tracer, fn_name)(*args, **kwargs)
        return captured

    def test_agent_message_carries_a_claim(self):
        captured = self._emit(
            "log_agent_message", "risk", "Entered AAPL.", "entered", entity_id="AAPL",
            claim={"signal": "realized_pnl", "value": 133.2, "confidence": 0.9},
        )
        assert captured[-1]["payload"]["provy_claim"] == {
            "signal": "realized_pnl", "value": 133.2, "confidence": 0.9,
        }

    def test_decision_carries_a_claim(self):
        captured = self._emit("log_decision", "orchestrator", "approved",
                              claim={"signal": "refund_posted", "value": True})
        assert captured[-1]["payload"]["provy_claim"] == {"signal": "refund_posted", "value": True}

    def test_a_list_of_claims_is_kept_as_a_list(self):
        captured = self._emit("log_decision", "orchestrator", "approved", claim=[
            {"signal": "pnl", "value": 1, "entity_id": "AAPL"},
            {"signal": "pnl", "value": 2, "entity_id": "MSFT"},
        ])
        assert len(captured[-1]["payload"]["provy_claim"]) == 2

    def test_a_malformed_claim_is_dropped_not_raised(self):
        # ⛔ TELEMETRY MUST NEVER BREAK THE CALLER'S RUN. This is called from inside a logging path,
        # so a claim the caller got wrong is logged and omitted, never raised into their code.
        for bad in ("a string", 42, {"value": 1}, {"signal": "  ", "value": 1}, {"signal": "s"}):
            captured = self._emit("log_decision", "orchestrator", "approved", claim=bad)
            assert "provy_claim" not in captured[-1]["payload"], bad

    def test_confidence_is_clamped_and_a_bad_one_omitted(self):
        captured = self._emit("log_decision", "a", "x",
                              claim={"signal": "s", "value": 1, "confidence": 80})
        assert captured[-1]["payload"]["provy_claim"]["confidence"] == 1.0
        captured = self._emit("log_decision", "a", "x",
                              claim={"signal": "s", "value": 1, "confidence": "high"})
        assert "confidence" not in captured[-1]["payload"]["provy_claim"]

    def test_a_payload_key_cannot_shadow_the_claim(self):
        # `payload.update()` merges caller keys, so an unvalidated `provy_claim` passed there would
        # otherwise silently replace the validated one.
        captured = self._emit(
            "log_agent_message", "risk", "r", "ok",
            payload={"provy_claim": {"signal": "spoofed", "value": 0}},
            claim={"signal": "realized_pnl", "value": 5},
        )
        assert captured[-1]["payload"]["provy_claim"]["signal"] == "realized_pnl"

    def test_no_claim_leaves_the_payload_untouched(self):
        captured = self._emit("log_agent_message", "risk", "r", "ok")
        assert "provy_claim" not in captured[-1]["payload"]
