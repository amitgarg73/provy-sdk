"""
The masker runs on EVERY path out of this SDK, and no future call site can forget it.

⛔ WHY A STRUCTURAL TEST AND NOT JUST BEHAVIOURAL ONES. The server shipped exactly this feature with
redaction inside two funnels, and fifteen call sites quietly built their own client around them and
sent raw tenant content for the whole life of the feature (argus #421). The behavioural tests below
prove today's five senders mask. The source test at the bottom is what stops the sixth.
"""

import re
from pathlib import Path

import pytest

import provy.client as client_mod
from provy.client import ProvyClient
from provy.redact import tokenizing_masker

SECRET = "boundary-test-secret"
PII = "ada@example.com"


class FakeResponse:
    status_code = 200

    def json(self):
        return {"session_id": "sess-1"}


@pytest.fixture
def captured(monkeypatch):
    """Intercept the transport and keep every payload the SDK tried to send."""
    sent = []

    def fake_post(url, payload, headers, timeout=10.0, attempts=4):
        sent.append({"url": url, "payload": payload})
        return FakeResponse()

    monkeypatch.setattr(client_mod, "post_with_retry", fake_post)
    monkeypatch.setenv("PROVY_EMIT", "1")
    return sent


def _blob(payloads) -> str:
    """Everything the SDK sent, as one string, so a leak anywhere is a substring match."""
    return repr(payloads)


class TestEveryExitMasks:
    """One test per sender. If a new sender appears, the source test below fails first."""

    def test_trace_unbuffered(self, captured):
        c = ProvyClient(ingest_key="k", mask=tokenizing_masker(SECRET), buffered=False)
        c.trace(session_id="s", agent="a", step_type="tool_call", outcome=f"mailed {PII}")
        assert PII not in _blob(captured)
        assert "[EMAIL_" in _blob(captured)

    def test_trace_buffered_batch(self, captured):
        c = ProvyClient(ingest_key="k", mask=tokenizing_masker(SECRET), buffered=True)
        c.trace(session_id="s", agent="a", step_type="tool_call", outcome=f"mailed {PII}")
        c.flush()
        assert PII not in _blob(captured)
        assert "[EMAIL_" in _blob(captured)

    def test_open_session(self, captured):
        c = ProvyClient(ingest_key="k", mask=tokenizing_masker(SECRET), buffered=False)
        c.open_session("support_run", metadata={"requester": PII})
        assert PII not in _blob(captured)

    def test_close_session(self, captured):
        c = ProvyClient(ingest_key="k", mask=tokenizing_masker(SECRET), buffered=False)
        c.close_session("s", result_summary=f"resolved for {PII}")
        assert PII not in _blob(captured)

    def test_eval(self, captured):
        c = ProvyClient(ingest_key="k", mask=tokenizing_masker(SECRET), buffered=False)
        c.eval(session_id="s", eval_name="tone", agent="a", score=8, passed=True,
               detail=f"addressed {PII} politely")
        assert PII not in _blob(captured)

    def test_report_outcome(self, captured):
        c = ProvyClient(ingest_key="k", mask=tokenizing_masker(SECRET), buffered=False)
        c.report_outcome(entity_id="TKT-1", source="crm", label="success",
                         signals={"contacted": PII})
        assert PII not in _blob(captured)


class TestJoinKeysStillArrive:
    def test_identity_is_not_masked_so_the_server_can_still_join(self, captured):
        c = ProvyClient(ingest_key="k", mask=tokenizing_masker(SECRET), buffered=False)
        c.trace(session_id="sess-abc", agent="refund_agent", step_type="tool_call",
                outcome=f"mailed {PII}", entity_id="TKT-000123", tool_name="lookup_customer")
        body = captured[0]["payload"]
        assert body["session_id"] == "sess-abc"
        assert body["entity_id"] == "TKT-000123"
        assert body["agent"] == "refund_agent"
        assert body["tool_name"] == "lookup_customer"
        assert body["span_id"]


class TestFailClosed:
    def test_a_raising_masker_drops_the_payload_instead_of_sending_it_raw(self, captured, caplog):
        def broken(_body):
            raise RuntimeError("regex blew up")

        c = ProvyClient(ingest_key="k", mask=broken, buffered=False)
        c.trace(session_id="s", agent="a", step_type="tool_call", outcome=f"mailed {PII}")

        assert captured == [], "nothing may be sent when the masker failed"
        assert PII not in _blob(captured)

    def test_the_drop_is_loud(self, captured, caplog):
        def broken(_body):
            raise RuntimeError("regex blew up")

        c = ProvyClient(ingest_key="k", mask=broken, buffered=False)
        with caplog.at_level("ERROR"):
            c.trace(session_id="s", agent="a", step_type="tool_call", outcome="x")
        assert any("DROPPED" in r.message or "DROPPED" in r.getMessage() for r in caplog.records)

    def test_trace_still_returns_an_id_so_the_caller_graph_is_unchanged(self, captured):
        def broken(_body):
            raise RuntimeError("nope")

        c = ProvyClient(ingest_key="k", mask=broken, buffered=False)
        span_id = c.trace(session_id="s", agent="a", step_type="tool_call", outcome="x")
        assert span_id and len(span_id) == 16


class TestNoMaskerIsUnchangedBehaviour:
    def test_without_a_masker_nothing_is_altered(self, captured):
        c = ProvyClient(ingest_key="k", buffered=False)
        c.trace(session_id="s", agent="a", step_type="tool_call", outcome=f"mailed {PII}")
        assert PII in _blob(captured), "masking is opt-in; the default must not change payloads"


class TestTheBoundaryIsStructural:
    """⛔ THE TEST THAT STOPS THE SIXTH SENDER. Behavioural tests only cover senders that exist."""

    def test_no_method_posts_directly_around_the_mask_gate(self):
        src = Path(client_mod.__file__).read_text()

        # The two legitimate sends: the client boundary, and the OTel exporter, which masks through
        # the same gate immediately above its post.
        allowed_post_with_retry = 'return post_with_retry(f"{self.base}{path}", masked, self._headers, timeout=timeout)'
        allowed_requests_post = "r = requests.post(self.endpoint, json=masked, headers=self._headers, timeout=10)"

        offenders = []
        for i, line in enumerate(src.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue
            if "post_with_retry(" in stripped and "def post_with_retry" not in stripped:
                if stripped != allowed_post_with_retry and "from .transport import" not in stripped:
                    offenders.append((i, stripped))
            if "requests.post(" in stripped and stripped != allowed_requests_post:
                offenders.append((i, stripped))

        assert not offenders, (
            "every send must go through _post (which masks) or through the exporter's gated post. "
            f"These bypass it: {offenders}"
        )

    def test_the_gate_is_applied_before_the_payload_is_serialised(self):
        src = Path(client_mod.__file__).read_text()
        # `masked`, not `payload`, is what reaches the wire. A refactor that passed the raw payload
        # would still typecheck and still pass every behavioural test above if the gate were a no-op.
        assert 'post_with_retry(f"{self.base}{path}", masked' in src
        assert "json=masked" in src
