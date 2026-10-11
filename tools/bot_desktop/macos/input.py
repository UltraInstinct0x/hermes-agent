"""RFB pointer and key events from the lease holder, as cua-driver desktop-scope input.

This is the only place the Mac's REAL cursor and focus are used: it runs only for a human holding the lease (the
bridge's filter and the server's ``input_allowed`` gate both enforce that), and the bot is paused for that whole
time. The bot's own actions never come through here (they use cua's background primitives).

cua-driver has no mouse-down / mouse-up primitive and costs 300 to 500 ms per desktop call, so a remote pointer
becomes gestures: a press and release without movement is a click (second click inside 400 ms is a double
click), a press, drag and release is one ``drag``, a wheel notch is a ``scroll``, plain movement is coalesced to
the latest position. Consecutive printable keys are typed as one string. Coordinates are framebuffer pixels
scaled to native display pixels.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from tools.bot_desktop.macos.cua import CuaError, CuaPort

logger = logging.getLogger(__name__)

_CLICK_SLOP_FB = 6          # framebuffer pixels between press and release that still count as a click
_DOUBLE_CLICK_S = 0.4
_SCROLL_NOTCHES = 2

_MODIFIERS = {
    0xFFE1: "shift", 0xFFE2: "shift", 0xFFE3: "ctrl", 0xFFE4: "ctrl", 0xFFE9: "option", 0xFFEA: "option",
    0xFFE7: "cmd", 0xFFE8: "cmd", 0xFFEB: "cmd", 0xFFEC: "cmd", 0xFF7E: "option", 0xFFE5: "",
}
_SPECIAL = {
    0xFF0D: "return", 0xFF8D: "return", 0xFF09: "tab", 0xFF1B: "escape", 0xFF08: "delete", 0x20: "space",
    0xFF51: "left", 0xFF52: "up", 0xFF53: "right", 0xFF54: "down", 0xFF50: "home", 0xFF57: "end",
    0xFF55: "pageup", 0xFF56: "pagedown", **{0xFFBE + i: f"f{i + 1}" for i in range(12)},
}
_FORWARD_DELETE = 0xFFFF
_BUTTONS = ((1, "left"), (2, "middle"), (4, "right"))
_WHEEL = {8: "up", 16: "down", 32: "left", 64: "right"}
_SHORTCUT_MODIFIERS = {"ctrl", "option", "cmd"}


def keysym_char(keysym: int) -> Optional[str]:
    """The printable character a keysym types, or None."""
    if 0x20 <= keysym <= 0x7E or 0xA0 <= keysym <= 0xFF:
        return chr(keysym)
    if 0x01000100 <= keysym <= 0x0110FFFF:
        return chr(keysym - 0x01000000)
    return None


@dataclass
class _Press:
    x: int
    y: int
    moved: bool = False


@dataclass
class InputDriver:
    cua: CuaPort
    native_per_fb: Callable[[], tuple[float, float]]
    clock: Callable[[], float] = time.monotonic
    on_activity: Callable[[str], None] = lambda kind: None
    _buttons: int = 0
    _presses: dict = field(default_factory=dict)
    _held: set = field(default_factory=set)
    _last_click: Optional[tuple[str, int, int, float]] = None
    _queue: "queue.Queue" = field(default_factory=queue.Queue)
    _thread: Optional[threading.Thread] = None
    _stop: threading.Event = field(default_factory=threading.Event)
    errors: int = 0

    # -- thread ------------------------------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="mac-bot-screen-input")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)

    def submit(self, event: tuple) -> None:
        self._queue.put(event)

    def _run(self) -> None:
        while not self._stop.is_set():
            first = self._queue.get()
            if first is None:
                return
            batch = [first]
            while True:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is not None:
                    batch.append(nxt)
            self.process(batch)

    # -- translation -------------------------------------------------------------------------------------

    def process(self, events: list[tuple]) -> None:
        """Apply a batch in order after coalescing; never raises (a failed injection is logged and dropped)."""
        for event in coalesce(events):
            try:
                if event[0] == "pointer":
                    self._pointer(event[1], event[2], event[3])
                elif event[0] == "key":
                    self._key(event[1], event[2])
                elif event[0] == "text":
                    self.cua.type_text(event[1])
                    self.on_activity("keys")
            except CuaError as exc:
                self.errors += 1
                logger.warning("mac bot screen input failed: %s", exc)

    def _native(self, x: int, y: int) -> tuple[int, int]:
        fx, fy = self.native_per_fb()
        return round(x * fx), round(y * fy)

    def _pointer(self, mask: int, x: int, y: int) -> None:
        previous, self._buttons = self._buttons, mask
        for bit, button in _BUTTONS:
            was, now = bool(previous & bit), bool(mask & bit)
            if now and not was:
                self._presses[button] = _Press(x, y)
            elif now and was:
                press = self._presses.get(button)
                if press and (abs(x - press.x) > _CLICK_SLOP_FB or abs(y - press.y) > _CLICK_SLOP_FB):
                    press.moved = True
            elif was and not now:
                self._release(button, x, y)
        for bit, direction in _WHEEL.items():
            if mask & bit and not previous & bit:
                nx, ny = self._native(x, y)
                self.cua.scroll(nx, ny, direction, _SCROLL_NOTCHES)
                self.on_activity("scroll")
        if not mask and not previous:
            nx, ny = self._native(x, y)
            self.cua.move(nx, ny)

    def _release(self, button: str, x: int, y: int) -> None:
        press = self._presses.pop(button, None)
        if press is None:
            return
        if press.moved:
            x0, y0 = self._native(press.x, press.y)
            x1, y1 = self._native(x, y)
            self.cua.drag(x0, y0, x1, y1, button=button)
            self.on_activity("drag")
            return
        nx, ny = self._native(press.x, press.y)
        now = self.clock()
        count = 1
        last = self._last_click
        if last and last[0] == button and now - last[3] <= _DOUBLE_CLICK_S and abs(last[1] - nx) < 8 and abs(last[2] - ny) < 8:
            count = 2
        self.cua.click(nx, ny, button=button, count=count)
        self._last_click = (button, nx, ny, now)
        self.on_activity("click")

    def _key(self, down: bool, keysym: int) -> None:
        modifier = _MODIFIERS.get(keysym)
        if modifier is not None:
            if modifier:
                (self._held.add if down else self._held.discard)(modifier)
            return
        if not down:
            return
        held = sorted(self._held)
        char = keysym_char(keysym)
        shortcut = bool(self._held & _SHORTCUT_MODIFIERS)
        if keysym == _FORWARD_DELETE:
            self.cua.press_key("delete", sorted(set(held) | {"fn"}))
        elif keysym in _SPECIAL and (shortcut or char is None):
            self.cua.press_key(_SPECIAL[keysym], held)
        elif char is not None and shortcut:
            self.cua.press_key(char.lower(), held)
        elif char is not None:
            self.cua.type_text(char)
        else:
            return  # a keysym with no Mac meaning (dead keys, media keys): ignored
        self.on_activity("keys")


def coalesce(events: list[tuple]) -> list[tuple]:
    """Drop superseded pointer moves (same button mask, nothing between them) and merge runs of printable key
    presses into one ``("text", s)`` event. Order and every button / modifier transition are preserved."""
    out: list[tuple] = []
    for ev in events:
        if ev[0] == "pointer" and ev[1] == 0 and out and out[-1][0] == "pointer" and out[-1][1] == 0:
            out[-1] = ev  # hover moves: only the last one matters
            continue
        out.append(ev)
    return _merge_text(out)


def _merge_text(events: list[tuple]) -> list[tuple]:
    """Collapse consecutive unmodified printable key-downs (key-ups of printable keys between them are
    dropped: the driver ignores them) into text events."""
    merged: list[tuple] = []
    run: list[str] = []
    held: set[str] = set()

    def flush() -> None:
        if run:
            merged.append(("text", "".join(run)))
            run.clear()

    for ev in events:
        if ev[0] == "key":
            _, down, keysym = ev
            mod = _MODIFIERS.get(keysym)
            if mod is not None:
                flush()
                if mod:
                    (held.add if down else held.discard)(mod)
                merged.append(ev)
                continue
            char = keysym_char(keysym)
            if char is not None and not (held & _SHORTCUT_MODIFIERS):
                if down:
                    run.append(char)
                continue
            flush()
        else:
            flush()
        merged.append(ev)
    flush()
    return merged
