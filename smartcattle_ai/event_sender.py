"""Sends events on a thread of their own: a slow or absent backend must never hold up detection."""

import sys
import time
from collections import deque
from threading import Condition, Event, Thread

from .backend_client import BackendError

# Far more than the per-camera cooldown produces during a long outage; only the newest survive a longer one.
MAX_PENDING = 200
# After this, telling someone an animal left says little they can act on, and the animal's next escape is reported anyway.
MAX_AGE_S = 15 * 60
MAX_BACKOFF_S = 30.0


def _is_permanent(exc: BackendError) -> bool:
    """The backend understood and refused: repeating the same event gets the same answer."""
    return exc.status is not None and 400 <= exc.status < 500 and exc.status not in (408, 429)


class BackgroundEventSender:
    """Has the client's send_event, but only queues: it returns at once and never raises.

    Each event keeps the time it was detected, so one delivered after an outage still says when it happened.
    """

    def __init__(self, client, max_pending: int = MAX_PENDING, max_age_s: float = MAX_AGE_S,
                 max_backoff_s: float = MAX_BACKOFF_S, clock=time.monotonic):
        self._client = client
        self._max_age_s = max_age_s
        self._max_backoff_s = max_backoff_s
        self._clock = clock
        self._pending: deque[tuple[float, dict]] = deque(maxlen=max_pending)
        self._cond = Condition()
        self._closing = Event()
        self._thread = Thread(target=self._run, name="event-sender", daemon=True)
        self._warned = False

    def start(self) -> None:
        self._thread.start()

    def send_event(self, event: dict) -> None:
        with self._cond:
            if len(self._pending) == self._pending.maxlen:
                print("Warning: too many events waiting for the backend; dropping the oldest", file=sys.stderr)
            self._pending.append((self._clock(), event))
            self._cond.notify()

    def pending(self) -> int:
        with self._cond:
            return len(self._pending)

    def close(self, timeout_s: float = 3.0) -> None:
        """Gives what is queued a moment to leave. Shutting down must not wait for a backend that is down."""
        self._closing.set()
        with self._cond:
            self._cond.notify()
        if self._thread.is_alive():
            self._thread.join(timeout_s)

    def _discard(self, entry: tuple[float, dict]) -> None:
        with self._cond:
            # The queue may have dropped this entry for being full while it was being sent.
            if self._pending and self._pending[0] is entry:
                self._pending.popleft()

    def _run(self) -> None:
        backoff = 1.0
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._pending or self._closing.is_set())
                if not self._pending:
                    return
                entry = self._pending[0]
            if self._clock() - entry[0] > self._max_age_s:
                print("Warning: dropped an event the backend could not take in time", file=sys.stderr)
                self._discard(entry)
                continue
            try:
                self._client.send_event(entry[1])
            except BackendError as exc:
                if _is_permanent(exc):
                    print(f"Warning: the backend refused an event: {exc}", file=sys.stderr)
                    self._discard(entry)
                    continue
                failure = str(exc)
            except Exception as exc:  # Whatever went wrong, this thread is the only way out for every later event.
                failure = type(exc).__name__
            else:
                self._discard(entry)
                self._warned = False
                backoff = 1.0
                continue
            if not self._warned:
                print(f"Warning: could not send events, retrying in the background: {failure}", file=sys.stderr)
            self._warned = True
            if self._closing.wait(backoff):
                return
            backoff = min(backoff * 2, self._max_backoff_s)
