"""Host side of the Mac Bot Screen: what ``tools.bot_desktop.runtime`` dispatches to on darwin.

Answers ``display.status`` truthfully (supported / installed / running, with a precise reason a client can show
when something is missing), starts and stops the ``macos.server`` process, and gates take-control on whether a
person is physically using the Mac. Nothing here touches the Linux Xvnc path.

State files under ``<HERMES_HOME>/bot-desktop/`` (same names as Linux where they mean the same thing):
``launcher.pid`` (``"<pid> <create_time>"``), ``env`` (written by the server once it serves), ``rfb.sock``,
``fb-size``, ``thumb.jpg``, ``audit.log``, plus ``consent`` / ``error`` when a start was refused.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from tools.bot_desktop import runtime as _rt
from tools.bot_desktop.macos import idle as _idle

logger = logging.getLogger(__name__)

_PROBE_TTL_S = 5.0
# One session start can include the person's 30 s consent plus the first capture.
START_WAIT_S = 50.0
DEFAULT_APP = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"

_REFUSALS = {
    "denied": "The person at the Mac declined to share the screen.",
    "timeout": "Nobody answered the prompt on the Mac, so the screen was not shared.",
    "unavailable": "The Mac could not show its permission prompt (is anyone logged in at the Mac?), so the screen "
                   "was not shared.",
}


@dataclass(frozen=True)
class Probe:
    supported: bool
    installed: bool
    missing: list[str] = field(default_factory=list)
    reason: Optional[str] = None


def find_cua_driver() -> Optional[str]:
    try:
        from tools.computer_use.cua_backend_driver import resolve_cua_driver_cmd
        found = resolve_cua_driver_cmd()
    except Exception:
        found = None
    return found or shutil.which("cua-driver") or (DEFAULT_APP if os.access(DEFAULT_APP, os.X_OK) else None)


def cua_child_env() -> dict[str, str]:
    """cua-driver is a third-party binary: it never inherits provider secrets."""
    try:
        from tools.computer_use.cua_backend import sanitized_cua_driver_env
        return sanitized_cua_driver_env()
    except Exception:
        return {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}


def _permissions(command: str) -> Optional[dict[str, Any]]:
    """``cua-driver permissions status --json`` (answered by the daemon, so it names CuaDriver's own grants).
    None when the daemon is down or the output is not JSON. Tests replace this function."""
    try:
        proc = subprocess.run([command, "permissions", "status", "--json"], capture_output=True, text=True,
                              timeout=10, env=cua_child_env(), stdin=subprocess.DEVNULL)
        data = json.loads(proc.stdout or "")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _judge(command: Optional[str], perms: Optional[dict[str, Any]]) -> Probe:
    if command is None:
        return Probe(False, False, ["cua-driver"],
                     "Bot Screen on this Mac needs cua-driver. Install it with: hermes computer-use install")
    if perms is None or not isinstance(perms.get("accessibility"), bool) or not isinstance(perms.get("screen_recording"), bool):
        return Probe(False, True, [],
                     "cua-driver is installed but its background service is not answering, so its permissions "
                     "cannot be checked. Open CuaDriver once, or run: hermes computer-use permissions grant")
    lacking = [label for key, label in (("accessibility", "Accessibility"), ("screen_recording", "Screen Recording"))
               if perms.get(key) is not True]
    if lacking:
        return Probe(False, True, [],
                     f"{' and '.join(lacking)} {'is' if len(lacking) == 1 else 'are'} not allowed for CuaDriver. "
                     "Open System Settings, Privacy & Security, and turn CuaDriver on, or run: "
                     "hermes computer-use permissions grant")
    return Probe(True, True)


_cache: dict[str, Any] = {"at": 0.0, "probe": None}


def probe(*, force: bool = False) -> Probe:
    now = time.monotonic()
    cached = _cache["probe"]
    if cached is not None and not force and now - _cache["at"] < _PROBE_TTL_S:
        return cached
    command = find_cua_driver()
    result = _judge(command, _permissions(command) if command else None)
    _cache.update(at=now, probe=result)
    return result


def reset_probe_cache() -> None:
    _cache.update(at=0.0, probe=None)


# -- configuration ---------------------------------------------------------------------------------------


def _cfg() -> dict[str, Any]:
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly().get("bot_desktop") or {}
    return cfg if isinstance(cfg, dict) else {}


def present_within_seconds() -> float:
    try:
        return float(_cfg().get("mac_present_seconds", _idle.DEFAULT_PRESENT_WITHIN_S))
    except (TypeError, ValueError):
        return _idle.DEFAULT_PRESENT_WITHIN_S


def _server_env() -> dict[str, str]:
    from hermes_constants import get_hermes_home
    from tools.environments.local import served_profile_child_env
    env = served_profile_child_env(inherit_credentials=False)
    env["HOME"] = env.get("HERMES_REAL_HOME", env.get("HOME", ""))
    env["HERMES_HOME"] = str(get_hermes_home())
    cfg = _cfg()
    env["HERMES_BD_PROFILE"] = _rt._profile_name()
    if cfg.get("mac_max_edge"):
        env["HERMES_BD_MAC_MAX_EDGE"] = str(cfg["mac_max_edge"])
    if cfg.get("mac_fps"):
        env["HERMES_BD_MAC_FPS"] = str(cfg["mac_fps"])
    if cfg.get("mac_capture") in ("auto", "cua", "quartz"):
        env["HERMES_BD_MAC_CAPTURE"] = str(cfg["mac_capture"])
    if isinstance(cfg.get("mac_deny_apps"), list):
        env["HERMES_BD_MAC_DENY"] = json.dumps([str(x) for x in cfg["mac_deny_apps"]])
    if cfg.get("mac_viewer_grace_seconds"):
        env["HERMES_BD_MAC_GRACE_S"] = str(cfg["mac_viewer_grace_seconds"])
    return env


# -- state -----------------------------------------------------------------------------------------------


def is_running() -> bool:
    sd = _rt.state_dir()
    return _rt._launcher_pid() is not None and (sd / "env").exists() and (sd / "rfb.sock").exists()


def rfb_socket_path() -> Optional[Path]:
    return _rt.state_dir() / "rfb.sock" if is_running() else None


def _geometry(sd: Path) -> str:
    try:
        return (sd / "fb-size").read_text(encoding="utf-8").strip() or "auto"
    except OSError:
        return "auto"


def status(profile: Optional[str] = None) -> "_rt.DesktopStatus":
    p = probe()
    sd = _rt.state_dir()
    running = is_running()
    return _rt.DesktopStatus(
        profile=profile or _rt._profile_name(),
        supported=p.supported,
        installed=p.installed,
        missing=list(p.missing),
        running=running,
        pid=_rt._launcher_pid(),
        display="primary" if running else None,
        socket=str(sd / "rfb.sock") if running else None,
        geometry=_geometry(sd) if running else "auto",
        install_command=None if p.installed else "hermes computer-use install",
        browser=None,
        blocker=None if p.supported or running else p.reason,
    )


def _spawn_guard() -> None:
    """A real session shows a native prompt on the Mac, so a test process must never reach the spawn. Tests that
    exercise start() replace this function and fake the process."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("the Mac screen session is not started from a test process")


def start(*, wait_seconds: float = START_WAIT_S) -> "_rt.DesktopStatus":
    """Start the session (idempotent). Blocks while the person at the Mac answers the prompt; raises
    ``RuntimeError`` with a calm sentence when it is declined, unanswered, or capture is unavailable."""
    p = probe(force=True)
    if not p.supported:
        raise RuntimeError(p.reason or "Bot Screen is not available on this Mac")
    sd = _rt.state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    os.chmod(sd, 0o700)
    with _rt._flocked(sd / "start.lock"):
        if is_running():
            return status()
        if _rt._launcher_pid() is None:
            for name in ("launcher.pid", "env", "rfb.sock", "consent", "error"):
                (sd / name).unlink(missing_ok=True)
        _spawn_guard()
        repo_root = Path(__file__).resolve().parents[3]
        log = open(sd / "launcher.log", "wb")
        proc = subprocess.Popen([sys.executable, "-m", "tools.bot_desktop.macos.server"], cwd=str(repo_root),
                                env=_server_env(), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, close_fds=True)
        log.close()
        born = _rt._create_time(proc.pid)
        (sd / "launcher.pid").write_text(f"{proc.pid} {born if born is not None else 0}", encoding="utf-8")
        deadline = time.monotonic() + max(wait_seconds, START_WAIT_S)
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(_why_it_ended(sd))
            if (sd / "env").exists() and (sd / "rfb.sock").exists():
                _rt.touch_activity()
                return status()
            time.sleep(0.1)
        _rt._kill_group_then_wait(proc.pid, proc.pid)
        proc.wait()
        raise RuntimeError("The Mac did not start sharing in time. Nothing is being shared.")


def _why_it_ended(sd: Path) -> str:
    for name in ("launcher.pid",):
        (sd / name).unlink(missing_ok=True)
    consent = _read(sd / "consent")
    if consent:
        return _REFUSALS.get(consent, _REFUSALS["denied"])
    error = _read(sd / "error")
    if error:
        return f"The Mac could not capture its screen: {error}"
    return "The Mac screen session ended before it started. See launcher.log for details."


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def stop() -> bool:
    sd = _rt.state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    with _rt._flocked(sd / "start.lock"):
        pid = _rt._launcher_pid()
        if pid is None:
            for name in ("env", "rfb.sock", "fb-size", "thumb.jpg"):
                (sd / name).unlink(missing_ok=True)
            return False
        _rt._kill_group_then_wait(pid, pid, grace=5.0)
        for name in ("launcher.pid", "env", "activity", "rfb.sock", "fb-size", "thumb.jpg"):
            (sd / name).unlink(missing_ok=True)
        return True


# -- take control ----------------------------------------------------------------------------------------


def take_control_gate(confirmed: bool, source: Optional[Callable[[], Optional[float]]] = None) -> Optional[dict[str, Any]]:
    """None when take-control may proceed. Otherwise the calm refusal (``code: mac_user_present``) to relay
    to the phone, which retries with ``confirm_user_present: true`` to proceed anyway. Take-control is the one
    mode that moves the Mac's real cursor and focus, so a person using the Mac is told instead of fought."""
    if confirmed:
        return None
    idle = _idle.user_present(source or _idle.seconds_since_input, present_within_seconds())
    if idle is None:
        return None
    return {"code": "mac_user_present", "idle_seconds": round(idle, 1),
            "message": "Someone is using this Mac right now. Taking control would move their cursor and keyboard "
                       "focus. Confirm to take control anyway."}


def thumbnail_jpeg(max_age_s: float = 15.0) -> Optional[bytes]:
    """The server's latest masked frame as a JPEG, while a viewer keeps it fresh; None otherwise."""
    path = _rt.state_dir() / "thumb.jpg"
    try:
        if time.time() - path.stat().st_mtime > max_age_s:
            return None
        return path.read_bytes()
    except OSError:
        return None
