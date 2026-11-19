import json
from threading import Event, Thread
from types import SimpleNamespace

from smartcattle_ai.backend_client import BackendError
from smartcattle_ai.camera_manager import CameraLimitError, CameraManager
from smartcattle_ai.imou import ImouUrlProvider
from smartcattle_ai.imou_bridge import (
    imou_cloud_configured, imou_cloud_worker, imou_profile, is_cloud_source, sync_cloud_cameras, wanted_cameras,
)

NORTH, SOUTH = "04797CEPSF59DA6", "1A2B3C4D5E6F7G8"


def provider(serial):
    return ImouUrlProvider(None, serial, "SD")


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
        if self.limit is not None and len(self.cameras) >= self.limit and camera_id not in self.cameras:
            raise CameraLimitError("full")
        self.calls.append(("add", camera_id))
        self.cameras[camera_id] = source

    def remove(self, camera_id):
        self.calls.append(("remove", camera_id))
        return self.cameras.pop(camera_id, None) is not None


class Backend:
    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error

    def cloud_cameras(self):
        if self.error is not None:
            raise self.error
        return self.items


def run_worker(backend, manager, env):
    stop = Event()
    worker = Thread(target=imou_cloud_worker, args=(backend, manager, stop, env, 0.01), daemon=True)
    worker.start()
    stop.wait(0.2)
    alive = worker.is_alive()
    stop.set()
    worker.join(2)
    return alive


ENV = {"IMOU_APP_ID": "app", "IMOU_APP_SECRET": "secret"}


def test_the_cloud_needs_the_accounts_two_values_and_no_device():
    assert imou_cloud_configured(ENV)
    assert not imou_cloud_configured({"IMOU_APP_ID": "app"})
    assert not imou_cloud_configured({"IMOU_APP_ID": " ", "IMOU_APP_SECRET": "secret"})


def test_an_unknown_profile_falls_back_to_the_light_one():
    assert imou_profile({"IMOU_PROFILE": "hd"}) == "HD"
    assert imou_profile({"IMOU_PROFILE": "4K"}) == "SD"
    assert imou_profile({}) == "SD"


def test_only_imou_cameras_with_a_serial_are_wanted():
    assert wanted_cameras([
        {"camera_id": "cam-1", "source": f"imou:{NORTH}"},
        {"camera_id": "cam-2", "source": "rtsp://10.0.0.1/live"},
        {"camera_id": "cam-3", "source": "imou:../../etc"},
        {"camera_id": "bad id", "source": f"imou:{SOUTH}"},
        {"camera_id": 7, "source": f"imou:{SOUTH}"},
        {"source": f"imou:{SOUTH}"},
    ]) == {"cam-1": NORTH}


def test_a_camera_connected_from_the_dashboard_opens_under_its_saved_id():
    manager = Manager()
    assert sync_cloud_cameras(manager, {"cam-1": NORTH}, provider) == []
    assert is_cloud_source(manager.cameras["cam-1"])
    assert manager.cameras["cam-1"].device_id == NORTH


def test_a_camera_already_open_is_not_restarted():
    manager = Manager({"cam-1": provider(NORTH)})
    sync_cloud_cameras(manager, {"cam-1": NORTH}, provider)
    assert manager.calls == []


def test_a_camera_its_owner_removed_is_closed():
    manager = Manager({"cam-1": provider(NORTH), "cam-2": provider(SOUTH)})
    sync_cloud_cameras(manager, {"cam-1": NORTH}, provider)
    assert list(manager.cameras) == ["cam-1"]


def test_cameras_of_other_kinds_are_left_alone():
    others = {"cam-a": "rtsp://user:pass@192.168.1.50/live", "cam-b": "pushed", "cam-c": "0"}
    manager = Manager(others)
    sync_cloud_cameras(manager, {}, provider)
    assert manager.cameras == others


def test_a_full_service_opens_what_fits_and_says_what_did_not():
    manager = Manager(limit=1)
    assert sync_cloud_cameras(manager, {"cam-1": NORTH, "cam-2": SOUTH}, provider) == ["cam-2"]
    assert list(manager.cameras) == ["cam-1"]


def test_the_worker_opens_what_the_backend_lists():
    manager = Manager()
    assert run_worker(Backend([{"camera_id": "cam-1", "source": f"imou:{NORTH}"}]), manager, ENV)
    assert manager.cameras["cam-1"].device_id == NORTH
    assert manager.calls == [("add", "cam-1")]


def test_an_unreachable_backend_closes_nothing_and_the_worker_keeps_going():
    manager = Manager({"cam-1": provider(NORTH)})
    assert run_worker(Backend(error=BackendError("down")), manager, ENV)
    assert list(manager.cameras) == ["cam-1"]


def test_the_saved_list_never_holds_a_cloud_camera(tmp_path):
    path = tmp_path / "cameras.json"
    started = lambda camera_id, source: SimpleNamespace(
        camera_id=camera_id, source=source, stop=Event(), stream=SimpleNamespace(stop=lambda: None, switch=lambda _: None),
        state=SimpleNamespace(set_camera_status=lambda _: None))
    manager = CameraManager(started, lambda camera_id: f"http://x/cameras/{camera_id}", path=path)
    manager.add("rtsp://10.0.0.1/live", "cam-ip")
    manager.add(provider(NORTH), "cam-imou")
    assert json.loads(path.read_text(encoding="utf-8")) == [{"camera_id": "cam-ip", "source": "rtsp://10.0.0.1/live"}]
