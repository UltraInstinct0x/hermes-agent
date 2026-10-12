"""session.workspace.move handler (``methods_session`` split).

Moved verbatim from ``tui_gateway/methods_session.py`` (file-line ratchet): the body closes over
server.py globals through ``method_ctx.bind_module`` exactly as before; ``@method`` registration and
module publication still run from the parent's ``register()`` via ``HandlerRegistry``.
"""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method


@method("session.workspace.move")
def _(rid, params: dict) -> dict:
    """Re-home a STORED session's workspace (by ``session_key``; no live agent required). git branch/root are
    REPLACED (a stale ``git_repo_root`` kept the session under the project it left); a live agent follows even
    mid-turn (refusing made the UI claim success while state.db kept the old cwd)."""
    if not (target := _str_param(params, "session_key")):
        return _err(rid, 4007, "session_key required")
    if not (raw := _str_param(params, "cwd")):
        return _err(rid, 4016, "cwd required")
    from hermes_constants import translate_cwd_for_wsl_backend
    # Snapshot under the lock — concurrent RPCs mutate _sessions.
    with _sessions_lock:
        live_sid, live = next(
            ((sid, sess) for sid, sess in list(_sessions.items()) if sess.get("session_key") == target), ("", None))
    # The live session's profile decides (as _set_session_cwd below does); an ssh workspace is never host-validated.
    home = live.get("profile_home") if live is not None else _profile_home(params.get("profile"))
    try:
        target_cwd = _workspace_cwd(home, translate_cwd_for_wsl_backend(raw))
    except ValueError:
        return _err(rid, 4017, f"working directory does not exist: {raw}")
    branch, root = git_probe.branch(target_cwd), git_probe.common_repo_root(target_cwd)
    with _profile_db(params, writer=True) as db:
        if db is None:
            return _db_unavailable_error(rid, code=5007)
        # A draft has no row yet; the live re-home still applies (row inherits cwd on write).
        if not db.get_session(target):
            if live is None:
                return _err(rid, 4007, "session not found")
        else:
            try:
                db.update_session_cwd(target, target_cwd, branch, root, replace_git_meta=True)
            except Exception as e:  # health: allow BLE001 -- moved verbatim from methods_session.py; the db error is surfaced to the client as 5007, never swallowed
                return _err(rid, 5007, f"move failed: {e}")
    if live is not None:
        try:
            _set_session_cwd(live, target_cwd)
        except ValueError as e:
            return _err(rid, 4017, str(e))
        _emit("session.info", live_sid, _cwd_info(live, target_cwd, branch=branch))
    return _ok(rid, {"cwd": target_cwd, "branch": branch, "git_repo_root": root})


def register(server) -> None:
    """Publish this module's helpers onto ``server`` (rebound to its globals) and install handlers."""
    bind_module(globals(), server, skip=("_",))
