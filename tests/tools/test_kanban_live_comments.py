"""A comment added to a RUNNING kanban task reaches the worker's next model request.

``inject_new_comments_from_env`` is wired into the tool executor's per-batch finalize (right before the steer
drain), so a note written while the worker runs is read at the next tool boundary, once, authored by whoever wrote
it, never for a finished task, and at no cost off a worker. The end-to-end test drives a REAL AIAgent loop and a
real kanban database against a scripted local model server.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
import tools.kanban_tools as kt
from tests.tools.stub_openai_server import Reply, StubServer, user_texts


class FakeAgent:
    def __init__(self):
        self.steers: list[str] = []

    def steer(self, text: str) -> bool:
        self.steers.append(text)
        return True


@pytest.fixture
def worker_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    kt._comment_watermark.clear()
    kt._comment_poll_last_attempt = 0.0
    return home


def _task(title="live task", **comments):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title=title)
        for author, body in comments.items():
            kb.add_comment(conn, tid, author=author, body=body)
        return tid
    finally:
        conn.close()


def _comment(tid, author, body):
    conn = kbc.connect()
    try:
        kb.add_comment(conn, tid, author=author, body=body)
    finally:
        conn.close()


def _set_status(tid, status):
    conn = kbc.connect()
    try:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        conn.commit()
    finally:
        conn.close()


def _as_worker(monkeypatch, tid):
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "worker-bot")
    kt._comment_poll_last_attempt = 0.0


# ── unit behaviour ───────────────────────────────────────────────────────────────────────────


def test_seed_at_worker_start_means_a_note_before_the_first_poll_is_not_lost(worker_home, monkeypatch):
    tid = _task(desktop="already in the context")
    _as_worker(monkeypatch, tid)
    assert kt.seed_comment_watermark_from_env() is True
    _comment(tid, "desktop", "added after spawn, before the first tool boundary")  # lazy seeding would swallow this
    agent = FakeAgent()
    assert kt.inject_new_comments_from_env(agent) is True
    assert len(agent.steers) == 1 and "before the first tool boundary" in agent.steers[0]
    assert "already in the context" not in agent.steers[0]


def test_each_comment_is_delivered_exactly_once(worker_home, monkeypatch):
    tid = _task()
    _as_worker(monkeypatch, tid)
    kt.seed_comment_watermark_from_env()
    agent = FakeAgent()
    _comment(tid, "desktop", "first note")
    assert kt.inject_new_comments_from_env(agent) is True
    kt._comment_poll_last_attempt = 0.0
    assert kt.inject_new_comments_from_env(agent) is False
    _comment(tid, "voice", "second note")
    kt._comment_poll_last_attempt = 0.0
    assert kt.inject_new_comments_from_env(agent) is True
    assert [("first note" in s, "second note" in s) for s in agent.steers] == [(True, False), (False, True)]
    assert "- voice: second note" in agent.steers[1]  # authored by the real writer, not forged


@pytest.mark.parametrize("status", ["done", "archived"])
def test_a_terminal_task_receives_nothing(worker_home, monkeypatch, status):
    tid = _task()
    _as_worker(monkeypatch, tid)
    kt.seed_comment_watermark_from_env()
    _comment(tid, "desktop", "too late")
    _set_status(tid, status)
    agent = FakeAgent()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []


def test_polling_is_rate_limited_and_free_off_a_worker(worker_home, monkeypatch):
    tid = _task()
    _as_worker(monkeypatch, tid)
    kt.seed_comment_watermark_from_env()
    reads = []
    real_board = kt._board

    def counting_board(*a, **k):
        reads.append(1)
        return real_board(*a, **k)

    monkeypatch.setattr(kt, "_board", counting_board)
    agent = FakeAgent()
    kt.inject_new_comments_from_env(agent)
    kt.inject_new_comments_from_env(agent)
    kt.inject_new_comments_from_env(agent)
    assert len(reads) == 1  # three tool boundaries inside the poll interval: one board read
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    reads.clear()
    kt._comment_poll_last_attempt = 0.0
    assert kt.inject_new_comments_from_env(agent) is False
    assert reads == []  # no worker, no board access


# ── end to end: a real agent loop ────────────────────────────────────────────────────────────


def test_comment_written_while_the_worker_runs_shows_up_in_its_next_model_request(worker_home, monkeypatch, tmp_path):
    from run_agent import AIAgent

    tid = _task(desktop="the original brief")
    _as_worker(monkeypatch, tid)
    monkeypatch.setattr(kt, "_COMMENT_POLL_MIN_INTERVAL_SECONDS", 0.0)  # the 6s cadence has its own test above
    probe = tmp_path / "notes.txt"
    probe.write_text("hello", encoding="utf-8")

    def script(index, messages):
        if index == 0:  # the model asks for a tool; while that request is in flight an operator writes a comment
            return Reply(text="reading", tool_name="read_file", tool_args={"path": str(probe)},
                         on_request=lambda: _comment(tid, "desktop", "actually focus on the v2 API"))
        return Reply(text="done, using the v2 API")

    server = StubServer(script)
    try:
        kt.seed_comment_watermark_from_env()
        agent = AIAgent(api_key="stub", base_url=server.base_url, provider="openrouter", api_mode="chat_completions",
                        model="stub-model", platform="cli", quiet_mode=True, skip_context_files=True, skip_memory=True,
                        save_trajectories=False, enabled_toolsets=["file"], session_id="worker-sess")
        try:
            result = agent.run_conversation("do the task")
        finally:
            agent.close()
    finally:
        server.close()

    assert result["final_response"] == "done, using the v2 API"
    first, second = (user_texts(messages) for _, messages in server.requests[:2])
    assert not any("v2 API" in t for t in first)
    note = next(t for t in second if "v2 API" in t)
    assert "New note on your kanban task from the operator" in note and "- desktop: actually focus on the v2 API" in note
    assert not any("the original brief" in t for t in second)  # history is not replayed as a note
