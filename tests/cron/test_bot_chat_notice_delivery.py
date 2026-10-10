"""``bot-chat`` delivery in ``notice`` mode: a durable ``cron_notice`` row, never an agent turn."""
import json
from pathlib import Path

import pytest

from cron import scheduler_delivery as delivery
from hermes_state import SessionDB

# Synthetic vendor-prefixed key, long enough to trip the redactor; never a real credential.
FAKE_SECRET = "sk-" + "B" * 32


def _seed_bot_chat(home: Path) -> str:
    """The target's authority owns creating Bot Chat; a notice only posts into an existing one."""
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session("chat", source="cli")
        db.set_session_title("chat", "Bot Chat")
    finally:
        db.close()
    return "chat"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    own = tmp_path / "own-home"
    own.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(own))
    monkeypatch.setattr(delivery._sched, "load_config", dict)
    _seed_bot_chat(own)
    return own


def _bot_chat_rows(home: Path):
    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        row = db.get_session_by_title("Bot Chat")
        assert row is not None, "Bot Chat session was not created"
        return row["id"], db.get_messages(row["id"])
    finally:
        db.close()


def _job(**fields):
    return {"id": "digest", "name": "Morning digest", "execution_id": "run-1", **fields}


# ── token / mode resolution ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("token, profile, mode", [
    ("bot-chat", "", ""),
    ("bot-chat+notice", "", "notice"),
    ("bot-chat:research+notice", "research", "notice"),
    ("BOT-CHAT:Research+Turn", "Research", "turn"),
    ("bot-chat+bogus", "", "bogus"),
])
def test_token_parsing_keeps_profile_name_and_exposes_mode(token, profile, mode):
    assert delivery.parse_bot_chat_deliver_token(token) == profile
    assert delivery.parse_bot_chat_deliver_mode(token) == mode


def test_non_bot_chat_tokens_have_no_mode():
    assert delivery.parse_bot_chat_deliver_token("telegram:+notice") is None
    assert delivery.parse_bot_chat_deliver_mode("telegram:123") is None


def test_mode_precedence_token_then_job_then_config_then_turn(monkeypatch):
    monkeypatch.setattr(delivery._sched, "load_config", lambda: {"cron": {"bot_chat_delivery_mode": "notice"}})
    assert delivery._bot_chat_delivery_mode({}) == "notice"                       # config
    assert delivery._bot_chat_delivery_mode({"bot_chat_delivery_mode": "turn"}) == "turn"   # job beats config
    assert delivery._bot_chat_delivery_mode({"bot_chat_delivery_mode": "turn"}, "notice") == "notice"  # token wins
    monkeypatch.setattr(delivery._sched, "load_config", dict)
    assert delivery._bot_chat_delivery_mode({}) == "turn"                         # upstream default


def test_unknown_mode_falls_back_to_turn(monkeypatch):
    monkeypatch.setattr(delivery._sched, "load_config", lambda: {"cron": {"bot_chat_delivery_mode": "loud"}})
    assert delivery._bot_chat_delivery_mode({}) == "turn"


def test_resolved_target_carries_mode(home):
    [target] = delivery._resolve_delivery_targets({"id": "j", "deliver": "bot-chat+notice"})
    assert (target["platform"], target["chat_id"], target["mode"]) == ("bot-chat", "", "notice")
    [target] = delivery._resolve_delivery_targets({"id": "j", "deliver": "bot-chat"})
    assert target["mode"] == "turn"


def test_create_time_validation_rejects_unknown_mode():
    from tools.cronjob_job_args import _validate_bot_chat_deliver

    assert "invalid bot-chat delivery mode" in _validate_bot_chat_deliver("bot-chat+bogus")
    assert _validate_bot_chat_deliver("bot-chat+notice") is None


# ── no live owner: the durable row is the delivery ───────────────────────────────────────────


def test_notice_without_owner_writes_assistant_row_and_no_turn(home):
    job = _job()
    assert delivery._deliver_to_bot_chat(job, "3 tasks done", "", mode="notice") is None
    session_id, rows = _bot_chat_rows(home)
    [row] = rows
    assert row["role"] == "assistant"
    assert row["content"] == "3 tasks done"
    assert row["display_kind"] == "cron_notice"
    meta = row["display_metadata"] if isinstance(row["display_metadata"], dict) else json.loads(row["display_metadata"])
    assert meta["cron_job_id"] == "digest" and meta["cron_job_name"] == "Morning digest"
    assert meta["cron_run_id"] == "run-1" and meta["source"] == "cron"
    assert meta["notification_category"] == "result"
    assert len(meta["delivery_id"]) == 64
    receipt = job["_bot_chat_delivery_receipts"]["bot-chat:(own)"]
    assert receipt == {"status": "settled", "delivery_id": meta["delivery_id"]}
    assert not (home / "runtime" / "bot_live_delivery").exists()  # no mailbox envelope, no wake


def test_notice_is_idempotent_per_run_and_new_run_posts_again(home):
    for _ in range(3):
        assert delivery._deliver_to_bot_chat(_job(), "output", "", mode="notice") is None
    assert len(_bot_chat_rows(home)[1]) == 1
    assert delivery._deliver_to_bot_chat(_job(execution_id="run-2"), "output", "", mode="notice") is None
    assert len(_bot_chat_rows(home)[1]) == 2


def test_notice_row_reaches_history_projection_with_display_fields(home):
    from tui_gateway.server import _history_to_messages

    delivery._deliver_to_bot_chat(_job(), "3 tasks done", "", mode="notice", for_failure=False)
    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        session_id = db.get_session_by_title("Bot Chat")["id"]
        history = db.get_messages_as_conversation(session_id, include_ancestors=True, include_row_ids=True)
    finally:
        db.close()
    [message] = _history_to_messages(history)
    assert message["role"] == "assistant" and message["text"] == "3 tasks done"
    assert message["display_kind"] == "cron_notice"
    assert message["display_metadata"]["cron_job_name"] == "Morning digest"
    assert isinstance(message["row_id"], int)


def test_failure_notice_is_marked_diagnostic(home):
    delivery._deliver_to_bot_chat(_job(), "job failed", "", mode="notice", for_failure=True)
    _, [row] = _bot_chat_rows(home)
    meta = row["display_metadata"] if isinstance(row["display_metadata"], dict) else json.loads(row["display_metadata"])
    assert meta["notification_category"] == "diagnostic"


def test_notice_is_scrubbed_fail_closed(home):
    job = _job(name=f"digest {FAKE_SECRET}")
    delivery._deliver_to_bot_chat(job, f"Token was {FAKE_SECRET}. Done.", "", mode="notice")
    _, [row] = _bot_chat_rows(home)
    assert FAKE_SECRET not in row["content"] and "Done." in row["content"]
    assert FAKE_SECRET not in json.dumps(row["display_metadata"])


def test_notice_never_leaks_to_other_profiles(home):
    from hermes_cli.profiles import get_profile_dir

    research = get_profile_dir("research")
    research.mkdir(parents=True)
    _seed_bot_chat(research)
    assert delivery._deliver_to_bot_chat(_job(), "for research", "research", mode="notice") is None
    assert [r["content"] for r in _bot_chat_rows(research)[1]] == ["for research"]
    assert _bot_chat_rows(home)[1] == []  # the sending profile was not touched


def test_notice_to_missing_profile_dir_says_target_no_longer_exists(home):
    from hermes_cli.profiles import get_profile_dir

    error = delivery._deliver_to_bot_chat(_job(), "x", "ghost", mode="notice")
    assert error and "no longer exists" in error and "do not resend" in error
    assert not get_profile_dir("ghost").exists()



def test_notice_to_profile_without_bot_chat_is_refused_and_creates_nothing(home):
    from hermes_cli.profiles import get_profile_dir

    fresh = get_profile_dir("fresh")
    fresh.mkdir(parents=True)
    error = delivery._deliver_to_bot_chat(_job(), "x", "fresh", mode="notice")
    assert error and "no Bot Chat yet" in error
    assert not (fresh / "state.db").exists()


def test_suppressed_failure_notice_is_booked_not_written(home, monkeypatch):
    monkeypatch.setattr("gateway.warning_notifications.warning_notifications_enabled", lambda *a, **k: False)
    job = _job()
    assert delivery._deliver_to_bot_chat(job, "job failed", "", mode="notice", for_failure=True) is None
    assert job["_bot_chat_delivery_receipts"]["bot-chat:(own)"]["status"] == "suppressed"
    assert job["_notification_all_targets_suppressed"] is True
    assert _bot_chat_rows(home)[1] == []


def test_turn_mode_never_posts_a_notice_row(home, monkeypatch):
    """``turn`` keeps going through the authority door: the notice lane must not capture the default."""
    import tools.bot_live_delivery as mailbox

    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda _home: None)
    error = delivery._deliver_to_bot_chat(_job(), "payload", "")
    assert error and "no canonical Bot Chat" in error
    assert _bot_chat_rows(home)[1] == []



# ── state layer ──────────────────────────────────────────────────────────────────────────────


def test_append_cron_notice_dedupes_on_delivery_id_and_counts_once(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("s", source="cli")
    meta = {"delivery_id": "f" * 64, "cron_job_id": "j"}
    first_id, inserted = db.append_cron_notice("s", "hello", meta)
    again_id, inserted_again = db.append_cron_notice("s", "hello", meta)
    assert (inserted, inserted_again, first_id) == (True, False, again_id)
    assert db.get_session("s")["message_count"] == 1
    with pytest.raises(ValueError):
        db.append_cron_notice("s", "hello", {})
    db.close()