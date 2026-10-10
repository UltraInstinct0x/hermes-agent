#!/usr/bin/env python3
"""Run one named UI action in the Hermes desktop app and return its JSON result.

The agent drives the app the same way it drives the in-app browser pane: this tool is
schema + a thin dispatcher over the platform-injected ``callback``. The callback
round-trips through the gateway's blocking-prompt bridge (``app.action`` on the wire,
mirroring ``preview.act``): tui_gateway asks the renderer, the renderer executes the
action against its REAL stores and answers with a JSON object, which is passed back
verbatim via ``passthrough_json``. An empty answer means no app window showed the chat.

``desktop_ui`` toolset: desktop-sourced sessions only. Action ids are stable strings
owned by the app's action registry (tabs, panes, layout, ...); the schema deliberately
does not list them, so the backend never goes stale against the app;
an unknown id is answered by the app as ``{ok: false, detail: 'unknown action id: …'}``.
"""

from typing import Callable, Optional

from tools.desktop_ui import passthrough_json
from tools.registry import registry, tool_error


def app_action_tool(
    action: str = "",
    args: Optional[dict] = None,
    callback: Optional[Callable] = None,
) -> str:
    """Dispatch one named action to the desktop renderer and return its JSON outcome verbatim."""
    if callback is None:
        return tool_error("app_action is only available in the Hermes desktop app.")
    name = (action or "").strip()
    if not name:
        return tool_error(
            "action is required: an app action id such as 'tab.list' or 'tab.open'.")
    if args is not None and not isinstance(args, dict):
        return tool_error("args must be an object (a JSON object of action parameters).")
    payload = {"action": name}
    if args:
        payload["args"] = args
    try:
        raw = callback(payload)
    except Exception as exc:
        return tool_error(f"Failed to run the app action: {exc}")
    if not raw:
        return tool_error(
            "No Hermes app window answered the action request. Open this chat in the "
            "desktop app (it must be the live client for this session) and try again. "
            "If the app IS open, it may be older than this backend; update it and retry.")
    return passthrough_json(raw)


APP_ACTION_SCHEMA = {
    "name": "app_action",
    "description": (
        "Run one UI action in the Hermes desktop app (tabs, panes, layout). `action` is an "
        "app-owned id such as tab.list, tab.open, pane.split; `args` are its parameters. "
        "Returns the app's JSON {ok, detail, ...}; ok:false means it did not work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "Action id, e.g. tab.list."},
            "args": {"type": "object", "description": "Action parameters."},
        },
        "required": ["action"],
    },
}


registry.register(
    name="app_action",
    toolset="desktop_ui",
    schema=APP_ACTION_SCHEMA,
    handler=lambda args, **kw: app_action_tool(
        action=args.get("action", ""), args=args.get("args"), callback=kw.get("callback"),
    ),
    emoji="🪄",
)
