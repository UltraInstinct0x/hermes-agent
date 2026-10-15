"""cua-driver as the Mac screen's eyes and hands: one persistent MCP-over-stdio session.

Measured on an M2 MacBook (see the Bot Screen on macOS notes): a persistent ``cua-driver mcp`` session is about
1.7x faster than spawning ``cua-driver call`` per frame, and the daemon serialises calls, so there is nothing to
gain from pipelining. A full-desktop capture costs roughly 300 ms whatever the size, and desktop-scope input
300 to 500 ms per call. :class:`CuaPort` is the narrow surface the rest of the package uses, so tests drive a
fake with no binary and no screen.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import weakref
from pathlib import Path
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)

_PROTOCOL = "2025-06-18"
CALL_TIMEOUT_S = 20.0
STDERR_LOG_MAX_BYTES = 256 * 1024
_active: Optional[weakref.ref[CuaSession]] = None  # the one session allowed to own a live ``cua-driver mcp``


class CuaError(RuntimeError):
    """cua-driver did not do what was asked (transport, tool error, or timeout)."""


class CuaPort(Protocol):
    def desktop_png(self) -> tuple[bytes, dict[str, Any]]: ...
    def list_windows(self) -> list[dict[str, Any]]: ...
    def move(self, x: int, y: int) -> None: ...
    def click(self, x: int, y: int, *, button: str = "left", count: int = 1) -> None: ...
    def drag(self, x0: int, y0: int, x1: int, y1: int, *, button: str = "left") -> None: ...
    def scroll(self, x: int, y: int, direction: str, amount: int) -> None: ...
    def press_key(self, key: str, modifiers: list[str]) -> None: ...
    def type_text(self, text: str) -> None: ...
    def close(self) -> None: ...


class CuaSession:
    """Spawns ``<cua-driver> mcp`` and speaks newline-delimited JSON-RPC to it. Calls are serialised by a lock;
    a dead process is reported as :class:`CuaError` and respawned by the next call."""

    def __init__(self, command: str, *, env: Optional[dict[str, str]] = None, scratch_dir: Optional[Path] = None,
                 log_path: Optional[Path] = None) -> None:
        self._command = command
        self._log_path = log_path
        self._log_start = 0
        self._env = env
        self._scratch = scratch_dir
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._lines: queue.Queue[Optional[dict[str, Any]]] = queue.Queue()
        self._next_id = 0

    # -- transport -------------------------------------------------------------------------------------

    def _open_stderr_log(self) -> Any:
        """Append-mode, owner-only stderr log (rotated once it grows past the cap); DEVNULL without a path.
        ``_log_start`` marks where this spawn's output begins so a failure quotes only its own line."""
        if self._log_path is None:
            return subprocess.DEVNULL
        try:
            if self._log_path.stat().st_size > STDERR_LOG_MAX_BYTES:
                os.replace(self._log_path, self._log_path.with_name(self._log_path.name + ".1"))
        except OSError:
            pass
        try:
            fd = os.open(self._log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        except OSError:
            return subprocess.DEVNULL
        self._log_start = os.fstat(fd).st_size
        return os.fdopen(fd, "ab", buffering=0)

    def _exit_detail(self) -> str:
        """``": <last stderr line>"`` of the dead child (redacted, short), or an empty string."""
        if self._log_path is None:
            return ""
        from tools.bot_desktop.macos import preflight
        line = preflight.log_tail_summary(self._log_path, since=self._log_start)
        return f": {line}" if line else ""

    def _spawn(self) -> None:
        global _active
        other = _active() if _active is not None else None
        if other is not None and other is not self and other._proc is not None and other._proc.poll() is None:
            raise CuaError("another Mac screen capture session is already running")
        stderr = self._open_stderr_log()
        try:
            self._proc = subprocess.Popen([self._command, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=stderr, env=self._env, bufsize=0)
        finally:
            if stderr is not subprocess.DEVNULL:
                stderr.close()
        _active = weakref.ref(self)
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self._proc, self._lines), daemon=True, name="cua-mcp-reader").start()
        self._rpc("initialize", {"protocolVersion": _PROTOCOL, "capabilities": {},
                                 "clientInfo": {"name": "hermes-bot-screen", "version": "1"}})
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    @staticmethod
    def _pump(proc: subprocess.Popen, out: queue.Queue[Optional[dict[str, Any]]]) -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            try:
                out.put(json.loads(raw))
            except ValueError:
                continue
        out.put(None)

    def _write(self, message: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write((json.dumps(message) + "\n").encode())
            self._proc.stdin.flush()
        except OSError as exc:
            try:  # a child that died at startup has written its reason by the time it is reaped
                self._proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
            raise CuaError(f"cua-driver exited{self._exit_detail()}") from exc

    def _rpc(self, method: str, params: dict[str, Any], timeout: float = CALL_TIMEOUT_S) -> dict[str, Any]:
        self._next_id += 1
        rid = self._next_id
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            try:
                message = self._lines.get(timeout=timeout)
            except queue.Empty as exc:
                self._kill()
                raise CuaError(f"cua-driver {method} timed out after {timeout:.0f}s") from exc
            if message is None:
                self._kill()
                raise CuaError(f"cua-driver exited{self._exit_detail()}")
            if message.get("id") == rid:
                if "error" in message:
                    raise CuaError(str(message["error"].get("message") or message["error"])[:300])
                return message.get("result") or {}

    def _kill(self) -> None:
        """Stop and reap the child so no ``cua-driver mcp`` outlives the session (no zombie either)."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        except OSError:
            pass

    def call(self, name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """One tool call; returns ``structuredContent`` (``{}`` when absent). Tool-level errors raise."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._kill()
                self._spawn()
            result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            text = " ".join(str(c.get("text", "")) for c in result.get("content", []) if isinstance(c, dict))
            raise CuaError(f"{name}: {text[:300] or 'tool error'}")
        structured = result.get("structuredContent")
        return structured if isinstance(structured, dict) else {}

    def close(self) -> None:
        with self._lock:
            self._kill()

    # -- the CuaPort surface -----------------------------------------------------------------------------

    def desktop_png(self) -> tuple[bytes, dict[str, Any]]:
        """Native-pixel PNG of the whole primary display plus the tool's metadata (screen size in points,
        ``scale_factor``, screenshot size in pixels)."""
        directory = str(self._scratch) if self._scratch else None
        fd, path = tempfile.mkstemp(suffix=".png", dir=directory)
        os.close(fd)
        try:
            info = self.call("get_desktop_state", {"screenshot_out_file": path})
            data = Path(path).read_bytes()
        finally:
            Path(path).unlink(missing_ok=True)
        if not data:
            raise CuaError("get_desktop_state returned no image")
        return data, info

    def list_windows(self) -> list[dict[str, Any]]:
        windows = self.call("list_windows").get("windows")
        return [w for w in windows if isinstance(w, dict)] if isinstance(windows, list) else []

    def move(self, x: int, y: int) -> None:
        self.call("move_cursor", {"x": x, "y": y, "scope": "desktop"})

    def click(self, x: int, y: int, *, button: str = "left", count: int = 1) -> None:
        self.call("click", {"x": x, "y": y, "scope": "desktop", "button": button, "count": count})

    def drag(self, x0: int, y0: int, x1: int, y1: int, *, button: str = "left") -> None:
        self.call("drag", {"from_x": x0, "from_y": y0, "to_x": x1, "to_y": y1, "scope": "desktop",
                           "button": button, "duration_ms": 200, "steps": 10})

    def scroll(self, x: int, y: int, direction: str, amount: int) -> None:
        self.call("scroll", {"x": x, "y": y, "scope": "desktop", "direction": direction, "amount": amount})

    def press_key(self, key: str, modifiers: list[str]) -> None:
        args: dict[str, Any] = {"key": key, "scope": "desktop"}
        if modifiers:
            args["modifiers"] = modifiers
        self.call("press_key", args)

    def type_text(self, text: str) -> None:
        self.call("type_text", {"text": text, "scope": "desktop"})
