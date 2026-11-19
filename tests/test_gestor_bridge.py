import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from smartcattle_ai.camera_manager import CameraLimitError
from smartcattle_ai.gestor_bridge import fetch_streams, gestor_worker, is_local_stream, sync_streams

LIVE = "rtsp://127.0.0.1:8554/live"
CORRAL = "rtsp://127.0.0.1:8554/corral"


class Manager:
    def __init__(self, cameras=None, limit=None):
        self.cameras = dict(cameras or {})
        self.limit = limit
        self.calls = []

    def all(self):
        return [SimpleNamespace(camera_id=key, source=value) for key, value in self.cameras.items()]

    def get(self, camera_id):
        source = self.cameras.get(camera_id)
        return None if source is None else SimpleNamespace(camera_id=camera_id, source=source)

    def add(self, source, camera_id):
        if self.limit is not None and len(self.cameras) >= self.limit:
            raise CameraLimitError("full")
        self.calls.append(("add", camera_id))
        self.cameras[camera_id] = source

    def remove(self, camera_id):
        self.calls.append(("remove", camera_id))
        return self.cameras.pop(camera_id, None) is not None


@pytest.fixture
def gestor():
    answer = {"status": 200, "body": {"camaras": []}, "keys": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            answer["keys"].append(self.headers.get("X-API-Key"))
            body = json.dumps(answer["body"]).encode() if not isinstance(answer["body"], bytes) else answer["body"]
            self.send_response(answer["status"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_address[1]}", answer=answer)
    server.shutdown()
    server.server_close()


def camera(camera_id, url):
    return {"camara_id": camera_id, "rtsp_url": url, "camara": "rtsp://***@10.0.0.1/a", "estado": "transmitiendo"}


@pytest.mark.parametrize(("source", "local"), [
    (LIVE, True), ("rtsp://localhost:8554/cam-1", True), ("rtsp://192.168.1.50:554/live", False),
    ("rtsp://user:pass@127.0.0.1:8554/live", False), ("0", False), ("pushed", False), (lambda: LIVE, False),
])
def test_only_a_stream_on_this_pc_counts_as_local(source, local):
    assert is_local_stream(source) is local


def test_a_new_stream_starts_detection_under_the_managers_id():
    manager = Manager()
    sync_streams(manager, {"live": LIVE})
    assert manager.cameras == {"live": LIVE}


def test_a_stream_already_running_is_not_restarted():
    manager = Manager({"live": LIVE})
    sync_streams(manager, {"live": LIVE})
    assert manager.calls == []


def test_a_stream_the_manager_no_longer_lists_stops_detection():
    manager = Manager({"live": LIVE, "corral": CORRAL})
    sync_streams(manager, {"live": LIVE})
    assert manager.cameras == {"live": LIVE}


def test_a_camera_added_from_the_dashboard_is_left_alone():
    dashboard = {"cam-1": "rtsp://user:pass@192.168.1.50/live", "cam-2": "http://192.168.1.51:8080/video", "cam-3": "0"}
    manager = Manager(dashboard)
    sync_streams(manager, {})
    assert manager.cameras == dashboard


def test_fetch_sends_the_key_and_reads_the_stream_of_each_camera(gestor):
    gestor.answer["body"] = {"camaras": [camera("live", LIVE), camera("Cam_2-b", "rtsp://localhost:8554/Cam_2-b")]}
    assert fetch_streams(gestor.url, "the-key") == {"live": LIVE, "Cam_2-b": "rtsp://localhost:8554/Cam_2-b"}
    assert gestor.answer["keys"] == ["the-key"]


@pytest.mark.parametrize("url", [
    "rtsp://192.168.1.50:554/live", "rtsp://user:pass@127.0.0.1:8554/live", "file:///C:/Windows/win.ini",
    "http://127.0.0.1:8554/live", "rtsp://127.0.0.1:8554/a/b", "rtsp://127.0.0.1.evil.example:8554/live",
])
def test_an_address_that_is_not_a_local_stream_is_never_opened(gestor, url):
    gestor.answer["body"] = {"camaras": [camera("live", url)]}
    assert fetch_streams(gestor.url, "k") == {}


@pytest.mark.parametrize(("status", "body"), [(401, {"detail": "no"}), (200, b"{broken"), (200, {"otra": []})])
def test_a_bad_answer_removes_nothing_and_the_worker_keeps_going(gestor, status, body):
    gestor.answer.update(status=status, body=body)
    manager = Manager({"live": LIVE})
    stop = Event()
    worker = Thread(target=gestor_worker, args=(gestor.url, "k", manager, stop, 0.01), daemon=True)
    worker.start()
    stop.wait(0.2)
    alive = worker.is_alive()
    stop.set()
    worker.join(2)
    assert alive
    assert manager.cameras == {"live": LIVE}


def test_an_unreachable_manager_removes_nothing():
    manager = Manager({"live": LIVE})
    stop = Event()
    worker = Thread(target=gestor_worker, args=("http://127.0.0.1:9", "k", manager, stop, 0.01), daemon=True)
    worker.start()
    stop.wait(0.2)
    stop.set()
    worker.join(7)
    assert manager.cameras == {"live": LIVE}


def test_the_camera_limit_does_not_stop_the_worker(gestor):
    gestor.answer["body"] = {"camaras": [camera("live", LIVE)]}
    manager = Manager(limit=0)
    stop = Event()
    worker = Thread(target=gestor_worker, args=(gestor.url, "k", manager, stop, 0.01), daemon=True)
    worker.start()
    stop.wait(0.2)
    alive = worker.is_alive()
    stop.set()
    worker.join(2)
    assert alive
