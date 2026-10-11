"""A small RFB 3.8 server: security None, one framebuffer, ZRLE or Raw updates, dirty-rect diffing.

It listens on a 0600 Unix socket, exactly where ``hermes_cli.web_routers.display`` dials Xvnc and where
``rfb_filter.RfbClientFilter`` expects a fixed handshake (12-byte version, 1-byte security choice, 1-byte
ClientInit). The bridge, not this server, decides who may send input; the optional ``input_allowed`` hook is a
second gate for defence in depth.

Encodings: ZRLE (one persistent zlib stream per client, solid tiles as one pixel, others raw CPIXEL), Raw, and
the DesktopSize / ExtendedDesktopSize pseudo-encodings for the primary display changing resolution. No
CopyRect, no cursor, no ContinuousUpdates, no Fence: every one of those is optional for the clients (the iOS
client requests ZRLE, CopyRect, Raw; noVNC accepts all).

Each client keeps its own copy of what it last received, so a slow phone and a fast desktop are diffed
independently and a partial update request never loses a dirty region.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageChops

logger = logging.getLogger(__name__)

ENC_RAW, ENC_ZRLE = 0, 16
ENC_DESKTOP_SIZE, ENC_EXT_DESKTOP_SIZE = -223, -308
TILE = 64
SERVER_NAME = "Hermes Mac"

Rect = tuple[int, int, int, int]


class RfbProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class PixelFormat:
    bpp: int = 32
    depth: int = 24
    big_endian: bool = False
    true_colour: bool = True
    red_max: int = 255
    green_max: int = 255
    blue_max: int = 255
    red_shift: int = 16
    green_shift: int = 8
    blue_shift: int = 0

    def pack(self) -> bytes:
        return struct.pack(">BBBBHHHBBB3x", self.bpp, self.depth, int(self.big_endian), int(self.true_colour),
                           self.red_max, self.green_max, self.blue_max, self.red_shift, self.green_shift, self.blue_shift)

    @classmethod
    def parse(cls, data: bytes) -> "PixelFormat":
        bpp, depth, be, tc, rm, gm, bm, rs, gs, bs = struct.unpack(">BBBBHHHBBB3x", data)
        fmt = cls(bpp, depth, bool(be), bool(tc), rm, gm, bm, rs, gs, bs)
        if not fmt.supported():
            raise RfbProtocolError(f"unsupported pixel format ({bpp} bpp, true colour {bool(tc)})")
        return fmt

    def supported(self) -> bool:
        """32 bpp true colour with 8-bit channels on byte boundaries: what noVNC and the iOS client ask for."""
        shifts = (self.red_shift, self.green_shift, self.blue_shift)
        return (self.bpp == 32 and self.true_colour and (self.red_max, self.green_max, self.blue_max) == (255, 255, 255)
                and all(s in (0, 8, 16, 24) for s in shifts) and len(set(shifts)) == 3)


def _byte_roles(fmt: PixelFormat) -> list[Optional[int]]:
    """For each of the 4 bytes of a pixel as sent on the wire: 0/1/2 = red/green/blue, None = unused."""
    roles: list[Optional[int]] = [None] * 4
    for channel, shift in enumerate((fmt.red_shift, fmt.green_shift, fmt.blue_shift)):
        index = (3 - shift // 8) if fmt.big_endian else (shift // 8)
        roles[index] = channel
    return roles


def cpixel_eligible(fmt: PixelFormat) -> bool:
    """ZRLE's 3-byte CPIXEL applies when the unused byte is the pixel's most or least significant one."""
    used = {fmt.red_shift, fmt.green_shift, fmt.blue_shift}
    unused = ({0, 8, 16, 24} - used).pop()
    return fmt.depth <= 24 and unused in (0, 24)


def pack_pixels(image: Image.Image, fmt: PixelFormat, *, cpixel: bool = False) -> bytes:
    """``image`` (RGB) as wire pixels in the client's format; ``cpixel`` drops the unused byte (ZRLE's 3-byte
    CPIXEL, valid when the format's channels fit in the low or the high three bytes)."""
    roles = _byte_roles(fmt)
    if cpixel:
        # CPIXEL drops the byte that holds no channel (the format has exactly one such byte).
        roles = [r for r in roles if r is not None]
    bands = image.split()
    zero = Image.new("L", image.size, 0)
    ordered = [bands[r] if r is not None else zero for r in roles]
    mode = "RGBA" if len(ordered) == 4 else "RGB"
    return Image.merge(mode, ordered).tobytes()


def dirty_rects(old: Optional[Image.Image], new: Image.Image, region: Optional[Rect] = None, tile: int = TILE) -> list[Rect]:
    """Tile-aligned rectangles of ``new`` that differ from ``old`` inside ``region`` (default: everything).
    ``old`` of None or another size means everything. Adjacent dirty tiles in a row are one rectangle and
    identical runs on consecutive rows are merged."""
    w, h = new.size
    rx, ry, rw, rh = region or (0, 0, w, h)
    rx, ry = max(0, rx), max(0, ry)
    rw, rh = min(rw, w - rx), min(rh, h - ry)
    if rw <= 0 or rh <= 0:
        return []
    if old is None or old.size != new.size:
        return [(rx, ry, rw, rh)]
    diff = ImageChops.difference(old, new)
    if diff.getbbox() is None:
        return []
    runs: list[tuple[int, int, int, int]] = []  # (x, y, w, h) per row of tiles
    ty = ry - ry % tile
    while ty < ry + rh:
        y0, y1 = max(ty, ry), min(ty + tile, ry + rh)
        x_start: Optional[int] = None
        tx = rx - rx % tile
        while tx < rx + rw + tile:
            x0, x1 = max(tx, rx), min(tx + tile, rx + rw)
            hit = x0 < x1 and diff.crop((x0, y0, x1, y1)).getbbox() is not None
            if hit and x_start is None:
                x_start = x0
            if not hit and x_start is not None:
                runs.append((x_start, y0, min(tx, rx + rw) - x_start, y1 - y0))
                x_start = None
            tx += tile
        ty += tile
    merged: list[list[int]] = []
    for x, y, rw_, rh_ in runs:
        last = merged[-1] if merged else None
        if last and last[0] == x and last[2] == rw_ and last[1] + last[3] == y:
            last[3] += rh_
        else:
            merged.append([x, y, rw_, rh_])
    return [(x, y, w_, h_) for x, y, w_, h_ in merged]


def encode_raw(image: Image.Image, rect: Rect, fmt: PixelFormat) -> bytes:
    x, y, w, h = rect
    return struct.pack(">HHHHi", x, y, w, h, ENC_RAW) + pack_pixels(image.crop((x, y, x + w, y + h)), fmt)


def encode_zrle(image: Image.Image, rect: Rect, fmt: PixelFormat, z: "zlib._Compress") -> bytes:
    """One ZRLE rectangle. ``z`` is the client's persistent zlib stream (RFC 6143 section 7.7.6)."""
    x, y, w, h = rect
    three = cpixel_eligible(fmt)
    out = bytearray()
    for ty in range(y, y + h, TILE):
        for tx in range(x, x + w, TILE):
            tile = image.crop((tx, ty, min(tx + TILE, x + w), min(ty + TILE, y + h)))
            solid = tile.getcolors(1)
            if solid:
                out.append(1)
                out += pack_pixels(Image.new("RGB", (1, 1), solid[0][1]), fmt, cpixel=three)
            else:
                out.append(0)
                out += pack_pixels(tile, fmt, cpixel=three)
    body = z.compress(bytes(out)) + z.flush(zlib.Z_SYNC_FLUSH)
    return struct.pack(">HHHHiI", x, y, w, h, ENC_ZRLE, len(body)) + body


def ext_desktop_size_rect(w: int, h: int) -> bytes:
    """ExtendedDesktopSize pseudo-rectangle: server-initiated change (x = 0), success (y = 0), one screen."""
    screen = struct.pack(">IHHHHI", 1, 0, 0, w, h, 0)
    return struct.pack(">HHHHi", 0, 0, w, h, ENC_EXT_DESKTOP_SIZE) + struct.pack(">B3x", 1) + screen


def desktop_size_rect(w: int, h: int) -> bytes:
    return struct.pack(">HHHHi", 0, 0, w, h, ENC_DESKTOP_SIZE)


class _Client:
    def __init__(self, server: "RfbServer", reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.server = server
        self.reader = reader
        self.writer = writer
        self.fmt = PixelFormat()
        self.encodings: list[int] = []
        self.request: Optional[tuple[Rect, bool]] = None
        self.sent: Optional[Image.Image] = None
        self.sent_size: Optional[tuple[int, int]] = None
        self.zlib = zlib.compressobj(2)
        self.wake = asyncio.Event()

    # -- handshake ---------------------------------------------------------------------------------------

    async def handshake(self) -> None:
        self.writer.write(b"RFB 003.008\n")
        version = await self.reader.readexactly(12)
        if not version.startswith(b"RFB 003.") or version[11:] != b"\n":
            raise RfbProtocolError("not an RFB client")
        minor = int(version[8:11])
        if minor < 7:
            raise RfbProtocolError("RFB 3.3 clients are not supported")
        self.writer.write(bytes([1, 1]))  # one security type: None
        choice = (await self.reader.readexactly(1))[0]
        if choice != 1:
            raise RfbProtocolError("security type None is the only one offered")
        if minor >= 8:
            self.writer.write(struct.pack(">I", 0))  # SecurityResult: OK
        await self.reader.readexactly(1)  # ClientInit: shared flag (always shared)
        w, h = self.server.size
        self.sent_size = (w, h)
        name = SERVER_NAME.encode()
        self.writer.write(struct.pack(">HH", w, h) + self.fmt.pack() + struct.pack(">I", len(name)) + name)
        await self.writer.drain()

    # -- client messages ---------------------------------------------------------------------------------

    async def read_loop(self) -> None:
        while True:
            kind = (await self.reader.readexactly(1))[0]
            if kind == 0:
                body = await self.reader.readexactly(19)
                self.fmt = PixelFormat.parse(body[3:19])
                self.zlib = zlib.compressobj(2)
                self.sent = None  # the client's pixels change meaning: resend everything
                self.wake.set()
            elif kind == 2:
                n = struct.unpack(">xH", await self.reader.readexactly(3))[0]
                data = await self.reader.readexactly(4 * n)
                self.encodings = list(struct.unpack(f">{n}i", data))
            elif kind == 3:
                incremental, x, y, w, h = struct.unpack(">BHHHH", await self.reader.readexactly(9))
                self.request = ((x, y, w, h), bool(incremental))
                self.wake.set()
            elif kind == 4:
                down, _, keysym = struct.unpack(">BHI", await self.reader.readexactly(7))
                self.server.emit_input(("key", bool(down), keysym))
            elif kind == 5:
                mask, x, y = struct.unpack(">BHH", await self.reader.readexactly(5))
                self.server.emit_input(("pointer", mask, x, y))
            elif kind == 6:
                length = struct.unpack(">3xI", await self.reader.readexactly(7))[0]
                await self.reader.readexactly(min(length, 1 << 20))  # no clipboard bridge: read and drop
            elif kind == 251:
                head = await self.reader.readexactly(7)
                await self.reader.readexactly(16 * head[5])  # resize requests are ignored: the Mac decides
            else:
                raise RfbProtocolError(f"unsupported client message {kind}")

    # -- updates -----------------------------------------------------------------------------------------

    def build_update(self) -> Optional[bytes]:
        """The next FramebufferUpdate for the pending request, or None when nothing changed (the request stays
        pending until a frame does)."""
        frame = self.server.frame
        if self.request is None or frame is None:
            return None
        (rx, ry, rw, rh), incremental = self.request
        rects_out: list[bytes] = []
        size = frame.size
        if self.sent_size != size:
            if ENC_EXT_DESKTOP_SIZE in self.encodings:
                rects_out.append(ext_desktop_size_rect(*size))
            elif ENC_DESKTOP_SIZE in self.encodings:
                rects_out.append(desktop_size_rect(*size))
            self.sent, self.sent_size = None, size
            rx, ry, rw, rh = 0, 0, size[0], size[1]
            incremental = False
        region = (rx, ry, rw, rh)
        if incremental:
            rects = dirty_rects(self.sent, frame, region)
        else:
            rects = dirty_rects(None, frame, region)
        if not rects and not rects_out:
            return None
        use_zrle = ENC_ZRLE in self.encodings
        for rect in rects:
            rects_out.append(encode_zrle(frame, rect, self.fmt, self.zlib) if use_zrle else encode_raw(frame, rect, self.fmt))
        if self.sent is None:
            self.sent = frame.copy()
        else:
            for x, y, w, h in rects:
                self.sent.paste(frame.crop((x, y, x + w, y + h)), (x, y))
        self.request = None
        return struct.pack(">BxH", 0, len(rects_out)) + b"".join(rects_out)

    async def send_loop(self) -> None:
        while True:
            await self.wake.wait()
            self.wake.clear()
            message = self.build_update()
            if message:
                self.writer.write(message)
                await self.writer.drain()


class RfbServer:
    """``publish`` frames from any thread; ``on_input`` receives ("key", down, keysym) and
    ("pointer", mask, x, y) from any client (gated by ``input_allowed`` when given)."""

    def __init__(self, path: Path, *, on_input: Callable[[tuple], None],
                 on_viewers: Optional[Callable[[int], None]] = None,
                 input_allowed: Optional[Callable[[], bool]] = None) -> None:
        self.path = Path(path)
        self._on_input = on_input
        self._on_viewers = on_viewers
        self._input_allowed = input_allowed
        self.frame: Optional[Image.Image] = None
        self._clients: set[_Client] = set()
        self._server: Optional[asyncio.AbstractServer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def size(self) -> tuple[int, int]:
        return self.frame.size if self.frame is not None else (0, 0)

    @property
    def viewer_count(self) -> int:
        return len(self._clients)

    def emit_input(self, event: tuple) -> None:
        if self._input_allowed is None or self._input_allowed():
            self._on_input(event)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if len(os.fsencode(self.path)) >= 100:  # sun_path is 104 bytes on macOS
            raise OSError(f"the socket path is too long for a Unix socket ({len(os.fsencode(self.path))} bytes): {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        old_umask = os.umask(0o177)  # the socket is created 0600: the lease and bridge are the only doors in
        try:
            self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for client in list(self._clients):
            client.writer.close()
        self.path.unlink(missing_ok=True)

    def publish(self, image: Image.Image) -> None:
        """Thread-safe: make ``image`` the current framebuffer and wake every client."""
        if self._loop is None:
            self.frame = image
            return
        self._loop.call_soon_threadsafe(self._set_frame, image)

    def _set_frame(self, image: Image.Image) -> None:
        self.frame = image
        for client in self._clients:
            client.wake.set()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(self, reader, writer)
        try:
            await client.handshake()
            self._clients.add(client)
            self._notify_viewers()
            tasks = [asyncio.create_task(client.read_loop()), asyncio.create_task(client.send_loop())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.exception()
        except (asyncio.IncompleteReadError, ConnectionError, RfbProtocolError) as exc:
            logger.debug("rfb client ended: %r", exc)
        finally:
            if client in self._clients:
                self._clients.discard(client)
                self._notify_viewers()
            writer.close()

    def _notify_viewers(self) -> None:
        if self._on_viewers is not None:
            self._on_viewers(len(self._clients))
