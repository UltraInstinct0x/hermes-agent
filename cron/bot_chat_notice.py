"""``bot-chat`` delivery in ``notice`` mode: a durable ``cron_notice`` row, never an agent turn.

The default ``turn`` mode hands job output to the target profile's authority as an inbound Bot
Chat turn (``tools.bot_live_delivery``). ``notice`` mode posts the scrubbed output into the target's
canonical Bot Chat as an assistant row (``display_kind=cron_notice``) with the job and run in
``display_metadata``. It starts no turn and makes no model call. The row is written straight to the
target profile's ``state.db`` (idempotent on the execution key), so it shows on the next history read.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def post_bot_chat_notice(home: Path, text: str, notice: dict) -> Optional[bool]:
    """Write ``text`` into ``home``'s canonical Bot Chat as a ``cron_notice`` row.

    Idempotent on ``notice["delivery_id"]``. Returns whether a new row was written, or ``None``
    when the profile has no Bot Chat yet: the target's authority owns creating it (a second
    writer minting the session would race its creation identity), so a notice never creates one.
    """
    from hermes_state import SessionDB

    if not (home / "state.db").is_file():
        return None
    db = SessionDB(db_path=home / "state.db")
    try:
        row = db.get_session_by_title("Bot Chat")
        if row is None:
            return None
        session_id = db.get_compression_tip(row["id"]) or row["id"]
        return db.append_cron_notice(session_id, text, notice)[1]
    finally:
        db.close()


def deliver_bot_chat_notice(job: dict, content: str, home: Path, key: str, target: str, *,
                            for_failure: bool, suppressed: bool) -> Optional[str]:
    """Post one job output as a Bot Chat notice. ``None`` means settled (or recorded as ``suppressed``
    for a failure notice the target profile hides); anything else is an explicit error string.
    """
    from cron.scheduler_delivery import _redact_cron_payload

    job_id = job.get("id", "?")
    receipts = job.setdefault("_bot_chat_delivery_receipts", {})
    if suppressed:
        receipts[target] = {"status": "suppressed", "delivery_id": key}
        job["_notification_all_targets_suppressed"] = True
        return None
    if not home.is_dir():
        return f"bot-chat delivery target no longer exists: {home}; do not resend"
    # Same fail-closed scrub as the turn lane: the row carries the scrubbed copy, never the raw output.
    text = _redact_cron_payload(content, "bot-chat payload")
    notice = {
        "delivery_id": key, "cron_job_id": str(job_id),
        "cron_job_name": _redact_cron_payload(job.get("name", job_id), "job name"),
        "cron_run_id": str(job.get("execution_id") or job.get("_bot_chat_run_id") or ""), "source": "cron",
        "notification_category": "diagnostic" if for_failure else "result"}
    try:
        wrote = post_bot_chat_notice(home, text, notice)
    except Exception as exc:
        return f"bot-chat delivery to {target} unverified: {exc}"
    if wrote is None:
        return f"bot-chat delivery to {target}: the profile has no Bot Chat yet; open it once, then it can receive notices"
    receipts[target] = {"status": "settled", "delivery_id": key}
    logger.info("Job '%s': Bot Chat notice %s for %s %s", job_id, key, target,
                "posted" if wrote else "already present")
    return None
