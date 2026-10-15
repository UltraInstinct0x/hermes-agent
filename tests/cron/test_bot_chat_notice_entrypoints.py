"""Notice mode through the entry points the scheduler and the CLI actually use.

``test_bot_chat_notice_delivery.py`` drives ``_deliver_to_bot_chat`` with ``mode=`` passed in. These tests
start one level earlier, where a live box went wrong in practice: the job's ``deliver`` / job field / config
decide the mode, the pre-dispatch validator must accept the ``+notice`` token, and ``cron edit --deliver
bot-chat+notice`` must be storable. Every path ends in a ``cron_notice`` row with no user row and no turn.
"""
from pathlib import Path
from unittest.mock import Mock

import pytest

from cron import scheduler_delivery as delivery
from cron.scheduler_preflight import _preflight_check_delivery
from hermes_state import SessionDB


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    own = tmp_path / "own-home"
    own.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(own))
    monkeypatch.setattr(delivery._sched, "load_config", dict)
    import tools.bot_live_delivery as mailbox

    monkeypatch.setattr(mailbox, "deliver_to_live_owner", Mock(side_effect=AssertionError("notice must not start a turn")))
    db = SessionDB(db_path=own / "state.db")
    db.create_session("chat", source="cli")
    db.set_session_title("chat", "Bot Chat")
    db.close()
    return own


def _roles_and_kinds(home: Path):
    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        row = db.get_session_by_title("Bot Chat")
        assert row is not None, "Bot Chat session was not created"
        return [(m["role"], m["display_kind"]) for m in db.get_messages(row["id"])]
    finally:
        db.close()


# ── pre-dispatch validator ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("deliver", [
    "bot-chat+notice",
    "bot-chat+turn",
    "BOT-CHAT+Notice",
    "bot-chat:research+notice",
    "local,bot-chat+notice",
])
def test_preflight_accepts_bot_chat_mode_suffix(deliver):
    assert _preflight_check_delivery({"id": "j", "deliver": deliver}) is None


def test_preflight_accepts_mode_suffix_on_the_failure_lane():
    job = {"id": "j", "deliver": "local", "failure_deliver": "bot-chat+notice"}
    assert _preflight_check_delivery(job) is None


def test_preflight_still_blocks_unknown_platform_even_with_a_suffix():
    err = _preflight_check_delivery({"id": "j", "deliver": "nonexistent-platform+notice"})
    assert err is not None and "not a known" in err


# ── create / edit validation ─────────────────────────────────────────────────────────────────


def test_edit_deliver_to_notice_token_is_accepted_and_stored(tmp_path, monkeypatch):
    from cron.jobs import create_job, get_job
    from tools.cronjob_tools import cronjob

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = create_job(prompt="p", schedule="every 1h", deliver="bot-chat")
    result = cronjob(action="update", job_id=job["id"], deliver="bot-chat+notice")
    assert '"success": true' in result, result
    assert get_job(job["id"])["deliver"] == "bot-chat+notice"


def test_edit_deliver_to_unknown_mode_is_rejected(tmp_path, monkeypatch):
    from cron.jobs import create_job, get_job
    from tools.cronjob_tools import cronjob

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    job = create_job(prompt="p", schedule="every 1h", deliver="bot-chat")
    result = cronjob(action="update", job_id=job["id"], deliver="bot-chat+bogus")
    assert "invalid bot-chat delivery mode" in result
    assert get_job(job["id"])["deliver"] == "bot-chat"


# ── _deliver_result: every source of the mode ends in a notice row, not a user turn ──────────


def _job(**fields):
    return {"id": "fabric", "name": "fabric-doctor", "execution_id": "run-1", "deliver": "bot-chat", **fields}


def test_deliver_result_notice_from_deliver_token(home):
    assert delivery._deliver_result(_job(deliver="bot-chat+notice"), "all clear") is None
    assert _roles_and_kinds(home) == [("assistant", "cron_notice")]


def test_deliver_result_notice_from_job_field(home):
    assert delivery._deliver_result(_job(bot_chat_delivery_mode="notice"), "all clear") is None
    assert _roles_and_kinds(home) == [("assistant", "cron_notice")]


def test_deliver_result_notice_from_config(home, monkeypatch):
    monkeypatch.setattr(delivery._sched, "load_config", lambda: {"cron": {"bot_chat_delivery_mode": "notice"}})
    assert delivery._deliver_result(_job(), "all clear") is None
    assert _roles_and_kinds(home) == [("assistant", "cron_notice")]


def test_delivery_logs_the_resolved_mode(home, caplog):
    caplog.set_level("INFO", logger=delivery.logger.name)
    delivery._deliver_result(_job(bot_chat_delivery_mode="notice"), "all clear")
    assert any("Bot Chat delivery mode=notice" in record.getMessage() for record in caplog.records)


def test_deliver_result_posts_job_output_verbatim_not_the_turn_wrapper(home):
    db_text = "Nothing to do"
    delivery._deliver_result(_job(bot_chat_delivery_mode="notice"), db_text)
    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        [message] = db.get_messages(db.get_session_by_title("Bot Chat")["id"])
    finally:
        db.close()
    assert message["content"] == db_text
    assert "scheduled job, not the user" not in message["content"]
