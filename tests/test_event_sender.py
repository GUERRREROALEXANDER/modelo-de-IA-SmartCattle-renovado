import time
from threading import Event

import pytest

from smartcattle_ai.backend_client import BackendError
from smartcattle_ai.event_sender import BackgroundEventSender


def wait_until(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class Client:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.sent = []
        self.attempts = 0

    def send_event(self, event):
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(event)


@pytest.fixture
def sender_for():
    created = []

    def build(client, **options):
        sender = BackgroundEventSender(client, max_backoff_s=0.05, **options)
        created.append(sender)
        return sender

    yield build
    for sender in created:
        sender.close(timeout_s=1)


def test_queuing_does_not_wait_for_a_backend_that_hangs(sender_for):
    release = Event()

    class Hanging:
        def send_event(self, _event):
            release.wait(5)

    sender = sender_for(Hanging())
    sender.start()
    started = time.monotonic()
    for number in range(50):
        sender.send_event({"n": number})
    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 0.1


def test_events_arrive_in_the_order_they_were_detected(sender_for):
    client = Client()
    sender = sender_for(client)
    sender.start()
    for number in range(5):
        sender.send_event({"n": number})
    assert wait_until(lambda: len(client.sent) == 5)
    assert [event["n"] for event in client.sent] == [0, 1, 2, 3, 4]


@pytest.mark.parametrize("failure", [BackendError("down"), BackendError("HTTP 500", 503), BackendError("slow", 429),
                                     BackendError("timeout", 408), RuntimeError("unexpected")])
def test_a_passing_failure_is_retried_until_the_event_arrives(sender_for, failure):
    client = Client([failure, failure])
    sender = sender_for(client)
    sender.start()
    sender.send_event({"n": 1})
    assert wait_until(lambda: client.sent == [{"n": 1}])
    assert client.attempts == 3


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_an_event_the_backend_refuses_is_not_repeated_and_does_not_block_the_next(sender_for, status):
    client = Client([BackendError("refused", status)])
    sender = sender_for(client)
    sender.start()
    sender.send_event({"n": 1})
    sender.send_event({"n": 2})
    assert wait_until(lambda: client.sent == [{"n": 2}])
    assert client.attempts == 2


@pytest.mark.parametrize(("age", "delivered"), [(100.0, [{"n": 1}]), (100.1, [])])
def test_an_event_too_old_to_act_on_is_dropped(sender_for, age, delivered):
    now = [0.0]
    client = Client()
    sender = sender_for(client, max_age_s=100.0, clock=lambda: now[0])
    sender.send_event({"n": 1})
    now[0] = age
    sender.start()
    assert wait_until(lambda: sender.pending() == 0)
    assert client.sent == delivered


def test_a_full_queue_keeps_the_newest_events(sender_for):
    client = Client()
    sender = sender_for(client, max_pending=2)
    for number in range(4):
        sender.send_event({"n": number})
    sender.start()
    assert wait_until(lambda: len(client.sent) == 2)
    assert [event["n"] for event in client.sent] == [2, 3]


def test_closing_delivers_what_is_queued(sender_for):
    client = Client()
    sender = sender_for(client)
    sender.start()
    sender.send_event({"n": 1})
    sender.close()
    assert client.sent == [{"n": 1}]


def test_closing_does_not_wait_for_a_backend_that_is_down(sender_for):
    client = Client([BackendError("down")] * 100)
    sender = sender_for(client)
    sender.start()
    sender.send_event({"n": 1})
    assert wait_until(lambda: client.attempts >= 1)
    started = time.monotonic()
    sender.close(timeout_s=3)
    assert time.monotonic() - started < 1
