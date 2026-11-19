"""Watches whatever the camera manager publishes in MediaMTX, so each camera it adds gets detection without extra steps."""

import json
import re
import sys
from threading import Event
from urllib.request import Request, urlopen

from .camera_manager import CameraLimitError

DEFAULT_GESTOR_URL = "http://127.0.0.1:8095"
SYNC_INTERVAL_S = 15.0
# The list only names streams on this PC; anything else in it is not something this bridge should open.
_LOCAL_STREAM = re.compile(r"rtsp://(127\.0\.0\.1|localhost):\d{1,5}/[A-Za-z0-9_-]{1,64}")
_CAMERA_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def is_local_stream(source) -> bool:
    return isinstance(source, str) and _LOCAL_STREAM.fullmatch(source) is not None


def fetch_streams(base_url: str, api_key: str, timeout: float = 5.0) -> dict[str, str]:
    """camera_id -> local stream URL. Raises OSError or ValueError when the answer cannot be trusted as complete."""
    request = Request(f"{base_url.rstrip('/')}/api/camaras", headers={"X-API-Key": api_key})
    with urlopen(request, timeout=timeout) as response:
        cameras = json.load(response)["camaras"]
    streams = {}
    for camera in cameras:
        camera_id, url = camera["camara_id"], camera["rtsp_url"]
        if _CAMERA_ID.fullmatch(camera_id) and _LOCAL_STREAM.fullmatch(url):
            streams[camera_id] = url
    return streams


def sync_streams(manager, streams: dict[str, str]) -> None:
    """Only touches local streams: a camera added from the dashboard is not this bridge's to remove."""
    for camera in manager.all():
        if is_local_stream(camera.source) and camera.camera_id not in streams:
            manager.remove(camera.camera_id)
    for camera_id, url in streams.items():
        current = manager.get(camera_id)
        if current is None or current.source != url:
            manager.add(url, camera_id)


def gestor_worker(base_url: str, api_key: str, manager, stop: Event, interval_s: float = SYNC_INTERVAL_S) -> None:
    warned = None
    while not stop.is_set():
        try:
            sync_streams(manager, fetch_streams(base_url, api_key))
            warned = None
        except (OSError, ValueError, KeyError, TypeError, CameraLimitError) as exc:
            # Nothing is removed: the streams keep running in MediaMTX while the manager is only unreachable.
            if warned != type(exc):
                print(f"Warning: could not read the camera manager: {type(exc).__name__}", file=sys.stderr)
            warned = type(exc)
        stop.wait(interval_s)
