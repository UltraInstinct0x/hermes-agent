"""Assistant-side display markers (cron_notice, failed_turn) are never folded into a neighbour.

Folding a ``cron_notice`` row into the previous assistant turn dropped its display_kind/display_metadata and
named its row in ``_absorbed_row_ids``; the in-place compaction then rewound the original (active=0,
compacted=0) and the paged history never returned it, so the notice vanished from Bot Chat.
"""

from __future__ import annotations

from agent.agent_runtime_helpers import _merge_consecutive_assistants

UID = 32


def _plain(text: str, row_id: int, uid_char: str) -> dict:
    return {"role": "assistant", "content": text, "_row_id": row_id, "message_uid": uid_char * UID}


def _notice(text: str, row_id: int, uid_char: str, job: str = "job-1") -> dict:
    return {"role": "assistant", "content": text, "_row_id": row_id, "message_uid": uid_char * UID,
            "display_kind": "cron_notice",
            "display_metadata": {"cron_job_id": job, "notification_category": "digest"}}


def _absorbed(messages: list[dict]) -> set[int]:
    return {rid for m in messages for rid in m.get("_absorbed_row_ids", ())}


def test_notice_after_assistant_stays_its_own_row():
    text, notice = _plain("hello", 1, "a"), _notice("Good morning", 2, "b")
    merged, repairs = _merge_consecutive_assistants([text, notice])
    assert repairs == 0 and merged == [text, notice]
    assert merged[1]["display_kind"] == "cron_notice"
    assert merged[1]["display_metadata"] == {"cron_job_id": "job-1", "notification_category": "digest"}
    assert merged[1]["message_uid"] == "b" * UID
    assert merged[0]["content"] == "hello" and not _absorbed(merged)


def test_notice_before_assistant_is_not_absorbing_either():
    notice, text = _notice("Good morning", 1, "a"), _plain("later", 2, "b")
    merged, repairs = _merge_consecutive_assistants([notice, text])
    assert repairs == 0 and merged == [notice, text]
    assert not _absorbed(merged)


def test_two_consecutive_notices_stay_separate():
    first, second = _notice("one", 1, "a", "job-1"), _notice("two", 2, "b", "job-2")
    merged, repairs = _merge_consecutive_assistants([first, second])
    assert repairs == 0 and merged == [first, second]
    assert merged[1]["display_metadata"]["cron_job_id"] == "job-2" and not _absorbed(merged)


def test_notice_between_assistants_blocks_the_merge_through_it():
    before, notice, after = _plain("a", 1, "a"), _notice("n", 2, "b"), _plain("b", 3, "c")
    merged, repairs = _merge_consecutive_assistants([before, notice, after])
    assert repairs == 0 and [m["_row_id"] for m in merged] == [1, 2, 3]
    assert not _absorbed(merged)


def test_plain_neighbours_still_merge_with_each_other():
    one, two = _plain("a", 1, "a"), _plain("b", 2, "b")
    notice = _notice("n", 3, "c")
    merged, repairs = _merge_consecutive_assistants([one, two, notice])
    assert repairs == 1 and len(merged) == 2 and merged[1] is notice
    assert _absorbed(merged) == {2}  # the notice's row id (3) is never retired


def test_failed_turn_marker_is_exempt_too():
    text = _plain("partial", 1, "a")
    failed = {"role": "assistant", "content": "turn failed", "display_kind": "failed_turn", "_row_id": 2}
    merged, repairs = _merge_consecutive_assistants([text, failed])
    assert repairs == 0 and merged == [text, failed]


def test_notice_row_stays_in_display_history_after_a_merge_pass(tmp_path):
    """Reload real rows from a temp state.db, run the repair, and check the notice is untouched and
    still part of the display read (active=1 OR compacted=1)."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s1", source="cli")
        db.append_message("s1", "assistant", content="plain answer")
        _notice_id, inserted = db.append_cron_notice(
            "s1", "Good morning", {"delivery_id": "d1", "cron_job_id": "job-1", "notification_category": "digest"})
        assert inserted
        rows = db.get_messages("s1")
        merged, _ = _merge_consecutive_assistants([dict(r) for r in rows])
        assert [m.get("display_kind") for m in merged] == [None, "cron_notice"]
        assert not _absorbed(merged)
        _, display = db.get_resume_conversations("s1")
        assert [m.get("display_kind") for m in display] == [None, "cron_notice"]
    finally:
        db.close()

