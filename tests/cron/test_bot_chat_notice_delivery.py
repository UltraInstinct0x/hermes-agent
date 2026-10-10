"""``bot-chat`` delivery in ``notice`` mode: a durable ``cron_notice`` row, never an agent turn."""
import contextlib
import json
import subprocess
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from cron import bot_chat_delivery as queue
from cron import scheduler_delivery as delivery
from hermes_cli.active_sessions import try_acquire_active_session
from hermes_state import SessionDB
from tools import bot_live_delivery as mailbox

# Synthetic vendor-prefixed key, long enough to trip the redactor; never a real credential.
FAKE_SECRET = "sk-" + "B" * 32


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    own = tmp_path / "own-home"
    own.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(own))
    monkeypatch.setattr(delivery._sched, "load_config", dict)
    monkeypatch.setattr(delivery, "_run_bot_chat_turn", Mock(side_effect=AssertionError("notice must not spawn a turn")))
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
    assert delivery._deliver_to_bot_chat(_job(), "for research", "research", mode="notice") is None
    assert [r["content"] for r in _bot_chat_rows(research)[1]] == ["for research"]
    assert not (home / "state.db").exists()  # the sending profile was not touched


def test_notice_to_missing_profile_dir_says_target_no_longer_exists(home):
    from hermes_cli.profiles import get_profile_dir

    error = delivery._deliver_to_bot_chat(_job(), "x", "ghost", mode="notice")
    assert error and "no longer exists" in error and "do not resend" in error
    assert not get_profile_dir("ghost").exists()


def test_turn_mode_is_untouched_by_default(home, monkeypatch):
    """``turn`` keeps injecting a user message: the notice lane must not capture the default."""
    owner = dict(profile_home=str(home.resolve()), session_id="bot", lease_id="lease", live_session_id="live")
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda _home: owner)
    assert "queued" in delivery._deliver_to_bot_chat(_job(), "payload", "")
    [record] = [mailbox.read_delivery_result(home, p.stem) for p in (home / "runtime/bot_live_delivery").glob("*.json")]
    assert "kind" not in record and record["message"].endswith("payload") and "Cronjob" in record["message"]


# ── live owner: mailbox envelope, applied by the poller ──────────────────────────────────────


def test_live_owner_receives_notice_envelope_not_a_turn(home, monkeypatch):
    owner = dict(profile_home=str(home.resolve()), session_id="bot", lease_id="lease", live_session_id="live")
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda _home: owner)
    job = _job()
    pending = delivery._deliver_to_bot_chat(job, "payload", "", mode="notice")
    assert pending and "queued" in pending
    [path] = list((home / "runtime/bot_live_delivery").glob("*.json"))
    record = mailbox.read_delivery_result(home, path.stem)
    assert record["kind"] == "notice" and record["message"] == "payload"  # raw text, no instruction framing
    assert record["notice"]["cron_job_name"] == "Morning digest" and record["notice"]["delivery_id"] == path.stem
    # Same run again: the receipt answers, nothing new is queued.
    assert delivery._deliver_to_bot_chat(dict(job), "payload", "", mode="notice") == pending
    assert len(list((home / "runtime/bot_live_delivery").glob("*.json"))) == 1
    # A turn envelope under the same id is a different payload, never an overwrite.
    with pytest.raises(ValueError):
        mailbox.deliver_to_live_owner(home, owner, "payload", delivery_id=path.stem)


def _poller(home):
    from tui_gateway import session_notifications
    from tui_gateway.method_ctx import rebind

    events = []
    db_holder = {}

    @contextlib.contextmanager
    def session_db(_session):
        yield db_holder["db"]

    apply = rebind(session_notifications._apply_cron_notice, {
        "contextlib": contextlib,
        "_emit": lambda event, sid, payload: events.append((event, sid, payload)),
        "_session_db": session_db,
        "_submit_row_target_key": lambda session: session["session_key"],
        "logger": __import__("logging").getLogger("test"),
    })
    return apply, events, db_holder


def test_live_poller_posts_row_updates_history_emits_event_and_settles(home):
    db = SessionDB(db_path=home / "state.db")
    db.create_session("chat", source="cli")
    db.set_session_title("chat", "Bot Chat")
    owner = dict(profile_home=str(home.resolve()), session_id="chat", lease_id="lease", live_session_id="live")
    notice = {"delivery_id": "a" * 64, "cron_job_id": "digest", "cron_job_name": "Morning digest",
              "cron_run_id": "run-1", "source": "cron", "notification_category": "result"}
    mailbox.deliver_to_live_owner(home, owner, "3 tasks done", delivery_id="a" * 64, notice=notice)
    claimed = mailbox.claim_pending_delivery(home, owner)
    apply, events, holder = _poller(home)
    holder["db"] = db
    session = dict(session_key="chat", history=[{"role": "user", "content": "hi"}],
                   history_lock=threading.RLock(), agent=None, running=False)
    apply("live", session, home, claimed)

    [row] = [r for r in db.get_messages("chat") if r["display_kind"] == "cron_notice"]
    assert row["role"] == "assistant" and row["content"] == "3 tasks done"
    [(event, sid, payload)] = events
    assert (event, sid) == ("message.notice", "live")
    assert payload["row_id"] == row["id"] and payload["role"] == "assistant"
    assert payload["display_kind"] == "cron_notice" and payload["text"] == "3 tasks done"
    assert payload["display_metadata"]["cron_job_name"] == "Morning digest"
    assert session["history"][-1]["display_kind"] == "cron_notice" and session["history"][-1]["_row_id"] == row["id"]
    assert session["running"] is False  # no agent turn was started
    assert mailbox.read_delivery_result(home, "a" * 64)["status"] == "settled"
    from tui_gateway.contracts.registry import check_payload
    check_payload("message.notice", payload)

    # A replayed envelope for the same delivery id adds neither a second row nor a second event.
    mailbox.deliver_to_live_owner(home, owner, "3 tasks done", delivery_id="b" * 64, notice=dict(notice, delivery_id="a" * 64))
    apply("live", session, home, mailbox.claim_pending_delivery(home, owner))
    assert len([r for r in db.get_messages("chat") if r["display_kind"] == "cron_notice"]) == 1
    assert len(events) == 1
    db.close()


def test_live_poller_failed_write_settles_failed_never_replays(home):
    db = SessionDB(db_path=home / "state.db")
    db.create_session("chat", source="cli")
    owner = dict(profile_home=str(home.resolve()), session_id="chat", lease_id="lease", live_session_id="live")
    mailbox.deliver_to_live_owner(home, owner, "x", delivery_id="c" * 64, notice={"delivery_id": "c" * 64})
    claimed = mailbox.claim_pending_delivery(home, owner)
    apply, events, holder = _poller(home)
    holder["db"] = db
    session = dict(session_key="missing-session", history=[], history_lock=threading.RLock(), agent=None)
    db.close()  # the write now fails
    apply("live", session, home, claimed)
    assert mailbox.read_delivery_result(home, "c" * 64)["status"] == "failed"
    assert events == []


# ── unsupported CLI owner: deferred, then posted when it exits ───────────────────────────────


def test_cli_owner_defers_notice_then_drain_posts_row_without_a_turn(home):
    db = SessionDB(db_path=home / "state.db")
    db.create_session(session_id="chat", source="cli")
    db.set_session_title("chat", "Bot Chat")
    lease, refusal = try_acquire_active_session(session_id="chat", surface="cli", config={}, registry_home=home)
    assert refusal is None and lease is not None
    job = _job()
    try:
        assert "queued" in delivery._deliver_to_bot_chat(job, "output", "", mode="notice")
        key = job["_bot_chat_delivery_receipts"]["bot-chat:(own)"]["delivery_id"]
        assert queue.read_pending(key)["mode"] == "notice"
        queue.drain()
        assert [r for r in db.get_messages("chat") if r["display_kind"] == "cron_notice"] == []  # still owned
        lease.release()
        queue.drain()
        [row] = [r for r in db.get_messages("chat") if r["display_kind"] == "cron_notice"]
        assert row["role"] == "assistant" and row["content"] == "output"
        assert queue.read_pending(key)["status"] == "transferred"
        queue.drain()
        assert len([r for r in db.get_messages("chat") if r["display_kind"] == "cron_notice"]) == 1
    finally:
        lease.release()
        db.close()


def test_deferred_notice_for_vanished_target_is_reported_not_recreated(home):
    db = SessionDB(db_path=home / "state.db")
    db.create_session(session_id="chat", source="cli")
    db.set_session_title("chat", "Bot Chat")
    db.close()
    record = queue.defer("d" * 64, _job(), "output", "", home, mode="notice")
    (home / "state.db").unlink()
    error = delivery._deliver_to_bot_chat(_job(), "output", "", deferred=record)
    assert error and "no longer exists" in error and "do not resend" in error
    assert not (home / "state.db").exists()


def test_defer_refuses_same_id_with_other_mode(home):
    queue.defer("e" * 64, _job(), "output", "", home, mode="notice")
    with pytest.raises(ValueError):
        queue.defer("e" * 64, _job(), "output", "", home)


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
