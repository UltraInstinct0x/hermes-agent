"""app_action's desktop bridge: the ``app.action`` server request that makes the renderer run one
named UI action against its real stores and answer with JSON.

Same probe ladder as ``drive_preview`` (``_preview_action_request``): until a session's client has
answered once it is held to a short deadline, an unanswered probe marks the bridge unavailable behind
a cooldown, and once answered the full deadline applies. The verdict lives on the session record, so a
new session probes again. Lives here, not in ``server.py``, so the session-sidecar module does not grow.
"""
from __future__ import annotations

import json
import threading
import time

APP_ACTION_TIMEOUT_S = 45
# Until a session's client has proven it answers app.action at all, hold it to a
# deadline a working renderer cannot miss (same ladder as preview.act / tour).
APP_ACTION_PROBE_TIMEOUT_S = 10
APP_ACTION_REPROBE_COOLDOWN_S = 30

BRIDGE_UNAVAILABLE = json.dumps({
    "success": False,
    "ok": False,
    "action": "",
    "detail": ("No Hermes app window answered the action request. The app.action bridge is "
               "served by the desktop app's renderer, which updates separately from this "
               "backend, so an app build older than the tool has nothing listening. Update "
               "the Hermes app, open this chat in it, and try again in this session after a "
               "short cooldown.")})

# One in-flight cooldown-expiry reprobe per session: concurrent callers fail fast.
_reprobe: dict[str, object] = {}
_reprobe_lock = threading.Lock()


def app_action_request(sid: str, payload: dict) -> str:
    """Bridge the app_action callback onto an ``app.action`` server request without paying for a
    client that cannot answer: first action per session gets the short probe deadline; unanswered ->
    bridge marked unavailable with a cooldown-gated reprobe; once answered, the full deadline."""
    from tui_gateway.server import _ask, _sessions, _sessions_lock

    with _sessions_lock:
        session = _sessions.get(sid)
        if session is None:
            session = {}
        state = session.get("app_action_bridge")
        now = time.monotonic()
        if state == "unanswered" and now < session.get("app_action_bridge_retry_at", 0):
            return BRIDGE_UNAVAILABLE
        if state == "unanswered":
            with _reprobe_lock:
                if _reprobe.get(sid) is not None:
                    return BRIDGE_UNAVAILABLE
                _reprobe[sid] = object()
                session["app_action_bridge_retry_at"] = now + APP_ACTION_REPROBE_COOLDOWN_S
    try:
        answer = _ask("app.action", sid, dict(payload),
                      timeout=APP_ACTION_TIMEOUT_S if state == "answered" else APP_ACTION_PROBE_TIMEOUT_S)
    finally:
        if state == "unanswered":
            with _reprobe_lock:
                _reprobe.pop(sid, None)
    with _sessions_lock:
        if answer:
            session["app_action_bridge"] = "answered"
        elif session.get("app_action_bridge") != "answered":
            session["app_action_bridge"] = "unanswered"
            session["app_action_bridge_retry_at"] = time.monotonic() + APP_ACTION_REPROBE_COOLDOWN_S
    return answer or BRIDGE_UNAVAILABLE
