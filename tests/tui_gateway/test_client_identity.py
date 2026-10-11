"""A connection's client identity: declared in ``client.capabilities``, overridden by the proxy's device handle."""

from __future__ import annotations

import pytest

from tui_gateway import client_identity


class _Peer:
    """Stands in for a transport: identity is keyed by object identity, nothing else is touched."""


@pytest.fixture(autouse=True)
def _clean():
    client_identity.reset_for_tests()
    yield
    client_identity.reset_for_tests()


def test_connection_without_identity_is_unknown():
    identity = client_identity.identity_for(_Peer())
    assert identity.client_id == "unknown" and identity.source == "none"
    assert identity.as_wire() == {"client_id": "unknown", "label": None, "kind": None}


def test_declared_identity_is_recorded_and_sanitised():
    peer = _Peer()
    identity = client_identity.declare(peer, client_id="ios-7f3a", label="  Gokhan's\x00 iPhone ", kind="IOS")
    assert (identity.client_id, identity.label, identity.kind, identity.source) == (
        "ios-7f3a", "Gokhan's iPhone", "ios", "declared")


def test_declared_label_is_clipped_and_bad_ids_are_ignored():
    peer = _Peer()
    identity = client_identity.declare(peer, client_id="has spaces and / slashes", label="x" * 500, kind="not a kind!")
    assert identity.client_id == "unknown"
    assert len(identity.label) == 80 and identity.kind is None


@pytest.mark.parametrize("claimed", ["unknown", "device:abc12345"])
def test_a_client_cannot_declare_the_absence_marker_or_a_device_id(claimed):
    assert client_identity.declare(_Peer(), client_id=claimed).client_id == "unknown"


def test_device_handle_from_the_upgrade_overrides_the_declared_id():
    peer = _Peer()
    client_identity.bind_device_handle(peer, "pairedPhone_01")
    identity = client_identity.declare(peer, client_id="whatever-it-says", label="Phone", kind="pwa")
    assert identity.client_id == "device:pairedPhone_01" and identity.source == "device"
    assert identity.label == "Phone" and identity.kind == "pwa"


def test_malformed_device_handle_is_ignored():
    peer = _Peer()
    client_identity.bind_device_handle(peer, "no spaces allowed")
    client_identity.bind_device_handle(peer, None)
    assert client_identity.identity_for(peer).source == "none"


def test_forget_drops_both_sources():
    peer = _Peer()
    client_identity.bind_device_handle(peer, "pairedPhone_01")
    client_identity.declare(peer, client_id="ios-1")
    client_identity.forget(peer)
    assert client_identity.identity_for(peer).client_id == "unknown"


def test_client_capabilities_rpc_records_the_caller_and_reports_it_back():
    from tui_gateway import server

    peer = _Peer()
    request = {"jsonrpc": "2.0", "id": 1, "method": "client.capabilities",
               "params": {"server_requests": True, "client_id": "desktop-main", "label": "Mac", "kind": "desktop"}}
    try:
        reply = server.dispatch(request, peer)
        assert reply["result"]["client_id"] == "desktop-main"
        assert reply["result"]["identity_source"] == "declared"
        assert client_identity.identity_for(peer).label == "Mac"
        # An older client that sends only server_requests still works and stays anonymous.
        old = _Peer()
        legacy = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "client.capabilities",
                                  "params": {"server_requests": True}}, old)
        assert legacy["result"]["client_id"] == "unknown" and legacy["result"]["identity_source"] == "none"
    finally:
        server.unregister_live_transport(peer)


def test_device_handle_wins_over_the_declared_id_through_the_rpc():
    from tui_gateway import server

    peer = _Peer()
    client_identity.bind_device_handle(peer, "pairedPhone_01")
    reply = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "client.capabilities",
                             "params": {"client_id": "forged-owner"}}, peer)
    assert reply["result"]["client_id"] == "device:pairedPhone_01"
    assert reply["result"]["identity_source"] == "device"
    server.unregister_live_transport(peer)
    assert client_identity.identity_for(peer).client_id == "unknown"
