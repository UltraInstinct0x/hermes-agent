"""The process-wide ``_session_resume_lock`` must never wait unbounded on one session's ``history_lock``.

Every reattach (``session.resume`` / ``session.activate`` / ``prompt.submit``) and every
``session.close`` serializes on ``_session_resume_lock``. A reattach that blocked on a live
session's ``history_lock`` while holding it wedged ALL of them process-wide: an unrelated
``session.close`` of an unknown id hung while ``gateway.ping`` and ``session.history`` (no resume
lock) still answered in ms. The rebind now waits a bounded time and answers 4009 (retryable), and
the heavy payload (history_lock snapshot + DB lineage read + message rendering) is built after the
global lock is released.
"""

import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tui_gateway import server

_JOIN_S = 3.0


def _live_session() -> dict:
    return dict(transport=server._detached_ws_transport, running=True, history_lock=threading.Lock(),
                history=[], session_key="stored", agent=SimpleNamespace(model="test"), queued_prompt=None)


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(rid=1, owns_db=False, db=None, cols=80, omit_messages=True,
                           defer_history=False, target="stored", profile=None,
                           profile_home=None, profile_resume_cwd=None, found={},
                           messages=lambda history: [], mint=lambda: ("unused", "tui", "."),
                           restore=lambda: ([], [], []), display_prefix=list, inline_images=True)


class _Socket:
    _closed = False

    def send(self, *a, **kw):
        pass


@pytest.fixture
def live(monkeypatch):
    sid, session = "busy-live", _live_session()
    monkeypatch.setattr(server, "_session_resume_lock", threading.Lock())
    monkeypatch.setattr(server, "_sessions", {sid: session})
    monkeypatch.setattr(server, "_pending_ws_reaps", {})
    monkeypatch.setattr(server, "current_transport", lambda: _Socket())
    monkeypatch.setattr(server, "_sess", lambda *a: (session, None))
    monkeypatch.setattr(server, "_REATTACH_HISTORY_LOCK_WAIT_S", 0.2, raising=False)
    return sid, session


def _reattach(path: str, sid: str, session: dict, monkeypatch) -> dict:
    ctx = _ctx()
    if path == "reuse":
        return server._resume_reuse_live(ctx, sid, session)
    if path == "unpersisted":
        return server._resume_live_unpersisted(ctx, sid, session)
    if path == "eager":
        monkeypatch.setattr(server, "_profile_build_scope", lambda *a: nullcontext())
        monkeypatch.setattr(server, "_make_agent_in_context", lambda *a, **kw: Mock())
        monkeypatch.setattr(server, "_find_live_session_by_key", lambda *a: (sid, session))
        return server._resume_eager(ctx)
    return server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "session.activate",
                                  "params": {"session_id": sid, "omit_messages": True}})


def _in_thread(fn) -> tuple[threading.Thread, list]:
    out: list = []
    thread = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    thread.start()
    return thread, out


@pytest.mark.parametrize("path", ["reuse", "unpersisted", "eager", "activate"])
def test_busy_history_lock_does_not_wedge_unrelated_close(monkeypatch, live, path):
    """A turn holds the live session's history_lock; a reattach of that session must not hold the
    global resume lock while it waits, so a session.close of an unrelated id still answers."""
    sid, session = live
    session["history_lock"].acquire()  # the long-running turn thread
    try:
        reattach, reattach_out = _in_thread(lambda: _reattach(path, sid, session, monkeypatch))
        deadline = time.monotonic() + 1.0
        while not server._session_resume_lock.locked() and reattach.is_alive() and time.monotonic() < deadline:
            time.sleep(0.005)
        close, close_out = _in_thread(lambda: server.handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "session.close", "params": {"session_id": "not-live"}}))
        close.join(_JOIN_S)
        assert not close.is_alive(), "session.close of an unrelated id queued behind a busy session's history_lock"
        assert close_out and "error" not in close_out[0]
        reattach.join(_JOIN_S)
        assert not reattach.is_alive(), "reattach waited unbounded on a busy history_lock"
        assert reattach_out[0]["error"]["code"] == 4009  # retryable: the client retries the resume
        assert sid in server._sessions  # a refused reattach never tears the live session down
    finally:
        session["history_lock"].release()


def test_reuse_live_builds_payload_outside_resume_lock(monkeypatch, live):
    """The live payload (history_lock snapshot, DB lineage read, message rendering) is built after the
    global lock is released; only the refusal check, reap cancel and transport rebind run under it."""
    sid, session = live
    held_during_payload = []
    real_payload = server._live_session_payload

    def payload(*a, **kw):
        held_during_payload.append(server._session_resume_lock.locked())
        return real_payload(*a, **kw)

    monkeypatch.setattr(server, "_live_session_payload", payload)
    response = server._resume_reuse_live(_ctx(), sid, session)
    assert "error" not in response
    assert held_during_payload == [False]
    assert response["result"]["session_id"] == sid
