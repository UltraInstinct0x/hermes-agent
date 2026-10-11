"""Durable ledger of the questions the agent puts to a human (F3) and the ``request.resolved`` payload (F2).

``server_requests`` holds an open question only in process memory, so before this module nobody could say
what was asked of the owner across all sessions, who answered, or what happened to a question when the
gateway restarted. The ledger lives in the profile's ``state.db`` (tables ``human_requests`` and
``human_request_answers``, declared in ``hermes_state_common.SCHEMA_SQL``) and is written from the
``ServerRequest`` lifecycle: when a request opens, and when it is answered, expires, is withdrawn or is
cancelled. A gateway that starts finds rows a dead process left ``open`` and tombstones them
``lost_on_restart``: the agent thread that waited on them is gone, so "the bot will ask again" is the only
honest state.

What is stored is deliberately small. The secret kinds (``sudo``, ``secret``, vault prompts) keep no
prompt, choices or answer text at all, only that a request of that kind existed and how it ended. Other
kinds keep a redacted, clipped prompt. Answered and closed rows are pruned after ``RETENTION_DAYS``.

Everything here takes the profile home as an argument: one gateway process serves several profiles, and a
write must land in the profile that owns the session, not the process's launch profile.

``python -m tui_gateway.human_requests open-count [--home PATH]`` prints the number of requests a live
process is still waiting on, for callers (the restart guard) that must not restart over a question.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from tui_gateway.client_identity import UNKNOWN, ClientIdentity

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90
ANSWERED_LIST_DEFAULT = 20
HISTORY_LIMIT_DEFAULT = 50
HISTORY_LIMIT_MAX = 200

# Server request method -> ledger kind. Only questions put to a person are recorded; the desktop read/act
# bridges (terminal.read, preview.act, tour, ...) are machine-to-machine and would only flood the table.
KIND_BY_METHOD = {
    "approval": "approval",
    "clarify": "clarify",
    "setup_choose": "other",
    "sudo": "sudo",
    "display.install.sudo": "sudo",
    "secret": "secret",
    "vault.unlock_prompt": "secret",
    "vault.save_login": "secret",
    "vault.code": "secret",
}
REDACTED_KINDS = frozenset({"sudo", "secret"})

OPEN = "open"
ANSWERED = "answered"
EXPIRED = "expired"
WITHDRAWN = "withdrawn"
CANCELLED = "cancelled"
LOST_ON_RESTART = "lost_on_restart"

# ``answered_via`` values.
VIA_FRAME = "frame"
VIA_APPROVAL_RESPOND = "approval.respond"
VIA_CLARIFY_LOCK = "clarify.lock"
VIA_REQUEST_ANSWER = "request.answer"
VIA_EXTERNAL = "external"  # resolved on a surface that never reached this gateway (Telegram /approve, CLI)

_PROMPT_CLIP = 500
_ANSWER_CLIP = 2000
_CHOICES_CLIP = 8000

_write_lock = threading.Lock()
_swept_homes: set[str] = set()
#: What reading or writing the ledger can raise (a locked or unwritable ``state.db``).
STORE_ERRORS = (sqlite3.Error, OSError)


def kind_for(method: str) -> str | None:
    """Ledger kind of a server request method, None when the method is not a human question."""
    return KIND_BY_METHOD.get(method)


def clip(text: Any, limit: int) -> str:
    value = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return value if len(value) <= limit else value[: limit - 1] + "…"


# ── storage ──────────────────────────────────────────────────────────────────────────────────────────


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state_repair import apply_durability_barriers
    from hermes_state_schema import reconcile_state_schema

    apply_durability_barriers(conn)
    reconcile_state_schema(conn)


def _connect(home: Path) -> sqlite3.Connection:
    """A short-lived connection to ``<home>/state.db`` (same opener as ``tools/async_delegation.py``: owner-only
    files, SessionDB owns the journal mode, the canonical SCHEMA_SQL is the one table shape)."""
    from hermes_cli.sqlite_util import open_db
    from hermes_constants import mkdir_under_hermes_home
    from hermes_state import _secure_state_db_files

    path = Path(home) / "state.db"
    mkdir_under_hermes_home(path.parent)
    _secure_state_db_files(path, create_main=True)
    conn = open_db(path, db_label="state.db (human_requests)", busy_timeout_ms=10_000, wal=False,
                   row_factory=sqlite3.Row, initialize=_initialize_schema)
    _secure_state_db_files(path)
    return conn


def _transaction(home: Path):
    from hermes_cli.sqlite_util import transaction

    conn = _connect(home)
    try:
        _sweep_once(conn, home)
    except BaseException:
        conn.close()
        raise
    return transaction(conn)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    from hermes_cli._subprocess_compat import pid_exists_stdlib

    return pid_exists_stdlib(int(pid))


def _current_epoch() -> str:
    from tui_gateway.event_replay import replay_epoch

    return replay_epoch()


def _sweep_once(conn: sqlite3.Connection, home: Path) -> None:
    """First ledger use of *home* in this process: tombstone what a dead process left open, then prune.
    Per process and home, so a pooled gateway sweeps each profile it hosts the first time it writes there."""
    key = str(home)
    if key in _swept_homes:
        return
    tombstone_orphans(conn)
    prune(conn)
    _swept_homes.add(key)


def tombstone_orphans(conn: sqlite3.Connection, *, epoch: str | None = None, now: float | None = None) -> int:
    """Mark ``open`` rows whose owning process is gone ``lost_on_restart``. A row belongs to a live process
    when it carries this process's epoch, or its ``owner_pid`` still exists (a sibling gateway on the same
    profile home must not have its open questions tombstoned by this one starting)."""
    mine = epoch or _current_epoch()
    stamp = time.time() if now is None else now
    rows = conn.execute("SELECT request_id, gateway_epoch, owner_pid FROM human_requests WHERE state = ?",
                        (OPEN,)).fetchall()
    dead = [r["request_id"] for r in rows if r["gateway_epoch"] != mine and not _pid_alive(r["owner_pid"])]
    for request_id in dead:
        conn.execute(
            "UPDATE human_requests SET state = ?, cancel_reason = 'gateway_restart', answered_at = NULL, "
            "outcome = NULL WHERE request_id = ? AND state = ?", (LOST_ON_RESTART, request_id, OPEN))
    if dead:
        conn.commit()
        logger.info("human_requests: %d open request(s) marked lost_on_restart at %s", len(dead), stamp)
    return len(dead)


def prune(conn: sqlite3.Connection, *, retention_days: float = RETENTION_DAYS, now: float | None = None) -> int:
    """Delete closed rows (and their answer locks) older than the retention window. Open rows are kept."""
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    stale = [r[0] for r in conn.execute(
        "SELECT request_id FROM human_requests WHERE state != ? AND COALESCE(answered_at, created_at) < ?",
        (OPEN, cutoff)).fetchall()]
    for request_id in stale:
        conn.execute("DELETE FROM human_request_answers WHERE request_id = ?", (request_id,))
        conn.execute("DELETE FROM human_requests WHERE request_id = ?", (request_id,))
    if stale:
        conn.commit()
    return len(stale)


# ── lifecycle writes ─────────────────────────────────────────────────────────────────────────────────


def record_open(home: Path, *, request_id: str, kind: str, profile: str, session_id: str, session_key: str,
                prompt: str | None, choices: Any, created_at: float, expires_at: float | None) -> None:
    """Insert the row for a request that was just sent. Secret kinds store no prompt or choices."""
    secret = kind in REDACTED_KINDS
    choices_json = None if secret or choices is None else clip(json.dumps(choices, ensure_ascii=False), _CHOICES_CLIP)
    with _write_lock, _transaction(home) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO human_requests (request_id, kind, profile, session_id, session_key, prompt, "
            "choices_json, state, created_at, expires_at, gateway_epoch, owner_pid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, kind, profile, session_id or None, session_key or None,
             None if secret or not prompt else clip(_redact(prompt), _PROMPT_CLIP), choices_json, OPEN,
             created_at, expires_at, _current_epoch(), os.getpid()))


def _redact(text: str) -> str:
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(text, force=True)


def record_settled(home: Path, *, request_id: str, state: str, outcome: str | None = None,
                   cancel_reason: str | None = None, answered_by: ClientIdentity | None = None,
                   via: str | None = None, answers: dict[str, str | None] | None = None,
                   at: float | None = None) -> None:
    """Close the row. Only an ``open`` row changes (the first settlement wins, like the in-memory request).
    ``answers`` are the clarify answers not yet recorded by a lock; the secret kinds never keep any."""
    stamp = time.time() if at is None else at
    by = answered_by or UNKNOWN
    with _write_lock, _transaction(home) as conn:
        row = conn.execute("SELECT kind FROM human_requests WHERE request_id = ? AND state = ?",
                           (request_id, OPEN)).fetchone()
        if row is None:
            return
        answered = state == ANSWERED
        conn.execute(
            "UPDATE human_requests SET state = ?, outcome = ?, cancel_reason = ?, answered_at = ?, "
            "answered_by_client_id = ?, answered_by_kind = ?, answered_by_label = ?, answered_via = ? "
            "WHERE request_id = ?",
            (state, outcome, cancel_reason, stamp if answered else None,
             by.client_id if answered else None, by.kind if answered else None, by.label if answered else None,
             via if answered else None, request_id))
        if answers and row["kind"] not in REDACTED_KINDS:
            _insert_missing_answers(conn, request_id, answers, by.client_id, stamp)


def _insert_missing_answers(conn: sqlite3.Connection, request_id: str, answers: dict[str, str | None],
                            client_id: str, locked_at: float) -> None:
    """Append the final answer of every question whose newest lock differs (or that was never locked)."""
    current = _latest_answers(conn, request_id)
    for question_id, answer in answers.items():
        if question_id in current and current[question_id] == answer:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO human_request_answers (request_id, question_id, answer, client_id, locked_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (request_id, question_id, None if answer is None else clip(answer, _ANSWER_CLIP), client_id, locked_at))


def record_lock(home: Path, *, request_id: str, question_id: str, answer: str | None, client_id: str,
                at: float | None = None) -> None:
    """Append one batch-clarify lock. Ignored for a request that is not an open clarify row."""
    with _write_lock, _transaction(home) as conn:
        row = conn.execute("SELECT kind FROM human_requests WHERE request_id = ? AND state = ?",
                           (request_id, OPEN)).fetchone()
        if row is None or row["kind"] != "clarify":
            return
        conn.execute(
            "INSERT OR REPLACE INTO human_request_answers (request_id, question_id, answer, client_id, locked_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (request_id, question_id, None if answer is None else clip(answer, _ANSWER_CLIP), client_id,
             time.time() if at is None else at))


# ── reads ────────────────────────────────────────────────────────────────────────────────────────────


def _latest_answers(conn: sqlite3.Connection, request_id: str) -> dict[str, str | None]:
    rows = conn.execute(
        "SELECT question_id, answer FROM human_request_answers WHERE request_id = ? ORDER BY locked_at",
        (request_id,)).fetchall()
    return {r["question_id"]: r["answer"] for r in rows}


def _row_to_wire(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    answered_by = None
    if row["answered_by_client_id"] is not None:
        answered_by = {"client_id": row["answered_by_client_id"], "label": row["answered_by_label"],
                       "kind": row["answered_by_kind"]}
    answers = _latest_answers(conn, row["request_id"]) if row["kind"] == "clarify" else {}
    return {
        "request_id": row["request_id"], "kind": row["kind"], "profile": row["profile"],
        "bot_key": row["bot_key"], "session_id": row["session_id"], "session_key": row["session_key"],
        "call_id": row["call_id"], "prompt": row["prompt"],
        "choices": json.loads(row["choices_json"]) if row["choices_json"] else None,
        "state": row["state"], "outcome": row["outcome"], "cancel_reason": row["cancel_reason"],
        "created_at": row["created_at"], "expires_at": row["expires_at"], "answered_at": row["answered_at"],
        "answered_by": answered_by, "answered_via": row["answered_via"],
        "answers": answers or None,
    }


def _live_open_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """``open`` rows a live process is waiting on: this process's own, or a sibling's whose pid still exists."""
    mine = _current_epoch()
    rows = conn.execute("SELECT * FROM human_requests WHERE state = ? ORDER BY created_at", (OPEN,)).fetchall()
    return [r for r in rows if r["gateway_epoch"] == mine or _pid_alive(r["owner_pid"])]


def open_count(home: Path) -> int:
    """Requests a live process is still waiting on in *home*'s ledger (what the restart guard asks)."""
    with _write_lock:
        conn = _connect(home)
        try:
            _sweep_once(conn, home)
            return len(_live_open_rows(conn))
        finally:
            conn.close()


def list_requests(home: Path, *, answered_limit: int = ANSWERED_LIST_DEFAULT) -> dict[str, Any]:
    """Every open request across ALL sessions of the profile, plus the ``answered_limit`` most recently closed
    ones (answered, expired, withdrawn, cancelled or lost)."""
    with _write_lock:
        conn = _connect(home)
        try:
            _sweep_once(conn, home)
            open_rows = _live_open_rows(conn)
            closed = conn.execute(
                "SELECT * FROM human_requests WHERE state != ? ORDER BY COALESCE(answered_at, created_at) DESC "
                "LIMIT ?", (OPEN, max(0, answered_limit))).fetchall() if answered_limit > 0 else []
            return {"open": [_row_to_wire(conn, r) for r in open_rows],
                    "recent": [_row_to_wire(conn, r) for r in closed], "open_count": len(open_rows)}
        finally:
            conn.close()


def history(home: Path, *, session_id: str | None = None, kind: str | None = None,
            states: Iterable[str] | None = None, limit: int = HISTORY_LIMIT_DEFAULT,
            before: float | None = None) -> dict[str, Any]:
    """Newest-first page of the ledger. ``before`` is the ``created_at`` cursor of the previous page's last row."""
    limit = max(1, min(int(limit), HISTORY_LIMIT_MAX))
    where, args = ["1 = 1"], []
    if session_id:
        where.append("(session_id = ? OR session_key = ?)")
        args += [session_id, session_id]
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if states:
        wanted = list(states)
        where.append(f"state IN ({','.join('?' for _ in wanted)})")
        args += wanted
    if before is not None:
        where.append("created_at < ?")
        args.append(before)
    with _write_lock:
        conn = _connect(home)
        try:
            _sweep_once(conn, home)
            rows = conn.execute(
                f"SELECT * FROM human_requests WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?",
                (*args, limit + 1)).fetchall()
            page = [_row_to_wire(conn, r) for r in rows[:limit]]
            return {"requests": page, "next_before": page[-1]["created_at"] if len(rows) > limit else None}
        finally:
            conn.close()


# ── request.resolved payload ─────────────────────────────────────────────────────────────────────────


def resolved_payload(*, request_id: str, method: str, kind: str, session_id: str, outcome: str | None,
                     answers: dict[str, str | None] | None, answered_by: ClientIdentity, at: float) -> dict[str, Any]:
    """The ``request.resolved`` event body. ``choice`` is the approval decision, ``answers`` the clarify answers
    (clipped); the secret kinds carry neither."""
    secret = kind in REDACTED_KINDS
    summary = None
    if answers and not secret:
        summary = {qid: None if a is None else clip(a, _PROMPT_CLIP) for qid, a in answers.items()}
    return {"id": request_id, "method": method, "kind": kind, "session_id": session_id,
            "outcome": None if secret else outcome,
            "choice": outcome if kind == "approval" else None, "answers": summary,
            "answered_by": answered_by.as_wire(), "at": at}


# ── ServerRequest lifecycle glue ─────────────────────────────────────────────────────────────────────
#
# ``server_requests`` calls these (duck-typed on its ``ServerRequest``, so this module never imports it)
# at the two moments that matter: a request is sent, and a request ends by any path.


class LedgerEntry:
    """What ``server_requests`` keeps on an open request: its ledger kind and, when the ledger is wired for
    its session, where the row lives. ``home`` None means events only (no database)."""

    __slots__ = ("home", "kind", "recorded", "session_id")

    def __init__(self, kind: str, home: Path | None, session_id: str) -> None:
        self.kind, self.home, self.session_id, self.recorded = kind, home, session_id, False


class AnswerNote:
    """Who answered an approval over ``approval.respond`` (the response never reaches ``server_requests`` as a
    frame, the agent's own queue settles the request later): stamped on the open request, read at settlement."""

    __slots__ = ("by", "choice", "via")

    def __init__(self, by: ClientIdentity, via: str, choice: str | None) -> None:
        self.by, self.via, self.choice = by, via, choice


def _prompt_and_choices(method: str, params: dict) -> tuple[str | None, Any]:
    if method == "approval":
        text = "\n".join(str(p) for p in (params.get("command"), params.get("description")) if p)
        return text or None, params.get("choices")
    if method == "clarify":
        questions = params.get("questions") or []
        return "\n".join(str(q.get("question", "")) for q in questions) or None, questions
    if method == "setup_choose":
        return params.get("question"), params.get("options")
    return None, None


def _approval_expiry(created_at: float) -> float:
    """When the agent's own approval wait ends (``approvals.timeout`` of the profile in scope)."""
    from tools.approval_context import _get_approval_timeout

    return created_at + _get_approval_timeout()


def track_open(req: Any, ctx: dict | None) -> LedgerEntry | None:
    """Record a just-created request. None for methods that are not human questions. A failing database never
    blocks the request: the entry stays, unrecorded, so ``request.resolved`` is still emitted."""
    kind = kind_for(req.method)
    if kind is None:
        return None
    entry = LedgerEntry(kind, ctx["home"] if ctx else None, ctx["session_id"] if ctx else req.sid)
    if ctx is None:
        return entry
    expires_at = req.expires_at if req.expires_at is not None else (
        _approval_expiry(req.created_at) if kind == "approval" else None)
    prompt, choices = _prompt_and_choices(req.method, req.params)
    try:
        record_open(ctx["home"], request_id=req.id, kind=kind, profile=ctx["profile"], session_id=ctx["session_id"],
                    session_key=ctx["session_key"], prompt=prompt, choices=choices, created_at=req.created_at,
                    expires_at=expires_at)
        entry.recorded = True
    except STORE_ERRORS:
        logger.warning("human_requests: could not record request %s", req.id, exc_info=True)
    return entry


def _settled_outcome(kind: str, state: str, result: dict | None, note: AnswerNote | None) -> str | None:
    result = result or {}
    if state == EXPIRED:
        return "timed_out"
    if state != ANSWERED:
        return None
    if kind == "approval":
        choice = note.choice if note is not None and note.choice else result.get("choice")
        return clip(choice, 32) if isinstance(choice, str) and choice else None
    if kind == "clarify":
        return str(result.get("outcome") or "submitted")
    return "submitted" if result.get("value") or result.get("picked") or result.get("said") else "skipped"


def track_settle(req: Any, state: str, *, reason: str | None = None, by: ClientIdentity | None = None,
                 via: str | None = None, result: dict | None = None) -> dict | None:
    """Close the ledger row of *req*. Returns the ``request.resolved`` payload when a person answered (the caller
    emits it to the session's clients), else None. *by* / *via* describe the answering call; an approval answered
    over RPC supplies them through the request's ``answer_note`` instead, and a request that ended with neither was
    resolved on another surface."""
    entry: LedgerEntry | None = req.ledger
    if entry is None:
        return None
    note: AnswerNote | None = req.answer_note
    answered = state == ANSWERED
    who = by or (note.by if note is not None else UNKNOWN)
    how = via or (note.via if note is not None else VIA_EXTERNAL)
    outcome = _settled_outcome(entry.kind, state, result, note)
    answers = (result or {}).get("answers") if entry.kind == "clarify" else None
    answers = answers if isinstance(answers, dict) else None
    at = time.time()
    if entry.recorded and entry.home is not None:
        try:
            record_settled(entry.home, request_id=req.id, state=state, outcome=outcome, cancel_reason=reason,
                           answered_by=who, via=how, answers=answers, at=at)
        except STORE_ERRORS:
            logger.warning("human_requests: could not settle request %s", req.id, exc_info=True)
    if not answered:
        return None
    return resolved_payload(request_id=req.id, method=req.method, kind=entry.kind, session_id=req.sid,
                            outcome=outcome, answers=answers, answered_by=who, at=at)


def track_lock(req: Any, question_id: str, answer: str | None, by: ClientIdentity) -> None:
    """Append one ``clarify.lock`` answer to the ledger."""
    entry: LedgerEntry | None = req.ledger
    if entry is None or not entry.recorded or entry.home is None:
        return
    try:
        record_lock(entry.home, request_id=req.id, question_id=question_id, answer=answer, client_id=by.client_id)
    except STORE_ERRORS:
        logger.warning("human_requests: could not record a lock on %s", req.id, exc_info=True)


def _cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m tui_gateway.human_requests")
    sub = parser.add_subparsers(dest="command", required=True)
    count = sub.add_parser("open-count", help="print how many requests a live gateway is still waiting on")
    count.add_argument("--home", help="profile home (default: the active HERMES_HOME)")
    args = parser.parse_args(argv)
    from hermes_constants import get_hermes_home

    home = Path(args.home) if args.home else get_hermes_home()
    if not (home / "state.db").exists():
        print(0)
        return 0
    print(open_count(home))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
