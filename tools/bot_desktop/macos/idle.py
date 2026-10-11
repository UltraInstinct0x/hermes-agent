"""Seconds since the last physical keyboard or mouse event on this Mac.

Used by take-control: when somebody is sitting at the Mac, the phone must not silently fight for their cursor.
Read from the HID system state (``CGEventSourceSecondsSinceLastEventType``), which counts hardware input and
does not move for screen captures. Returns ``None`` when the value cannot be read, which callers treat as
"unknown", never as "idle".
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
from typing import Callable, Optional

_HID_SYSTEM_STATE = 1
_ANY_INPUT_EVENT = 0xFFFFFFFF  # kCGAnyInputEventType

# Take-control asks for a second confirmation when input was seen more recently than this.
DEFAULT_PRESENT_WITHIN_S = 20.0

IdleSource = Callable[[], Optional[float]]


def seconds_since_input() -> Optional[float]:
    if sys.platform != "darwin":
        return None
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("CoreGraphics") or "")
        fn = lib.CGEventSourceSecondsSinceLastEventType
        fn.restype = ctypes.c_double
        fn.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        value = float(fn(_HID_SYSTEM_STATE, _ANY_INPUT_EVENT))
    except (OSError, AttributeError, ValueError):
        return None
    return value if value >= 0 else None


def user_present(source: IdleSource = seconds_since_input, within_s: float = DEFAULT_PRESENT_WITHIN_S) -> Optional[float]:
    """Idle seconds when the user touched the Mac within ``within_s``, else ``None`` (nobody there, or unknown)."""
    idle = source()
    return idle if idle is not None and idle < within_s else None
