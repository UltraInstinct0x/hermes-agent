"""``request.list`` / ``request.history``: the durable ledger of questions the agent put to a human.

``request.list`` answers "what waits for me anywhere": every open request across ALL sessions of the profile
(``session.resume`` only replays one session's), plus the most recent closed ones so a client that was away can
show what happened. ``request.history`` pages the whole ledger. Answering stays on the existing paths (response
frame, ``approval.respond``, ``clarify.lock``, ``request.answer``); the request id in a row is what
``request.answer`` takes, from any client attached to the gateway.

Both are profile-scoped: a multiplexed gateway answers for the profile the caller names. Bodies are rebound onto
server.py's globals (method_ctx.bind_module) and reference them bare.
"""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped

_LEDGER_ERR = 5310


def _human_request_context(sid: str):
    """Where a session's human questions are recorded: the profile home that owns the session (not the process's
    launch profile), plus the ids a client can match a row to. None when the session is unknown."""
    session = _sessions.get(sid)
    if not isinstance(session, dict):
        return None
    profile_home = session.get("profile_home")
    return {
        "home": Path(profile_home) if profile_home else _launch_home(),
        "profile": profile_name_for_home(profile_home) or _current_profile_name(),
        "session_id": _session_lookup_key(session, fallback=sid),
        "session_key": str(session.get("session_key") or ""),
    }


@method("request.list")
@_profile_scoped
def _(rid, params: dict) -> dict:
    from tui_gateway import human_requests
    limit = params.get("answered_limit")
    try:
        listing = human_requests.list_requests(
            get_hermes_home(), answered_limit=human_requests.ANSWERED_LIST_DEFAULT if limit is None else limit)
    except human_requests.STORE_ERRORS as e:
        return _err(rid, _LEDGER_ERR, str(e))
    return _ok(rid, listing)


@method("request.history")
@_profile_scoped
def _(rid, params: dict) -> dict:
    from tui_gateway import human_requests
    try:
        page = human_requests.history(
            get_hermes_home(), session_id=params.get("session_id") or None, kind=params.get("kind") or None,
            states=params.get("states") or None,
            limit=human_requests.HISTORY_LIMIT_DEFAULT if params.get("limit") is None else params["limit"],
            before=params.get("before"))
    except human_requests.STORE_ERRORS as e:
        return _err(rid, _LEDGER_ERR, str(e))
    return _ok(rid, page)


def register(server) -> None:
    bind_module(globals(), server, skip=("_",))
