"""Posting a cron notice must not deadlock the poller on the session's own ``history_lock``.

``_session_turn_admission`` holds ``session["history_lock"]`` for the whole claim, and the lock is a plain,
non-re-entrant ``threading.Lock`` in production. ``_apply_cron_notice`` used to take it again, so the poller
blocked on itself after writing the row and the whole session stayed wedged (every ``session.resume`` answered
``4009 session busy``). The other tests use an ``RLock``, which hides exactly this."""
import contextlib
import logging
import threading
from types import SimpleNamespace

from hermes_state import SessionDB
from tools import bot_live_delivery as mailbox
from tui_gateway import session_notifications
from tui_gateway.method_ctx import rebind
from tui_gateway.session_lifecycle import _session_turn_admission


def test_the_poller_posts_a_notice_and_releases_the_session_lock(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("chat", source="cli")
    db.set_session_title("chat", "Bot Chat")
    owner = dict(profile_home=str(tmp_path.resolve()), session_id="chat", lease_id="lease", live_session_id="live")
    notice = {"delivery_id": "a" * 64, "cron_job_id": "digest", "cron_job_name": "Morning digest",
              "cron_run_id": "run-1", "source": "cron", "notification_category": "result"}
    mailbox.deliver_to_live_owner(tmp_path, owner, "good morning", delivery_id="a" * 64, notice=notice)
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", lambda home: owner)

    events = []

    @contextlib.contextmanager
    def session_db(_session):
        yield db

    apply = rebind(session_notifications._apply_cron_notice, {
        "contextlib": contextlib,
        "_emit": lambda event, sid, payload: events.append((event, sid)),
        "_session_db": session_db,
        "_submit_row_target_key": lambda session: session["session_key"],
        "logger": logging.getLogger("test"),
    })
    poll = rebind(session_notifications._poll_bot_live_delivery_once, {
        "_session_home": lambda session: tmp_path,
        "_session_turn_admission": _session_turn_admission,
        "_apply_cron_notice": apply,
        "logger": logging.getLogger("test"),
    })
    session = {"history_lock": threading.Lock(), "history": [], "session_key": "chat", "agent": object(),
               "active_session_lease": SimpleNamespace(lease_id="lease", released=False)}

    result = []
    worker = threading.Thread(target=lambda: result.append(poll("live", session)), daemon=True)
    worker.start()
    worker.join(10)

    assert not worker.is_alive(), "the poller deadlocked on its own history_lock"
    assert result == [True]
    assert [e[0] for e in events] == ["message.notice"]
    assert session["history"][-1]["content"] == "good morning"
    assert mailbox.read_delivery_result(tmp_path, "a" * 64)["status"] == "settled"
    assert session["history_lock"].acquire(blocking=False), "the session lock must be free afterwards"
    session["history_lock"].release()
    db.close()
