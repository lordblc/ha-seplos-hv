"""Async transport for the Seplos HV BCU protocol (TCP or serial).

Pure standard-library asyncio, plus an optional, lazily-imported serial
backend. TCP mode must keep working even when ``serial_asyncio_fast`` is not
installed, so that import only happens inside the serial connect branch.

Read-only frames come exclusively from ``protocol.build_request()``, which itself
refuses any command outside ``protocol.ALLOWED_CMDS``. Since v0.4.0, ``write_params()``
below can also send a write frame - but ONLY a frame built by
``protocol.build_write_request()``, which refuses anything blacklisted or not a known
parameter write. Nothing else in this module builds an outgoing frame of any kind.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

try:
    from .protocol import (
        CMD_BMS_SERIAL,
        CMD_CELLS,
        CMD_FIRMWARE,
        CMD_PACK_SERIAL,
        CMD_PROTOCOL,
        CMD_STATUS,
        CMD_SUMMARY,
        CMD_TEMPS,
        PARAM_CMDS,
        WRITE_CMDS,
        Frame,
        FrameParser,
        ModuleCells,
        PackSummary,
        ParamBlock,
        Status,
        Temperatures,
        build_request,
        build_write_request,
        decode_cells,
        decode_params,
        decode_status,
        decode_string,
        decode_summary,
        decode_temps,
        encode_params,
        format_frame_hex,
        param_blocks_close,
    )
except ImportError:  # pragma: no cover - fallback for standalone (non-package) import
    from protocol import (  # type: ignore[no-redef]
        CMD_BMS_SERIAL,
        CMD_CELLS,
        CMD_FIRMWARE,
        CMD_PACK_SERIAL,
        CMD_PROTOCOL,
        CMD_STATUS,
        CMD_SUMMARY,
        CMD_TEMPS,
        PARAM_CMDS,
        WRITE_CMDS,
        Frame,
        FrameParser,
        ModuleCells,
        PackSummary,
        ParamBlock,
        Status,
        Temperatures,
        build_request,
        build_write_request,
        decode_cells,
        decode_params,
        decode_status,
        decode_string,
        decode_summary,
        decode_temps,
        encode_params,
        format_frame_hex,
        param_blocks_close,
    )

_LOGGER = logging.getLogger(__name__)

# Minimum gap enforced between two requests on the shared half-duplex bus.
MIN_REQUEST_GAP_S = 0.030

# Bytes read per recv() call while draining stale data or waiting for a reply.
_READ_CHUNK = 4096


class SeplosError(Exception):
    """Base exception for all Seplos HV client errors."""


class SeplosConnectionError(SeplosError):
    """Raised when the connection cannot be established, or is lost mid-request."""


class SeplosTimeout(SeplosError):
    """Raised when a request receives no matching reply within the timeout."""


@dataclass
class WriteResult:
    """Outcome of one write_params() call.

    ``frame_hex`` is always populated, dry-run or not, so a caller (the write_param
    service, in particular) can inspect exactly what would be/was sent. ``reply`` is the
    raw ack Frame from the BCU. The ack shape was VERIFIED on 2026-10-01 from a vendor-tool
    capture: the BCU echoes the write command id with an EMPTY payload. ``ack_ok`` is True
    when the reply has exactly that shape. ``readback``/``verified`` come
    from a fresh read of the same parameter group performed immediately after a real
    (non-dry-run) write. ``baseline`` is set by the caller (SeplosHvCoordinator.
    async_write_param), not by write_params() itself, to "fresh" or "cached" depending on
    whether a fresh pre-write read of the group succeeded - it defaults to "fresh" so a
    WriteResult built anywhere else (e.g. directly against the client, as in tests) is not
    misleadingly marked "cached".
    """

    frame_hex: str
    sent: bool
    reply: Frame | None
    readback: ParamBlock | None
    verified: bool
    ack_ok: bool = False
    baseline: str = "fresh"


class SeplosHvClient:
    """Serialised, read-only client for one Seplos HV BCU.

    Every public ``read_*``/``request`` call goes through a single
    ``asyncio.Lock`` - the bus is half-duplex and replies carry no transaction
    id, so two requests in flight at once would interleave and corrupt both.
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int = 8899,
        serial_port: str | None = None,
        baudrate: int = 57600,
        timeout: float = 1.5,
    ) -> None:
        if not host and not serial_port:
            raise ValueError("either host or serial_port must be given")
        self._host = host
        self._port = port
        self._serial_port = serial_port
        self._baudrate = baudrate
        self._timeout = timeout

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._parser = FrameParser()
        self._last_request_time = 0.0

    @property
    def is_connected(self) -> bool:
        return self._writer is not None

    async def connect(self) -> None:
        """Open the transport. Raises SeplosConnectionError on failure."""
        if self._serial_port:
            try:
                import serial_asyncio_fast  # lazy: keeps TCP mode working without it
            except ImportError as exc:
                raise SeplosConnectionError(
                    "serial transport requested but serial_asyncio_fast is not installed"
                ) from exc
            try:
                self._reader, self._writer = await serial_asyncio_fast.open_serial_connection(
                    url=self._serial_port, baudrate=self._baudrate
                )
            except OSError as exc:
                raise SeplosConnectionError(f"serial connect failed: {exc}") from exc
        else:
            try:
                self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
            except OSError as exc:
                raise SeplosConnectionError(f"TCP connect to {self._host}:{self._port} failed: {exc}") from exc
        self._parser.reset()

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass

    async def _drain_stale(self) -> None:
        """Discard any bytes already sitting in the read buffer and reset the parser."""
        assert self._reader is not None
        self._parser.reset()
        while True:
            try:
                chunk = await asyncio.wait_for(self._reader.read(_READ_CHUNK), timeout=0.01)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break

    async def _throttle(self) -> None:
        gap = time.monotonic() - self._last_request_time
        if gap < MIN_REQUEST_GAP_S:
            await asyncio.sleep(MIN_REQUEST_GAP_S - gap)

    async def _send_frame_and_wait(self, frame_bytes: bytes, match_cmds: frozenset[int],
                                    *, label: str) -> Frame:
        """Write ``frame_bytes`` and wait for a reply whose cmd is in ``match_cmds``.

        Shared by the read path (``_send_and_wait``, ``match_cmds`` = {cmd}) and the write
        path (``write_params``, ``match_cmds`` = {write_cmd, read_cmd}; the verified ack is
        the write cmd echoed with an empty payload, the read cmd is tolerated as a fallback).
        ``label`` is only used in the timeout message.
        """
        assert self._reader is not None and self._writer is not None
        await self._drain_stale()
        await self._throttle()

        try:
            self._writer.write(frame_bytes)
            await self._writer.drain()
        except (OSError, ConnectionError) as exc:
            raise SeplosConnectionError(f"write failed: {exc}") from exc
        self._last_request_time = time.monotonic()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SeplosTimeout(f"no reply to {label} within {self._timeout}s")
            try:
                chunk = await asyncio.wait_for(self._reader.read(_READ_CHUNK), timeout=remaining)
            except asyncio.TimeoutError:
                raise SeplosTimeout(f"no reply to {label} within {self._timeout}s") from None
            except (OSError, ConnectionError) as exc:
                raise SeplosConnectionError(f"read failed: {exc}") from exc
            if not chunk:
                raise SeplosConnectionError("connection closed by peer")
            for parsed in self._parser.feed(chunk):
                if parsed.cmd in match_cmds:
                    return parsed
                # a frame for a different cmd should not happen under the lock;
                # ignore it and keep waiting for the one we asked for.

    async def _send_and_wait(self, cmd: int) -> Frame:
        return await self._send_frame_and_wait(
            build_request(cmd), frozenset({cmd}), label=f"cmd 0x{cmd:04X}"
        )

    async def request(self, cmd: int) -> Frame:
        """Send ``cmd`` and return the matching reply Frame.

        Serialised by the client's lock. Raises SeplosTimeout if no reply
        arrives in time, or SeplosConnectionError if the link is down/drops.
        On a connection error, one reconnect is attempted automatically before
        giving up.
        """
        async with self._lock:
            if self._writer is None:
                await self.connect()
            try:
                return await self._send_and_wait(cmd)
            except SeplosConnectionError:
                await self.close()
                await self.connect()
                return await self._send_and_wait(cmd)

    async def read_identity(self) -> dict[str, str]:
        protocol = decode_string((await self.request(CMD_PROTOCOL)).payload)
        firmware = decode_string((await self.request(CMD_FIRMWARE)).payload)
        bms_serial = decode_string((await self.request(CMD_BMS_SERIAL)).payload)
        pack_serial = decode_string((await self.request(CMD_PACK_SERIAL)).payload)
        return {
            "protocol": protocol,
            "firmware": firmware,
            "bms_serial": bms_serial,
            "pack_serial": pack_serial,
        }

    async def read_summary(self) -> PackSummary:
        return decode_summary((await self.request(CMD_SUMMARY)).payload)

    async def read_status(self) -> Status:
        return decode_status((await self.request(CMD_STATUS)).payload)

    async def read_cells(self) -> list[ModuleCells]:
        return decode_cells((await self.request(CMD_CELLS)).payload)

    async def read_temps(self) -> Temperatures:
        return decode_temps((await self.request(CMD_TEMPS)).payload)

    async def read_params(self) -> dict[str, ParamBlock]:
        result: dict[str, ParamBlock] = {}
        for cmd, (key, _unit) in PARAM_CMDS.items():
            frame = await self.request(cmd)
            result[key] = decode_params(cmd, frame.payload)
        return result

    async def read_param(self, read_cmd: int) -> ParamBlock:
        """Read a single parameter group, fresh, through the normal request()/lock path.

        Used by the write path to get an up-to-date baseline for exactly one group
        without re-polling all ``len(PARAM_CMDS)`` of them (unlike ``read_params()``).
        """
        frame = await self.request(read_cmd)
        return decode_params(read_cmd, frame.payload)

    async def write_params(
        self, read_cmd: int, block: ParamBlock, *, dry_run: bool = True
    ) -> WriteResult:
        """Write a full parameter block, then read it back to verify.

        ``read_cmd`` is the READ command for the parameter group (e.g. 0x0209 for
        charge_over_current) - the same id ``PARAM_CMDS``/``read_params()`` use. The
        actual write command is looked up via ``protocol.WRITE_CMDS``.

        With ``dry_run=True`` (the default): builds the frame and returns it in
        ``WriteResult.frame_hex`` WITHOUT touching the wire at all - ``sent`` is False,
        ``reply``/``readback`` are None, ``verified`` is False. Nothing is written,
        connected, or locked.

        With ``dry_run=False``: under the client's lock, sends the write frame, waits for
        a reply matching either the write cmd or the read cmd. The verified ack (vendor
        capture 2026-10-01, cmd 0x0200) is the write cmd echoed with an EMPTY payload;
        ``WriteResult.ack_ok`` reports whether that exact shape came back. Anything else
        is logged at WARNING but does not raise. The method then immediately re-reads
        ``read_cmd`` and compares the freshly decoded block against ``block`` with
        ``protocol.param_blocks_close`` to set ``verified``.

        The only frame this method ever writes to the wire is the one
        ``protocol.build_write_request()`` returns - never a bare ``payload``.
        """
        write_cmd = WRITE_CMDS[read_cmd]  # KeyError -> not a known parameter cmd; let it raise
        payload = encode_params(block)
        frame_bytes = build_write_request(read_cmd, payload)
        frame_hex = format_frame_hex(frame_bytes)

        if dry_run:
            return WriteResult(frame_hex=frame_hex, sent=False, reply=None, readback=None,
                                verified=False)

        async with self._lock:
            if self._writer is None:
                await self.connect()

            async def _do_write() -> Frame:
                return await self._send_frame_and_wait(
                    frame_bytes, frozenset({write_cmd, read_cmd}),
                    label=f"write cmd 0x{write_cmd:04X}",
                )

            try:
                reply = await _do_write()
            except SeplosConnectionError:
                await self.close()
                await self.connect()
                reply = await _do_write()

            ack_ok = reply.cmd == write_cmd and len(reply.payload) == 0
            if ack_ok:
                _LOGGER.info("write cmd 0x%04X sent: %s ; BCU acknowledged (empty echo)",
                             write_cmd, frame_hex)
            else:
                _LOGGER.warning(
                    "write cmd 0x%04X sent: %s ; unexpected reply cmd 0x%04X payload: %s",
                    write_cmd, frame_hex, reply.cmd, format_frame_hex(reply.payload),
                )

            readback_frame = await self._send_and_wait(read_cmd)
            readback = decode_params(read_cmd, readback_frame.payload)
            verified = param_blocks_close(readback, block)
            if not verified:
                _LOGGER.warning(
                    "write cmd 0x%04X: read-back does not match intended value "
                    "(intended=%r, read back=%r)",
                    write_cmd, block, readback,
                )

        return WriteResult(frame_hex=frame_hex, sent=True, reply=reply, readback=readback,
                            verified=verified, ack_ok=ack_ok)
