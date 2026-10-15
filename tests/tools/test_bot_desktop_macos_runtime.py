"""Mac Bot Screen host side: display.status tells the truth with a precise reason, the Linux runtime dispatches to
it on darwin only, start surfaces a declined prompt calmly, and the Linux path is unchanged."""

from __future__ import annotations

import os
import types

import pytest

from tools.bot_desktop import runtime
from tools.bot_desktop.macos import runtime as mac


@pytest.fixture(autouse=True)
def _mac_env(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(mac, "_modules_missing_for_server", list)  # no real child in unrelated tests
    mac.reset_probe_cache()
    yield
    mac.reset_probe_cache()


def _granted(**over):
    perms = {"accessibility": True, "screen_recording": True}
    perms.update(over)
    return perms


# ── the reason a client can show ──────────────────────────────────────────────────────────────────────


def test_missing_cua_driver_is_named_with_the_install_command():
    p = mac._judge(None, None)
    assert (p.supported, p.installed, p.missing) == (False, False, ["cua-driver"])
    assert "hermes computer-use install" in p.reason


def test_a_silent_daemon_is_not_reported_as_a_missing_grant():
    p = mac._judge("/bin/cua-driver", None)
    assert p.installed and not p.supported and "not answering" in p.reason
    assert not mac._judge("/bin/cua-driver", {"accessibility": None, "screen_recording": None}).supported


@pytest.mark.parametrize("perms,named", [
    (_granted(accessibility=False), "Accessibility is not allowed"),
    (_granted(screen_recording=False), "Screen Recording is not allowed"),
    (_granted(accessibility=False, screen_recording=False), "Accessibility and Screen Recording are not allowed"),
])
def test_each_missing_permission_is_named(perms, named):
    p = mac._judge("/bin/cua-driver", perms)
    assert not p.supported and p.installed and named in p.reason and "CuaDriver" in p.reason


def test_everything_present_is_supported_and_reasons_use_no_em_dashes():
    assert mac._judge("/bin/cua-driver", _granted()) == mac.Probe(True, True)
    for reason in (mac._judge(None, None).reason, mac._judge("x", None).reason, mac._judge("x", _granted(accessibility=False)).reason):
        assert "—" not in reason


def test_probe_is_cached_briefly_and_refreshable(monkeypatch):
    calls = []
    monkeypatch.setattr(mac, "find_cua_driver", lambda: "/bin/cua-driver")
    monkeypatch.setattr(mac, "_permissions", lambda cmd: calls.append(cmd) or _granted())
    assert mac.probe().supported and mac.probe().supported
    assert len(calls) == 1
    mac.probe(force=True)
    assert len(calls) == 2


# ── display.status ────────────────────────────────────────────────────────────────────────────────────


def test_status_reports_supported_installed_running_truthfully(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_mac_backend", lambda: True)
    monkeypatch.setattr(mac, "probe", lambda **kw: mac.Probe(True, True))
    monkeypatch.setattr(runtime.placement, "resolve", lambda: runtime.placement.Placement("gateway", "local"))
    stopped = runtime.status()
    assert (stopped.supported, stopped.installed, stopped.running, stopped.blocker, stopped.socket) == (True, True, False, None, None)

    (tmp_path / "launcher.pid").write_text(f"{os.getpid()} {runtime._create_time(os.getpid())}", encoding="utf-8")
    (tmp_path / "env").write_text("HERMES_BD_BACKEND=macos\n", encoding="utf-8")
    (tmp_path / "rfb.sock").write_text("", encoding="utf-8")
    (tmp_path / "fb-size").write_text("1280x832", encoding="utf-8")
    running = runtime.status()
    assert running.running and running.display == "primary" and running.geometry == "1280x832"
    assert running.socket == str(tmp_path / "rfb.sock") and running.pid == os.getpid()
    assert runtime.rfb_socket_path() == tmp_path / "rfb.sock" and runtime.is_running()


@pytest.mark.parametrize("configured,effective", [
    (None, "ask"), ("ask", "ask"), ("allow", "allow"), ("deny", "deny"), ("Allow", "allow"), ("nonsense", "ask"), (True, "ask"),
])
def test_status_reports_the_effective_consent_mode(monkeypatch, configured, effective):
    monkeypatch.setattr(runtime, "_mac_backend", lambda: True)
    monkeypatch.setattr(mac, "probe", lambda **kw: mac.Probe(True, True))
    monkeypatch.setattr(runtime.placement, "resolve", lambda: runtime.placement.Placement("gateway", "local"))
    monkeypatch.setattr(mac, "_cfg", lambda: {} if configured is None else {"mac_consent": configured})
    assert runtime.status().consent == effective
    assert runtime.status().as_dict()["consent"] == effective


def test_the_session_child_receives_the_effective_mode(monkeypatch):
    monkeypatch.setattr(mac, "_cfg", lambda: {"mac_consent": "allow"})
    monkeypatch.setattr(mac._preflight, "with_parent_site_packages", lambda env: env)
    monkeypatch.setattr("tools.environments.local.served_profile_child_env", lambda **kw: {})
    assert mac._server_env()["HERMES_BD_MAC_CONSENT"] == "allow"
    monkeypatch.setattr(mac, "_cfg", lambda: {"mac_consent": "bogus"})
    assert mac._server_env()["HERMES_BD_MAC_CONSENT"] == "ask"


def test_a_policy_refusal_is_a_calm_sentence(tmp_path, monkeypatch, _startable):
    def popen(argv, **kw):
        (tmp_path / "consent").write_text("policy", encoding="utf-8")
        return _Proc(999999, code=3)
    monkeypatch.setattr(mac.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="never share") as exc:
        mac.start()
    assert "—" not in str(exc.value)


def test_unsupported_mac_status_carries_the_reason_as_the_blocker(monkeypatch):
    monkeypatch.setattr(runtime, "_mac_backend", lambda: True)
    monkeypatch.setattr(runtime.placement, "resolve", lambda: runtime.placement.Placement("gateway", "local"))
    monkeypatch.setattr(mac, "probe", lambda **kw: mac._judge("/bin/cua-driver", _granted(screen_recording=False)))
    st = runtime.status()
    assert (st.supported, st.installed, st.running) == (False, True, False)
    assert "Screen Recording" in st.blocker and st.install_command is None


def test_missing_cua_driver_offers_the_install_command(monkeypatch):
    monkeypatch.setattr(runtime, "_mac_backend", lambda: True)
    monkeypatch.setattr(runtime.placement, "resolve", lambda: runtime.placement.Placement("gateway", "local"))
    monkeypatch.setattr(mac, "probe", lambda **kw: mac._judge(None, None))
    st = runtime.status()
    assert not st.installed and st.missing == ["cua-driver"] and st.install_command == "hermes computer-use install"


# ── dispatch: darwin only, Linux untouched ────────────────────────────────────────────────────────────


def test_is_supported_host_on_darwin_follows_the_probe(monkeypatch):
    monkeypatch.setattr(runtime, "sys", types.SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(runtime, "_mac_backend", lambda: True)
    monkeypatch.setattr(mac, "probe", lambda **kw: mac.Probe(True, True))
    assert runtime.is_supported_host()
    monkeypatch.setattr(mac, "probe", lambda **kw: mac.Probe(False, True, [], "nope"))
    assert not runtime.is_supported_host()


def test_linux_stays_linux_and_windows_stays_unsupported(monkeypatch):
    monkeypatch.setattr(runtime, "sys", types.SimpleNamespace(platform="linux"))
    assert runtime.is_supported_host()
    monkeypatch.setattr(runtime, "sys", types.SimpleNamespace(platform="win32"))
    assert not runtime.is_supported_host()


def test_mac_children_never_inherit_a_display(monkeypatch):
    monkeypatch.setattr(runtime, "_mac_backend", lambda: True)
    from tools.bot_desktop import sandbox_host
    monkeypatch.setattr(sandbox_host, "_read_marker", lambda: None)
    assert runtime.published_env() == {}


def test_a_mac_screen_is_never_started_silently_by_a_tool(monkeypatch):
    monkeypatch.setattr(runtime, "sys", types.SimpleNamespace(platform="darwin"))
    assert runtime._should_auto_start({}) is False


def test_start_and_stop_dispatch_to_the_mac_backend(monkeypatch):
    monkeypatch.setattr(runtime.placement, "resolve", lambda: runtime.placement.Placement("gateway", "local"))
    monkeypatch.setattr(runtime, "_mac_backend", lambda: True)
    monkeypatch.setattr(mac, "start", lambda wait_seconds=0: "started")
    monkeypatch.setattr(mac, "stop", lambda: "stopped")
    assert runtime.start() == "started" and runtime.stop() == "stopped"


# ── start ─────────────────────────────────────────────────────────────────────────────────────────────


class _Proc:
    def __init__(self, pid, code=None, on_poll=None):
        self.pid, self._code, self._on_poll = pid, code, on_poll

    def poll(self):
        if self._on_poll:
            self._on_poll()
        return self._code

    def wait(self, timeout=None):
        return self._code


@pytest.fixture
def _startable(monkeypatch):
    monkeypatch.setattr(mac, "_spawn_guard", lambda: None)  # Popen is faked in these tests
    monkeypatch.setattr(mac, "probe", lambda **kw: mac.Probe(True, True))
    monkeypatch.setattr(mac, "_server_env", dict)


@pytest.mark.parametrize("reason,sentence", [
    ("denied", "declined"), ("timeout", "Nobody answered"), ("unavailable", "could not show its permission prompt"),
])
def test_a_declined_prompt_becomes_a_calm_sentence(tmp_path, monkeypatch, _startable, reason, sentence):
    def popen(argv, **kw):
        (tmp_path / "consent").write_text(reason, encoding="utf-8")
        return _Proc(999999, code=3)
    monkeypatch.setattr(mac.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match=sentence) as exc:
        mac.start()
    assert "Traceback" not in str(exc.value) and "—" not in str(exc.value)
    assert not (tmp_path / "launcher.pid").exists()


def test_a_capture_failure_is_reported_with_its_reason(tmp_path, monkeypatch, _startable):
    def popen(argv, **kw):
        (tmp_path / "error").write_text("get_desktop_state: permission denied", encoding="utf-8")
        return _Proc(999999, code=4)
    monkeypatch.setattr(mac.subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="could not capture its screen: get_desktop_state"):
        mac.start()


def test_a_test_process_can_never_reach_the_real_spawn(monkeypatch):
    monkeypatch.setattr(mac, "probe", lambda **kw: mac.Probe(True, True))
    monkeypatch.setattr(mac.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(RuntimeError, match="not started from a test process"):
        mac.start()


def test_start_refuses_when_the_mac_is_not_ready_and_never_spawns(monkeypatch):
    monkeypatch.setattr(mac, "probe", lambda **kw: mac._judge("/bin/cua-driver", _granted(accessibility=False)))
    monkeypatch.setattr(mac.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(RuntimeError, match="Accessibility is not allowed"):
        mac.start()


def test_start_returns_a_running_status_once_the_server_publishes(tmp_path, monkeypatch, _startable):
    def publish():
        (tmp_path / "env").write_text("HERMES_BD_BACKEND=macos\n", encoding="utf-8")
        (tmp_path / "rfb.sock").write_text("", encoding="utf-8")
    monkeypatch.setattr(mac.subprocess, "Popen", lambda argv, **kw: _Proc(os.getpid(), on_poll=publish))
    monkeypatch.setattr(runtime, "_mac_backend", lambda: True)
    st = mac.start()
    assert st.running and st.supported
    assert mac.start().running  # idempotent: a running session is returned, not respawned


def test_stop_signals_the_session_group_and_clears_the_state(tmp_path, monkeypatch):
    (tmp_path / "launcher.pid").write_text(f"{os.getpid()} {runtime._create_time(os.getpid())}", encoding="utf-8")
    for name in ("env", "rfb.sock", "fb-size", "thumb.jpg"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    killed = []
    monkeypatch.setattr(runtime, "_kill_group_then_wait", lambda pgid, pid, grace=2.0: killed.append((pgid, pid)))
    assert mac.stop() is True and killed == [(os.getpid(), os.getpid())]
    assert sorted(p.name for p in tmp_path.iterdir() if p.name not in ("start.lock", "hermes_test")) == []
    assert mac.stop() is False


def test_thumbnail_is_served_only_while_a_viewer_keeps_it_fresh(tmp_path):
    assert mac.thumbnail_jpeg() is None
    (tmp_path / "thumb.jpg").write_bytes(b"\xff\xd8jpeg")
    assert mac.thumbnail_jpeg() == b"\xff\xd8jpeg"
    old = os.stat(tmp_path / "thumb.jpg").st_mtime - 120
    os.utime(tmp_path / "thumb.jpg", (old, old))
    assert mac.thumbnail_jpeg() is None
