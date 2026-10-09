"""Live STT through a PLUGIN provider must discover the active profile's plugins before looking the
provider up.

A dashboard process that hosts a second profile (``?profile=B``) registers that profile's plugin
providers only when its plugins are discovered. TTS happens to trigger that; live STT did not, so the
profile's FIRST live-STT request answered "no live STT for the configured provider" and only worked
after a TTS call had run.
"""

from __future__ import annotations

import agent.transcription_registry as registry
from hermes_cli import plugins
from tools import transcription_streaming as ts


class _NotStreamable:
    streaming_capable = False


def _record(monkeypatch, order, *, discover_raises=False):
    def discover(*_a, **_k):
        order.append("discover")
        if discover_raises:
            raise RuntimeError("plugin scan failed")

    def lookup(name, **_k):
        order.append("lookup:" + name)
        return _NotStreamable()

    monkeypatch.setattr(plugins, "discover_plugins", discover)
    monkeypatch.setattr(registry, "get_provider", lookup)


def test_plugin_session_discovers_plugins_before_the_provider_lookup(monkeypatch):
    order: list[str] = []
    _record(monkeypatch, order)
    assert ts._plugin_session("zipformer", None, None, None) is None  # not streamable: no session
    assert order == ["discover", "lookup:zipformer"]


def test_a_failing_discovery_does_not_break_the_lookup(monkeypatch):
    order: list[str] = []
    _record(monkeypatch, order, discover_raises=True)
    assert ts._plugin_session("zipformer", None, None, None) is None
    assert order == ["discover", "lookup:zipformer"]


def test_streaming_available_also_discovers_first(monkeypatch):
    order: list[str] = []
    _record(monkeypatch, order)
    monkeypatch.setattr(ts.tt if hasattr(ts, "tt") else __import__("tools.transcription_tools", fromlist=["x"]),
                        "_get_provider", lambda cfg: "zipformer")
    assert ts.streaming_available({"streaming": True, "provider": "zipformer"}) is False
    assert order == ["discover", "lookup:zipformer"]
