"""The human-request ledger: every question put to a person is recorded in state.db (who answered, how it ended),
``request.resolved`` tells the other clients, and a restart tombstones what the dead process left open.

Drives ``server_requests`` through its public API with fake sinks (a frame list and an event list) and a real
SQLite file under the isolated HERMES_HOME; the event fake runs the same contract check the server does."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from tui_gateway import client_identity, human_requests, server_requests
from tui_gateway.contracts import registry as contracts

DEAD_PID = 4_000_000  # above every platform's pid ceiling that matters here


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "profile-home"
    home.mkdir()
    frames, events = [], []

    def emit(event, sid, payload):
        contracts.check_payload(event, payload)
        events.append((event, sid, payload))
        return True

    monkeypatch.setattr(server_requests, "_write", frames.append)
    monkeypatch.setattr(server_requests, "_emit", emit)
    monkeypatch.setattr(server_requests, "_ledger_context", lambda sid: {
        "home": home, "profile": "default", "session_id": f"stored-{sid}", "session_key": f"key-{sid}"})
    monkeypatch.setattr(human_requests, "_swept_homes", set())
    server_requests.reset_for_tests()
    client_identity.reset_for_tests()
    yield SimpleNamespace(home=home, frames=frames, events=events)
    server_requests.reset_for_tests()
    client_identity.reset_for_tests()


class _Peer:
    pass


def _identified(client_id, label, kind="ios"):
    peer = _Peer()
    client_identity.declare(peer, client_id=client_id, label=label, kind=kind)
    return peer


def _rows(home):
    conn = human_requests._connect(home)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM human_requests ORDER BY created_at").fetchall()]
    finally:
        conn.close()


def _approval_params(request_id="ap-1", command="systemctl restart nginx"):
    return {"request_id": request_id, "command": command, "description": "restart the web server",
            "choices": ["once", "session", "always", "deny"]}


def _events(env, name):
    return [payload for event, _, payload in env.events if event == name]


# ── approvals ─────────────────────────────────────────────────────────────────────────────────────────


def test_approval_answered_by_response_frame_names_the_answerer(env):
    phone = _identified("ios-7f3a", "Gokhan's iPhone")
    server_requests.send_async("approval", "s1", _approval_params(), lambda result: None)
    request_id = env.frames[0]["id"]

    assert server_requests.resolve_response({"jsonrpc": "2.0", "id": request_id, "result": {"choice": "once"}}, phone)

    (row,) = _rows(env.home)
    assert (row["request_id"], row["kind"], row["state"], row["outcome"]) == (request_id, "approval", "answered", "once")
    assert (row["answered_by_client_id"], row["answered_by_label"], row["answered_via"]) == (
        "ios-7f3a", "Gokhan's iPhone", "frame")
    assert row["session_id"] == "stored-s1" and row["session_key"] == "key-s1" and row["owner_pid"] == os.getpid()
    assert "systemctl restart nginx" in row["prompt"] and row["expires_at"] is not None
    (resolved,) = _events(env, "request.resolved")
    assert resolved["id"] == request_id and resolved["choice"] == "once" and resolved["session_id"] == "s1"
    assert resolved["answered_by"] == {"client_id": "ios-7f3a", "label": "Gokhan's iPhone", "kind": "ios"}


def test_approval_answered_over_rpc_is_attributed_and_still_cancels_for_old_clients(env):
    """``approval.respond`` reaches the agent's queue, which settles the request later: the answerer travels as a note."""
    desktop = _identified("desktop-main", "Mac", "desktop")
    settle = server_requests.send_async("approval", "s1", _approval_params(), lambda result: None)
    server_requests.note_approval_answer("s1", "ap-1", "session", transport=desktop)

    settle("resolved")

    (row,) = _rows(env.home)
    assert (row["state"], row["outcome"], row["answered_via"], row["answered_by_client_id"]) == (
        "answered", "session", "approval.respond", "desktop-main")
    assert [e for e, _, _ in env.events] == ["request.cancel", "request.resolved"]
    assert env.events[0][2]["reason"] == "resolved"
    assert env.events[1][2]["choice"] == "session"


def test_approval_resolved_on_another_surface_has_an_unknown_answerer(env):
    settle = server_requests.send_async("approval", "s1", _approval_params(), lambda result: None)

    settle("resolved")

    (row,) = _rows(env.home)
    assert (row["state"], row["answered_by_client_id"], row["answered_via"]) == ("answered", "unknown", "external")
    assert _events(env, "request.resolved")[0]["answered_by"]["client_id"] == "unknown"


def test_first_answer_note_wins(env):
    first, second = _identified("a-phone", "A"), _identified("b-phone", "B")
    settle = server_requests.send_async("approval", "s1", _approval_params(), lambda result: None)
    server_requests.note_approval_answer("s1", "ap-1", "once", transport=first)
    server_requests.note_approval_answer("s1", "ap-1", "deny", transport=second)

    settle("resolved")

    (row,) = _rows(env.home)
    assert (row["answered_by_client_id"], row["outcome"]) == ("a-phone", "once")


def test_approval_timeout_expires_without_a_resolved_event(env):
    settle = server_requests.send_async("approval", "s1", _approval_params(), lambda result: None)

    settle("timeout")

    (row,) = _rows(env.home)
    assert (row["state"], row["outcome"], row["cancel_reason"], row["answered_by_client_id"]) == (
        "expired", "timed_out", "timeout", None)
    assert _events(env, "request.resolved") == []


def test_approval_withdrawn_when_the_turn_ends_under_it(env):
    settle = server_requests.send_async("approval", "s1", _approval_params(), lambda result: None)

    settle("session_closed")

    (row,) = _rows(env.home)
    assert (row["state"], row["cancel_reason"]) == ("withdrawn", "session_closed")


def test_command_in_the_ledger_prompt_is_redacted(env):
    token = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
    server_requests.send_async("approval", "s1", _approval_params(command=f"curl -H 'Authorization: Bearer {token}' x"),
                               lambda result: None)
    (row,) = _rows(env.home)
    assert token not in (row["prompt"] or "")


# ── clarifications ────────────────────────────────────────────────────────────────────────────────────


def _clarify_questions():
    return [{"qid": "q1", "question": "Which region?", "choices": ["eu", "us"]},
            {"qid": "q2", "question": "Which size?"}]


def _ask_clarify(env, timeout=5):
    holder = {}

    def run():
        holder["result"] = server_requests.send("clarify", "s1", {"questions": _clarify_questions()},
                                                timeout=timeout, qids=["q1", "q2"])

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.time() + 5
    while not env.frames and time.time() < deadline:
        time.sleep(0.01)
    return thread, holder, env.frames[0]["id"]


def test_clarify_locks_are_appended_and_the_last_lock_resolves(env):
    phone, desktop = _identified("ios-1", "Phone"), _identified("desktop-1", "Mac", "desktop")
    thread, holder, request_id = _ask_clarify(env)

    assert server_requests.lock_answer(request_id, "q1", "eu", transport=phone) == ["q2"]
    assert server_requests.lock_answer(request_id, "q1", "us", transport=phone) == ["q2"]  # edited before the end
    assert server_requests.lock_answer(request_id, "q2", "large", transport=desktop) == []
    thread.join(5)

    assert holder["result"] == {"answers": {"q1": "us", "q2": "large"}, "outcome": "submitted"}
    (row,) = _rows(env.home)
    assert (row["state"], row["outcome"], row["answered_by_client_id"], row["answered_via"]) == (
        "answered", "submitted", "desktop-1", "clarify.lock")
    assert "Which region?" in row["prompt"]
    listing = human_requests.list_requests(env.home)
    assert listing["recent"][0]["answers"] == {"q1": "us", "q2": "large"}
    (resolved,) = _events(env, "request.resolved")
    assert resolved["answers"] == {"q1": "us", "q2": "large"} and resolved["choice"] is None


def test_clarify_answered_by_one_response_frame_records_every_answer(env):
    phone = _identified("ios-1", "Phone")
    thread, _holder, request_id = _ask_clarify(env)

    server_requests.resolve_response(
        {"jsonrpc": "2.0", "id": request_id, "result": {"answers": {"q1": "eu", "q2": None}}}, phone)
    thread.join(5)

    answers = human_requests.list_requests(env.home)["recent"][0]["answers"]
    assert answers == {"q1": "eu", "q2": None}


def test_clarify_that_times_out_is_expired(env):
    thread, holder, _ = _ask_clarify(env, timeout=0.05)
    thread.join(5)
    assert holder["result"]["outcome"] == "timed_out"
    (row,) = _rows(env.home)
    assert (row["state"], row["outcome"]) == ("expired", "timed_out")
    assert _events(env, "request.resolved") == []


def test_session_cancel_marks_open_requests_cancelled(env):
    thread, _holder, _ = _ask_clarify(env)

    assert server_requests.cancel("s1", reason="interrupted") == 1
    thread.join(5)

    (row,) = _rows(env.home)
    assert (row["state"], row["cancel_reason"]) == ("cancelled", "interrupted")


# ── secret kinds ──────────────────────────────────────────────────────────────────────────────────────


def test_sudo_request_keeps_no_text_and_its_event_carries_no_answer(env):
    typed = "hunter2-do-not-store"
    phone = _identified("ios-1", "Phone")
    holder = {}
    thread = threading.Thread(target=lambda: holder.update(
        result=server_requests.send("sudo", "s1", {"command": "sudo apt install thing"}, timeout=5)))
    thread.start()
    deadline = time.time() + 5
    while not env.frames and time.time() < deadline:
        time.sleep(0.01)

    server_requests.resolve_response({"jsonrpc": "2.0", "id": env.frames[0]["id"], "result": {"value": typed}}, phone)
    thread.join(5)

    assert holder["result"] == {"value": typed}  # the agent still gets it
    (row,) = _rows(env.home)
    assert (row["kind"], row["state"], row["outcome"]) == ("sudo", "answered", "submitted")
    assert row["prompt"] is None and row["choices_json"] is None
    assert typed not in repr(row) and "apt install" not in repr(row)
    (resolved,) = _events(env, "request.resolved")
    assert resolved["kind"] == "sudo" and resolved["outcome"] is None and resolved["choice"] is None
    assert resolved["answers"] is None and typed not in repr(resolved)


def test_secret_prompt_never_stores_the_env_var_prompt(env):
    server_requests.send_async("secret", "s1", {"env_var": "OPENAI_API_KEY", "prompt": "Paste the key"},
                               lambda result: None)
    (row,) = _rows(env.home)
    assert row["kind"] == "secret" and row["prompt"] is None and "OPENAI" not in repr(row)


def test_desktop_bridge_requests_are_not_ledgered(env):
    server_requests.send_async("terminal.read", "s1", {}, lambda result: None)
    assert _rows(env.home) == []
    assert env.frames  # the request itself still went out


# ── resilience ────────────────────────────────────────────────────────────────────────────────────────


def test_a_broken_ledger_never_blocks_a_request_or_its_event(env, tmp_path, monkeypatch):
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("x")
    monkeypatch.setattr(server_requests, "_ledger_context", lambda sid: {
        "home": not_a_directory, "profile": "default", "session_id": sid, "session_key": sid})
    phone = _identified("ios-1", "Phone")
    server_requests.send_async("approval", "s1", _approval_params(), lambda result: None)

    assert server_requests.resolve_response(
        {"jsonrpc": "2.0", "id": env.frames[0]["id"], "result": {"choice": "deny"}}, phone)

    assert _events(env, "request.resolved")[0]["choice"] == "deny"


# ── restart, retention, queries ───────────────────────────────────────────────────────────────────────


def _seed_open_row(home, request_id, *, epoch, pid, created_at=None):
    human_requests.record_open(home, request_id=request_id, kind="approval", profile="default", session_id="stored",
                               session_key="key", prompt="rm -rf /tmp/x", choices=["once", "deny"],
                               created_at=created_at or time.time(), expires_at=None)
    conn = human_requests._connect(home)
    try:
        with conn:
            conn.execute("UPDATE human_requests SET gateway_epoch = ?, owner_pid = ? WHERE request_id = ?",
                         (epoch, pid, request_id))
    finally:
        conn.close()


def test_gateway_start_marks_rows_a_dead_process_left_open_as_lost_on_restart(env):
    _seed_open_row(env.home, "srq-dead", epoch="old-epoch", pid=DEAD_PID)
    _seed_open_row(env.home, "srq-sibling", epoch="other-epoch", pid=os.getpid())  # a live sibling gateway
    human_requests._swept_homes.clear()  # a fresh process: nothing swept yet

    listing = human_requests.list_requests(env.home)

    assert [r["request_id"] for r in listing["open"]] == ["srq-sibling"]
    lost = [r for r in listing["recent"] if r["request_id"] == "srq-dead"]
    assert lost and lost[0]["state"] == "lost_on_restart" and lost[0]["cancel_reason"] == "gateway_restart"
    assert lost[0]["answered_by"] is None


def test_open_count_counts_only_requests_a_live_process_waits_on(env):
    server_requests.send_async("approval", "s1", _approval_params("ap-1"), lambda result: None)
    server_requests.send_async("approval", "s2", _approval_params("ap-2"), lambda result: None)
    _seed_open_row(env.home, "srq-dead", epoch="old-epoch", pid=DEAD_PID)

    assert human_requests.open_count(env.home) == 2


def test_open_count_cli_prints_the_count(env, capsys):
    server_requests.send_async("approval", "s1", _approval_params(), lambda result: None)

    assert human_requests._cli(["open-count", "--home", str(env.home)]) == 0

    assert capsys.readouterr().out.strip() == "1"


def test_open_count_cli_without_a_database_is_zero(tmp_path, capsys):
    assert human_requests._cli(["open-count", "--home", str(tmp_path / "nowhere")]) == 0
    assert capsys.readouterr().out.strip() == "0"


def test_retention_prunes_old_closed_rows_and_keeps_open_ones(env):
    now = time.time()
    old = now - 91 * 86400
    settle_old = server_requests.send_async("approval", "s1", _approval_params("ap-old"), lambda result: None)
    settle_old("resolved")
    server_requests.send_async("approval", "s1", _approval_params("ap-open"), lambda result: None)
    conn = human_requests._connect(env.home)
    try:
        old_id = conn.execute("SELECT request_id FROM human_requests WHERE state = 'answered'").fetchone()[0]
        with conn:
            conn.execute("UPDATE human_requests SET created_at = ?, answered_at = ? WHERE request_id = ?",
                         (old, old, old_id))
            conn.execute("INSERT INTO human_request_answers VALUES (?, 'q1', 'a', 'c', ?)", (old_id, old))
        assert human_requests.prune(conn, now=now) == 1
        assert conn.execute("SELECT COUNT(*) FROM human_request_answers").fetchone()[0] == 0
        assert [r[0] for r in conn.execute("SELECT state FROM human_requests")] == ["open"]
    finally:
        conn.close()


def test_history_pages_newest_first_and_filters(env):
    for index in range(3):
        settle = server_requests.send_async("approval", "s1", _approval_params(f"ap-{index}"), lambda result: None)
        settle("resolved")
        time.sleep(0.01)  # distinct created_at cursors

    first = human_requests.history(env.home, limit=2)
    assert len(first["requests"]) == 2 and first["next_before"] is not None
    second = human_requests.history(env.home, limit=2, before=first["next_before"])
    assert len(second["requests"]) == 1 and second["next_before"] is None
    assert first["requests"][0]["created_at"] > second["requests"][0]["created_at"]
    assert human_requests.history(env.home, kind="clarify")["requests"] == []
    assert len(human_requests.history(env.home, session_id="stored-s1", states=["answered"])["requests"]) == 3


def test_schema_has_both_tables_with_the_documented_columns(env):
    human_requests.open_count(env.home)
    conn = sqlite3.connect(env.home / "state.db")
    try:
        request_cols = {r[1] for r in conn.execute("PRAGMA table_info(human_requests)")}
        answer_cols = {r[1] for r in conn.execute("PRAGMA table_info(human_request_answers)")}
    finally:
        conn.close()
    assert {"request_id", "kind", "profile", "bot_key", "session_id", "session_key", "call_id", "prompt",
            "choices_json", "state", "outcome", "cancel_reason", "created_at", "expires_at", "answered_at",
            "answered_by_client_id", "answered_by_kind", "answered_by_label", "answered_via",
            "gateway_epoch"} <= request_cols
    assert answer_cols == {"request_id", "question_id", "answer", "client_id", "locked_at"}


# ── RPCs ──────────────────────────────────────────────────────────────────────────────────────────────


def test_request_list_and_history_rpcs_read_the_profile_ledger():
    from hermes_constants import get_hermes_home
    from tui_gateway import server

    home = get_hermes_home()
    human_requests._swept_homes.discard(str(home))
    _seed_open_row(home, "srq-open", epoch=human_requests._current_epoch(), pid=os.getpid())
    human_requests.record_open(home, request_id="srq-done", kind="clarify", profile="default", session_id="stored",
                               session_key="key", prompt="Which region?", choices=[{"qid": "q1", "question": "?"}],
                               created_at=time.time() - 5, expires_at=None)
    human_requests.record_settled(home, request_id="srq-done", state="answered", outcome="submitted",
                                  answered_by=client_identity.ClientIdentity("ios-1", "Phone", "ios", "declared"),
                                  via="clarify.lock", answers={"q1": "eu"})

    listing = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "request.list", "params": {}})["result"]
    assert [r["request_id"] for r in listing["open"]] == ["srq-open"] and listing["open_count"] == 1
    assert listing["recent"][0]["request_id"] == "srq-done"
    assert listing["recent"][0]["answers"] == {"q1": "eu"}
    assert listing["recent"][0]["answered_by"] == {"client_id": "ios-1", "label": "Phone", "kind": "ios"}

    none_recent = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "request.list",
                                         "params": {"answered_limit": 0}})["result"]
    assert none_recent["recent"] == [] and none_recent["open_count"] == 1

    page = server.handle_request({"jsonrpc": "2.0", "id": 3, "method": "request.history",
                                  "params": {"kind": "clarify", "states": ["answered"], "limit": 10}})["result"]
    assert [r["request_id"] for r in page["requests"]] == ["srq-done"] and page["next_before"] is None


def test_request_rpcs_reject_unknown_params():
    from tui_gateway import server

    reply = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "request.list", "params": {"nope": 1}})
    assert reply["error"]["code"] == 4000
