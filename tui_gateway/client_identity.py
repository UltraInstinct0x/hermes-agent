"""Who is on the other end of a gateway connection.

The gateway used to know nothing about its clients: a request answered from a phone, the desktop or a
second window looked the same. A stable ``client_id`` plus a human label now rides on the connection so
the ledger (``human_requests.py``) and the ``request.resolved`` event can say who answered, and the Bot
Screen lease can name a holder that survives a reconnect.

Two sources, strongest first:

* **device handle**: the ``device`` query parameter of the WebSocket upgrade. A fronting proxy that has
  authenticated a paired device appends it; it is read from the connection, never from an RPC payload,
  so a client cannot talk its way into another device's id over the wire. It is only as trustworthy as
  the hop that sets it: anyone who can open the socket directly with the gateway token can also send the
  query themselves, which is inside the single-owner trust boundary the gateway token already defines.
* **declared**: ``client.capabilities {client_id, label, kind}``, sent by the client itself. Good enough
  to tell the owner's own devices apart; not an authentication.

A connection with neither is ``unknown`` (older clients keep working unchanged).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any

UNKNOWN_ID = "unknown"
SOURCE_DEVICE = "device"
SOURCE_DECLARED = "declared"
SOURCE_NONE = "none"

_DEVICE_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{4,128}$")
_DECLARED_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,63}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
_LABEL_MAX = 80
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class ClientIdentity:
    client_id: str = UNKNOWN_ID
    label: str | None = None
    kind: str | None = None
    source: str = SOURCE_NONE

    def as_wire(self) -> dict[str, str | None]:
        """The ``answered_by`` shape shared by the ledger rows and the ``request.resolved`` payload."""
        return {"client_id": self.client_id, "label": self.label, "kind": self.kind}


UNKNOWN = ClientIdentity()

_lock = threading.Lock()
# Keyed by transport identity; StdioTransport has __slots__ and cannot be weak-referenced, so
# ``forget`` (called from ``server.unregister_live_transport``) is the only cleanup, like
# ``server_requests._answering_clients``.
_device_handles: dict[Any, str] = {}
_declared: dict[Any, tuple[str | None, str | None, str | None]] = {}


def _clean_label(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    label = _CONTROL_CHARS_RE.sub("", raw).strip()[:_LABEL_MAX]
    return label or None


def _clean_kind(raw: Any) -> str | None:
    kind = raw.strip().lower() if isinstance(raw, str) else ""
    return kind if _KIND_RE.match(kind) else None


def _clean_declared_id(raw: Any) -> str | None:
    value = raw.strip() if isinstance(raw, str) else ""
    # "unknown" is the absence marker; a client must not be able to claim it, nor a "device:" id.
    if not _DECLARED_ID_RE.match(value) or value == UNKNOWN_ID or value.startswith("device:"):
        return None
    return value


def valid_device_handle(raw: Any) -> str | None:
    """The handle when it is shaped like one, else None (a malformed value is ignored, not trusted)."""
    return raw if isinstance(raw, str) and _DEVICE_HANDLE_RE.match(raw) else None


def bind_device_handle(transport: Any, handle: Any) -> None:
    """Record the proxy-supplied device handle read from the upgrade request (see module docstring)."""
    if (clean := valid_device_handle(handle)) is not None:
        with _lock:
            _device_handles[transport] = clean


def declare(transport: Any, *, client_id: Any = None, label: Any = None, kind: Any = None) -> ClientIdentity:
    """Record what the client said about itself in ``client.capabilities``; returns the effective identity.
    A later call replaces the earlier one, so a client may refresh its label."""
    with _lock:
        _declared[transport] = (_clean_declared_id(client_id), _clean_label(label), _clean_kind(kind))
    return identity_for(transport)


def identity_for(transport: Any) -> ClientIdentity:
    """The effective identity of *transport*: device handle first, then the declared id, else unknown."""
    with _lock:
        handle = _device_handles.get(transport)
        declared_id, label, kind = _declared.get(transport, (None, None, None))
    if handle is not None:
        return ClientIdentity(f"device:{handle}", label, kind, SOURCE_DEVICE)
    if declared_id is not None:
        return ClientIdentity(declared_id, label, kind, SOURCE_DECLARED)
    return ClientIdentity(UNKNOWN_ID, label, kind, SOURCE_NONE)


def forget(transport: Any) -> None:
    with _lock:
        _device_handles.pop(transport, None)
        _declared.pop(transport, None)


def reset_for_tests() -> None:
    with _lock:
        _device_handles.clear()
        _declared.clear()
