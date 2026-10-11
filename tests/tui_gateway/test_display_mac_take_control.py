"""On a Mac host, take-control asks for a second confirmation when somebody used the Mac a moment ago, instead of
silently fighting for their cursor. Linux hosts and idle Macs see no change."""

from __future__ import annotations

import pytest

from tui_gateway import client_identity, server
from tools.bot_desktop import lease, lease_watchdog, runtime
from tools.bot_desktop.macos import runtime as mac


class _Peer:
    pass


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    lease._reset_for_tests()
    lease_watchdog.reset_for_tests()
    client_identity.reset_for_tests()
    monkeypatch.setattr(runtime, "rfb_socket_path", lambda: tmp_path / "rfb.sock")
    monkeypatch.setattr(server, "_broadcast_global_event", lambda *a, **k: None)
    monkeypatch.setattr(mac, "present_within_seconds", lambda: 20.0)
    yield
    lease._reset_for_tests()
    lease_watchdog.reset_for_tests()
    client_identity.reset_for_tests()


def _call(peer, method, params=None):
    return server.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, peer)


def _viewer(peer):
    return _call(peer, "display.observe")["result"]["viewer_id"]


@pytest.mark.parametrize("method", ["display.take", "display.lease.acquire"])
def test_someone_at_the_mac_gets_a_calm_refusal_then_can_confirm(monkeypatch, method):
    monkeypatch.setattr(runtime, "_mac_backend", lambda: True)
    monkeypatch.setattr(mac._idle, "seconds_since_input", lambda: 3.0)
    peer = _Peer()
    vid = _viewer(peer)

    refused = _call(peer, method, {"viewer_id": vid})
    assert refused["error"]["data"]["code"] == "mac_user_present" and refused["error"]["data"]["idle_seconds"] == 3.0
    assert "using this Mac" in refused["error"]["message"]
    assert lease.get().holder == lease.AGENT  # nothing changed hands

    taken = _call(peer, method, {"viewer_id": vid, "confirm_user_present": True})
    assert "error" not in taken and lease.get().holder == lease.HUMAN


def test_an_idle_mac_needs_no_confirmation(monkeypatch):
    monkeypatch.setattr(runtime, "_mac_backend", lambda: True)
    monkeypatch.setattr(mac._idle, "seconds_since_input", lambda: 900.0)
    peer = _Peer()
    assert "error" not in _call(peer, "display.take", {"viewer_id": _viewer(peer)})


def test_unknown_idle_time_never_blocks_take_control(monkeypatch):
    monkeypatch.setattr(runtime, "_mac_backend", lambda: True)
    monkeypatch.setattr(mac._idle, "seconds_since_input", lambda: None)
    peer = _Peer()
    assert "error" not in _call(peer, "display.lease.acquire", {"viewer_id": _viewer(peer)})


def test_the_current_holder_is_not_asked_again(monkeypatch):
    monkeypatch.setattr(runtime, "_mac_backend", lambda: False)
    peer = _Peer()
    vid = _viewer(peer)
    assert "error" not in _call(peer, "display.take", {"viewer_id": vid})
    monkeypatch.setattr(runtime, "_mac_backend", lambda: True)
    monkeypatch.setattr(mac._idle, "seconds_since_input", lambda: 1.0)
    assert "error" not in _call(peer, "display.take", {"viewer_id": vid})


def test_a_non_mac_host_ignores_the_gate_entirely(monkeypatch):
    monkeypatch.setattr(runtime, "_mac_backend", lambda: False)
    monkeypatch.setattr(mac._idle, "seconds_since_input", lambda: 1.0)
    peer = _Peer()
    assert "error" not in _call(peer, "display.take", {"viewer_id": _viewer(peer)})
