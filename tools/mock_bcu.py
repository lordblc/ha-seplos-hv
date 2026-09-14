#!/usr/bin/env python3
"""Replay TCP server for the Seplos HV BCU protocol - for tests without hardware.

Parses the vendor-tool capture fixtures (each line ``【Data】<hex bytes>`` with
a ``↑``/``↓`` marker for reply/request), keeps the *last* seen reply payload
per command id, and answers incoming requests by rebuilding a fresh, correctly
CRC'd frame (src=0x21 BCU, dst=0x01 host) around that payload.

Unknown or forbidden command ids are logged and get no reply at all - this
mock never talks back to a client asking for something outside the whitelist,
mirroring the real device's read-only contract.

Usage::

    python3 tools/mock_bcu.py [--port 8899] [--delay 15] [fixture ...]

``serve(port, fixtures)`` is importable for tests: it starts the server and
returns the ``asyncio.Server`` instance.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SEPLOS_DIR = _REPO_ROOT / "custom_components" / "seplos_hv"
# Import protocol.py directly as a flat module (not through the
# custom_components.seplos_hv package) so this never triggers that package's
# __init__.py, which imports homeassistant - unneeded for this replay server.
if str(_SEPLOS_DIR) not in sys.path:
    sys.path.insert(0, str(_SEPLOS_DIR))

from protocol import (  # noqa: E402
    ALLOWED_CMDS,
    BCU_ADDR,
    EOF,
    HOST_ADDR,
    SOF,
    FrameParser,
    crc16,
)

log = logging.getLogger("mock_bcu")

DEFAULT_FIXTURES = [
    str(_REPO_ROOT / "tests" / "fixtures" / "bcu_frames_1.txt"),
    str(_REPO_ROOT / "tests" / "fixtures" / "bcu_frames_2.txt"),
]

_DATA_RE = re.compile(r"【Data】\s*([0-9A-Fa-f ]+)")


def _is_reply_line(line: str) -> bool:
    """True for a BCU->host (↑) line. The column header itself contains a
    literal '↑' character ('↑-↓'), so we must match the marker cell, not just
    the presence of the glyph anywhere in the line."""
    return "】 ↑" in line or "】\t↑" in line


def load_fixture_replies(fixture_paths: list[str]) -> dict[int, bytes]:
    """Parse fixture files, return {cmd: last-seen reply payload}."""
    replies: dict[int, bytes] = {}
    for path in fixture_paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("could not read fixture %s: %s", path, exc)
            continue
        for line in text.splitlines():
            if "【Data】" not in line or not _is_reply_line(line):
                continue
            m = _DATA_RE.search(line)
            if not m:
                continue
            try:
                raw = bytes(int(tok, 16) for tok in m.group(1).split())
            except ValueError:
                continue
            if len(raw) < 11 or raw[0] != SOF or raw[-1] != EOF:
                continue
            length = (raw[6] << 8) | raw[7]
            if len(raw) != 11 + length:
                continue
            if crc16(raw[:-3]) != ((raw[-3] << 8) | raw[-2]):
                continue
            cmd = (raw[3] << 8) | raw[4]
            payload = raw[8:8 + length]
            replies[cmd] = payload  # later occurrences overwrite earlier ones
    return replies


def build_reply(cmd: int, payload: bytes) -> bytes:
    """Rebuild a BCU->host reply frame around ``payload`` with a fresh CRC."""
    header = (
        bytes([SOF, BCU_ADDR, HOST_ADDR])
        + cmd.to_bytes(2, "big")
        + b"\x00"
        + len(payload).to_bytes(2, "big")
    )
    frame = header + payload
    frame += crc16(frame).to_bytes(2, "big") + bytes([EOF])
    return frame


async def serve(port: int, fixtures: list[str], *, delay_ms: int = 15,
                 host: str = "127.0.0.1") -> asyncio.Server:
    """Start the replay server and return the running asyncio.Server."""
    replies = load_fixture_replies(fixtures)
    log.info("loaded %d distinct command replies from %d fixture file(s)", len(replies), len(fixtures))

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        parser = FrameParser()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                for frame in parser.feed(data):
                    if frame.cmd not in ALLOWED_CMDS:
                        log.warning("forbidden cmd 0x%04X requested by %s - not replying", frame.cmd, peer)
                        continue
                    payload = replies.get(frame.cmd)
                    if payload is None:
                        log.warning("no fixture reply for cmd 0x%04X requested by %s - not replying",
                                    frame.cmd, peer)
                        continue
                    if delay_ms:
                        await asyncio.sleep(delay_ms / 1000)
                    writer.write(build_reply(frame.cmd, payload))
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    server = await asyncio.start_server(handle_client, host=host, port=port)
    return server


async def _run_forever(port: int, fixtures: list[str], delay_ms: int) -> None:
    server = await serve(port, fixtures, delay_ms=delay_ms)
    addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    log.info("mock_bcu listening on %s (delay=%dms)", addrs, delay_ms)
    async with server:
        await server.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fixtures", nargs="*", default=DEFAULT_FIXTURES,
                     help="fixture files to replay (default: tests/fixtures/bcu_frames_*.txt)")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--delay", type=int, default=15, help="reply delay in ms (default 15)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        asyncio.run(_run_forever(args.port, args.fixtures, args.delay))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
