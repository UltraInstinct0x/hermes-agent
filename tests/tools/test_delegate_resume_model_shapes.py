"""The model's real resume call shapes (field report: three action='resume' calls were refused, then the model
spawned a fresh child with a 'Continue SAME ...' goal and lost the finished child's context).

Root causes pinned here: ``run_agent._dispatch_delegate_task`` (the live path for model calls) dropped
``resume_session_id`` and ``mode``; and the schema has no top-level ``goal``, so the follow-up arrives in
``tasks[0].goal`` or ``message`` and was refused as "not both"."""
import pytest

# Fixtures and helpers of the resume suite (real SessionDB, real AIAgent, recorded model loop).
from tests.tools.test_delegate_resume import _run, agents, db


def test_model_dispatch_forwards_resume_session_id_and_mode(monkeypatch):
    import tools.delegate_tool as delegate_module
    from run_agent import AIAgent

    seen = {}
    monkeypatch.setattr(delegate_module, "delegate_task", lambda **kw: seen.update(kw) or "{}")
    AIAgent._dispatch_delegate_task(
        type("P", (), {"_delegate_depth": 0})(),
        {"action": "resume", "resume_session_id": "child-x", "tasks": [{"goal": "follow up"}], "mode": "redirect"})
    assert seen["resume_session_id"] == "child-x" and seen["mode"] == "redirect" and seen["action"] == "resume"


def test_resume_with_the_follow_up_in_tasks_zero_goal_and_the_id_at_top_level(agents):
    parent, seen = agents
    sid = _run(parent, goal="research competitor pricing")["results"][0]["child_session_id"]
    out = _run(parent, action="resume", resume_session_id=sid, tasks=[{"goal": "apply the review fixes"}])["results"][0]
    assert out["child_session_id"] == sid and out["resumed"] is True
    assert seen[1]["user"] == "apply the review fixes"


def test_resume_with_the_follow_up_in_message_only(agents):
    parent, seen = agents
    sid = _run(parent, goal="research competitor pricing")["results"][0]["child_session_id"]
    out = _run(parent, action="resume", resume_session_id=sid, message="apply the review fixes")["results"][0]
    assert out["child_session_id"] == sid and seen[1]["user"] == "apply the review fixes"


def test_resume_refuses_a_multi_task_call_clearly(agents):
    parent, _seen = agents
    out = _run(parent, action="resume", resume_session_id="x",
               tasks=[{"goal": "follow up one"}, {"goal": "follow up two"}])
    assert "ONE child" in out["error"]
