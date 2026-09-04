"""
Outcome reporting: the work-item address has two halves and the SDK used to send one (#733).

A ledger row is keyed by (tenant, workflow, entity_id, business_date). Tenant and workflow come
from the ingest key, so the caller supplies the other two. Until this change `report_outcome`
could not send `business_date` at all, which pushed every SDK caller onto the server's fallback:
the most recent open prediction for that entity wins. That silently reconciles against the wrong
attempt for a retry, a re-run, or the same entity worked on two days.

These tests assert the field reaches the wire, and that omitting it stays omitted rather than
being defaulted to today, which would be worse than absent: a wrong date matches a wrong row,
where a missing one at least lands on a documented fallback.
"""

import provy.client as client_mod
from provy.client import ProvyClient


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


def _capture(monkeypatch):
    """Intercept the outcome POST and hand back the body it would have sent."""
    sent = {}

    def fake_post(url, body, headers, **_kw):
        sent["url"] = url
        sent["body"] = body
        return FakeResponse(200)

    monkeypatch.setattr(client_mod, "post_with_retry", fake_post)
    return sent


def _client():
    return ProvyClient(ingest_key="k", base_url="http://x", enabled=True)


def test_business_date_reaches_the_wire(monkeypatch):
    sent = _capture(monkeypatch)
    _client().report_outcome(entity_id="TKT-1", label="success", business_date="2026-09-03")

    assert sent["url"].endswith("/api/ingest/outcome")
    assert sent["body"]["business_date"] == "2026-09-03"
    assert sent["body"]["entity_id"] == "TKT-1"


def test_business_date_is_absent_when_not_given(monkeypatch):
    """Absent, never defaulted. A guessed date addresses a row that is wrong with confidence."""
    sent = _capture(monkeypatch)
    _client().report_outcome(entity_id="TKT-1", label="success")

    assert "business_date" not in sent["body"]


def test_the_rest_of_the_payload_is_unchanged(monkeypatch):
    sent = _capture(monkeypatch)
    _client().report_outcome(
        entity_id="TKT-2",
        value=-1.5,
        signals={"resolution_persists": False, "reopen_count": 2},
        session_id="sess-9",
        source="proxy",
        occurred_at="2026-09-03T10:00:00Z",
        business_date="2026-09-02",
    )

    assert sent["body"] == {
        "entity_id":     "TKT-2",
        "source":        "proxy",
        "value":         -1.5,
        "signals":       {"resolution_persists": False, "reopen_count": 2},
        "session_id":    "sess-9",
        "occurred_at":   "2026-09-03T10:00:00Z",
        "business_date": "2026-09-02",
    }


def test_emission_off_sends_nothing(monkeypatch):
    sent = _capture(monkeypatch)
    ProvyClient(ingest_key="k", base_url="http://x", enabled=False).report_outcome(
        entity_id="TKT-3", label="success", business_date="2026-09-03",
    )

    assert sent == {}
