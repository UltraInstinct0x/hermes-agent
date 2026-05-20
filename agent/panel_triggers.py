"""panel trigger helpers — three V2 surfaces for hermes-agent.

This module only exposes convenience constructors. Wiring into the actual
agent code paths (skill_manage, plan-mode output, user-correction detection)
is intentionally deferred — see Agent Emitter Architecture doc.

All helpers truncate per the unit-type caps (passage/diff ≤8000, choice
text ≤2000, prompt_context ≤2000) before emitting, and use a sha1-derived
external_ref so re-firing the same trigger is idempotent.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os

from .panel_emitter import emit

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    """Top-level gate. Avoids any work (incl. diff computation) when off."""
    return os.environ.get("PANEL_EMIT_ENABLED") == "1"

PASSAGE_CAP = 8000
DIFF_CAP = 8000
CHOICE_CAP = 2000
CONTEXT_CAP = 2000


def _trunc(s: str | None, cap: int) -> str:
    if s is None:
        return ""
    if len(s) <= cap:
        return s
    return s[:cap]


def _ref(*parts: str) -> str:
    h = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return h[:16]


def emit_skill_diff(
    skill_name: str,
    diff: str,
    reason: str,
    profile: str | None = None,
) -> dict:
    """rater judges whether a skill update is an improvement."""
    unit = {
        "type": "skill_diff_review",
        "external_ref": _ref(skill_name, reason),
        "diff": _trunc(diff, DIFF_CAP),
        "prompt_context": _trunc(reason, CONTEXT_CAP),
        "binary": {"yes": "improvement", "no": "regression"},
    }
    return emit([unit], profile=profile)


def emit_process_output(
    passage: str,
    user_goal: str,
    profile: str | None = None,
) -> dict:
    """rater rates the quality of a single agent process output."""
    trimmed_passage = _trunc(passage, PASSAGE_CAP)
    unit = {
        "type": "process_output_rating",
        "external_ref": _ref(trimmed_passage[:500]),
        "passage": trimmed_passage,
        "prompt_context": _trunc(user_goal, CONTEXT_CAP),
        "choices": [
            {"label": "1", "text": _trunc("great", CHOICE_CAP)},
            {"label": "2", "text": _trunc("ok", CHOICE_CAP)},
            {"label": "3", "text": _trunc("meh", CHOICE_CAP)},
            {"label": "4", "text": _trunc("bad", CHOICE_CAP)},
        ],
    }
    return emit([unit], profile=profile)


# ---------------------------------------------------------------------------
# on_* hook wrappers — these are the call sites used by host integration
# points (skill_manage, conversation_loop, prompt steering). Each one:
#   1. Early-returns when PANEL_EMIT_ENABLED != "1" (no work, no imports).
#   2. Wraps every call in try/except so emitter never breaks the host.
#   3. Logs failures at WARN level only.
# ---------------------------------------------------------------------------


def on_skill_diff(
    skill_name: str,
    before_text: str,
    after_text: str,
    agent_profile: str | None = None,
    session_id: str | None = None,
    action: str = "edit",
) -> None:
    """Hook: a skill was patched/edited. Computes a unified diff and emits."""
    if not _enabled():
        return
    try:
        before = before_text or ""
        after = after_text or ""
        if before == after:
            return
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{skill_name}",
                tofile=f"b/{skill_name}",
                n=3,
            )
        )
        if not diff:
            return
        reason_parts = [f"skill_manage:{action}", f"skill={skill_name}"]
        if session_id:
            reason_parts.append(f"session={session_id}")
        reason = " ".join(reason_parts)
        emit_skill_diff(skill_name, diff, reason, profile=agent_profile)
    except Exception as exc:  # noqa: BLE001 — never break the host
        logger.warning("panel on_skill_diff failed: %s", exc)


def on_process_output(
    passage: str,
    user_goal: str = "",
    agent_profile: str | None = None,
    session_id: str | None = None,
    min_chars: int = 500,
) -> None:
    """Hook: agent emitted a meaningful final output. Skips short outputs."""
    if not _enabled():
        return
    try:
        text = passage or ""
        if len(text) < min_chars:
            return
        goal = user_goal or ""
        if session_id and goal:
            goal = f"session={session_id} :: {goal}"
        elif session_id:
            goal = f"session={session_id}"
        emit_process_output(text, goal, profile=agent_profile)
    except Exception as exc:  # noqa: BLE001
        logger.warning("panel on_process_output failed: %s", exc)


def on_prompt_rewrite(
    original: str,
    corrected: str,
    context: str = "",
    agent_profile: str | None = None,
    session_id: str | None = None,
) -> None:
    """Hook: a prompt was rewritten/steered. No clean call site wired yet."""
    if not _enabled():
        return
    try:
        if not original or not corrected or original == corrected:
            return
        ctx = context or ""
        if session_id:
            ctx = f"session={session_id} :: {ctx}" if ctx else f"session={session_id}"
        emit_prompt_rewrite(original, corrected, ctx, profile=agent_profile)
    except Exception as exc:  # noqa: BLE001
        logger.warning("panel on_prompt_rewrite failed: %s", exc)


def emit_prompt_rewrite(
    original: str,
    corrected: str,
    context: str,
    profile: str | None = None,
) -> dict:
    """rater picks the better of two phrasings."""
    a = _trunc(original, CHOICE_CAP)
    b = _trunc(corrected, CHOICE_CAP)
    unit = {
        "type": "prompt_rewrite_pair",
        "external_ref": _ref(original, corrected),
        "choices": [
            {"label": "A", "text": a},
            {"label": "B", "text": b},
        ],
        "prompt_context": _trunc(context, CONTEXT_CAP),
    }
    return emit([unit], profile=profile)
