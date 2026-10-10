"""Tests for the GUI-surface ``app_action`` tool (``desktop_ui`` toolset).

The tool is schema + a thin dispatcher over the platform-injected callback; the app owns the
action ids and the outcome, so these tests pin what the BACKEND guarantees: validation,
verbatim pass-through of the app's answer (success or ``ok: false``), and an honest error
when nothing answers.
"""

import json

from tools import app_action_tool as aat
from tools.registry import registry


def _run(**kwargs):
    return json.loads(aat.app_action_tool(**kwargs))


def test_requires_callback():
    """Outside the desktop GUI there is no bridge: a clear error, no crash."""
    assert "desktop app" in _run(action="tab.list", callback=None)["error"]


def test_action_id_is_required():
    seen = []
    for blank in ("", "   ", None):
        assert "action is required" in _run(action=blank, callback=seen.append)["error"]
    assert seen == []


def test_args_must_be_an_object():
    seen = []
    assert "args must be an object" in _run(action="tab.open", args=["x"], callback=seen.append)["error"]
    assert seen == []


def test_payload_is_the_trimmed_id_and_args_only():
    seen = []

    def cb(payload):
        seen.append(payload)
        return json.dumps({"ok": True})

    _run(action="  tab.open ", args={"url": "https://example.com"}, callback=cb)
    _run(action="tab.list", callback=cb)
    _run(action="tab.list", args={}, callback=cb)

    assert seen == [
        {"action": "tab.open", "args": {"url": "https://example.com"}},
        {"action": "tab.list"},
        {"action": "tab.list"},
    ]


def test_the_apps_answer_passes_through_verbatim():
    answer = {"ok": True, "action": "tab.list", "tabs": [{"id": "t1", "title": "Files"}]}
    assert _run(action="tab.list", callback=lambda _p: json.dumps(answer)) == answer


def test_an_app_side_failure_is_not_rewritten_into_success():
    """Unknown ids are the app's call: its ``ok: false`` reaches the model unchanged."""
    answer = {"ok": False, "detail": "unknown action id: tab.teleport"}
    assert _run(action="tab.teleport", callback=lambda _p: json.dumps(answer)) == answer


def test_non_json_answer_is_wrapped_not_dropped():
    assert _run(action="tab.list", callback=lambda _p: "done") == {"text": "done"}


def test_no_answer_is_an_honest_error_naming_the_cause():
    error = _run(action="tab.list", callback=lambda _p: "")["error"]
    assert "No Hermes app window answered" in error
    assert "older" in error


def test_a_raising_callback_becomes_a_tool_error():
    def boom(_payload):
        raise RuntimeError("socket closed")

    assert "socket closed" in _run(action="tab.list", callback=boom)["error"]


def test_registered_in_desktop_ui_with_a_tight_schema():
    entry = registry.get_entry("app_action")
    assert entry is not None and entry.toolset == "desktop_ui"
    assert aat.APP_ACTION_SCHEMA["parameters"]["required"] == ["action"]
    assert set(aat.APP_ACTION_SCHEMA["parameters"]["properties"]) == {"action", "args"}
    # Ids belong to the app registry: the backend must not enumerate them in the schema
    # (a stale list misleads the model and costs prompt tokens on every turn).
    assert len(json.dumps(aat.APP_ACTION_SCHEMA)) < 900


def test_listed_in_the_desktop_ui_toolset():
    from toolsets import TOOLSETS

    assert "app_action" in TOOLSETS["desktop_ui"]["tools"]
