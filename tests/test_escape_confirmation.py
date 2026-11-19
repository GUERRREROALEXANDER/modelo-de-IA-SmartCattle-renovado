"""An animal must stay outside for a moment before anyone is told: one wrong box should not reach a phone."""

import pytest

from smartcattle_ai.backend_client import EventThrottle
from smartcattle_ai.pipeline import EventPublisher

ZONE = (0.0, 0.0, 0.5, 0.5)
WIDTH = HEIGHT = 100


class Client:
    def __init__(self):
        self.sent = []

    def send_event(self, event):
        self.sent.append(event)


def cow(outside=True, track_id=1):
    y = 90 if outside else 40
    item = {"class": "cow", "confidence": 0.9, "bbox": [10, y - 10, 20, y]}
    return item if track_id is None else {**item, "track_id": track_id}


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def publisher(client):
    return EventPublisher(client, "camera-01", ZONE, EventThrottle(0), confirm_after_s=3.0)


def feed(publisher, moments, **options):
    for now in moments:
        publisher.handle([cow(**options)], WIDTH, HEIGHT, now)


def test_nothing_is_sent_just_under_the_confirmation_time(publisher, client):
    feed(publisher, [0.0, 1.0, 2.99])
    assert client.sent == []


def test_it_is_sent_once_at_the_confirmation_time(publisher, client):
    feed(publisher, [0.0, 1.5, 3.0, 4.0, 5.0])
    assert len(client.sent) == 1


def test_a_single_wrong_box_is_never_sent(publisher, client):
    feed(publisher, [0.0])
    publisher.handle([cow(outside=False)], WIDTH, HEIGHT, 1.0)
    feed(publisher, [2.0, 4.9])
    assert client.sent == []


def test_a_box_that_flickers_for_a_frame_keeps_counting(publisher, client):
    feed(publisher, [0.0, 2.0])
    publisher.handle([], WIDTH, HEIGHT, 2.5)
    feed(publisher, [3.0])
    assert len(client.sent) == 1


@pytest.mark.parametrize(("gap", "sent"), [(4.9, 1), (5.1, 0)])
def test_an_animal_unseen_for_longer_than_the_grace_starts_counting_again(publisher, client, gap, sent):
    feed(publisher, [0.0, gap])
    assert len(client.sent) == sent


def test_each_animal_counts_on_its_own(publisher, client):
    feed(publisher, [0.0, 2.0], track_id=1)
    feed(publisher, [2.0, 4.0], track_id=2)
    assert client.sent == []
    feed(publisher, [5.0], track_id=2)
    assert len(client.sent) == 1


def test_it_can_escape_again_after_returning(publisher, client):
    feed(publisher, [0.0, 3.0])
    publisher.handle([cow(outside=False)], WIDTH, HEIGHT, 4.0)
    feed(publisher, [5.0, 7.9])
    assert len(client.sent) == 1
    feed(publisher, [8.0])
    assert len(client.sent) == 2


def test_without_tracking_it_is_confirmed_across_detections(publisher, client):
    feed(publisher, [0.0, 2.0], track_id=None)
    assert client.sent == []
    feed(publisher, [3.0], track_id=None)
    assert len(client.sent) == 1


def test_without_tracking_a_cow_inside_does_not_reset_one_outside(publisher, client):
    for now in (0.0, 1.5, 3.0):
        publisher.handle([cow(outside=False, track_id=None), cow(track_id=None)], WIDTH, HEIGHT, now)
    assert len(client.sent) == 1


def test_a_person_never_starts_the_count(publisher, client):
    for now in (0.0, 3.0, 6.0):
        publisher.handle([{**cow(), "class": "person"}], WIDTH, HEIGHT, now)
    assert client.sent == []


def test_with_no_confirmation_time_the_first_frame_is_enough(client):
    publisher = EventPublisher(client, "camera-01", ZONE, EventThrottle(0))
    feed(publisher, [0.0])
    assert len(client.sent) == 1


def test_a_long_outside_stretch_does_not_grow_memory(publisher):
    for now in range(1000):
        publisher.handle([cow(track_id=now)], WIDTH, HEIGHT, float(now))
    assert len(publisher._outside) < 10
