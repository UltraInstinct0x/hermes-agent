"""Classify the approval shown to a client without changing its answer policy."""


def classify_approval(data: dict, display: dict | None = None) -> str:
    """Only a producer-identified terminal command with an unchanged display is plain."""
    kind = data.get("approval_kind")
    if data.get("smart_denied") or kind == "smart_denied":
        return "smart_denied"
    if kind in ("flagged", "redacted"):
        return kind
    if kind != "command" or data.get("kind") not in (None, "approval", "command") or "edit" in data:
        return "consent"
    if data.get("allow_session") is False:
        return "flagged"
    if not data.get("command") or (display is not None and any(
            display.get(field) != data.get(field) for field in ("command", "description"))):
        return "redacted"
    return "command"


def client_approval_fields(data: dict, *, extra_fields: tuple[str, ...] = ()) -> dict:
    """Redact display text at egress, downgrading a command if the redactor changes it."""
    from agent.redact import redact_approval_display

    display = {field: redact_approval_display(data[field], force=True)
               for field in ("command", "description", *extra_fields) if field in data}
    return {**display, "approval_kind": classify_approval(data, display)}
