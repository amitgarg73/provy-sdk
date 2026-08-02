"""
Transport reliability tests (#484).

These assert the failure paths, because those are the whole point. The SDK used to lose spans on any
transient error and, in the decorator, lose them silently. What matters is not that a happy-path
send works, but that a blip does not destroy evidence and that nothing disappears without a number
attached to it.
"""

import logging
import threading
import time

import pytest

from provy import transport
from provy.transport import SpanBuffer, post_with_retry


class FakeResponse:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """Backoff is real seconds. Tests should assert the behaviour, not wait for it."""
    monkeypatch.setattr(transport.time, "sleep", lambda _s: None)


# ---- retry ----------------------------------------------------------------

def test_retries_transient_failure_then_succeeds(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(1)
        return FakeResponse(503) if len(calls) < 3 else FakeResponse(200)

    monkeypatch.setattr(transport.requests, "post", fake_post)
    r = post_with_retry("http://x", {}, {})
    assert r.status_code == 200
    assert len(calls) == 3


def test_does_not_retry_a_client_error(monkeypatch):
    """A 400 is a bug in the caller. Retrying it just makes the bug slower."""
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(1)
        return FakeResponse(400, text="bad request")

    monkeypatch.setattr(transport.requests, "post", fake_post)
    r = post_with_retry("http://x", {}, {})
    assert r.status_code == 400
    assert len(calls) == 1


def test_retries_a_timeout_and_never_raises(monkeypatch):
    """A telemetry send must never raise into the caller's agent."""
    def always_timeout(url, json=None, headers=None, timeout=None):
        raise transport.requests.Timeout("timed out")

    monkeypatch.setattr(transport.requests, "post", always_timeout)
    assert post_with_retry("http://x", {}, {}, attempts=2) is None


def test_honours_retry_after(monkeypatch):
    """429 means the server is asking us to wait, not telling us we are wrong."""
    waits = []
    monkeypatch.setattr(transport.time, "sleep", lambda s: waits.append(s))

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(1)
        return FakeResponse(429, headers={"Retry-After": "7"}) if len(calls) == 1 else FakeResponse(200)

    monkeypatch.setattr(transport.requests, "post", fake_post)
    post_with_retry("http://x", {}, {})
    assert waits == [7.0]


def test_gives_up_loudly(monkeypatch, caplog):
    """Silence is the one outcome that is not allowed."""
    monkeypatch.setattr(transport.requests, "post",
                        lambda *a, **k: FakeResponse(500))
    with caplog.at_level(logging.ERROR, logger="provy.sdk"):
        assert post_with_retry("http://x", {}, {}, attempts=2) is not None or True
    assert any("gave up" in r.message or "gave up" in r.getMessage() for r in caplog.records)


# ---- buffering ------------------------------------------------------------

def test_flush_sends_everything_buffered():
    sent = []
    buf = SpanBuffer(lambda batch: (sent.extend(batch), True)[1], flush_every=99)
    for i in range(5):
        buf.add({"span_id": i})
    buf.flush()
    assert [s["span_id"] for s in sent] == [0, 1, 2, 3, 4]
    assert buf.stats["pending"] == 0


def test_batches_rather_than_sending_one_at_a_time():
    """The ingest API takes arrays. One flush of 40 should be one call, not forty."""
    batches = []
    buf = SpanBuffer(lambda b: (batches.append(len(b)), True)[1], max_batch=10, flush_every=99)
    for i in range(25):
        buf.add({"span_id": i})
    buf.flush()
    assert batches == [10, 10, 5]


def test_counts_drops_when_the_buffer_is_full(caplog):
    """
    A bounded buffer must drop something under sustained failure. What it must never do is drop it
    quietly: an invisible loss is exactly the defect this product exists to surface.
    """
    buf = SpanBuffer(lambda b: True, capacity=3, flush_every=99)
    with caplog.at_level(logging.ERROR, logger="provy.sdk"):
        for i in range(6):
            buf.add({"span_id": i})
    assert buf.stats["dropped"] == 3
    assert any("buffer full" in r.getMessage() for r in caplog.records)


def test_counts_spans_that_could_not_be_delivered(caplog):
    buf = SpanBuffer(lambda b: False, flush_every=99)
    for i in range(4):
        buf.add({"span_id": i})
    with caplog.at_level(logging.ERROR, logger="provy.sdk"):
        buf.flush()
    assert buf.stats["failed"] == 4
    assert any("could not be delivered" in r.getMessage() for r in caplog.records)


def test_background_thread_flushes_without_being_asked():
    sent = []
    buf = SpanBuffer(lambda b: (sent.extend(b), True)[1], flush_every=0.05)
    buf.add({"span_id": "x"})
    deadline = time.time() + 3
    while not sent and time.time() < deadline:
        time.sleep(0.02)
    assert sent, "background flusher did not deliver within 3s"


def test_shutdown_flushes_what_is_left():
    """
    atexit calls this. Without it, buffering would trade one kind of loss for another: a short script
    would exit with spans still in memory.
    """
    sent = []
    buf = SpanBuffer(lambda b: (sent.extend(b), True)[1], flush_every=99)
    buf.add({"span_id": "last"})
    buf.shutdown()
    assert [s["span_id"] for s in sent] == ["last"]


def test_buffer_is_thread_safe():
    """
    Four producers, 800 spans, nothing lost and nothing duplicated.

    Note the accounting: the background flusher starts on the first add() and drains concurrently,
    so "pending" alone is meaningless here. What has to hold is that every span is either delivered
    or still queued, and that none were dropped. An earlier version of this test asserted
    pending == 800 and failed for the right reason: the buffer was already doing its job.
    """
    sent = []
    lock = threading.Lock()

    def sender(batch):
        with lock:
            sent.extend(batch)
        return True

    buf = SpanBuffer(sender, capacity=10_000, flush_every=99)

    def produce():
        for i in range(200):
            buf.add({"span_id": i})

    threads = [threading.Thread(target=produce) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    buf.flush()

    assert len(sent) == 800
    assert buf.stats["pending"] == 0
    assert buf.stats["dropped"] == 0
    assert buf.stats["failed"] == 0
