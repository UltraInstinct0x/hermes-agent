"""Windows that must never reach a remote viewer: password managers, Messages, Keychain Access.

The whole primary display is shared, so these are masked in every frame rather than trusted to be closed. The
mask is the window's whole bounds (even when another window covers it: conservative on purpose). Matching is a
case-insensitive substring of the owning app name. ``bot_desktop.mac_deny_apps`` in config.yaml extends the list.
"""

from __future__ import annotations

from typing import Any, Iterable

DEFAULT_DENY_APPS = (
    "1Password", "Bitwarden", "Dashlane", "LastPass", "KeePassXC", "Enpass", "NordPass", "Keeper",
    "Passwords", "Keychain Access", "Messages",
)

Rect = tuple[int, int, int, int]  # x, y, w, h in native display pixels


def deny_list(extra: Iterable[str] = ()) -> tuple[str, ...]:
    names = list(DEFAULT_DENY_APPS)
    names += [str(n).strip() for n in extra if str(n).strip()]
    return tuple(dict.fromkeys(names))


def is_denied(app_name: str, deny: Iterable[str]) -> bool:
    lowered = (app_name or "").lower()
    return bool(lowered) and any(name.lower() in lowered for name in deny)


def masked_rects(windows: Iterable[dict[str, Any]], deny: Iterable[str], scale: float) -> list[Rect]:
    """Native-pixel rectangles of the on-screen windows whose app is on the deny list. ``windows`` is cua
    ``list_windows`` output (bounds in points), ``scale`` the display's backing scale factor."""
    deny = tuple(deny)
    rects: list[Rect] = []
    for win in windows:
        if win.get("is_on_screen") is False or not is_denied(str(win.get("app_name") or ""), deny):
            continue
        b = win.get("bounds") or {}
        try:
            x, y, w, h = (float(b[k]) for k in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError):
            continue
        rects.append((int(x * scale) - 2, int(y * scale) - 2, int(w * scale) + 4, int(h * scale) + 4))
    return rects
