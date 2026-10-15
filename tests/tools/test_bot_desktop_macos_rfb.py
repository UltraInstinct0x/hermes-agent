"""Mac Bot Screen RFB server: handshake the bridge's filter expects, ZRLE and Raw updates that decode back to the
frame, dirty-rect diffing, resize via ExtendedDesktopSize, and the input events it forwards."""

from __future__ import annotations

import asyncio
import struct
import zlib

import pytest
from PIL import Image, ImageDraw

from tools.bot_desktop.macos import rfb_server as rfb
from tools.bot_desktop.rfb_filter import RfbClientFilter


def _frame(w=200, h=130, colour=(10, 20, 30)) -> Image.Image:
    return Image.new("RGB", (w, h), colour)


def _noisy(w=200, h=130) -> Image.Image:
    img = _frame(w, h)
    draw = ImageDraw.Draw(img)
    for i in range(0, w, 7):
        draw.line((i, 0, w - i, h), fill=((i * 3) % 256, (i * 5) % 256, (i * 11) % 256))
    return img


# ── pure encoders ─────────────────────────────────────────────────────────────────────────────────────


def test_pack_pixels_follows_the_clients_byte_order():
    img = Image.new("RGB", (1, 1), (1, 2, 3))
    assert rfb.pack_pixels(img, rfb.PixelFormat()) == bytes([3, 2, 1, 0])  # little-endian, red<<16: B G R x
    rgb = rfb.PixelFormat(red_shift=0, green_shift=8, blue_shift=16)
    assert rfb.pack_pixels(img, rgb) == bytes([1, 2, 3, 0])
    big = rfb.PixelFormat(big_endian=True)
    assert rfb.pack_pixels(img, big) == bytes([0, 1, 2, 3])
    assert rfb.pack_pixels(img, rfb.PixelFormat(), cpixel=True) == bytes([3, 2, 1])


def test_unsupported_pixel_formats_are_refused_not_misrendered():
    sixteen = struct.pack(">BBBBHHHBBB3x", 16, 16, 0, 1, 31, 63, 31, 11, 5, 0)
    with pytest.raises(rfb.RfbProtocolError):
        rfb.PixelFormat.parse(sixteen)


def test_dirty_rects_cover_exactly_the_changed_tiles_and_merge_runs():
    old = _frame(256, 256)
    new = old.copy()
    ImageDraw.Draw(new).rectangle((70, 70, 140, 80), fill=(255, 0, 0))
    rects = rfb.dirty_rects(old, new)
    assert rects == [(64, 64, 128, 64)]  # tiles x 1..2, y 1: one merged rectangle
    assert rfb.dirty_rects(old, old.copy()) == []
    assert rfb.dirty_rects(None, new) == [(0, 0, 256, 256)]
    assert rfb.dirty_rects(old, _frame(100, 100)) == [(0, 0, 100, 100)]  # a resize is everything


def test_dirty_rects_respect_the_requested_region():
    old = _frame(256, 256)
    new = old.copy()
    ImageDraw.Draw(new).rectangle((200, 200, 210, 210), fill=(255, 0, 0))
    assert rfb.dirty_rects(old, new, (0, 0, 100, 100)) == []
    assert rfb.dirty_rects(old, new, (192, 192, 64, 64)) == [(192, 192, 64, 64)]


def test_zrle_rectangle_decodes_back_to_the_pixels_through_one_persistent_stream():
    img = _noisy()
    fmt = rfb.PixelFormat()
    z = zlib.compressobj(2)
    first = rfb.encode_zrle(img, (0, 0, 200, 130), fmt, z)
    out = _decode_zrle(first, fmt, zlib.decompressobj(), img.size)
    assert out.tobytes() == img.tobytes()
    # Second rectangle on the SAME stream: a fresh decompressor would fail, the persistent one must not.
    d = zlib.decompressobj()
    a = _decode_zrle(rfb.encode_zrle(img, (0, 0, 100, 64), fmt, z := zlib.compressobj(2)), fmt, d, img.size)
    b = _decode_zrle(rfb.encode_zrle(img, (100, 0, 100, 64), fmt, z), fmt, d, img.size)
    assert a.crop((0, 0, 100, 64)).tobytes() == img.crop((0, 0, 100, 64)).tobytes()
    assert b.crop((100, 0, 200, 64)).tobytes() == img.crop((100, 0, 200, 64)).tobytes()


def test_a_solid_screen_costs_a_few_bytes_in_zrle_and_the_full_amount_in_raw():
    img = _frame(640, 480)
    zrle = rfb.encode_zrle(img, (0, 0, 640, 480), rfb.PixelFormat(), zlib.compressobj(2))
    raw = rfb.encode_raw(img, (0, 0, 640, 480), rfb.PixelFormat())
    assert len(zrle) < 200 and len(raw) == 12 + 640 * 480 * 4


def _decode_zrle(message: bytes, fmt, decompressor, size) -> Image.Image:
    x, y, w, h, enc, length = struct.unpack(">HHHHiI", message[:16])
    assert enc == rfb.ENC_ZRLE and length == len(message) - 16
    data = decompressor.decompress(message[16:])
    out = Image.new("RGB", size)
    pos = 0
    for ty in range(y, y + h, 64):
        for tx in range(x, x + w, 64):
            tw, th = min(64, x + w - tx), min(64, y + h - ty)
            sub = data[pos]
            pos += 1
            if sub == 1:
                b, g, r = data[pos], data[pos + 1], data[pos + 2]
                pos += 3
                out.paste((r, g, b), (tx, ty, tx + tw, ty + th))
            else:
                assert sub == 0
                raw = data[pos:pos + tw * th * 3]
                pos += tw * th * 3
                px = Image.frombytes("RGB", (tw, th), raw, "raw", "BGR")
                out.paste(px, (tx, ty))
    assert pos == len(data)
    return out


# ── over a real Unix socket ───────────────────────────────────────────────────────────────────────────


class _Viewer:
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer
        self.z = zlib.decompressobj()
        self.size = (0, 0)

    @classmethod
    async def connect(cls, path, *, encodings=(rfb.ENC_ZRLE, rfb.ENC_EXT_DESKTOP_SIZE), minor=8):
        r, w = await asyncio.open_unix_connection(str(path))
        self = cls(r, w)
        assert await r.readexactly(12) == b"RFB 003.008\n"
        w.write(f"RFB 003.00{minor}\n".encode())
        n = (await r.readexactly(1))[0]
        assert n == 1 and (await r.readexactly(1))[0] == 1
        w.write(bytes([1]))
        if minor >= 8:
            assert struct.unpack(">I", await r.readexactly(4))[0] == 0
        w.write(bytes([1]))  # ClientInit
        width, height = struct.unpack(">HH", await r.readexactly(4))
        self.size = (width, height)
        self.fmt = rfb.PixelFormat.parse(await r.readexactly(16))
        (name_len,) = struct.unpack(">I", await r.readexactly(4))
        self.name = (await r.readexactly(name_len)).decode()
        w.write(struct.pack(">BxH", 2, len(encodings)) + b"".join(struct.pack(">i", e) for e in encodings))
        return self

    async def request(self, incremental=False, rect=None):
        x, y, w, h = rect or (0, 0, *self.size)
        self.w.write(struct.pack(">BBHHHH", 3, int(incremental), x, y, w, h))

    async def read_update(self, timeout=3):
        async def _go():
            head = await self.r.readexactly(4)
            assert head[0] == 0
            rects = []
            for _ in range(struct.unpack(">H", head[2:])[0]):
                x, y, w, h, enc = struct.unpack(">HHHHi", await self.r.readexactly(12))
                if enc == rfb.ENC_ZRLE:
                    (n,) = struct.unpack(">I", await self.r.readexactly(4))
                    rects.append((x, y, w, h, enc, await self.r.readexactly(n)))
                elif enc == rfb.ENC_RAW:
                    rects.append((x, y, w, h, enc, await self.r.readexactly(w * h * 4)))
                elif enc == rfb.ENC_EXT_DESKTOP_SIZE:
                    await self.r.readexactly(4 + 16)
                    self.size = (w, h)
                    rects.append((x, y, w, h, enc, b""))
                elif enc == rfb.ENC_DESKTOP_SIZE:
                    self.size = (w, h)
                    rects.append((x, y, w, h, enc, b""))
            return rects
        return await asyncio.wait_for(_go(), timeout)

    def close(self):
        self.w.close()


@pytest.fixture
def sock_path():
    """AF_UNIX paths are limited to ~104 bytes on macOS: pytest's tmp_path is far longer."""
    import shutil
    import tempfile
    from pathlib import Path
    root = Path(tempfile.mkdtemp(prefix="mbs", dir="/tmp"))  # no-tmp: ok — AF_UNIX path length limit
    yield root / "rfb.sock"
    shutil.rmtree(root, ignore_errors=True)


def _run(coro):
    return asyncio.run(coro)


def test_handshake_is_exactly_what_the_bridge_filter_forwards(sock_path):
    """The filter forwards 12 (version) + 1 (security choice) + 1 (ClientInit) before it parses messages; a server
    that needed a different shape would deadlock behind it. Run the real client bytes through the real filter."""
    async def go():
        server = rfb.RfbServer(sock_path, on_input=lambda e: None)
        server.frame = _frame()
        await server.start()
        gate = RfbClientFilter(lambda: True)
        r, w = await asyncio.open_unix_connection(str(sock_path))
        await r.readexactly(12)
        client_bytes = b"RFB 003.008\n" + bytes([1]) + bytes([0])  # ClientInit with shared flag 0
        w.write(gate.feed(client_bytes))
        assert (await r.readexactly(2)) == bytes([1, 1])  # security types
        assert struct.unpack(">I", await r.readexactly(4))[0] == 0
        width, height = struct.unpack(">HH", await r.readexactly(4))
        assert (width, height) == (200, 130)
        w.close()
        await server.stop()
    _run(go())
    assert not sock_path.exists()


def test_socket_is_private_and_serverinit_names_the_mac(sock_path):
    async def go():
        server = rfb.RfbServer(sock_path, on_input=lambda e: None)
        server.frame = _frame()
        await server.start()
        assert (sock_path).stat().st_mode & 0o777 == 0o600
        v = await _Viewer.connect(sock_path)
        assert v.name == "Hermes Mac" and v.size == (200, 130)
        v.close()
        await server.stop()
    _run(go())


def test_full_update_decodes_to_the_published_frame_and_idle_requests_wait(sock_path):
    async def go():
        frame = _noisy()
        server = rfb.RfbServer(sock_path, on_input=lambda e: None)
        server.frame = frame
        await server.start()
        v = await _Viewer.connect(sock_path)
        await v.request()
        rects = await v.read_update()
        got = _decode_zrle(struct.pack(">HHHHiI", *rects[0][:5], len(rects[0][5])) + rects[0][5], v.fmt, v.z, v.size)
        assert got.tobytes() == frame.tobytes()
        await v.request(incremental=True)
        with pytest.raises(asyncio.TimeoutError):  # nothing changed: the request stays pending
            await v.read_update(timeout=0.3)
        v.close()
        await server.stop()
    _run(go())


def test_changed_region_arrives_as_a_small_incremental_update(sock_path):
    async def go():
        frame = _frame(256, 256)
        server = rfb.RfbServer(sock_path, on_input=lambda e: None)
        server.frame = frame
        await server.start()
        v = await _Viewer.connect(sock_path)
        await v.request()
        await v.read_update()
        await v.request(incremental=True)
        changed = frame.copy()
        ImageDraw.Draw(changed).rectangle((130, 5, 140, 15), fill=(200, 0, 0))
        server.publish(changed)
        rects = await v.read_update()
        assert [(r[0], r[1], r[2], r[3]) for r in rects] == [(128, 0, 64, 64)]
        v.close()
        await server.stop()
    _run(go())


def test_raw_is_used_when_the_client_does_not_offer_zrle(sock_path):
    async def go():
        frame = _noisy(70, 40)
        server = rfb.RfbServer(sock_path, on_input=lambda e: None)
        server.frame = frame
        await server.start()
        v = await _Viewer.connect(sock_path, encodings=(rfb.ENC_RAW,))
        await v.request()
        (_x, _y, w, h, enc, data), = await v.read_update()
        assert enc == rfb.ENC_RAW and (w, h) == (70, 40)
        assert Image.frombytes("RGB", (w, h), data, "raw", "BGRX").tobytes() == frame.tobytes()
        v.close()
        await server.stop()
    _run(go())


def test_display_resize_is_announced_with_extended_desktop_size_then_repainted(sock_path):
    async def go():
        server = rfb.RfbServer(sock_path, on_input=lambda e: None)
        server.frame = _frame(200, 130)
        await server.start()
        v = await _Viewer.connect(sock_path)
        await v.request()
        await v.read_update()
        await v.request(incremental=True)
        server.publish(_frame(300, 160, (9, 9, 9)))
        rects = await v.read_update()
        assert rects[0][4] == rfb.ENC_EXT_DESKTOP_SIZE and v.size == (300, 160)
        assert any(r[4] == rfb.ENC_ZRLE and (r[2], r[3]) == (300, 160) for r in rects[1:])
        v.close()
        await server.stop()
    _run(go())


def test_input_reaches_the_sink_only_when_the_gate_allows_it(sock_path):
    events: list = []
    allow = {"on": False}

    async def go():
        server = rfb.RfbServer(sock_path, on_input=events.append, input_allowed=lambda: allow["on"])
        server.frame = _frame()
        await server.start()
        v = await _Viewer.connect(sock_path)
        v.w.write(struct.pack(">BBHH", 5, 1, 10, 20))
        await v.w.drain()
        await asyncio.sleep(0.1)
        assert events == []
        allow["on"] = True
        v.w.write(struct.pack(">BBHH", 5, 1, 11, 21) + struct.pack(">BBHI", 4, 1, 0, 0x61))
        await v.w.drain()
        await asyncio.sleep(0.1)
        v.close()
        await server.stop()
    _run(go())
    assert events == [("pointer", 1, 11, 21), ("key", True, 0x61)]


def test_viewer_count_follows_connections(sock_path):
    seen: list[int] = []

    async def go():
        server = rfb.RfbServer(sock_path, on_input=lambda e: None, on_viewers=seen.append)
        server.frame = _frame()
        await server.start()
        v = await _Viewer.connect(sock_path)
        await asyncio.sleep(0.1)
        v.close()
        await asyncio.sleep(0.2)
        await server.stop()
    _run(go())
    assert seen == [1, 0]


def test_an_old_rfb_3_3_client_is_refused(sock_path):
    async def go():
        server = rfb.RfbServer(sock_path, on_input=lambda e: None)
        server.frame = _frame()
        await server.start()
        r, w = await asyncio.open_unix_connection(str(sock_path))
        await r.readexactly(12)
        w.write(b"RFB 003.003\n")
        assert await r.read() == b""  # closed without a security list
        await server.stop()
    _run(go())
