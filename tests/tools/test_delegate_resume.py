"""delegate_task resume: continue a FINISHED child on its own session, and find children to continue.

A child is a session row in the parent's state.db. ``action='resume'`` rebuilds a child agent on that SAME
session id, replays the stored transcript and runs the follow-up as the next user turn (the machinery of
``hermes chat --resume``). These tests use a real SessionDB and a real AIAgent whose model loop is replaced by a
recorder, so nothing talks to a provider.
"""
import json
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB
from tools import delegate_tool as dt
from tools import delegate_tool_resume as resume
from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock, _register_subagent, _unregister_subagent


@pytest.fixture
def db(tmp_path):
    database = SessionDB(db_path=tmp_path / "state.db")
    yield database
    database.close()


class _Parent:
    def __init__(self, db, session_id="parent-1"):
        self._session_db = db
        self.session_id = session_id


def _child_session(db, sid, parent="parent-1", *, goal="research competitor pricing", messages=2, age_s=60.0,
                   ended=True, delegate=True):
    cfg = {"_delegate_from": parent} if delegate else {}
    db.ensure_session(parent, "cli")  # parent_session_id is a foreign key
    db.create_session(sid, source="subagent", parent_session_id=parent, model_config=cfg)
    db.set_session_title(sid, f"Subagent: {goal}")
    now = time.time() - age_s
    db.append_message(sid, "user", goal, timestamp=now - 5)
    for i in range(messages - 1):
        db.append_message(sid, "assistant", f"finding {i}", timestamp=now - 4 + i)
    if ended:
        db.end_session(sid, "agent_close")
    db._execute_write(lambda c: c.execute(
        "UPDATE sessions SET started_at = ?, ended_at = CASE WHEN ended_at IS NULL THEN NULL ELSE ? END, "
        "last_activity_at = ? WHERE id = ?", (now - 6, now, now, sid)))


# ── validation ───────────────────────────────────────────────────────────────────────────────


def test_prepare_resume_returns_same_session_and_stored_history(db):
    _child_session(db, "child-a", messages=2)
    plan = resume.prepare_resume(_Parent(db), "child-a")
    assert plan.session_id == "child-a"
    assert [m["role"] for m in plan.history] == ["user", "assistant"]
    assert plan.history[0]["content"] == "research competitor pricing"


@pytest.mark.parametrize("setup, fragment", [
    (lambda db: None, "No subagent session 'ghost'"),
    (lambda db: _child_session(db, "ghost", delegate=False), "not a delegated subagent session"),
    (lambda db: _child_session(db, "ghost", parent="somebody-else"), "different conversation"),
    (lambda db: _child_session(db, "ghost", age_s=3600 * 30), "beyond the 24h resume window"),
])
def test_prepare_resume_refusals_are_honest_sentences(db, setup, fragment):
    setup(db)
    with pytest.raises(resume.ResumeError) as err:
        resume.prepare_resume(_Parent(db), "ghost")
    assert fragment in str(err.value)


def test_prepare_resume_refuses_a_running_child_and_points_at_steer(db):
    _child_session(db, "child-run")

    class _Agent:
        session_id = "child-run"

    _register_subagent({"subagent_id": "sa-0-live", "agent": _Agent(), "goal": "g"})
    try:
        with pytest.raises(resume.ResumeError) as err:
            resume.prepare_resume(_Parent(db), "child-run")
    finally:
        _unregister_subagent("sa-0-live")
    assert "still running" in str(err.value) and "sa-0-live" in str(err.value) and "steer" in str(err.value)


def test_prepare_resume_refuses_an_oversized_transcript(db, monkeypatch):
    _child_session(db, "child-big", messages=5)
    monkeypatch.setattr("hermes_state.resolved_max_resume_messages", lambda: 2)
    with pytest.raises(resume.ResumeError) as err:
        resume.prepare_resume(_Parent(db), "child-big")
    assert "too large to resume" in str(err.value)


def test_prepare_resume_follows_the_parent_through_a_compression_rotation(db):
    _child_session(db, "child-old", parent="parent-before-rotation")
    db.ensure_session("parent-before-rotation", "cli")
    db.create_session("parent-1", source="cli", parent_session_id="parent-before-rotation")
    db.end_session("parent-before-rotation", "compression")
    assert resume.prepare_resume(_Parent(db), "child-old").session_id == "child-old"


def test_claim_blocks_a_second_resume_until_released():
    assert resume.claim_resume("s1") is True
    assert resume.claim_resume("s1") is False
    resume.release_resume("s1")
    assert resume.claim_resume("s1") is True
    resume.release_resume("s1")


# ── listing ──────────────────────────────────────────────────────────────────────────────────


def test_finished_children_are_newest_first_bounded_and_windowed(db):
    for n in range(14):
        _child_session(db, f"c{n:02d}", goal=f"topic {n}", age_s=100.0 + n * 10)
    _child_session(db, "stale", goal="an old topic", age_s=3600 * 30)
    _child_session(db, "foreign", goal="someone else's topic", parent="other")
    entries = resume.finished_children(_Parent(db))
    assert [e["child_session_id"] for e in entries] == [f"c{n:02d}" for n in range(resume.FINISHED_LIST_LIMIT)]
    first = entries[0]
    assert first["goal"] == "topic 0" and first["status"] == "finished" and first["finished_at"].endswith("Z")
    assert set(first) == {"child_session_id", "goal", "status", "finished_at", "messages"}


def test_finished_children_skip_running_ones_and_cap_long_goals(db):
    _child_session(db, "c-run", goal="x" * 85)
    _child_session(db, "c-done", goal="y" * 85)

    class _Agent:
        session_id = "c-run"

    _register_subagent({"subagent_id": "sa-1-live", "agent": _Agent(), "goal": "g"})
    try:
        entries = resume.finished_children(_Parent(db))
    finally:
        _unregister_subagent("sa-1-live")
    assert [e["child_session_id"] for e in entries] == ["c-done"]
    assert len(entries[0]["goal"]) <= 100


def test_list_action_includes_finished_children(db):
    _child_session(db, "c-done", goal="research pricing")
    out = json.loads(dt._handle_control_action("list", None, None, _Parent(db)))
    assert out["finished"][0]["child_session_id"] == "c-done"
    assert "action='resume'" in out["finished_note"]


# ── batch-complete message ───────────────────────────────────────────────────────────────────


def test_batch_complete_header_names_child_session_id_and_keeps_earlier_fields():
    from tools.process_registry_notifications import _format_batch_delegation

    evt = {"goals": ["research"], "results": [{
        "task_index": 0, "status": "completed", "summary": "done", "api_calls": 3, "duration_seconds": 12.5,
        "child_session_id": "20261010_1_abc", "resumed": True}]}
    text = _format_batch_delegation(evt, "deleg_x", time.time())
    assert "--- ✓ TASK 1/1: research  (status=completed, api_calls=3, 12.5s, child_session_id=20261010_1_abc, resumed) ---" in text
    assert "action='resume'" in text
    plain = _format_batch_delegation({"goals": ["g"], "results": [{"task_index": 0, "status": "completed", "summary": "s"}]},
                                     "deleg_y", time.time())
    assert "--- ✓ TASK 1/1: g  (status=completed) ---" in plain and "child_session_id" not in plain


# ── schema ───────────────────────────────────────────────────────────────────────────────────


def test_schema_advertises_resume_and_tells_the_model_when_to_use_it():
    props = dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    assert "resume" in props["action"]["enum"] and "resume_session_id" in props
    assert "FOLLOW-UP ON FINISHED WORK" in dt._build_top_level_description()


def test_resume_without_a_session_id_is_refused(db):
    out = json.loads(dt.delegate_task(action="resume", goal="more", parent_agent=_Parent(db)))
    assert "requires resume_session_id" in out["error"]


# ── through a real child agent ───────────────────────────────────────────────────────────────


@pytest.fixture
def agents(tmp_path, monkeypatch, db):
    """A real parent AIAgent on ``db`` and a recorder in place of the model loop."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  default: anthropic/claude-sonnet-4.6\n", encoding="utf-8")
    import tools.delegate_tool_config as dtc
    from run_agent import AIAgent
    monkeypatch.setattr(dt, "_load_config", dict)
    monkeypatch.setattr(dtc, "_load_config", dict)
    seen: list[dict] = []

    def fake_run(self, user_message, task_id=None, stream_callback=None, conversation_history=None, **_kw):
        self._ensure_db_session()
        seen.append({"session_id": self.session_id, "history": list(conversation_history or []),
                     "user": user_message, "title_before": self._session_db.get_session_title(self.session_id)})
        # The real per-turn titling hook: a fresh child is titled from its goal, a resumed one keeps its title.
        from agent.turn_context import _maybe_title_session_at_turn_start
        _maybe_title_session_at_turn_start(self, [{"role": "user", "content": user_message}])
        self._session_db.append_message(self.session_id, "user", user_message)
        self._session_db.append_message(self.session_id, "assistant", f"answer to: {user_message}")
        return {"final_response": f"answer to: {user_message}", "completed": True, "api_calls": 1,
                "messages": [{"role": "assistant", "content": f"answer to: {user_message}"}]}

    monkeypatch.setattr(AIAgent, "run_conversation", fake_run)
    kw = dict(api_key="k", base_url="https://openrouter.ai/api/v1", provider="openrouter", api_mode="chat_completions",
              model="anthropic/claude-sonnet-4.6", platform="cli", quiet_mode=True, skip_context_files=True,
              skip_memory=True, save_trajectories=False, enabled_toolsets=["file"])
    db.ensure_session("parent-1", "cli")
    parent = AIAgent(session_id="parent-1", session_db=db, **kw)
    yield parent, seen
    parent.close()


def _run(parent, **kwargs):
    return json.loads(dt.delegate_task(parent_agent=parent, background=False, **kwargs))


def test_resume_continues_the_same_session_with_its_history(agents, db):
    parent, seen = agents
    first = _run(parent, goal="research competitor pricing")["results"][0]
    sid = first["child_session_id"]
    assert sid and sid != "parent-1" and "resumed" not in first
    sessions_before = db._read_all("SELECT id FROM sessions ORDER BY id")

    second = _run(parent, action="resume", resume_session_id=sid, goal="turn that into an install draft")["results"][0]

    assert second["child_session_id"] == sid and second["resumed"] is True
    assert second["summary"] == "answer to: turn that into an install draft"
    assert [r["id"] for r in db._read_all("SELECT id FROM sessions ORDER BY id")] == [r["id"] for r in sessions_before]
    assert seen[1]["session_id"] == sid and seen[1]["user"] == "turn that into an install draft"
    assert [m["content"] for m in seen[1]["history"]] == [
        "research competitor pricing", "answer to: research competitor pricing"]
    assert db.get_session_title(sid) == "Subagent: research competitor pricing"  # not retitled by the follow-up
    rows = [(m["role"], m["content"]) for m in db.get_messages(sid)]
    assert rows[-2:] == [("user", "turn that into an install draft"), ("assistant", "answer to: turn that into an install draft")]


def test_resume_emits_subagent_frames_with_the_same_child_session_id_and_resumed_marker(agents):
    parent, _seen = agents
    frames: list[tuple] = []
    parent.tool_progress_callback = lambda event, name=None, preview=None, args=None, **kw: frames.append((event, kw))
    sid = _run(parent, goal="research competitor pricing")["results"][0]["child_session_id"]
    frames.clear()
    _run(parent, action="resume", resume_session_id=sid, goal="now summarise")
    lifecycle = [kw for event, kw in frames if event in ("subagent.start", "subagent.complete")]
    assert {event for event, _ in frames} >= {"subagent.start", "subagent.complete"}
    assert lifecycle and all(kw.get("child_session_id") == sid and kw.get("resumed") is True for kw in lifecycle)


def test_resume_of_an_unknown_or_foreign_session_returns_the_honest_error(agents, db):
    parent, _seen = agents
    assert "No subagent session 'nope'" in _run(parent, action="resume", resume_session_id="nope", goal="more")["error"]
    _child_session(db, "foreign-child", parent="another-parent")
    assert "different conversation" in _run(parent, action="resume", resume_session_id="foreign-child", goal="more")["error"]


def test_the_session_claim_is_released_after_a_failed_and_a_finished_resume(agents):
    parent, _seen = agents
    sid = _run(parent, goal="research competitor pricing")["results"][0]["child_session_id"]
    _run(parent, action="resume", resume_session_id=sid, goal="one more thing")
    assert resume.claim_resume(sid) is True  # free again
    resume.release_resume(sid)


def test_resume_per_task_inside_a_batch_and_duplicates_are_refused(agents):
    parent, _seen = agents
    sid = _run(parent, goal="research competitor pricing")["results"][0]["child_session_id"]
    out = _run(parent, tasks=[
        {"goal": "follow up on the research", "resume_session_id": sid},
        {"goal": "an unrelated fresh job to do"},
    ])
    by_index = {r["task_index"]: r for r in out["results"]}
    assert by_index[0]["child_session_id"] == sid and by_index[0]["resumed"] is True
    assert by_index[1]["child_session_id"] != sid and "resumed" not in by_index[1]
    dup = _run(parent, tasks=[{"goal": "follow up number one", "resume_session_id": sid},
                              {"goal": "follow up number two", "resume_session_id": sid}])
    assert "already being resumed" in dup["error"]
    assert resume.claim_resume(sid) is True  # the refused call left nothing claimed
    resume.release_resume(sid)
