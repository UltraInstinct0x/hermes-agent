"""Bot Screen lease identity and the dropped-holder watchdog: the holder is named, a displaced holder is recorded,
and a human whose viewer socket never came back hands control to the agent after 30 s."""

from __future__ import annotations

import pytest

from hermes_constants import get_hermes_home
from tools.bot_desktop import lease, lease_watchdog


@pytest.fixture(autouse=True)
def _fresh():
    lease._reset_for_tests()
    lease_watchdog.reset_for_tests()
    yield
    lease._reset_for_tests()
    lease_watchdog.reset_for_tests()


def _home() -> str:
    return str(get_hermes_home())


# ── identity on the lease ─────────────────────────────────────────────────────────────────────────────


def test_acquire_records_who_holds_and_the_public_view_keeps_the_id_secret():
    taken = lease.acquire("v-1", client_id="ios-1", label="Gokhan's iPhone", watched=True)

    assert (taken.client_id, taken.label, taken.watched, taken.forced) == ("ios-1", "Gokhan's iPhone", True, False)
    public = lease.public_view(taken)
    assert public["viewer_id"] is None and public["viewer_hash"]
    assert public["label"] == "Gokhan's iPhone" and public["client_id"] == "ios-1"


def test_displacing_another_human_is_recorded_as_forced_with_who_it_was_taken_from():
    lease.acquire("v-phone", client_id="ios-1", label="Gokhan's iPhone", watched=True)

    taken = lease.acquire("v-mac", client_id="desktop-1", label="Mac", watched=True)

    assert taken.forced is True and taken.taken_from == "Gokhan's iPhone"
    assert (taken.client_id, taken.label) == ("desktop-1", "Mac")


def test_taking_a_free_screen_is_not_forced():
    assert lease.acquire("v-mac", client_id="desktop-1").forced is False


def test_release_clears_the_holder_and_can_say_why():
    lease.acquire("v-1", client_id="ios-1", label="Phone", watched=True)

    back = lease.release("v-1", reason="holder_dropped")

    assert back.holder == lease.AGENT and back.reason == "holder_dropped"
    assert (back.client_id, back.label, back.forced, back.taken_from, back.watched) == (None, None, False, None, False)


def test_stable_viewer_id_is_per_client_and_repeatable():
    assert lease.stable_viewer_id("ios-1") == lease.stable_viewer_id("ios-1")
    assert lease.stable_viewer_id("ios-1") != lease.stable_viewer_id("ios-2")
    assert "ios-1" not in lease.stable_viewer_id("ios-1")


def test_a_lease_file_from_an_older_build_still_loads():
    lease.acquire("v-1")
    path = get_hermes_home() / "bot-desktop" / "lease.json"
    path.write_text('{"holder": "human", "viewer_id": "old", "since": 1.0, "reason": "", "epoch": 3}')
    held = lease.get()
    assert (held.holder, held.viewer_id, held.watched, held.client_id) == ("human", "old", False, None)


# ── watchdog ──────────────────────────────────────────────────────────────────────────────────────────


def test_holder_without_a_socket_gets_control_taken_back_after_the_grace_period():
    lease.acquire("v-1", client_id="ios-1", label="Phone", watched=True)

    assert lease_watchdog.check(_home(), now=100.0) is None  # the clock starts here
    assert lease_watchdog.check(_home(), now=129.0) is None
    released = lease_watchdog.check(_home(), now=130.0)

    assert released is not None and released.holder == lease.AGENT and released.reason == "holder_dropped"
    assert lease.get().holder == lease.AGENT


def test_reconnecting_inside_the_window_keeps_the_lease_and_resets_the_clock():
    lease.acquire("v-1", watched=True)
    lease_watchdog.check(_home(), now=0.0)

    lease_watchdog.attach(_home(), "v-1")  # the same stable id comes back
    assert lease_watchdog.check(_home(), now=29.0) is None
    lease_watchdog.detach(_home(), "v-1")  # and drops again

    assert lease_watchdog.check(_home(), now=40.0) is None  # a fresh 30 s starts at the first absent tick
    assert lease_watchdog.check(_home(), now=69.0) is None
    assert lease_watchdog.check(_home(), now=70.0) is not None


def test_a_holder_with_a_live_socket_is_never_reclaimed():
    lease.acquire("v-1", watched=True)
    lease_watchdog.attach(_home(), "v-1")
    lease_watchdog.attach(_home(), "v-1")  # two windows of one client
    lease_watchdog.detach(_home(), "v-1")

    assert lease_watchdog.check(_home(), now=0.0) is None
    assert lease_watchdog.check(_home(), now=10_000.0) is None
    assert lease.get().holder == lease.HUMAN


def test_a_takeover_from_the_cli_is_never_reclaimed():
    """No viewer socket exists for it, so absence proves nothing."""
    lease.acquire("cli-viewer", reason="probe")  # watched defaults to False

    assert lease_watchdog.check(_home(), now=0.0) is None
    assert lease_watchdog.check(_home(), now=10_000.0) is None
    assert lease.get().holder == lease.HUMAN


def test_the_agent_holding_resets_the_clock():
    lease.acquire("v-1", watched=True)
    lease_watchdog.check(_home(), now=0.0)
    lease.release("v-1")
    lease_watchdog.check(_home(), now=20.0)  # agent holds: nothing to watch
    lease.acquire("v-2", watched=True)

    assert lease_watchdog.check(_home(), now=40.0) is None  # a new holder gets its own full window
    assert lease_watchdog.check(_home(), now=69.0) is None
    assert lease_watchdog.check(_home(), now=70.0) is not None


def test_a_takeover_that_races_the_watchdog_stays_with_the_new_holder(monkeypatch):
    lease.acquire("v-old", watched=True)
    lease_watchdog.check(_home(), now=0.0)
    real_release = lease.release

    def racing_release(viewer_id=None, **kwargs):
        lease.acquire("v-new", client_id="mac", watched=True)  # lands between the watchdog's read and its release
        return real_release(viewer_id, **kwargs)

    monkeypatch.setattr(lease, "release", racing_release)

    assert lease_watchdog.check(_home(), now=31.0) is None
    assert lease.get().viewer_id == "v-new" and lease.get().holder == lease.HUMAN
