"""Per-session consent on the Mac itself: a native dialog, default deny after 30 seconds.

macOS permissions are one-time and belong to the capturing process; they are not consent to show the screen to a
phone. Every Bot Screen session therefore asks the person at the Mac. Anything but a clear "Allow" is a no: a
timeout, a closed dialog, no GUI session (SSH, a locked login window), a missing ``osascript``, a nonzero exit.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

TIMEOUT_S = 30
TITLE = "Hermes Bot Screen"


@dataclass(frozen=True)
class Consent:
    allowed: bool
    reason: str  # allowed | denied | timeout | unavailable


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dialog_script(summary: str, timeout_s: int = TIMEOUT_S) -> list[str]:
    """The AppleScript lines (``osascript -e`` each). "Deny" is both the default and the cancel button."""
    return [
        "tell current application",
        "activate",
        f"display dialog {_quote(summary)} with title {_quote(TITLE)} buttons {{\"Deny\", \"Allow\"}} "
        f"default button \"Deny\" cancel button \"Deny\" with icon caution giving up after {int(timeout_s)}",
        "end tell",
    ]


def parse_result(stdout: str) -> Consent:
    text = (stdout or "").strip()
    if "gave up:true" in text:
        return Consent(False, "timeout")
    if "button returned:Allow" in text:
        return Consent(True, "allowed")
    return Consent(False, "denied")


def request(summary: str, *, timeout_s: int = TIMEOUT_S,
            run: Optional[Callable[..., subprocess.CompletedProcess]] = None) -> Consent:
    """Ask once. Blocks for at most ``timeout_s`` plus a short grace; never raises."""
    runner = run or subprocess.run
    argv = ["osascript"]
    for line in dialog_script(summary, timeout_s):
        argv += ["-e", line]
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout_s + 10, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return Consent(False, "timeout")
    except OSError:
        return Consent(False, "unavailable")
    if proc.returncode != 0:
        # -128 is the cancel button; anything else is "no GUI session" and fails closed the same way.
        return Consent(False, "denied" if "-128" in (proc.stderr or "") else "unavailable")
    return parse_result(proc.stdout)


def notify(text: str, *, run: Optional[Callable[..., subprocess.CompletedProcess]] = None) -> None:
    """Best-effort Notification Center banner (viewer attached, control taken). Never raises."""
    runner = run or subprocess.run
    script = f"display notification {_quote(text)} with title {_quote(TITLE)}"
    try:
        runner(["osascript", "-e", script], capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass
