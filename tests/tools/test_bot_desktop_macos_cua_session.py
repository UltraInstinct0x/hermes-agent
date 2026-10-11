"""CuaSession against fake ``cua-driver`` scripts: a child that dies says why, stderr is kept privately, the child
is reaped, and only one MCP session may own a live child. No real driver, screen or consent prompt."""

from __future__ import annotations

import os
import stat
import sys

import pytest

from tools.bot_desktop.macos import cua, runtime as mac
from tools.bot_desktop.macos.cua import CuaError, CuaSession

_DIES = """#!{py}
import sys
sys.stderr.write("cua-driver-rs: incompatible daemon: contract version 0.8.0 does not match SDK 0.7.0\\n")
sys.exit(1)
"""

_SERVES = """#!{py}
import json, sys
for raw in sys.stdin:
    msg = json.loads(raw)
    if "id" in msg:
        result = {{"structuredContent": {{"ok": True}}}} if msg["method"] == "tools/call" else {{}}
        print(json.dumps({{"jsonrpc": "2.0", "id": msg["id"], "result": result}}), flush=True)
"""


def _fake_driver(tmp_path, body: str, name: str = "cua-driver") -> str:
    path = tmp_path / name
    path.write_text(body.format(py=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture(autouse=True)
def _no_leaked_owner():
    cua._active = None
    yield
    cua._active = None


def test_a_driver_that_exits_at_once_has_its_stderr_line_in_the_error(tmp_path):
    session = CuaSession(_fake_driver(tmp_path, _DIES), env={}, log_path=tmp_path / "cua-mcp.log")
    with pytest.raises(CuaError) as exc:
        session.call("list_windows")
    assert "contract version 0.8.0 does not match SDK 0.7.0" in str(exc.value)
    assert "—" not in str(exc.value)
    assert (tmp_path / "cua-mcp.log").stat().st_mode & 0o777 == 0o600


def test_the_error_quotes_only_the_failing_spawn_and_redacts_secrets(tmp_path):
    log = tmp_path / "cua-mcp.log"
    log.write_text("old line from an earlier run\n", encoding="utf-8")
    body = _DIES.replace("cua-driver-rs: incompatible", "API_KEY=sk-abcdef123456789 incompatible")
    session = CuaSession(_fake_driver(tmp_path, body), env={}, log_path=log)
    with pytest.raises(CuaError) as exc:
        session.call("list_windows")
    assert "old line" not in str(exc.value) and "sk-abcdef" not in str(exc.value)
    assert len(str(exc.value)) < 300


def test_without_a_log_path_the_error_is_still_calm(tmp_path):
    session = CuaSession(_fake_driver(tmp_path, _DIES), env={})
    with pytest.raises(CuaError, match="cua-driver exited"):
        session.call("list_windows")


def test_the_stderr_log_is_rotated_past_its_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(cua, "STDERR_LOG_MAX_BYTES", 10)
    log = tmp_path / "cua-mcp.log"
    log.write_text("x" * 50, encoding="utf-8")
    session = CuaSession(_fake_driver(tmp_path, _DIES), env={}, log_path=log)
    with pytest.raises(CuaError):
        session.call("list_windows")
    assert (tmp_path / "cua-mcp.log.1").read_text(encoding="utf-8") == "x" * 50
    assert "x" * 50 not in log.read_text(encoding="utf-8")


def test_close_stops_and_reaps_the_child(tmp_path):
    session = CuaSession(_fake_driver(tmp_path, _SERVES), env={})
    assert session.call("list_windows") == {"ok": True}
    proc = session._proc
    assert proc is not None and proc.poll() is None
    session.close()
    assert proc.poll() is not None  # reaped: a return code, not a zombie
    assert session._proc is None


def test_a_second_session_cannot_start_a_concurrent_child(tmp_path):
    driver = _fake_driver(tmp_path, _SERVES)
    first, second = CuaSession(driver, env={}), CuaSession(driver, env={})
    first.call("list_windows")
    try:
        with pytest.raises(CuaError, match="already running"):
            second.call("list_windows")
    finally:
        first.close()
    assert second.call("list_windows") == {"ok": True}  # allowed once the first is gone
    second.close()


def test_the_app_binary_that_matches_the_daemon_beats_the_pinned_copy(tmp_path, monkeypatch):
    app = _fake_driver(tmp_path, _SERVES, "app-driver")
    monkeypatch.delenv("HERMES_CUA_DRIVER_CMD", raising=False)
    monkeypatch.setattr(mac, "DEFAULT_APP", app)
    import tools.computer_use.cua_backend_driver as drv
    monkeypatch.setattr(drv, "resolve_cua_driver_cmd", lambda *a, **k: "/pinned/old/cua-driver")
    assert mac.find_cua_driver() == app


def test_an_explicit_override_still_wins(tmp_path, monkeypatch):
    app = _fake_driver(tmp_path, _SERVES, "app-driver")
    mine = _fake_driver(tmp_path, _SERVES, "mine")
    monkeypatch.setenv("HERMES_CUA_DRIVER_CMD", mine)
    monkeypatch.setattr(mac, "DEFAULT_APP", app)
    assert mac.find_cua_driver() == mine
