"""Bot Screen on macOS: the same ``display.*`` + RFB contract as the Linux Xvnc desktop, fed by cua-driver.

There is no X server on a Mac, so the screen the owner watches is the real primary display. A small RFB 3.8
server (``rfb_server``, security None, loopback Unix socket exactly where the bridge dials) is fed by a capture
loop (``capture``: cua-driver ``get_desktop_state``, or Quartz when the gateway's own Python holds the Screen
Recording grant). Pointer and key events from the lease holder are injected through cua-driver desktop-scope
input (``input``). ``consent`` asks the person at the Mac before every session, ``denylist`` masks password
managers, Messages and Keychain Access, ``idle`` tells take-control whether somebody is physically using the Mac.

``runtime`` is the host side the Linux ``tools.bot_desktop.runtime`` dispatches to on darwin; ``server`` is the
long-lived process it spawns (like the Xvnc launcher: it survives a gateway restart, and the lease file on disk is
the authority both sides read).
"""
