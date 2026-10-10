"""``subagent.steer`` modes and fallback, the relaxed (lineage) authority, and ``subagent.resume``.

Real gateway dispatch and the real registries; children are small doubles. The guard rails of the resume itself
(window, size, ownership, running, claim) are covered at the tool level in tests/tools/test_delegate_resume.py; here
the RPC must hand the SAME call the model makes the right session identity and report its outcome.
"""
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def runtime(monkeypatch):
    from tui_gateway import server
    from tools import async_delegation, delegate_tool_registry

    transport = SimpleNamespace(write=lambda frame: True)
    parent_agent = SimpleNamespace(session_id="parent-sess")
    owner = {"session_key": "parent-sess", "history": [], "transport": transport, "agent": parent_agent}
    monkeypatch.setattr(server, "_sessions", {"ui-owner": owner})
    registries = (delegate_tool_registry._active_subagents, delegate_tool_registry._recent_subagents, async_delegation._records)
    saved = [(reg, dict(reg)) for reg in registries]
    for reg in registries:
        reg.clear()

    def call(method, *, via=transport, **params):
        return server.dispatch({"id": 1, "method": method,
                                "params": {"session_id": "ui-owner", **params}}, transport=via)

    yield server, owner, transport, call
    for reg, snapshot in saved:
        reg.clear()
        reg.update(snapshot)


class _Child:
    """A live child double: records which delivery channel the text took."""

    def __init__(self, parent_session="parent-sess", session_id="child-sess"):
        self._subagent_id = "child"
        self._delegate_depth = 1
        self._parent_session_id = parent_session
        self.session_id = session_id
        self.model = "test"
        self.steered: list[str] = []
        self.redirected: list[str] = []

    def steer(self, text):
        self.steered.append(text)
        return True

    def redirect(self, text):
        self.redirected.append(text)
        return True


def _spawn(owner, transport, child=None):
    from tools.delegate_tool_child_run import _register_child
    child = child or _Child()
    _register_child(child, None, "owned task", owner_session_id="ui-owner", owner_transport=transport,
                    owner_session_record=owner)
    return child


# ── steer mode ───────────────────────────────────────────────────────────────────────────────


def test_steer_defaults_to_queue_and_redirect_mode_uses_redirect(runtime):
    _server, owner, transport, call = runtime
    child = _spawn(owner, transport)
    queued = call("subagent.steer", subagent_id="child", text="after this tool")["result"]
    assert queued["status"] == "queued" and queued["mode"] == "queue"
    redirected = call("subagent.steer", subagent_id="child", text="stop, do X instead", mode="redirect")["result"]
    assert redirected["status"] == "queued" and redirected["mode"] == "redirect"
    assert child.steered == ["after this tool"] and child.redirected == ["stop, do X instead"]


def test_redirect_falls_back_to_queue_when_no_turn_is_live(runtime):
    _server, owner, transport, call = runtime
    child = _spawn(owner, transport)
    child.redirect = lambda text: False  # AIAgent.redirect() returns False between model requests
    assert call("subagent.steer", subagent_id="child", text="change course", mode="redirect")["result"]["status"] == "queued"
    assert child.steered == ["change course"]


def test_unknown_mode_is_an_error(runtime):
    _server, owner, transport, call = runtime
    _spawn(owner, transport)
    assert "error" in call("subagent.steer", subagent_id="child", text="x", mode="shout")


# ── fallback for an undeliverable steer ──────────────────────────────────────────────────────


def test_steer_to_a_finished_child_returns_a_resume_fallback_with_the_same_text(runtime):
    from tools.delegate_tool_registry import _unregister_subagent
    _server, owner, transport, call = runtime
    child = _spawn(owner, transport)
    _unregister_subagent("child", agent=child)
    result = call("subagent.steer", subagent_id="child", text="also cover pricing", mode="redirect")["result"]
    assert result["status"] == "rejected"
    assert result["fallback"] == {"action": "resume", "resume_session_id": "child-sess", "goal": "also cover pricing"}


def test_fallback_never_names_another_conversations_child(runtime):
    from tools.delegate_tool_registry import _unregister_subagent
    _server, owner, transport, call = runtime
    child = _spawn(owner, transport, _Child(parent_session="somebody-elses-sess", session_id="their-child"))
    _unregister_subagent("child", agent=child)
    result = call("subagent.steer", subagent_id="child", text="peek")["result"]
    assert result["status"] == "rejected" and "fallback" not in result


def test_tool_steer_of_a_finished_child_carries_the_fallback(runtime):
    from tools.delegate_tool import _handle_control_action
    from tools.delegate_tool_registry import _unregister_subagent
    _server, owner, transport, _call = runtime
    child = _spawn(owner, transport)
    _unregister_subagent("child", agent=child)
    out = json.loads(_handle_control_action("steer", "child", "also cover pricing", owner["agent"], "redirect"))
    assert out["fallback"] == {"action": "resume", "resume_session_id": "child-sess", "goal": "also cover pricing"}
    live = _Child()
    _spawn(owner, transport, live)
    ok = json.loads(_handle_control_action("steer", "child", "now", owner["agent"], "redirect"))
    assert ok["status"] == "queued" and ok["mode"] == "redirect" and live.redirected == ["now"]


# ── authority after a Desktop reconnect (#114909) ────────────────────────────────────────────


def _reconnect(server, owner, transport, *, agent_session="parent-sess"):
    """The UI session id survives but the live record is rebuilt (new generation), as on a Desktop reconnect."""
    server._sessions["ui-owner"] = {**owner, "agent": SimpleNamespace(session_id=agent_session)}
    return server._sessions["ui-owner"]


def test_steer_survives_a_reconnect_for_the_same_conversation(runtime):
    server, owner, transport, call = runtime
    child = _spawn(owner, transport)
    _reconnect(server, owner, transport)
    result = call("subagent.steer", subagent_id="child", text="still mine", mode="redirect")["result"]
    assert result["status"] == "queued" and child.redirected == ["still mine"]


def test_steer_after_a_reconnect_is_refused_for_an_unrelated_session(runtime):
    server, owner, transport, call = runtime
    child = _spawn(owner, transport)
    _reconnect(server, owner, transport, agent_session="some-other-conversation")
    assert call("subagent.steer", subagent_id="child", text="not yours")["result"]["status"] == "rejected"
    assert child.steered == [] and child.redirected == []


def test_steer_from_a_transport_not_attached_to_the_session_is_refused_even_for_the_same_conversation(runtime):
    server, owner, transport, call = runtime
    child = _spawn(owner, transport)
    _reconnect(server, owner, transport)
    stranger = SimpleNamespace(write=lambda frame: True)
    assert "error" in call("subagent.steer", subagent_id="child", text="x", via=stranger) or \
        call("subagent.steer", subagent_id="child", text="x", via=stranger)["result"]["status"] == "rejected"
    assert child.steered == [] and child.redirected == []


# ── subagent.resume ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def dispatcher(monkeypatch):
    """Replace the tool entry point the RPC calls; record what it saw and the identity bound while it ran."""
    import tools.delegate_tool as dt
    from gateway.session_context import get_session_env
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs, ui_session=get_session_env("HERMES_UI_SESSION_ID", ""))
        return seen.get("reply") or json.dumps({"status": "dispatched", "mode": "background", "delegation_id": "deleg_1",
                                                  "child_session_ids": [kwargs["resume_session_id"]]})

    monkeypatch.setattr(dt, "delegate_task", fake)
    return seen


def test_resume_rpc_dispatches_the_same_call_the_model_makes(runtime, dispatcher):
    _server, owner, _transport, call = runtime
    result = call("subagent.resume", resume_session_id="child-sess", goal="turn that into an install draft")["result"]
    assert result == {"status": "dispatched", "resume_session_id": "child-sess", "delegation_id": "deleg_1",
                      "child_session_ids": ["child-sess"]}
    assert dispatcher["action"] == "resume" and dispatcher["background"] is True
    assert dispatcher["resume_session_id"] == "child-sess" and dispatcher["goal"] == "turn that into an install draft"
    assert dispatcher["parent_agent"] is owner["agent"]
    assert dispatcher["ui_session"] == "ui-owner"  # the identity a turn binds, so the completion routes back


def test_resume_rpc_reports_the_honest_refusal(runtime, dispatcher):
    _server, _owner, _transport, call = runtime
    dispatcher["reply"] = json.dumps({"error": "Subagent session 'x' is still running (subagent_id 'sa-1')."})
    result = call("subagent.resume", resume_session_id="x", goal="more")["result"]
    assert result["status"] == "rejected" and "still running" in result["error"]


def test_resume_rpc_requires_params_and_an_attached_transport(runtime, dispatcher):
    _server, _owner, _transport, call = runtime
    assert "error" in call("subagent.resume", goal="more")
    assert "error" in call("subagent.resume", resume_session_id="x")
    assert "error" in call("subagent.resume", resume_session_id="x", goal="more", via=SimpleNamespace(write=lambda f: True))
    assert "action" not in dispatcher  # nothing was dispatched for any of them
