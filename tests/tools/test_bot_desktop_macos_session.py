"""Mac Bot Screen session, end to end with fakes: consent gate, masked frames over RFB, lease-gated input, audit
lines, idle end, resize, and the same lease/watchdog/computer_use fence the Linux screen has."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from tools.bot_desktop import lease, lease_watchdog
from tools.bot_desktop.macos import consent, server
from tests.tools._macos_bot_screen_fakes import FakeCua, png_bytes
from tests.tools.test_bot_desktop_macos_rfb import _Viewer, _decode_zrle

ALLOW = lambda summary, timeout_s=30: consent.Consent(True, "allowed")  # noqa: E731
DENY = lambda summary, timeout_s=30: consent.Consent(False, "denied")  # noqa: E731
LEFT = 1


@pytest.fixture(autouse=True)
def _fresh_lease():
    lease._reset_for_tests()
    lease_watchdog.reset_for_tests()
    yield
    lease._reset_for_tests()
    lease_watchdog.reset_for_tests()


@pytest.fixture
def state():
    root = Path(tempfile.mkdtemp(prefix="mbs", dir="/tmp"))  # no-tmp: ok — AF_UNIX path length limit
    yield root
    shutil.rmtree(root, ignore_errors=True)


class _Running:
    """A Session on a thread. ``code`` is its exit code once it ended."""

    def __init__(self, state, cua, consent_fn=ALLOW, grace=60.0, notices=None, **kw):
        self.notices = notices if notices is not None else []
        self.session = server.Session(state, cua, server.Settings(viewer_grace_s=grace, max_fps=50, profile="skynet"),
                                      consent_fn=consent_fn, notify_fn=self.notices.append, **kw)
        self.code = None
        self.thread = threading.Thread(target=lambda: setattr(self, "code", self.session.run()), daemon=True)
        self.thread.start()

    def wait_serving(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline and self.code is None:
            if (self.session.state_dir / "env").exists() and (self.session.state_dir / "rfb.sock").exists():
                return
            time.sleep(0.02)
        raise AssertionError(f"not serving (exit code {self.code})")

    def finish(self, timeout=8):
        self.session.request_stop()
        self.thread.join(timeout)
        assert not self.thread.is_alive()
        return self.code

    def audit(self):
        path = self.session.state_dir / "audit.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _events(run):
    return [a["event"] for a in run.audit()]


# ── consent ───────────────────────────────────────────────────────────────────────────────────────────


def test_denied_consent_never_captures_never_listens_and_says_why(state):
    cua = FakeCua()
    run = _Running(state, cua, consent_fn=DENY)
    run.thread.join(5)
    assert run.code == server.EXIT_DENIED and cua.captures == 0
    assert (state / "consent").read_text() == "denied" and not (state / "rfb.sock").exists()
    assert _events(run) == ["session_requested", "consent"] and run.audit()[1]["allowed"] is False


def test_the_prompt_names_the_bot_and_what_is_hidden(state):
    seen = []
    run = _Running(state, FakeCua(), consent_fn=lambda summary, timeout_s: seen.append((summary, timeout_s)) or consent.Consent(False, "timeout"))
    run.thread.join(5)
    assert "skynet" in seen[0][0] and "password managers" in seen[0][0] and seen[0][1] == 30
    assert (state / "consent").read_text() == "timeout"


def test_no_capture_means_no_session_and_an_error_the_runtime_can_relay(state):
    cua = FakeCua()
    cua.fail_capture = True
    run = _Running(state, cua)
    run.thread.join(5)
    assert run.code == server.EXIT_NO_CAPTURE and "capture refused" in (state / "error").read_text()
    assert "session_refused" in _events(run)


def test_a_window_list_that_cannot_be_read_refuses_to_start(state):
    cua = FakeCua()
    cua.fail_windows = True  # without it the deny list cannot be applied, so nothing is shared
    run = _Running(state, cua)
    run.thread.join(5)
    assert run.code == server.EXIT_NO_CAPTURE and not (state / "rfb.sock").exists()


# ── serving ───────────────────────────────────────────────────────────────────────────────────────────


def test_a_viewer_sees_the_frame_with_deny_listed_windows_blacked_out(state):
    windows = [{"app_name": "Messages", "bounds": {"x": 0, "y": 0, "width": 50, "height": 50}, "is_on_screen": True}]
    run = _Running(state, FakeCua(windows=windows))
    run.wait_serving()

    async def go():
        v = await _Viewer.connect(state / "rfb.sock")
        await v.request()
        (x, y, w, h, enc, data), = await v.read_update()
        img = _decode_zrle(__import__("struct").pack(">HHHHiI", x, y, w, h, enc, len(data)) + data, v.fmt, v.z, v.size)
        v.close()
        return img
    img = asyncio.run(go())
    assert img.size == (400, 260)
    assert img.getpixel((10, 10)) == (0, 0, 0) and img.getpixel((300, 200)) == (30, 60, 90)
    run.finish()
    events = _events(run)
    assert events[:3] == ["session_requested", "consent", "serving"] and "viewer_attached" in events
    assert events[-1] == "session_end"
    assert any("shown in Hermes" in n for n in run.notices)


def test_state_files_and_socket_are_private_then_removed_at_the_end(state):
    run = _Running(state, FakeCua())
    run.wait_serving()
    assert (state / "rfb.sock").stat().st_mode & 0o777 == 0o600
    assert (state / "audit.log").stat().st_mode & 0o777 == 0o600
    assert (state / "fb-size").read_text() == "400x260"
    run.finish()
    for name in ("rfb.sock", "env", "fb-size"):
        assert not (state / name).exists()


def test_the_session_ends_by_itself_when_no_viewer_stays_so_the_next_one_asks_again(state):
    run = _Running(state, FakeCua(), grace=0.4)
    run.wait_serving()
    run.thread.join(6)
    assert run.code == server.EXIT_OK and "idle_end" in _events(run)


def test_capture_runs_only_while_a_viewer_is_attached(state):
    cua = FakeCua()
    run = _Running(state, cua)
    run.wait_serving()
    before = cua.captures
    time.sleep(0.6)
    assert cua.captures == before  # nobody watching: no polling of the daemon
    run.finish()


def test_the_primary_display_changing_resolution_reaches_the_viewer_as_a_resize(state):
    cua = FakeCua(frames=[png_bytes((400, 260))] * 3 + [png_bytes((300, 200))])
    run = _Running(state, cua)
    run.wait_serving()

    async def go():
        from tools.bot_desktop.macos import rfb_server as rfb
        v = await _Viewer.connect(state / "rfb.sock")
        await v.request()
        await v.read_update()
        for _ in range(40):
            await v.request(incremental=True)
            try:
                rects = await v.read_update(timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if any(r[4] == rfb.ENC_EXT_DESKTOP_SIZE for r in rects):
                v.close()
                return v.size
        v.close()
    assert asyncio.run(go()) == (300, 200)
    run.finish()


def test_masks_fail_closed_when_the_window_list_stays_unavailable(state, monkeypatch):
    monkeypatch.setattr(server, "MASK_STALE_S", 0.3)
    monkeypatch.setattr(server, "MASK_REFRESH_S", 0.05)
    cua = FakeCua()
    run = _Running(state, cua)
    run.wait_serving()
    cua.fail_windows = True

    async def go():
        v = await _Viewer.connect(state / "rfb.sock")
        deadline = time.time() + 5
        last = None
        while time.time() < deadline:
            await v.request()
            (x, y, w, h, enc, data), = await v.read_update()
            last = _decode_zrle(__import__("struct").pack(">HHHHiI", x, y, w, h, enc, len(data)) + data, v.fmt, v.z, v.size)
            if last.getpixel((300, 200)) == (0, 0, 0):
                break
            await asyncio.sleep(0.1)
        v.close()
        return last
    assert asyncio.run(go()).getpixel((300, 200)) == (0, 0, 0)
    run.finish()


# ── lease: the same fence as Linux ─────────────────────────────────────────────────────────────────────


def _send_click(state, x, y):
    async def go():
        import struct
        v = await _Viewer.connect(state / "rfb.sock")
        v.w.write(struct.pack(">BBHH", 5, LEFT, x, y) + struct.pack(">BBHH", 5, 0, x, y))
        await v.w.drain()
        await asyncio.sleep(0.4)
        v.close()
    asyncio.run(go())


def test_input_is_dropped_while_the_agent_holds_the_lease_and_injected_for_a_human_holder(state):
    cua = FakeCua()
    run = _Running(state, cua)
    run.wait_serving()
    _send_click(state, 20, 20)
    assert not [c for c in cua.acted() if c[0] == "click"]

    lease.acquire("v-phone", client_id="ios-1", label="Gokhan's iPhone", watched=True)
    time.sleep(0.35)  # the server re-reads lease.json at most every 250 ms
    _send_click(state, 20, 20)
    clicks = [c for c in cua.acted() if c[0] == "click"]
    assert clicks == [("click", 20, 20, "left", 1)]  # framebuffer == native here (400 wide, below the 1280 cap)
    run.finish()
    assert "control_human" in _events(run) and any(a.get("label") == "Gokhan's iPhone" for a in run.audit())
    assert any("paused" in n for n in run.notices)


def test_computer_use_is_refused_while_a_human_holds_and_the_watchdog_returns_control(state):
    """The fence ``computer_use`` consults is the lease file this session reads: nothing Mac-specific to drift."""
    lease.acquire("v-phone", client_id="ios-1", label="iPhone", watched=True)
    with pytest.raises(lease.HumanHasControl):
        lease.assert_agent_may_act()
    from hermes_constants import get_hermes_home
    home = str(get_hermes_home())
    lease_watchdog.attach(home, "v-phone")
    lease_watchdog.detach(home, "v-phone")
    assert lease_watchdog.check(home, now=1000.0) is None          # first sight of the drop starts the clock
    returned = lease_watchdog.check(home, now=1031.0)               # 30 s later control goes back
    assert returned is not None and returned.holder == lease.AGENT and returned.reason == lease_watchdog.HOLDER_DROPPED
    assert lease.assert_agent_may_act().holder == lease.AGENT


def test_input_stops_again_after_the_watchdog_hands_control_back(state):
    cua = FakeCua()
    run = _Running(state, cua)
    run.wait_serving()
    lease.acquire("v-phone", client_id="ios-1", label="iPhone", watched=True)
    time.sleep(0.35)
    _send_click(state, 20, 20)
    assert len([c for c in cua.acted() if c[0] == "click"]) == 1
    lease.release("v-phone", reason=lease_watchdog.HOLDER_DROPPED)
    time.sleep(0.35)
    _send_click(state, 20, 20)
    assert len([c for c in cua.acted() if c[0] == "click"]) == 1
    run.finish()
    assert "control_human" in _events(run) and "control_agent" in _events(run)
