"""Continue a FINISHED delegated child (``delegate_task(action='resume', ...)``) and find one to continue.

A child is an ordinary session in the parent's state.db (``parent_session_id`` = the spawner, ``_delegate_from`` in
``model_config``), so "resume" is the same machinery as ``hermes chat --resume``: build a child agent on the SAME
session id, hand it the stored transcript, run the follow-up as its next user turn. No new session row, no re-research.

Guard rails live here, before anything is built: the child must belong to the calling conversation (compression
lineage aware), must not be running (that is ``steer``), must be inside the resume window and under the resume size
cap, and two resumes of one session never overlap. Every refusal is an honest sentence the model can act on.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, UTC
from typing import Any, Optional

from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock

logger = logging.getLogger("tools.delegate_tool")  # log-record parity with the origin module

DEFAULT_RESUME_WINDOW_HOURS = 24.0
FINISHED_LIST_LIMIT = 10  # bounded output of action='list'
_TITLE_PREFIX = "Subagent: "
_GOAL_LABEL_CHARS = 100


class ResumeError(Exception):
    """A refusal whose text is shown to the model verbatim."""


@dataclass
class ResumePlan:
    """What a validated resume carries into child construction."""

    session_id: str
    history: list[dict[str, Any]] = field(default_factory=list)
    title: Optional[str] = None


_claimed: set[str] = set()
_claimed_lock = threading.Lock()


def claim_resume(session_id: str) -> bool:
    """Reserve ``session_id`` for one resumed run; False when another resume already holds it."""
    with _claimed_lock:
        if session_id in _claimed:
            return False
        _claimed.add(session_id)
        return True


def release_resume(session_id: Optional[str]) -> None:
    if session_id:
        with _claimed_lock:
            _claimed.discard(session_id)


def resume_window_hours() -> float:
    """``delegation.resume_window_hours`` (default 24): how far back children are listed and may be resumed."""
    from tools.delegate_tool_config import _cfg
    try:
        hours = float(_cfg().get("resume_window_hours", DEFAULT_RESUME_WINDOW_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_RESUME_WINDOW_HOURS
    return hours if hours > 0 else DEFAULT_RESUME_WINDOW_HOURS


def _session_db(parent_agent: Any) -> Any:
    return getattr(parent_agent, "_session_db", None)


def _parent_lineage_ids(parent_agent: Any, db: Any) -> list[str]:
    """The calling conversation's session ids: itself plus its verified compression ancestors, because a child
    spawned before a compression rotation still names the pre-rotation session as its parent."""
    sid = str(getattr(parent_agent, "session_id", "") or "")
    if not sid:
        return []
    try:
        ids = [str(s) for s in db._resume_lineage_ids(sid) if s]
    except Exception:
        ids = []
    return list(dict.fromkeys([sid, *ids]))


def _running_session_ids() -> dict[str, str]:
    """``session_id -> subagent_id`` of every child agent that is running right now."""
    with _active_subagents_lock:
        records = list(_active_subagents.values())
    running: dict[str, str] = {}
    for record in records:
        child_sid = str(getattr(record.get("agent"), "session_id", "") or "")
        if child_sid:
            running[child_sid] = str(record.get("subagent_id") or "")
    return running


def _last_active(row: dict[str, Any]) -> float:
    stamps = [row.get(k) for k in ("last_activity_at", "ended_at", "started_at")]
    return max((float(s) for s in stamps if isinstance(s, (int, float))), default=0.0)


def _iso(epoch: Optional[float]) -> Optional[str]:
    if not isinstance(epoch, (int, float)):
        return None
    return datetime.fromtimestamp(float(epoch), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _goal_label(title: Optional[str]) -> str:
    text = str(title or "").strip()
    text = text.removeprefix(_TITLE_PREFIX)
    return text if len(text) <= _GOAL_LABEL_CHARS else text[: _GOAL_LABEL_CHARS - 3] + "..."


def prepare_resume(parent_agent: Any, session_id: Any) -> ResumePlan:
    """Validate ``session_id`` as a resumable child of ``parent_agent``'s conversation and load its transcript.
    Raises :class:`ResumeError` (model-facing text) on any refusal. Pure read: nothing is reopened or claimed."""
    sid = str(session_id or "").strip()
    if not sid:
        raise ResumeError("resume needs resume_session_id (the child_session_id from a completion message or action='list').")
    db = _session_db(parent_agent)
    if db is None:
        raise ResumeError("Cannot resume: this session has no session store, so no child transcript is available. Spawn a new subagent.")
    from hermes_state_sessions import _parse_model_config
    row = db.get_session(sid)
    if not row:
        raise ResumeError(
            f"No subagent session '{sid}' exists in this profile (deleted, pruned, or from another profile). "
            "Spawn a new subagent and pass what it needs in 'context'.")
    delegated_from = _parse_model_config(row.get("model_config")).get("_delegate_from")
    if not delegated_from:
        raise ResumeError(f"Session '{sid}' is not a delegated subagent session; only children spawned by delegate_task can be resumed.")
    lineage = _parent_lineage_ids(parent_agent, db)
    if not ({str(delegated_from), str(row.get("parent_session_id") or "")} & set(lineage)):
        raise ResumeError(f"Subagent session '{sid}' belongs to a different conversation; a conversation can only resume its own children.")
    try:
        tip = str(db.get_compression_tip(sid) or sid)
    except Exception:
        tip = sid
    running = _running_session_ids()
    if tip in running or sid in running:
        busy = running.get(tip) or running.get(sid)
        raise ResumeError(
            f"Subagent session '{sid}' is still running (subagent_id '{busy}'). Use action='steer' with that subagent_id to "
            "redirect it, or wait for its completion message and then resume.")
    tip_row = row if tip == sid else (db.get_session(tip) or row)
    window = resume_window_hours()
    idle_hours = (time.time() - _last_active(tip_row)) / 3600.0
    if idle_hours > window:
        raise ResumeError(
            f"Subagent session '{sid}' was last active {idle_hours:.0f}h ago, beyond the {window:g}h resume window "
            "(delegation.resume_window_hours). Spawn a new subagent and pass the earlier findings in 'context'.")
    try:
        db.assert_resume_safe(tip, tip_only=True)
    except Exception as exc:
        if type(exc).__name__ != "SessionResumeTooLargeError":
            raise ResumeError(f"Subagent session '{sid}' cannot be resumed: {exc}") from exc
        raise ResumeError(
            f"Subagent session '{sid}' is too large to resume ({exc}). Spawn a new subagent and pass its key findings in 'context'."
        ) from exc
    restored, _display = db.get_resume_conversations(tip)
    history = [m for m in restored if m.get("role") != "session_meta"]
    if not history:
        raise ResumeError(f"Subagent session '{sid}' has no stored conversation to continue. Spawn a new subagent.")
    return ResumePlan(session_id=tip, history=history, title=tip_row.get("title"))


def finished_children(parent_agent: Any) -> list[dict[str, Any]]:
    """Recent finished children of this conversation for ``action='list'``: newest first, bounded, never the ones
    still running (those are in the live list). Empty when no session store is available."""
    db = _session_db(parent_agent)
    if db is None:
        return []
    try:
        since = time.time() - resume_window_hours() * 3600.0
        rows = db.list_delegate_children(_parent_lineage_ids(parent_agent, db), since=since, limit=FINISHED_LIST_LIMIT * 2)
    except Exception:
        logger.debug("delegate_task list: finished-children query failed", exc_info=True)
        return []
    running = _running_session_ids()
    entries = []
    for row in rows:
        if row["id"] in running:
            continue
        entries.append({
            "child_session_id": row["id"],
            "goal": _goal_label(row.get("title")),
            # ended_at is stamped when the child agent closed; without it the run died or was cut off mid-way.
            "status": "finished" if row.get("ended_at") else "idle",
            "finished_at": _iso(row.get("ended_at") or row.get("last_active")),
            "messages": row.get("message_count"),
        })
        if len(entries) >= FINISHED_LIST_LIMIT:
            break
    return entries
