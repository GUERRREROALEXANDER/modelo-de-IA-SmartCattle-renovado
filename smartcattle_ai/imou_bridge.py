"""Keeps open every Imou camera the accounts connected from the dashboard, through Imou's cloud.

A camera at a farm has a LAN address no server on the internet can reach. An Imou camera also answers by its serial
number through Imou's own cloud, wherever this service runs. The backend lists those cameras (the dashboard saved
them as "imou:<serial>"); this bridge opens the ones that are missing and closes the ones their owner removed.
Nobody types an address, and nothing has to run at the farm.
"""

import os
import re
import sys
from threading import Event

from .backend_client import BackendError
from .camera_manager import CameraLimitError
from .imou import DEFAULT_API_URL, ImouClient, ImouUrlProvider

SYNC_INTERVAL_S = 20.0
IMOU_PREFIX = "imou:"
_SERIAL = re.compile(r"[A-Z0-9]{8,32}")
_CAMERA_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_PROFILES = ("HD", "SD")


def imou_cloud_configured(env=os.environ) -> bool:
    """The developer account's two values; unlike the single camera of .env, no device id is needed here."""
    return bool(env.get("IMOU_APP_ID", "").strip() and env.get("IMOU_APP_SECRET", "").strip())


def imou_client(env=os.environ) -> ImouClient:
    return ImouClient(env["IMOU_APP_ID"].strip(), env["IMOU_APP_SECRET"].strip(),
                      env.get("IMOU_API_URL", "").strip() or DEFAULT_API_URL)


def imou_profile(env=os.environ) -> str:
    profile = env.get("IMOU_PROFILE", "").strip().upper() or "SD"
    return profile if profile in _PROFILES else "SD"


def wanted_cameras(items) -> dict[str, str]:
    """camera_id -> serial, from the backend's list. An entry that is not an Imou camera is left out."""
    wanted = {}
    for item in items:
        camera_id, source = item.get("camera_id"), item.get("source")
        if not isinstance(camera_id, str) or not isinstance(source, str) or not source.startswith(IMOU_PREFIX):
            continue
        serial = source.removeprefix(IMOU_PREFIX)
        if _CAMERA_ID.fullmatch(camera_id) and _SERIAL.fullmatch(serial):
            wanted[camera_id] = serial
    return wanted


def is_cloud_source(source) -> bool:
    return isinstance(source, ImouUrlProvider)


def sync_cloud_cameras(manager, wanted: dict[str, str], provider_for) -> list[str]:
    """Only touches Imou cloud cameras: one added by address or pushed from a phone is not this bridge's to remove.

    Returns the cameras that did not fit; the rest are still opened.
    """
    for camera in manager.all():
        if is_cloud_source(camera.source) and camera.camera_id not in wanted:
            manager.remove(camera.camera_id)
    left_out = []
    for camera_id, serial in wanted.items():
        current = manager.get(camera_id)
        if current is not None and is_cloud_source(current.source) and current.source.device_id == serial:
            continue
        try:
            manager.add(provider_for(serial), camera_id)
        except CameraLimitError:
            left_out.append(camera_id)
    return left_out


def imou_cloud_worker(client, manager, stop: Event, env=os.environ, interval_s: float = SYNC_INTERVAL_S) -> None:
    imou = imou_client(env)
    profile = imou_profile(env)
    warned = None
    while not stop.is_set():
        try:
            left_out = sync_cloud_cameras(manager, wanted_cameras(client.cloud_cameras()),
                                          lambda serial: ImouUrlProvider(imou, serial, profile))
            problem = f"no room for {len(left_out)} Imou camera(s); raise SMARTCATTLE_MAX_CAMERAS" if left_out else None
        except (BackendError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # Nothing is closed: the cameras keep running while the backend is only unreachable.
            problem = f"could not read the Imou cameras from the backend: {type(exc).__name__}"
        if problem and problem != warned:
            print(f"Warning: {problem}", file=sys.stderr, flush=True)
        warned = problem
        stop.wait(interval_s)
