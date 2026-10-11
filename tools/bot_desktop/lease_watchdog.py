"""Hand the Bot Screen back from a human whose connection dropped and never returned.

A viewer socket that closes cleanly (the window was closed) hands control back at once (``web_routers/display``).
A DROPPED link (lid shut, Wi-Fi gone, 1006) deliberately keeps the human's exclusion at first: they may be
mid-login on that screen and the agent must not resume into it. Without a deadline a phone that vanished then
left ``holder: human`` for good until someone forced a release. This watchdog is that deadline: once the holder
of a *watched* lease (taken through a gateway viewer, ``Lease.watched``) has had no live viewer socket for
:data:`GRACE_S`, control goes back to the agent with reason :data:`HOLDER_DROPPED`. A reconnect inside the window
(same stable viewer id, ``lease.stable_viewer_id``) re-attaches and resets the clock.

Presence is the RFB bridge's own bookkeeping (``attach`` / ``detach`` around each socket); the check runs on the
gateway's existing lease-watcher tick. A lease taken from the CLI has no socket to watch and is never reclaimed here.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from hermes_constants import hermes_home_key

from . import lease as _lease

logger = logging.getLogger(__name__)

GRACE_S = 30.0
HOLDER_DROPPED = "holder_dropped"

_lock = threading.Lock()
_attached: dict[tuple[str, str], int] = {}  # (home key, viewer id) -> live viewer sockets
_absent_since: dict[str, float] = {}  # home key -> monotonic time the holder was first seen without a socket


def attach(profile_home: str, viewer_id: str) -> None:
    """A viewer socket for *viewer_id* opened on *profile_home*'s screen."""
    key = (hermes_home_key(profile_home), viewer_id)
    with _lock:
        _attached[key] = _attached.get(key, 0) + 1


def detach(profile_home: str, viewer_id: str) -> None:
    key = (hermes_home_key(profile_home), viewer_id)
    with _lock:
        left = _attached.get(key, 0) - 1
        if left > 0:
            _attached[key] = left
        else:
            _attached.pop(key, None)


def check(profile_home: str, *, now: Optional[float] = None, grace: float = GRACE_S) -> Optional[_lease.Lease]:
    """One watchdog pass for *profile_home*. Returns the lease when it handed control back, else None."""
    home_key = hermes_home_key(profile_home)
    clock = time.monotonic() if now is None else now
    lease = _lease.get(profile_home)
    with _lock:
        watched_human = lease.holder == _lease.HUMAN and lease.watched and lease.viewer_id is not None
        if not watched_human or _attached.get((home_key, lease.viewer_id), 0) > 0:
            _absent_since.pop(home_key, None)
            return None
        since = _absent_since.setdefault(home_key, clock)
    if clock - since < grace:
        return None
    # Release names the holder: a takeover that raced this pass is another viewer's and stays theirs.
    released = _lease.release(lease.viewer_id, profile_key=profile_home, reason=HOLDER_DROPPED)
    with _lock:
        _absent_since.pop(home_key, None)
    if released.holder == _lease.AGENT:
        logger.info("bot-desktop lease: holder %s dropped for %.0fs, control returned to the agent",
                    lease.label or lease.client_id or "viewer", clock - since)
        return released
    return None


def reset_for_tests() -> None:
    with _lock:
        _attached.clear()
        _absent_since.clear()
