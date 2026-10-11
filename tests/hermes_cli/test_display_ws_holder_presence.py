"""The RFB bridge reports a live viewer socket to the dropped-holder watchdog for as long as the socket is open,
and forgets it when the socket ends, however it ends."""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from hermes_cli.web_routers import display
from tools.bot_desktop import lease, lease_watchdog


class _Ws:
    def __init__(self, close_code: int):
        self._code = close_code
        self.finish = asyncio.Event()

    async def accept(self):
        pass

    async def receive(self):
        await self.finish.wait()
        return {"type": "websocket.disconnect", "code": self._code}

    async def send_bytes(self, data):
        pass

    async def close(self, code=1000, reason=""):
        pass


async def _run_bridge(home: str, close_code: int, probe) -> None:
    sock_dir = os.path.join(home, "bot-desktop")
    os.makedirs(sock_dir, exist_ok=True)

    async def _xvnc(reader, writer):
        await asyncio.sleep(1)
        writer.close()

    server = await asyncio.start_unix_server(_xvnc, path=os.path.join(sock_dir, "rfb.sock"))
    ws = _Ws(close_code)
    task = asyncio.create_task(display._bridge(ws, {"hermes_home": home, "viewer_id": "v-phone"}))
    try:
        await asyncio.sleep(0.2)  # the bridge is up and pumping
        probe()
        ws.finish.set()
        await asyncio.wait_for(task, 5)
    finally:
        server.close()


@pytest.fixture(autouse=True)
def _fresh():
    lease._reset_for_tests()
    lease_watchdog.reset_for_tests()
    yield
    lease._reset_for_tests()
    lease_watchdog.reset_for_tests()


@pytest.mark.parametrize("close_code", [1006, 1000])
def test_holder_counts_as_present_while_its_socket_is_open_and_absent_after(close_code):
    with tempfile.TemporaryDirectory() as home:
        lease.acquire("v-phone", profile_key=home, watched=True)
        seen = {}

        def probe():
            # Mid-session: far past the grace period, the watchdog must leave a connected holder alone.
            seen["during"] = lease_watchdog.check(home, now=0.0) or lease_watchdog.check(home, now=10_000.0)

        asyncio.run(_run_bridge(home, close_code, probe))
        after = lease.get(profile_key=home)

        assert seen["during"] is None
        if close_code == 1006:
            # A dropped link keeps the exclusion at first; the watchdog now sees nobody attached and reclaims it.
            assert after.holder == lease.HUMAN
            assert lease_watchdog.check(home, now=100.0) is None
            assert lease_watchdog.check(home, now=131.0).holder == lease.AGENT
        else:
            assert after.holder == lease.AGENT  # a clean close hands back at once, as before
