"""
The closed set of step types, and the four columns this client could not send (argus#1071).

⛔ EVERY DEFECT PINNED HERE WAS FOUND BY AN OUTSIDE INTEGRATION, NOT BY THIS SUITE, AND THE REASON IS
STRUCTURAL. The tests in this repo assert what the code does. These defects were all disagreements
between what the code does and what the README tells a customer to do, and nothing compared the two.

The integration built a five-agent pipeline against the README alone. Had it followed the quickstart,
every step would have been typed `agent_step`, which the product accepts, stores, displays, and then
skips in attribution, the judge, pattern detection and embeddings. It avoided that only by reading
the TypeScript SDK's type union, which happens to be exhaustive.
"""
import logging

import pytest

from provy.client import ProvyClient, STEP_TYPES, STEP_TYPE_LOOKALIKES


def _client(monkeypatch):
    """A client that captures the wire body instead of sending it."""
    c = ProvyClient(ingest_key="k", base_url="http://example.invalid",
                    buffered=False, enabled=True)
    sent = []
    monkeypatch.setattr(c, "_post", lambda path, body, **kw: sent.append(body))
    return c, sent


class TestTheClosedSet:
    def test_is_exactly_what_the_product_reasons_about(self):
        assert STEP_TYPES == {"tool_call", "agent_message", "decision", "error", "skip"}

    @pytest.mark.parametrize("gone", ["agent_step", "llm_call"])
    def test_the_values_the_readme_used_to_advertise_are_not_real(self, gone):
        assert gone not in STEP_TYPES
        # and each maps to the value the author meant, for the warning to name
        assert STEP_TYPE_LOOKALIKES[gone] == "agent_message"

    def test_the_decorator_no_longer_defaults_to_an_invisible_step(self):
        """
        ⛔ THE WORST ONE. `trace_fn` existed to make instrumentation effortless and defaulted to the
        one value that makes it inert. Every span it produced was invisible to the whole intelligence
        layer, with no error at any point.
        """
        import inspect
        default = inspect.signature(ProvyClient.trace_fn).parameters["step_type"].default
        assert default in STEP_TYPES
        assert default == "agent_message"


class TestItSaysSoWhenAStepWillBeInvisible:
    def test_warns_and_names_the_replacement(self, monkeypatch, caplog):
        c, _ = _client(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="provy.sdk"):
            c.trace(session_id="s", agent="a", step_type="agent_step", outcome="ok")
        msg = caplog.text
        assert "agent_step" in msg
        assert "agent_message" in msg          # the suggestion
        assert "invisible" in msg              # and what it costs

    def test_says_nothing_for_a_valid_one(self, monkeypatch, caplog):
        c, _ = _client(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="provy.sdk"):
            c.trace(session_id="s", agent="a", step_type="tool_call", outcome="ok")
        assert "invisible" not in caplog.text

    def test_warns_rather_than_raises(self, monkeypatch):
        """
        Deliberate. A caller sending `agent_step` today would break on upgrade, and breaking
        someone's agent because telemetry disapproved of a string is the wrong trade. The step is
        still sent, exactly as typed: guessing at intent would file it under something nobody chose.
        """
        c, sent = _client(monkeypatch)
        c.trace(session_id="s", agent="a", step_type="agent_step", outcome="ok")
        assert sent[-1]["step_type"] == "agent_step"


class TestTheFourColumnsThisClientCouldNotSend:
    """
    Each is stored in its own column, and both other doors are warned never to leave them in the
    payload because anything there is dropped when the trace body moves to object storage. This
    client had no parameter for any of them, so the door that could not express them was the one
    whose customers were never warned.
    """

    def test_they_reach_the_wire_at_the_top_level(self, monkeypatch):
        c, sent = _client(monkeypatch)
        c.trace(session_id="s", agent="extract", step_type="agent_message", outcome="ok",
                model="claude-haiku-4-5", prompt_version="v3",
                cache_read_tokens=120, cache_write_tokens=8)
        body = sent[-1]
        assert body["model"] == "claude-haiku-4-5"
        assert body["prompt_version"] == "v3"
        assert body["cache_read_tokens"] == 120
        assert body["cache_write_tokens"] == 8

    def test_they_are_never_buried_in_output_json(self, monkeypatch):
        c, sent = _client(monkeypatch)
        c.trace(session_id="s", agent="extract", step_type="agent_message", outcome="ok",
                model="claude-haiku-4-5", output_json={"lines": 4})
        body = sent[-1]
        assert "model" not in (body.get("output_json") or {})
        assert body["output_json"] == {"lines": 4}

    def test_omitted_when_unset_so_a_caller_asserts_nothing(self, monkeypatch):
        c, sent = _client(monkeypatch)
        c.trace(session_id="s", agent="a", step_type="decision", outcome="ok")
        for k in ("model", "prompt_version", "cache_read_tokens", "cache_write_tokens"):
            assert k not in sent[-1]


class TestTheReadmeAgreesWithTheCode:
    """
    ⛔ THE CHECK THAT DID NOT EXIST, AND IS THE ONLY KIND THAT COULD HAVE CAUGHT ANY OF THIS. Every
    other test here asserts behaviour. These compare the document a customer reads against the
    package they install, which is where all five defects lived.
    """

    @staticmethod
    def _readme():
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent / "README.md").read_text()

    def test_it_does_not_advertise_a_step_type_that_is_not_real(self):
        import re
        readme = self._readme()
        # Fenced code only: the prose deliberately names the retired values to explain the change.
        code = "\n".join(re.findall(r"```python\n(.*?)```", readme, re.S))
        for gone in STEP_TYPE_LOOKALIKES:
            # ⛔ THE QUOTED VALUE, NOT THE BARE WORD. `"tool"` is a lookalike and it appears inside
            # `tool_call`, so a substring check fails on a README that is entirely correct. The
            # first version of this test did exactly that.
            assert f'"{gone}"' not in code, f"README example still uses step_type {gone!r}"
            assert f"'{gone}'" not in code, f"README example still uses step_type {gone!r}"

    def test_the_api_reference_names_report_outcome(self):
        assert "report_outcome(" in self._readme(), (
            "report_outcome is how anything reconciles; a README without it describes a client "
            "that cannot do the one thing the product is for"
        )

    def test_the_trace_signature_is_not_an_ellipsis(self):
        readme = self._readme()
        for arg in ("entity_id", "claim", "inputs", "output_json", "model", "prompt_version"):
            assert arg in readme, f"README never mentions trace({arg}=...)"


class TestWhenTheWorkActuallyRan:
    """
    Late telemetry is not telemetry about now (argus#1072).

    ⛔ THE OTel DOOR HAS ALWAYS CARRIED THE SPAN'S OWN CLOCK and this one could not, so the door we
    recommend was the door that lost the time. A collector catching up after an outage was recorded
    as a burst of work at the moment it drained.
    """

    def test_a_step_can_say_when_it_ran(self, monkeypatch):
        c, sent = _client(monkeypatch)
        c.trace(session_id="s", agent="a", step_type="tool_call", outcome="ok",
                occurred_at="2026-09-08T06:20:41Z")
        assert sent[-1]["occurred_at"] == "2026-09-08T06:20:41Z"

    def test_omitted_when_not_given_so_the_server_stamps_arrival(self, monkeypatch):
        c, sent = _client(monkeypatch)
        c.trace(session_id="s", agent="a", step_type="tool_call", outcome="ok")
        assert "occurred_at" not in sent[-1]

    def test_a_session_can_say_when_it_ran(self, monkeypatch):
        c = ProvyClient(ingest_key="k", base_url="http://example.invalid",
                        buffered=False, enabled=True)
        seen = {}

        class _R:
            status_code = 200
            @staticmethod
            def json():
                return {"session_id": "sid"}

        def _post(path, payload, **kw):
            seen["path"], seen["payload"] = path, payload
            return _R()

        monkeypatch.setattr(c, "_post", _post)
        c.open_session("invoice_audit", started_at="2026-09-08T06:20:41Z")
        assert seen["payload"]["started_at"] == "2026-09-08T06:20:41Z"

    def test_a_session_omits_it_when_running_now(self, monkeypatch):
        c = ProvyClient(ingest_key="k", base_url="http://example.invalid",
                        buffered=False, enabled=True)
        seen = {}

        class _R:
            status_code = 200
            @staticmethod
            def json():
                return {"session_id": "sid"}

        monkeypatch.setattr(c, "_post", lambda path, payload, **kw: (seen.update(payload=payload), _R())[1])
        c.open_session("invoice_audit")
        assert "started_at" not in seen["payload"]
