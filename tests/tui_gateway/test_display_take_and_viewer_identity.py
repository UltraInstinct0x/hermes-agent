"""display.observe hands an identified client the same viewer id on every connection, and display.take is the
explicit, recorded and announced takeover of the Bot Screen lease."""

from __future__ import annotations

import time

import pytest

from tui_gateway import client_identity, server
from tools.bot_desktop import lease, lease_watchdog, runtime


class _Peer:
    """A connection: identity and minted viewer ids are keyed by object identity."""


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    lease._reset_for_tests()
    lease_watchdog.reset_for_tests()
    client_identity.reset_for_tests()
    monkeypatch.setattr(runtime, "rfb_socket_path", lambda: tmp_path / "rfb.sock")
    events = []
    monkeypatch.setattr(server, "_broadcast_global_event", lambda event, payload=None: events.append((event, payload)))
    yield events
    lease._reset_for_tests()
    lease_watchdog.reset_for_tests()
    client_identity.reset_for_tests()


def _call(peer, method, params=None):
    return server.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, peer)


def _identified(client_id, label, kind="ios"):
    peer = _Peer()
    client_identity.declare(peer, client_id=client_id, label=label, kind=kind)
    return peer


def test_identified_client_gets_the_same_viewer_id_on_every_connection():
    first, again = _identified("ios-1", "Phone"), _identified("ios-1", "Phone")
    other = _identified("ios-2", "iPad")

    a = _call(first, "display.observe")["result"]["viewer_id"]
    b = _call(again, "display.observe")["result"]["viewer_id"]
    c = _call(other, "display.observe")["result"]["viewer_id"]

    assert a == b == lease.stable_viewer_id("ios-1") and a != c


def test_anonymous_connections_keep_per_connection_viewer_ids():
    one, two = _Peer(), _Peer()
    a = _call(one, "display.observe")["result"]["viewer_id"]
    assert _call(one, "display.observe", {"viewer_id": a})["result"]["viewer_id"] == a
    assert _call(two, "display.observe")["result"]["viewer_id"] != a
    assert _call(two, "display.observe", {"viewer_id": a})["result"]["viewer_id"] != a


def test_a_reconnecting_phone_is_still_the_holder_and_can_hand_back_without_force():
    phone = _identified("ios-1", "Gokhan's iPhone")
    viewer = _call(phone, "display.observe")["result"]["viewer_id"]
    held = _call(phone, "display.lease.acquire", {"viewer_id": viewer})["result"]["lease"]
    assert held["label"] == "Gokhan's iPhone" and held["client_id"] == "ios-1" and held["watched"] is True

    reconnected = _identified("ios-1", "Gokhan's iPhone")  # the socket dropped, a new one identifies itself
    viewer_again = _call(reconnected, "display.observe")["result"]
    assert viewer_again["lease"]["holder"] == "human"
    released = _call(reconnected, "display.lease.release", {"viewer_id": viewer_again["viewer_id"]})["result"]["lease"]
    assert released["holder"] == "agent"


def test_take_displaces_the_holder_and_announces_who_took_it_from_whom(_env):
    phone, mac = _identified("ios-1", "Gokhan's iPhone"), _identified("desktop-1", "Mac", "desktop")
    phone_viewer = _call(phone, "display.observe")["result"]["viewer_id"]
    mac_viewer = _call(mac, "display.observe")["result"]["viewer_id"]
    _call(phone, "display.lease.acquire", {"viewer_id": phone_viewer})
    _env.clear()

    taken = _call(mac, "display.take", {"viewer_id": mac_viewer, "reason": "phone is gone"})["result"]

    assert taken["forced"] is True and taken["taken_from"] == "Gokhan's iPhone"
    assert taken["lease"]["label"] == "Mac" and taken["lease"]["reason"] == "phone is gone"
    (announce,) = [payload for event, payload in _env if event == "display.lease.taken"]
    assert announce["taken_by"] == {"client_id": "desktop-1", "label": "Mac", "kind": "desktop"}
    assert announce["taken_from"] == "Gokhan's iPhone" and announce["lease"]["forced"] is True
    assert lease.get().viewer_id == mac_viewer


def test_taking_a_free_screen_is_announced_but_not_forced(_env):
    mac = _identified("desktop-1", "Mac", "desktop")
    viewer = _call(mac, "display.observe")["result"]["viewer_id"]

    taken = _call(mac, "display.take", {"viewer_id": viewer})["result"]

    assert taken["forced"] is False and taken["taken_from"] is None
    (announce,) = [payload for event, payload in _env if event == "display.lease.taken"]
    assert announce["taken_from"] is None


def test_taking_what_you_already_hold_changes_and_announces_nothing(_env):
    mac = _identified("desktop-1", "Mac", "desktop")
    viewer = _call(mac, "display.observe")["result"]["viewer_id"]
    _call(mac, "display.take", {"viewer_id": viewer})
    _env.clear()

    again = _call(mac, "display.take", {"viewer_id": viewer})["result"]

    assert again["forced"] is False
    assert [event for event, _ in _env if event == "display.lease.taken"] == []


def test_take_refuses_a_viewer_id_this_connection_never_minted():
    mac, other = _identified("desktop-1", "Mac"), _identified("ios-1", "Phone")
    other_viewer = _call(other, "display.observe")["result"]["viewer_id"]

    refused = _call(mac, "display.take", {"viewer_id": other_viewer})

    assert refused["error"]["data"]["code"] == "viewer_mismatch" and lease.get().holder == lease.AGENT


def test_a_dropped_phone_is_reclaimed_by_the_watchdog_and_the_release_is_broadcast(_env, monkeypatch):
    from hermes_constants import get_hermes_home

    phone = _identified("ios-1", "Gokhan's iPhone")
    viewer = _call(phone, "display.observe")["result"]["viewer_id"]
    _call(phone, "display.lease.acquire", {"viewer_id": viewer})
    monkeypatch.setattr(server, "_watched_lease_homes", lambda: [get_hermes_home()])
    server._lease_listener_installed.clear()  # the fixture reset dropped the listener a previous test installed
    server._install_lease_listener()
    real_check = lease_watchdog.check

    # No viewer socket was ever attached: the first pass starts the clock, a pass 31 s later releases.
    server._poll_holder_watchdog()
    assert lease.get().holder == lease.HUMAN
    later = time.monotonic() + lease_watchdog.GRACE_S + 1
    monkeypatch.setattr(lease_watchdog, "check", lambda home, **kw: real_check(home, now=later, **kw))
    server._poll_holder_watchdog()

    assert lease.get().holder == lease.AGENT and lease.get().reason == "holder_dropped"
    assert any(event == "display.lease" and payload["lease"]["holder"] == "agent" for event, payload in _env)
