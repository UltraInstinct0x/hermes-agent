"""``/yolo`` from the slash worker must reach the gateway's live bypass flag, not only the session row.

The worker toggles YOLO in its own process and persists the row; approvals are checked against the gateway's own
in-memory flag. Without a mirror, ``/yolo off`` read OFF in the row while every approval stayed bypassed (and a
restart then restored the old flag from the row)."""

import contextlib
import json
from types import SimpleNamespace

import pytest

from tools import approval
from tui_gateway import server

KEY = "20261006_yolo_mirror"


class _Db:
    def __init__(self, rows):
        self.rows = rows

    def get_session(self, session_id):
        return self.rows.get(session_id)


def _row(yolo):
    return {"model_config": json.dumps({"yolo_mode": yolo})}


@pytest.fixture(autouse=True)
def _clean_flag():
    approval.disable_session_yolo(KEY)
    yield
    approval.disable_session_yolo(KEY)


def _wire(monkeypatch, rows):
    db = _Db(rows)
    monkeypatch.setattr(server, "_session_db", lambda session: contextlib.nullcontext(db))
    emitted = []
    monkeypatch.setattr(server, "_emit", lambda *a, **k: emitted.append(a))
    monkeypatch.setattr(server, "_session_info", lambda agent, session: {"yolo": approval.is_session_yolo_enabled(KEY)})
    return emitted


def _session():
    return {"session_key": KEY, "agent": SimpleNamespace(session_id=KEY)}


def test_yolo_off_in_the_row_turns_the_live_flag_off(monkeypatch):
    emitted = _wire(monkeypatch, {KEY: _row(False)})
    approval.enable_session_yolo(KEY)

    server._mirror_slash_side_effects("sid", _session(), "/yolo off")

    assert approval.is_session_yolo_enabled(KEY) is False
    assert emitted and emitted[-1][0] == "session.info"


def test_yolo_on_in_the_row_turns_the_live_flag_on(monkeypatch):
    _wire(monkeypatch, {KEY: _row(True)})

    server._mirror_slash_side_effects("sid", _session(), "/yolo")

    assert approval.is_session_yolo_enabled(KEY) is True


def test_a_missing_row_changes_nothing(monkeypatch):
    emitted = _wire(monkeypatch, {})
    approval.enable_session_yolo(KEY)

    server._mirror_slash_side_effects("sid", _session(), "/yolo off")

    assert approval.is_session_yolo_enabled(KEY) is True
    assert emitted == []
