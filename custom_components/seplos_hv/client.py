"""Async transport for the Seplos HV BCU protocol (TCP or serial).

Pure standard-library asyncio, plus an optional, lazily-imported serial
backend. TCP mode must keep working even when ``serial_asyncio_fast`` is not
installed, so that import only happens inside the serial connect branch.

Read-only by construction: the only frames ever written to the wire come from
``protocol.build_request()``, which itself refuses any command outside
``protocol.ALLOWED_CMDS``.
"""

from __future__ import annotations

import asyncio
import time

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
        Frame,
        FrameParser,
        ModuleCells,
        PackSummary,
        ParamBlock,
        Status,
        Temperatures,
        build_request,
        decode_cells,
        decode_params,
        decode_status,
        decode_string,
        decode_summary,
        decode_temps,
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
        Frame,
        FrameParser,
        ModuleCells,
        PackSummary,
        ParamBlock,
        Status,
        Temperatures,
        build_request,
        decode_cells,
        decode_params,
        decode_status,
        decode_string,
        decode_summary,
        decode_temps,
    )

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

    async def _send_and_wait(self, cmd: int) -> Frame:
        assert self._reader is not None and self._writer is not None
        await self._drain_stale()
        await self._throttle()

        frame_bytes = build_request(cmd)
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
                raise SeplosTimeout(f"no reply to cmd 0x{cmd:04X} within {self._timeout}s")
            try:
                chunk = await asyncio.wait_for(self._reader.read(_READ_CHUNK), timeout=remaining)
            except asyncio.TimeoutError:
                raise SeplosTimeout(f"no reply to cmd 0x{cmd:04X} within {self._timeout}s") from None
            except (OSError, ConnectionError) as exc:
                raise SeplosConnectionError(f"read failed: {exc}") from exc
            if not chunk:
                raise SeplosConnectionError("connection closed by peer")
            for parsed in self._parser.feed(chunk):
                if parsed.cmd == cmd:
                    return parsed
                # a frame for a different cmd should not happen under the lock;
                # ignore it and keep waiting for the one we asked for.

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
