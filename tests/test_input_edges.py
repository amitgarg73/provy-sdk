"""
`inputs=` records which spans' output a step consumed — argus#1009.

⛔ WHY THIS EXISTS. Provy could previously only see which steps ran after which, and "ran later" is
not "was affected by". It has twice had to remove logic that treated the two as the same: once when
every agent in a session was charged with one agent's incident (an orchestrator showing an 87%
incident rate having caused none of them), and again in the quality window. Without a declared input
edge there is no evidence for a downstream claim, only position.

Measured before this shipped: across 8,337 production spans and 256 sessions there were ZERO edges
between two different agents. Every cross-agent-looking parent edge was one agent fanning out per
work item.
"""
import provy.client as client_mod
from provy.client import ProvyClient


class _Ok:
    status_code = 200
    text = "{}"

    def json(self):
        return {}


def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(client_mod, "post_with_retry",
                        lambda url, payload, headers, **kw: sent.append(payload) or _Ok())
    return sent


def _client():
    return ProvyClient(ingest_key="k", base_url="http://example.invalid", buffered=False)


class TestInputEdges:
    def test_inputs_are_sent_as_input_span_ids(self, monkeypatch):
        monkeypatch.setenv("PROVY_EMIT", "1")
        sent = _capture(monkeypatch)
        c = _client()
        upstream = c.trace(session_id="s", agent="research", step_type="decision", outcome="success")
        c.trace(session_id="s", agent="risk", step_type="decision", outcome="success",
                inputs=[upstream])
        assert sent[1]["input_span_ids"] == [upstream]

    # ⛔ THE DISTINCTION THIS WHOLE TEST FILE PROTECTS. The server stores NULL for "the client said
    # nothing" and [] for "this step read nothing", and they are different claims. If an unset
    # `inputs` started sending [], every caller that has not adopted the field would begin asserting
    # that its steps consume nothing — turning silence into a positive statement about the pipeline.
    def test_omitted_when_the_caller_says_nothing(self, monkeypatch):
        monkeypatch.setenv("PROVY_EMIT", "1")
        sent = _capture(monkeypatch)
        _client().trace(session_id="s", agent="a", step_type="llm", outcome="success")
        assert "input_span_ids" not in sent[0]

    def test_empty_list_is_sent_as_an_explicit_claim(self, monkeypatch):
        monkeypatch.setenv("PROVY_EMIT", "1")
        sent = _capture(monkeypatch)
        _client().trace(session_id="s", agent="a", step_type="llm", outcome="success", inputs=[])
        assert sent[0]["input_span_ids"] == []

    def test_junk_entries_are_dropped_not_coerced(self, monkeypatch):
        monkeypatch.setenv("PROVY_EMIT", "1")
        sent = _capture(monkeypatch)
        _client().trace(session_id="s", agent="a", step_type="llm", outcome="success",
                        inputs=["abc123", "", None, 7])  # type: ignore[list-item]
        assert sent[0]["input_span_ids"] == ["abc123"]

    # An input edge is a DATA dependency between siblings; parent_trace_id is the CALL TREE. A step
    # that sets both must send both, unchanged, or the two relationships collapse into one.
    def test_inputs_do_not_overwrite_the_parent(self, monkeypatch):
        monkeypatch.setenv("PROVY_EMIT", "1")
        sent = _capture(monkeypatch)
        _client().trace(session_id="s", agent="risk", step_type="decision", outcome="success",
                        parent_trace_id="1111111111111111", inputs=["2222222222222222"])
        assert sent[0]["parent_span_id"] == "1111111111111111"
        assert sent[0]["input_span_ids"] == ["2222222222222222"]

    # The call graph must not change shape depending on whether telemetry is on, and that promise
    # now covers a step that declares inputs too.
    def test_still_returns_a_usable_id_when_disabled(self, monkeypatch):
        monkeypatch.delenv("PROVY_EMIT", raising=False)
        sent = _capture(monkeypatch)
        rid = _client().trace(session_id="s", agent="risk", step_type="decision",
                              outcome="success", inputs=["2222222222222222"])
        assert rid and len(rid) == 16
        assert not sent
