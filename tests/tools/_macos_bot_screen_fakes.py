"""Fakes for the Mac Bot Screen tests: a cua-driver that records what it was asked and serves canned frames."""

from __future__ import annotations

import io
import threading
from typing import Any

from PIL import Image, ImageDraw

from tools.bot_desktop.macos.cua import CuaError


def png_bytes(size=(400, 260), colour=(30, 60, 90), box=None) -> bytes:
    img = Image.new("RGB", size, colour)
    if box:
        ImageDraw.Draw(img).rectangle(box, fill=(250, 250, 250))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class FakeCua:
    """Implements ``CuaPort``. ``frames`` is consumed in order (the last one repeats)."""

    def __init__(self, frames=None, windows=None, scale=2.0):
        self.frames = list(frames or [png_bytes()])
        self.windows = windows or []
        self.scale = scale
        self.calls: list[tuple] = []
        self.fail_windows = False
        self.fail_capture = False
        self._lock = threading.Lock()
        self.captures = 0

    def _log(self, *call: Any) -> None:
        with self._lock:
            self.calls.append(call)

    def desktop_png(self):
        if self.fail_capture:
            raise CuaError("capture refused")
        with self._lock:
            data = self.frames[min(self.captures, len(self.frames) - 1)]
            self.captures += 1
        w, h = Image.open(io.BytesIO(data)).size
        return data, {"scale_factor": self.scale, "screenshot_width": w, "screenshot_height": h}

    def list_windows(self):
        if self.fail_windows:
            raise CuaError("list_windows failed")
        return self.windows

    def move(self, x, y): self._log("move", x, y)
    def click(self, x, y, *, button="left", count=1): self._log("click", x, y, button, count)
    def drag(self, x0, y0, x1, y1, *, button="left"): self._log("drag", x0, y0, x1, y1, button)
    def scroll(self, x, y, direction, amount): self._log("scroll", x, y, direction, amount)
    def press_key(self, key, modifiers): self._log("press_key", key, list(modifiers))
    def type_text(self, text): self._log("type_text", text)
    def close(self): self._log("close")

    def acted(self) -> list[tuple]:
        with self._lock:
            return [c for c in self.calls if c[0] != "close"]
