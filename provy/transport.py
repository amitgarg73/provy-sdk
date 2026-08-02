"""
HTTP transport for the Provy SDK: retry, batching, and a bounded buffer.

⛔ WHY THIS EXISTS. Before this module every send was a bare `requests.post(timeout=10)` with no
retry at all, so a timeout, a DNS blip or a 503 lost the span outright. Worse, the `trace_fn`
decorator posted inside `try/except: pass`, so auto-instrumented spans disappeared in silence. For a
product whose entire pitch is that an agent's own account of itself cannot be trusted, an SDK that
quietly drops the evidence is the wrong failure to have.

The rules this module follows:

  1. NEVER raise into the caller's code from a telemetry send. Breaking someone's agent because our
     ingest was briefly unavailable is worse than the data being late.
  2. NEVER drop silently. If something is discarded, it is counted and logged. A number nobody can
     see is the thing this company exists to complain about.
  3. Retry what is retryable, and only that. Timeouts, connection errors, 429 and 5xx are transient.
     A 400 or a 401 is a bug in the caller or a bad key, and retrying it just makes the bug slower.
"""

from __future__ import annotations

import atexit
import logging
import random
import threading
import time
from collections import deque
from typing import Any, Callable, Iterable

import requests

log = logging.getLogger("provy.sdk")

# Retryable: transient by nature. 429 is included because the server is asking us to wait, not
# telling us we are wrong.
RETRY_STATUS = {408, 429, 500, 502, 503, 504}

DEFAULT_ATTEMPTS = 4
DEFAULT_BACKOFF  = 0.5   # seconds, doubled each attempt, with jitter
MAX_BACKOFF      = 30.0

# The ingest API accepts up to 500 spans per request (#479). Stay under it so a flush never trips
# the cap and gets rejected wholesale.
MAX_BATCH   = 250
FLUSH_EVERY = 2.0        # seconds

# Bounded so a long outage cannot grow the process's memory without limit. When it is full the
# OLDEST spans are dropped, because recent ones are more likely to still be reconcilable, and the
# count is reported rather than hidden.
DEFAULT_CAPACITY = 10_000


def _sleep_for(attempt: int, retry_after: str | None) -> float:
    """Honour Retry-After when the server sends it, otherwise exponential backoff with jitter."""
    if retry_after:
        try:
            return min(float(retry_after), MAX_BACKOFF)
        except (TypeError, ValueError):
            pass  # Retry-After can be an HTTP date; fall through to backoff rather than parse it
    return min(DEFAULT_BACKOFF * (2 ** attempt), MAX_BACKOFF) * (0.5 + random.random())


def post_with_retry(
    url: str,
    payload: Any,
    headers: dict,
    timeout: float = 10.0,
    attempts: int = DEFAULT_ATTEMPTS,
) -> requests.Response | None:
    """
    POST, retrying transient failures. Returns the response, or None when every attempt failed.

    Does NOT raise. Callers that need the body (open_session) check for None and decide; callers
    that are fire-and-forget ignore it.
    """
    last_err: Exception | None = None
    for attempt in range(attempts):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if r.status_code < 400:
                return r
            if r.status_code not in RETRY_STATUS:
                # A client error is a bug, not weather. Report it once and stop.
                log.error("provy: %s returned %s: %s", url, r.status_code, r.text[:200])
                return r
            last_err = RuntimeError(f"HTTP {r.status_code}")
            wait = _sleep_for(attempt, r.headers.get("Retry-After"))
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            wait = _sleep_for(attempt, None)
        except Exception as e:  # noqa: BLE001 — a transport bug must not reach the caller's agent
            log.error("provy: unexpected error posting to %s: %s", url, e)
            return None

        if attempt < attempts - 1:
            log.warning("provy: %s failed (%s), retrying in %.1fs", url, last_err, wait)
            time.sleep(wait)

    log.error("provy: gave up on %s after %d attempts: %s", url, attempts, last_err)
    return None


class SpanBuffer:
    """
    Collects spans and flushes them in batches on a background thread.

    Buffering is what turns a transient outage from data loss into a delay. It also makes batching
    free: the ingest API takes an array, so one flush of 40 spans is one request instead of 40.

    ⛔ FLUSH BEFORE THE PROCESS EXITS. Registered with atexit, and close_session() flushes too, so a
    short-lived script does not end with spans still in memory. Without that, buffering would trade
    one kind of loss for another.
    """

    def __init__(
        self,
        sender: Callable[[list[dict]], bool],
        capacity: int = DEFAULT_CAPACITY,
        max_batch: int = MAX_BATCH,
        flush_every: float = FLUSH_EVERY,
    ):
        self._sender = sender
        self._q: deque[dict] = deque(maxlen=capacity)
        self._max_batch = max_batch
        self._flush_every = flush_every
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._dropped = 0
        self._failed = 0
        self._thread: threading.Thread | None = None
        atexit.register(self.shutdown)

    # -- producer side ---------------------------------------------------

    def add(self, span: dict) -> None:
        with self._lock:
            # deque(maxlen) silently discards the oldest, so count it ourselves. Silent is the one
            # thing this module will not do.
            if len(self._q) == self._q.maxlen:
                self._dropped += 1
                if self._dropped in (1, 10, 100) or self._dropped % 1000 == 0:
                    log.error(
                        "provy: span buffer full (%d), dropping oldest. %d dropped so far. "
                        "Ingest is failing or the process is producing faster than it can send.",
                        self._q.maxlen, self._dropped,
                    )
            self._q.append(span)
            ready = len(self._q) >= self._max_batch
        self._ensure_thread()
        if ready:
            self._wake.set()

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # Started lazily so importing the SDK costs nothing. Daemon so it can never keep a process
        # alive; atexit does the final flush.
        self._thread = threading.Thread(target=self._run, name="provy-span-flush", daemon=True)
        self._thread.start()

    # -- consumer side ---------------------------------------------------

    def _drain(self) -> list[dict]:
        with self._lock:
            batch = [self._q.popleft() for _ in range(min(self._max_batch, len(self._q)))]
        return batch

    def _send(self, batch: list[dict]) -> None:
        if not batch:
            return
        if not self._sender(batch):
            self._failed += len(batch)
            log.error(
                "provy: %d span(s) could not be delivered and are lost. %d lost in total.",
                len(batch), self._failed,
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._flush_every)
            self._wake.clear()
            while True:
                batch = self._drain()
                if not batch:
                    break
                self._send(batch)

    # -- lifecycle -------------------------------------------------------

    def flush(self) -> None:
        """Send everything buffered, synchronously. Called by close_session and at exit."""
        while True:
            batch = self._drain()
            if not batch:
                return
            self._send(batch)

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        self.flush()

    @property
    def stats(self) -> dict:
        """Visible counts, so loss can be asserted on rather than guessed at."""
        with self._lock:
            pending = len(self._q)
        return {"pending": pending, "dropped": self._dropped, "failed": self._failed}


def batched(items: Iterable[dict], size: int) -> Iterable[list[dict]]:
    batch: list[dict] = []
    for it in items:
        batch.append(it)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
