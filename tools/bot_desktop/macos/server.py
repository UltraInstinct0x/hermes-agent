"""The long-lived Mac Bot Screen process: ``python -m tools.bot_desktop.macos.server``.

Spawned by ``macos.runtime.start`` the way the Linux launcher spawns Xvnc, so it survives a gateway restart and
the bridge dials its Unix socket like any other RFB server. One run is one SESSION:

1. ask the person at the Mac (``consent``, unless ``bot_desktop.mac_consent`` is allow or deny); anything but
   Allow ends the run with ``EXIT_DENIED``;
2. grab a first frame and the window list, refusing to start when either fails (fail closed);
3. serve RFB on ``<home>/bot-desktop/rfb.sock``, capturing only while a viewer is attached;
4. end when stopped, or when no viewer has been attached for ``viewer_grace_s``, so the next session asks again.

Every session leaves audit lines in ``<home>/bot-desktop/audit.log`` (JSON, 0600): the consent outcome, each
viewer attach and detach, each control hand-over seen on the lease, and the end of the session. Frames never
contain deny-listed windows (``denylist``), and no frame is published while the window list is unavailable for
more than ``MASK_STALE_S`` (a blank frame is sent instead).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image

from tools.bot_desktop.macos import capture, consent as _consent, denylist
from tools.bot_desktop.macos.cua import CuaError, CuaPort
from tools.bot_desktop.macos.input import InputDriver
from tools.bot_desktop.macos.rfb_server import RfbServer

logger = logging.getLogger("tools.bot_desktop.macos.server")

EXIT_OK, EXIT_DENIED, EXIT_NO_CAPTURE = 0, 3, 4
MASK_REFRESH_S = 1.0
MASK_STALE_S = 5.0
THUMB_EVERY_S = 2.0
REMINDER_EVERY_S = 300.0
_MAX_FAILURES = 20


@dataclass
class Settings:
    max_edge: int = capture.DEFAULT_MAX_EDGE
    max_fps: float = 10.0
    capture_mode: str = "auto"          # auto | cua | quartz
    deny_extra: tuple[str, ...] = ()
    viewer_grace_s: float = 120.0
    consent_timeout_s: int = _consent.TIMEOUT_S
    consent_mode: str = _consent.DEFAULT_MODE  # ask | allow | deny, see consent.MODES
    profile: str = "default"

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "Settings":
        def num(key: str, default: float) -> float:
            try:
                return float(env.get(key, default))
            except ValueError:
                return default
        extra: tuple[str, ...] = ()
        try:
            parsed = json.loads(env.get("HERMES_BD_MAC_DENY", "[]"))
            extra = tuple(str(x) for x in parsed) if isinstance(parsed, list) else ()
        except ValueError:
            pass
        return cls(max_edge=int(num("HERMES_BD_MAC_MAX_EDGE", capture.DEFAULT_MAX_EDGE)),
                   max_fps=max(0.5, num("HERMES_BD_MAC_FPS", 10.0)),
                   capture_mode=env.get("HERMES_BD_MAC_CAPTURE", "auto"),
                   deny_extra=extra, viewer_grace_s=num("HERMES_BD_MAC_GRACE_S", 120.0),
                   profile=env.get("HERMES_BD_PROFILE", "default"),
                   consent_mode=_consent.normalize_mode(env.get("HERMES_BD_MAC_CONSENT")))


class Audit:
    """Append-only JSON lines, private to the owner."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock

    def line(self, event: str, **fields: Any) -> None:
        record = {"ts": round(self._clock(), 3), "event": event, **fields}
        logger.info("bot screen audit: %s", record)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            logger.warning("could not write the Bot Screen audit line", exc_info=True)


def default_lease_holder() -> tuple[bool, Optional[str]]:
    from tools.bot_desktop import lease
    current = lease.get()
    return current.holder == lease.HUMAN, current.label


@dataclass
class Session:
    state_dir: Path
    cua: CuaPort
    settings: Settings
    consent_fn: Callable[..., _consent.Consent] = _consent.request
    notify_fn: Callable[[str], None] = _consent.notify
    lease_fn: Callable[[], tuple[bool, Optional[str]]] = default_lease_holder
    source: Optional[capture.FrameSource] = None
    clock: Callable[[], float] = time.monotonic
    audit: Optional[Audit] = None
    frames: int = 0
    inputs: int = 0
    _stop: threading.Event = field(default_factory=threading.Event)
    _active: threading.Event = field(default_factory=threading.Event)
    _loop: Optional[asyncio.AbstractEventLoop] = None
    _native_per_fb: tuple[float, float] = (1.0, 1.0)
    _viewers: int = 0
    _last_viewer_at: float = 0.0
    _holder: bool = False
    _holder_checked: float = -1.0

    def __post_init__(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.audit is None:
            self.audit = Audit(self.state_dir / "audit.log")
        self.deny = denylist.deny_list(self.settings.deny_extra)

    # -- public ------------------------------------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: None)

    def run(self) -> int:
        self._clear_markers()
        summary = (f"A device connected through Hermes wants to view and, if you allow it later, control this "
                   f"Mac's screen for the bot \"{self.settings.profile}\".\n\nThe whole main display is shared; "
                   f"password managers, Messages and Keychain Access are hidden. Deny stops the request.")
        started = self.clock()
        self.audit.line("session_requested", profile=self.settings.profile)
        outcome = self._decide_consent(summary)
        if not outcome.allowed:
            (self.state_dir / "consent").write_text(outcome.reason, encoding="utf-8")
            return EXIT_DENIED
        try:
            first, masks = self._first_frame()
        except (CuaError, OSError, RuntimeError) as exc:
            (self.state_dir / "error").write_text(str(exc)[:300], encoding="utf-8")
            self.audit.line("session_refused", reason="capture_unavailable")
            return EXIT_NO_CAPTURE
        asyncio.run(self._serve(first, masks))
        self.audit.line("session_end", seconds=round(self.clock() - started, 1), frames=self.frames, inputs=self.inputs)
        return EXIT_OK

    # -- internals ---------------------------------------------------------------------------------------

    def _decide_consent(self, summary: str) -> _consent.Consent:
        """``ask`` shows the dialog; ``allow`` and ``deny`` decide without one. Every other guard (auth at the
        bridge, deny list, audit, banners, take-control confirmation, idle end) is independent of this step."""
        mode = _consent.normalize_mode(self.settings.consent_mode)
        if mode == "allow":
            outcome = _consent.Consent(True, "auto_allow")
        elif mode == "deny":
            outcome = _consent.Consent(False, "policy")
        else:
            outcome = self.consent_fn(summary, timeout_s=self.settings.consent_timeout_s)
        self.audit.line("consent", allowed=outcome.allowed, reason=outcome.reason, mode=mode)
        return outcome

    def _clear_markers(self) -> None:
        for name in ("consent", "error", "env", "fb-size"):
            (self.state_dir / name).unlink(missing_ok=True)

    def _first_frame(self) -> tuple[capture.Frame, list]:
        probe = self.cua.desktop_png() if self.source is None else None
        if self.source is None:
            scale = float((probe[1] or {}).get("scale_factor") or 2.0)
            self.source = capture.pick_source(self.settings.capture_mode, self.cua, self.settings.max_edge, scale_factor=scale)
        frame = self.source.grab()
        masks = denylist.masked_rects(self.cua.list_windows(), self.deny, frame.scale_factor)
        return frame, masks

    def _apply(self, frame: capture.Frame, masks: list) -> Image.Image:
        image = frame.image.copy() if masks else frame.image
        capture.apply_masks(image, masks, frame.native_size)
        self._native_per_fb = frame.native_per_fb
        return image

    def _holder_now(self) -> bool:
        now = self.clock()
        if now - self._holder_checked >= 0.25:
            self._holder_checked = now
            try:
                holder, label = self.lease_fn()
            except Exception:
                holder, label = False, None
            if holder != self._holder:
                self._holder = holder
                self.audit.line("control_human" if holder else "control_agent", label=label or "")
                if holder:
                    self.notify_fn("A remote viewer now controls this Mac. The bot is paused.")
        return self._holder

    def _on_input(self, event: tuple) -> None:
        self.inputs += 1
        self.driver.submit(event)

    def _on_viewers(self, count: int) -> None:
        if count > self._viewers:
            self.audit.line("viewer_attached", viewers=count)
            self.notify_fn("Your screen is being shown in Hermes. Stop it from the Bot Screen pane.")
            self._active.set()
        elif count < self._viewers:
            self.audit.line("viewer_detached", viewers=count)
        self._viewers = count
        self._last_viewer_at = self.clock()
        if count == 0:
            self._active.clear()

    async def _serve(self, first: capture.Frame, masks: list) -> None:
        self._loop = asyncio.get_running_loop()
        self.driver = InputDriver(self.cua, lambda: self._native_per_fb)
        server = RfbServer(self.state_dir / "rfb.sock", on_input=self._on_input, on_viewers=self._on_viewers,
                           input_allowed=self._holder_now)
        server.frame = self._apply(first, masks)
        await server.start()
        self.driver.start()
        capture_thread = threading.Thread(target=self._capture_loop, args=(server, masks), daemon=True,
                                          name="mac-bot-screen-capture")
        capture_thread.start()
        w, h = server.size
        (self.state_dir / "fb-size").write_text(f"{w}x{h}", encoding="utf-8")
        (self.state_dir / "env").write_text(f"HERMES_BD_BACKEND=macos\nHERMES_BD_CAPTURE={self.source.name}\n", encoding="utf-8")
        self._last_viewer_at = self.clock()
        self.audit.line("serving", capture=self.source.name, size=f"{w}x{h}")
        try:
            while not self._stop.is_set():
                await asyncio.sleep(0.2)
                if self._viewers == 0 and self.clock() - self._last_viewer_at > self.settings.viewer_grace_s:
                    self.audit.line("idle_end", grace_s=self.settings.viewer_grace_s)
                    break
        finally:
            self._stop.set()
            self.driver.stop()
            await server.stop()
            capture_thread.join(timeout=3)
            for name in ("env", "fb-size", "thumb.jpg"):
                (self.state_dir / name).unlink(missing_ok=True)

    def _capture_loop(self, server: RfbServer, masks: list) -> None:
        interval = 1.0 / self.settings.max_fps
        masks_at = self.clock()
        mask_ok_at = masks_at
        failures = 0
        thumb_at = 0.0
        while not self._stop.is_set():
            if not self._active.wait(timeout=0.25):
                continue
            began = self.clock()
            try:
                frame = self.source.grab()
                if began - masks_at >= MASK_REFRESH_S:
                    masks_at = began
                    try:
                        masks = denylist.masked_rects(self.cua.list_windows(), self.deny, frame.scale_factor)
                        mask_ok_at = began
                    except CuaError:
                        logger.warning("window list unavailable; keeping the previous masks")
                if began - mask_ok_at > MASK_STALE_S:
                    image = Image.new("RGB", frame.image.size, (0, 0, 0))  # fail closed
                    self._native_per_fb = frame.native_per_fb
                else:
                    image = self._apply(frame, masks)
                server.publish(image)
                self.frames += 1
                failures = 0
                if began - thumb_at >= THUMB_EVERY_S:
                    thumb_at = began
                    self._save_thumbnail(image)
            except (CuaError, OSError, RuntimeError) as exc:
                failures += 1
                logger.warning("capture failed (%d): %s", failures, exc)
                if failures >= _MAX_FAILURES:
                    self.audit.line("capture_failed", failures=failures)
                    self.request_stop()
                    return
                self._stop.wait(1.0)
                continue
            spare = interval - (self.clock() - began)
            if spare > 0:
                self._stop.wait(spare)

    def _save_thumbnail(self, image: Image.Image) -> None:
        try:
            thumb = image.copy()
            thumb.thumbnail((960, 600))
            tmp = self.state_dir / "thumb.jpg.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                thumb.save(fh, "JPEG", quality=70)
            os.replace(tmp, self.state_dir / "thumb.jpg")
        except OSError:
            pass


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    from hermes_constants import get_hermes_home
    from tools.bot_desktop.macos.runtime import find_cua_driver, cua_child_env
    command = find_cua_driver()
    if command is None:
        print("cua-driver is not installed", file=sys.stderr)
        return EXIT_NO_CAPTURE
    state_dir = get_hermes_home() / "bot-desktop"
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    from tools.bot_desktop.macos.cua import CuaSession
    cua = CuaSession(command, env=cua_child_env(), scratch_dir=state_dir, log_path=state_dir / "cua-mcp.log")
    session = Session(state_dir, cua, Settings.from_env(dict(os.environ)))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: session.request_stop())
    try:
        return session.run()
    finally:
        cua.close()


if __name__ == "__main__":
    sys.exit(main())
