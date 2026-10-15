"""``app.action`` server request: wiring, wire contract and the no-hang guarantee.

The renderer's ``app.action`` handler ships in the desktop bundle while the ``app_action``
tool is offered by this backend, and the two update on different clocks. An app build
that does not answer must cost one short probe, never a full 45s deadline per action
(same ladder as ``preview.act``, #94272), and must fail honestly.
"""

import json
import time

import pytest

from tui_gateway import server
from tui_gateway.contracts import server_requests as contracts


@pytest.fixture
def session(monkeypatch):
    record = {}
    monkeypatch.setitem(server._sessions, "s1", record)
    return record


@pytest.fixture
def bridge(monkeypatch):
    """Record every ``_ask`` (server request) and serve canned answers."""
    calls = []

    def fake_ask(event, sid, payload, timeout=None, **_kw):
        answer = fake_ask.answers.pop(0) if fake_ask.answers else ""
        calls.append({"event": event, "sid": sid, "payload": payload, "timeout": timeout})
        return answer

    fake_ask.answers = []
    fake_ask.calls = calls
    monkeypatch.setattr(server, "_ask", fake_ask)
    return fake_ask


@pytest.fixture(autouse=True)
def no_leftover_reprobe():
    yield
    server._app_action_reprobe.clear()


def test_request_is_declared_on_the_wire_contract():
    from tui_gateway.contracts.registry import SERVER_REQUESTS

    assert "app.action" in set(SERVER_REQUESTS)
    spec = contracts.AppActionRequestParams(session_id="s1", action="tab.open", args={"url": "https://example.com"})
    assert spec.model_dump(exclude_none=True) == {
        "session_id": "s1", "action": "tab.open", "args": {"url": "https://example.com"}}
    assert contracts.AppActionRequestParams(session_id="s1", action="tab.list").args is None


def test_agent_callbacks_route_to_the_app_action_bridge(monkeypatch):
    # agent_callbacks bodies are rebound onto server.py globals, so the bridge resolves there.
    seen = []
    monkeypatch.setattr(server, "_app_action_request", lambda sid, payload: seen.append((sid, payload)) or "{}")
    cbs = server._agent_cbs("s1")
    cbs["app_action_callback"]({"action": "tab.list"})
    assert seen == [("s1", {"action": "tab.list"})]


def test_agent_accepts_and_forwards_the_callback():
    from agent import agent_init

    assert "app_action_callback" in agent_init._CALLBACK_PARAMS


def test_inline_executor_hands_the_agent_callback_to_the_tool():
    from agent.inline_tool_executors import _RAW_INLINE_TOOL_EXECUTORS

    assert "app_action" in _RAW_INLINE_TOOL_EXECUTORS


def test_first_action_is_probed_on_a_short_deadline(session, bridge):
    bridge.answers = [json.dumps({"ok": True})]
    server._app_action_request("s1", {"action": "tab.list"})

    assert bridge.calls[0]["event"] == "app.action"
    assert bridge.calls[0]["payload"] == {"action": "tab.list"}
    assert bridge.calls[0]["timeout"] == server._APP_ACTION_PROBE_TIMEOUT_S
    assert server._APP_ACTION_PROBE_TIMEOUT_S <= 10 < server._APP_ACTION_TIMEOUT_S


def test_a_client_that_answers_gets_the_full_deadline_back(session, bridge):
    bridge.answers = [json.dumps({"ok": True}), json.dumps({"ok": True})]
    server._app_action_request("s1", {"action": "tab.list"})
    server._app_action_request("s1", {"action": "tab.open", "args": {"url": "x"}})

    assert bridge.calls[1]["timeout"] == server._APP_ACTION_TIMEOUT_S


def test_the_answer_is_returned_verbatim(session, bridge):
    answer = json.dumps({"ok": False, "detail": "unknown action id: nope"})
    bridge.answers = [answer]
    assert server._app_action_request("s1", {"action": "nope"}) == answer


def test_unanswered_probe_returns_an_actionable_failure(session, bridge):
    result = json.loads(server._app_action_request("s1", {"action": "tab.list"}))

    assert result["ok"] is False and result["success"] is False
    assert "Update the Hermes app" in result["detail"]


def test_repeat_actions_short_circuit_instead_of_stalling_again(session, bridge):
    server._app_action_request("s1", {"action": "tab.list"})
    for action in ("tab.open", "tab.close", "pane.split"):
        assert json.loads(server._app_action_request("s1", {"action": action}))["ok"] is False

    assert len(bridge.calls) == 1


def test_cooldown_expiry_reprobes_once_then_restores_the_full_deadline(session, bridge, monkeypatch):
    monkeypatch.setattr(server, "_APP_ACTION_REPROBE_COOLDOWN_S", 0.0)
    server._app_action_request("s1", {"action": "tab.list"})

    bridge.answers = [json.dumps({"ok": True})]
    assert json.loads(server._app_action_request("s1", {"action": "tab.list"}))["ok"] is True
    assert bridge.calls[1]["timeout"] == server._APP_ACTION_PROBE_TIMEOUT_S

    server._app_action_request("s1", {"action": "tab.list"})
    assert bridge.calls[2]["timeout"] == server._APP_ACTION_TIMEOUT_S


def test_within_cooldown_a_second_caller_fails_fast(session, bridge):
    server._app_action_request("s1", {"action": "tab.list"})
    session["app_action_bridge_retry_at"] = 0.0
    server._app_action_reprobe["s1"] = object()

    assert json.loads(server._app_action_request("s1", {"action": "tab.list"}))["ok"] is False
    assert len(bridge.calls) == 1


def test_a_new_session_reprobes(bridge, monkeypatch):
    monkeypatch.setitem(server._sessions, "dead", {})
    monkeypatch.setitem(server._sessions, "fresh", {})
    server._app_action_request("dead", {"action": "tab.list"})

    bridge.answers = [json.dumps({"ok": True})]
    assert json.loads(server._app_action_request("fresh", {"action": "tab.list"}))["ok"] is True


def test_a_proven_client_is_not_condemned_by_one_miss(session, bridge):
    bridge.answers = [json.dumps({"ok": True})]
    server._app_action_request("s1", {"action": "tab.list"})
    server._app_action_request("s1", {"action": "tab.list"})  # unanswered once

    assert session["app_action_bridge"] == "answered"
    server._app_action_request("s1", {"action": "tab.list"})
    assert bridge.calls[2]["timeout"] == server._APP_ACTION_TIMEOUT_S


def test_no_window_means_a_fast_honest_failure_end_to_end(session, monkeypatch):
    """Real tool + real bridge + a silent ``server_requests.send`` that honours its timeout.

    Shrinks the probe deadline so the test is fast, then checks the model-visible result and
    that the second call returns without waiting at all."""
    from tools import app_action_tool as aat
    from tui_gateway import server_requests

    waits = []

    def silent_send(method, sid, params, timeout=None, **_kw):
        waits.append(timeout)
        time.sleep(timeout)

    monkeypatch.setattr(server_requests, "send", silent_send)
    monkeypatch.setattr(server, "_APP_ACTION_PROBE_TIMEOUT_S", 0.2)

    def callback(payload):
        return server._app_action_request("s1", payload)

    started = time.monotonic()
    first = json.loads(aat.app_action_tool(action="tab.list", callback=callback))
    second_started = time.monotonic()
    second = json.loads(aat.app_action_tool(action="tab.list", callback=callback))

    assert 0.2 <= second_started - started < 1.0
    assert time.monotonic() - second_started < 0.1
    assert waits == [0.2]
    assert first["ok"] is False or "error" in first
    assert second["ok"] is False
