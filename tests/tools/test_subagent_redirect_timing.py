"""steer ``mode='redirect'`` takes effect during a slow model request; ``queue`` waits for a tool boundary.

A real AIAgent loop against a scripted local model server whose first answer takes a few seconds. A queued steer
cannot reach that request (there is no tool batch to land on), so the child finishes and the text is reported as
``pending_steer``/missed; a redirect cancels the in-flight request and the correction is in the very next request.
"""
import threading
import time

import pytest

from tests.tools.stub_openai_server import Reply, StubServer, user_texts
from tools.delegate_tool_registry import _register_subagent, _unregister_subagent, steer_subagent

SLOW = 6.0


def _run(monkeypatch, tmp_path, mode):
    from run_agent import AIAgent

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    server = StubServer(lambda index, messages: Reply(text="first answer", delay=SLOW) if index == 0 else Reply(text="revised answer"))
    agent = AIAgent(api_key="stub", base_url=server.base_url, provider="openrouter", api_mode="chat_completions",
                    model="stub-model", platform="cli", quiet_mode=True, skip_context_files=True, skip_memory=True,
                    save_trajectories=False, enabled_toolsets=["file"], session_id=f"slow-{mode}")
    outcome: dict = {}

    def work():
        outcome["result"] = agent.run_conversation("research the topic")
        outcome["finished_at"] = time.monotonic()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.requests and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.requests, "the model request never started"
        _register_subagent({"subagent_id": "sa-slow", "agent": agent, "goal": "g"})
        steered_at = time.monotonic()
        accepted = steer_subagent("sa-slow", "forget that, answer in French", mode=mode)
        thread.join(timeout=30)
        assert not thread.is_alive()
    finally:
        _unregister_subagent("sa-slow")
        agent.close()
        server.close()
    return accepted, outcome, server.requests, steered_at


def test_redirect_cancels_the_slow_request_and_the_correction_is_in_the_next_one(monkeypatch, tmp_path):
    accepted, outcome, requests, steered_at = _run(monkeypatch, tmp_path, "redirect")
    assert accepted is True
    assert len(requests) == 2
    assert requests[1][0] - steered_at < SLOW / 2  # the retry went out right away, not after the slow answer
    assert any("answer in French" in t for t in user_texts(requests[1][1]))
    assert outcome["result"]["final_response"] == "revised answer"
    assert outcome["finished_at"] - steered_at < SLOW - 1  # the turn never waited out the slow request


def test_queue_mode_cannot_reach_a_request_already_in_flight(monkeypatch, tmp_path):
    accepted, outcome, requests, steered_at = _run(monkeypatch, tmp_path, "queue")
    assert accepted is True
    assert len(requests) == 1  # no tool boundary was ever reached
    assert outcome["finished_at"] - steered_at >= SLOW - 1.5  # the slow request ran to completion first
    assert outcome["result"]["final_response"] == "first answer"
    assert outcome["result"].get("pending_steer") == "forget that, answer in French"  # surfaced as missed_steer upstream
