"""Mac Bot Screen guards: consent defaults to deny, the deny list masks the right windows, take-control notices a
person using the Mac (fake idle source), and capture fitting keeps the phone-sized framebuffer honest."""

from __future__ import annotations

import subprocess

from PIL import Image

from tools.bot_desktop.macos import capture, consent, denylist, idle
from tools.bot_desktop.macos import runtime as mac_runtime
from tests.tools._macos_bot_screen_fakes import FakeCua, png_bytes


# ── consent ───────────────────────────────────────────────────────────────────────────────────────────


def _runner(stdout="", returncode=0, stderr="", raises=None):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        if raises:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
    run.seen = seen
    return run


def test_only_an_explicit_allow_is_consent():
    assert consent.request("x", run=_runner("button returned:Allow, gave up:false")) == consent.Consent(True, "allowed")
    assert consent.request("x", run=_runner("button returned:Deny, gave up:false")) == consent.Consent(False, "denied")
    assert consent.request("x", run=_runner("button returned:, gave up:true")) == consent.Consent(False, "timeout")
    assert consent.request("x", run=_runner("")) == consent.Consent(False, "denied")


def test_closing_the_dialog_or_having_no_gui_session_is_a_no():
    cancelled = _runner(returncode=1, stderr="execution error: User canceled. (-128)")
    assert consent.request("x", run=cancelled) == consent.Consent(False, "denied")
    no_gui = _runner(returncode=1, stderr="execution error: no user interaction allowed. (-1713)")
    assert consent.request("x", run=no_gui) == consent.Consent(False, "unavailable")
    assert consent.request("x", run=_runner(raises=OSError("no osascript"))) == consent.Consent(False, "unavailable")


def test_a_hung_dialog_times_out_as_deny():
    hung = _runner(raises=subprocess.TimeoutExpired("osascript", 40))
    assert consent.request("x", run=hung) == consent.Consent(False, "timeout")


def test_dialog_defaults_to_deny_and_gives_up_after_30_seconds():
    run = _runner("button returned:Allow, gave up:false")
    consent.request("Share my screen", run=run)
    script = " ".join(run.seen["argv"])
    assert 'default button "Deny"' in script and 'cancel button "Deny"' in script and "giving up after 30" in script
    assert run.seen["kwargs"]["timeout"] >= 30


def test_summary_text_cannot_break_out_of_the_applescript_string():
    run = _runner("button returned:Deny, gave up:false")
    consent.request('a "quoted" \\ summary', run=run)
    assert '\\"quoted\\"' in " ".join(run.seen["argv"])


def test_notify_never_raises():
    consent.notify("hi", run=_runner(raises=OSError("gone")))


# ── deny list ─────────────────────────────────────────────────────────────────────────────────────────


def _win(app, x=0, y=0, w=100, h=50, on=True):
    return {"app_name": app, "bounds": {"x": x, "y": y, "width": w, "height": h}, "is_on_screen": on}


def test_password_managers_messages_and_keychain_are_masked_in_native_pixels():
    windows = [_win("1Password 7", 10, 20, 100, 50), _win("Messages", 0, 0, 10, 10), _win("Keychain Access", 5, 5, 20, 20),
               _win("Google Chrome", 0, 0, 1470, 900), _win("Passwords", 1, 1, 2, 2)]
    rects = denylist.masked_rects(windows, denylist.deny_list(), scale=2.0)
    assert len(rects) == 4 and (18, 38, 204, 104) in rects  # 1Password: (10,20,100,50) * 2 plus a 2 px margin


def test_hidden_windows_and_unrelated_apps_are_not_masked_and_extra_apps_can_be_added():
    windows = [_win("Messages", on=False), _win("Obsidian"), _win("Signal")]
    assert denylist.masked_rects(windows, denylist.deny_list(), 1.0) == []
    assert len(denylist.masked_rects(windows, denylist.deny_list(["signal"]), 1.0)) == 1
    assert denylist.is_denied("BITWARDEN", denylist.deny_list()) and not denylist.is_denied("", denylist.deny_list())


def test_masks_paint_the_deny_listed_area_black_in_framebuffer_coordinates():
    img = Image.new("RGB", (100, 50), (200, 200, 200))
    capture.apply_masks(img, [(40, 20, 40, 20)], native_size=(200, 100))  # native is 2x the framebuffer
    assert img.getpixel((30, 15)) == (0, 0, 0) and img.getpixel((5, 5)) == (200, 200, 200)


# ── capture fitting ───────────────────────────────────────────────────────────────────────────────────


def test_fit_size_caps_the_long_edge_and_never_upscales():
    assert capture.fit_size((2940, 1912), 1280) == (1280, 832)
    assert capture.fit_size((800, 600), 1280) == (800, 600)
    assert capture.fit_size((1000, 3000), 1500) == (500, 1500)


def test_cua_capture_returns_a_phone_sized_frame_and_the_native_geometry():
    cua = FakeCua(frames=[png_bytes((2940, 1912))], scale=2.0)
    frame = capture.CuaCapture(cua, 1280).grab()
    assert frame.image.size == (1280, 832) and frame.native_size == (2940, 1912) and frame.scale_factor == 2.0
    fx, fy = frame.native_per_fb
    assert abs(fx - 2.296875) < 1e-6 and abs(fy - 1912 / 832) < 1e-6


def test_auto_mode_uses_cua_unless_this_process_may_capture(monkeypatch):
    monkeypatch.setattr(capture, "quartz_available", lambda: False)
    assert capture.pick_source("auto", FakeCua()).name == "cua"
    assert capture.pick_source("cua", FakeCua()).name == "cua"
    monkeypatch.setattr(capture, "_Quartz", lambda: object())
    monkeypatch.setattr(capture, "quartz_available", lambda: True)
    assert capture.pick_source("auto", FakeCua()).name == "quartz"


# ── take-control and a person at the keyboard ──────────────────────────────────────────────────────────


def test_idle_user_present_only_when_input_was_recent():
    assert idle.user_present(lambda: 3.0, 20) == 3.0
    assert idle.user_present(lambda: 300.0, 20) is None
    assert idle.user_present(lambda: None, 20) is None  # unknown is never treated as present


def test_take_control_asks_for_a_second_confirmation_when_someone_is_at_the_mac(monkeypatch):
    monkeypatch.setattr(mac_runtime, "present_within_seconds", lambda: 20.0)
    refusal = mac_runtime.take_control_gate(False, source=lambda: 4.2)
    assert refusal["code"] == "mac_user_present" and refusal["idle_seconds"] == 4.2
    assert "using this Mac" in refusal["message"] and "—" not in refusal["message"]
    assert mac_runtime.take_control_gate(True, source=lambda: 4.2) is None      # confirmed
    assert mac_runtime.take_control_gate(False, source=lambda: 600.0) is None   # nobody there: no friction
    assert mac_runtime.take_control_gate(False, source=lambda: None) is None
