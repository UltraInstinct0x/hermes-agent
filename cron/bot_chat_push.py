"""Tell the Hermes Bot BFF that a cron job posted a Bot Chat notice, so the owner's phone can alert.

A ``notice`` row is written straight into the target profile's ``state.db``. Nothing else sees it
until a client reads history, so without this hook an iPhone never learns a routine finished. The
event is the same ``message.proactive`` the companion plugin sends; the BFF decides whether to
alert (notifications allowed, a device token, no Hermes surface in front of the owner).

Best effort and quiet: no intake secret (APNs not configured), BFF down or a non-loopback URL is a
debug log line, never an exception into delivery. The text is never logged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Mapping, Optional

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8791/bff/internal/push-event"
SECRET_HEADER = "x-hermes-push-secret"
SECRET_NAME = Path("runtime") / "bff" / "push-intake-secret"
TEXT_MAX = 600
TIMEOUT_SECONDS = 3.0


def intake_url(env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    return (env.get("HERMES_BOT_PUSH_URL") or "").strip() or DEFAULT_URL


def is_loopback_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")


def secret_candidates(home: Path, env: Optional[Mapping[str, str]] = None) -> list[Path]:
    """The BFF keeps its intake secret under the ONE profile it serves; any profile's cron may notify it."""
    env = os.environ if env is None else env
    explicit = (env.get("HERMES_BOT_PUSH_SECRET_FILE") or "").strip()
    home = Path(home)
    root = home.parent.parent if home.parent.name == "profiles" else home
    paths = [Path(explicit)] if explicit else []
    paths += [home / SECRET_NAME, root / SECRET_NAME]
    paths += sorted((root / "profiles").glob(f"*/{SECRET_NAME.as_posix()}"))
    return paths


def read_secret(home: Path, env: Optional[Mapping[str, str]] = None) -> str:
    for path in secret_candidates(home, env):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if len(value) >= 32:
            return value
    return ""


def build_event(profile: str, message_id: str, text: str) -> dict:
    # The BFF accepts ids matching [A-Za-z0-9_.:-]{1,80} only (push-planner.mjs normalizeEvent).
    message_id = re.sub(r"[^A-Za-z0-9_.:-]", "", message_id)[:80]
    return {"type": "message.proactive", "profile": profile, "messageId": message_id,
            "text": " ".join(str(text).split())[:TEXT_MAX]}


def notify_phone(home: Path, message_id: str, text: str, *, env: Optional[Mapping[str, str]] = None,
                 opener: Optional[Callable] = None) -> tuple[str, str]:
    """(status, detail): sent | skipped | failed. Never raises; ``detail`` holds no text and no secret."""
    try:
        url = intake_url(env)
        if not is_loopback_url(url):
            return "skipped", "intake url is not loopback"
        secret = read_secret(home, env)
        if not secret:
            return "skipped", "no intake secret (push not configured)"
        event = build_event(Path(home).name, message_id, text)
        request = urllib.request.Request(url, data=json.dumps(event).encode("utf-8"), method="POST",
                                         headers={"content-type": "application/json", SECRET_HEADER: secret})
        with (opener or urllib.request.urlopen)(request, timeout=TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 200))
        return ("sent", "") if 200 <= status < 300 else ("failed", f"http {status}")
    except urllib.error.HTTPError as exc:
        return "failed", f"http {exc.code}"
    except Exception as exc:  # best effort by contract
        return "failed", type(exc).__name__
