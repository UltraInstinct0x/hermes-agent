"""tui_gateway test fixtures.

Several files here import ``tui_gateway.server`` inside a ``patch.dict("sys.modules", {"hermes_constants":
MagicMock(...)})`` window so the module binds a fixed home. The server's import graph reaches
``agent.process_bootstrap`` → ``hermes_bootstrap``, which is process boot: PM dependency activation reads
the real install root through ``hermes_constants`` and exits the process when that is a MagicMock.
Importing it once here, before any window opens, keeps boot out of the mocked import.
"""

import hermes_bootstrap
import pytest


@pytest.fixture(autouse=True)
def _bot_desktop_is_linux_unless_testing_the_mac(request, monkeypatch):
    """The Linux Bot Desktop tests assume the Linux runtime. On a Mac host the runtime dispatches to the cua-driver
    backend (which would raise a native consent prompt), so pin it off; the Mac tests opt in explicitly."""
    name = request.module.__name__
    if "macos" in name or "mac_take_control" in name:
        yield
        return
    try:
        from tools.bot_desktop import runtime
    except Exception:  # a test module that stubs the import graph: nothing to pin
        yield
        return
    monkeypatch.setattr(runtime, "_mac_backend", lambda: False)
    yield
