"""Frame sources for the Mac screen: cua-driver (default) or Quartz (when the gateway's own Python may capture).

Both return a :class:`Frame`: an RGB image already fitted to the phone-sized framebuffer, plus the native pixel
size and backing scale (input is injected in native pixels, so the translator needs the ratio).

Measured ceilings on an M2 MacBook with a persistent cua-driver session: about 3 to 4 frames per second at any
size, daemon bound (~300 ms per call, window scope too), so cua alone cannot reach 8 to 10 fps. Quartz
``CGDisplayCreateImage`` takes ~40 ms but only returns other apps' windows when the *capturing process* holds
Screen Recording. macOS then needs a separate grant for the gateway's Python; ``auto`` mode therefore uses Quartz
only when ``CGPreflightScreenCaptureAccess`` says yes, so a missing grant can never yield a degraded picture.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import io
import sys
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from PIL import Image, ImageDraw

from tools.bot_desktop.macos.cua import CuaPort

DEFAULT_MAX_EDGE = 1280


@dataclass
class Frame:
    image: Image.Image          # RGB, framebuffer size
    native_size: tuple[int, int]
    scale_factor: float         # backing scale of the display (2.0 on Retina)

    @property
    def native_per_fb(self) -> tuple[float, float]:
        w, h = self.image.size
        return self.native_size[0] / w, self.native_size[1] / h


class FrameSource(Protocol):
    name: str

    def grab(self) -> Frame: ...


def fit_size(native: tuple[int, int], max_edge: int) -> tuple[int, int]:
    """Largest size with the native aspect whose long edge is at most ``max_edge`` (never upscales)."""
    w, h = native
    long_edge = max(w, h)
    if long_edge <= max_edge:
        return w, h
    ratio = max_edge / long_edge
    return max(1, round(w * ratio)), max(1, round(h * ratio))


def fit_image(image: Image.Image, max_edge: int) -> Image.Image:
    target = fit_size(image.size, max_edge)
    if target == image.size:
        return image
    if image.size[0] >= 3 * target[0]:  # box-reduce first: 7 ms against 38 ms for a straight bilinear at 2x
        image = image.reduce(2)
    return image.resize(target, Image.Resampling.BILINEAR)


def apply_masks(image: Image.Image, rects_native: list[tuple[int, int, int, int]], native_size: tuple[int, int]) -> None:
    """Paint the deny-listed windows black, in place. Rects are native pixels; ``image`` is framebuffer size."""
    if not rects_native:
        return
    fx = image.size[0] / native_size[0]
    fy = image.size[1] / native_size[1]
    draw = ImageDraw.Draw(image)
    for x, y, w, h in rects_native:
        draw.rectangle((int(x * fx), int(y * fy), int((x + w) * fx) + 1, int((y + h) * fy) + 1), fill=(0, 0, 0))


class CuaCapture:
    name = "cua"

    def __init__(self, cua: CuaPort, max_edge: int = DEFAULT_MAX_EDGE) -> None:
        self._cua = cua
        self._max_edge = max_edge

    def grab(self) -> Frame:
        data, info = self._cua.desktop_png()
        image = Image.open(io.BytesIO(data)).convert("RGB")
        native = image.size
        scale = float(info.get("scale_factor") or 1.0)
        return Frame(fit_image(image, self._max_edge), native, scale)


class _Quartz:
    """Minimal ctypes binding: no pyobjc dependency."""

    def __init__(self) -> None:
        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics") or "")
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation") or "")
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayCreateImage.restype = ctypes.c_void_p
        cg.CGDisplayCreateImage.argtypes = [ctypes.c_uint32]
        for fn in ("CGImageGetWidth", "CGImageGetHeight", "CGImageGetBytesPerRow"):
            getattr(cg, fn).restype = ctypes.c_size_t
            getattr(cg, fn).argtypes = [ctypes.c_void_p]
        cg.CGImageGetDataProvider.restype = ctypes.c_void_p
        cg.CGImageGetDataProvider.argtypes = [ctypes.c_void_p]
        cg.CGDataProviderCopyData.restype = ctypes.c_void_p
        cg.CGDataProviderCopyData.argtypes = [ctypes.c_void_p]
        cg.CGImageRelease.argtypes = [ctypes.c_void_p]
        cf.CFDataGetBytePtr.restype = ctypes.c_void_p
        cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        cf.CFDataGetLength.restype = ctypes.c_long
        cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.cg, self.cf = cg, cf

    def has_access(self) -> bool:
        return bool(self.cg.CGPreflightScreenCaptureAccess())

    def grab_bgrx(self) -> tuple[bytes, int, int, int]:
        cg, cf = self.cg, self.cf
        img = cg.CGDisplayCreateImage(cg.CGMainDisplayID())
        if not img:
            raise RuntimeError("CGDisplayCreateImage returned NULL")
        try:
            w, h, stride = cg.CGImageGetWidth(img), cg.CGImageGetHeight(img), cg.CGImageGetBytesPerRow(img)
            data = cg.CGDataProviderCopyData(cg.CGImageGetDataProvider(img))
            try:
                raw = ctypes.string_at(cf.CFDataGetBytePtr(data), cf.CFDataGetLength(data))
            finally:
                cf.CFRelease(data)
        finally:
            cg.CGImageRelease(img)
        return raw, w, h, stride


class QuartzCapture:
    name = "quartz"

    def __init__(self, scale_factor: float, max_edge: int = DEFAULT_MAX_EDGE, quartz: Optional[Any] = None) -> None:
        self._q = quartz or _Quartz()
        self._scale = scale_factor
        self._max_edge = max_edge

    def grab(self) -> Frame:
        raw, w, h, stride = self._q.grab_bgrx()
        image = Image.frombuffer("RGB", (w, h), raw, "raw", "BGRX", stride, 1)
        return Frame(fit_image(image, self._max_edge), (w, h), self._scale)


def quartz_available() -> bool:
    """True only on a Mac where THIS process holds Screen Recording (so Quartz returns real windows)."""
    if sys.platform != "darwin":
        return False
    try:
        return _Quartz().has_access()
    except (OSError, AttributeError):
        return False


def pick_source(mode: str, cua: CuaPort, max_edge: int = DEFAULT_MAX_EDGE, *, scale_factor: float = 2.0) -> FrameSource:
    """``cua`` | ``quartz`` | ``auto`` (Quartz when this process may capture, else cua)."""
    if mode == "quartz" or (mode == "auto" and quartz_available()):
        return QuartzCapture(scale_factor, max_edge)
    return CuaCapture(cua, max_edge)
