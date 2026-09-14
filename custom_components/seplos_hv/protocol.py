"""Seplos HV BCU wire protocol: framing, CRC and payload decoders.

Pure standard library. No Home Assistant imports. Importable standalone as
``protocol.py`` (e.g. by adding this directory to ``sys.path``) or as part of
the ``custom_components.seplos_hv`` package.

Frame format (see docs/protocol-reference.md)::

    9A | src | dst | CMD u16 BE | 00 | LEN u16 BE | payload[LEN] | CRC u16 BE | 9D

``src``/``dst``: host = 0x01, BCU = 0x21; replies swap the two. CRC is
CRC-16/MODBUS (poly 0xA001 reflected, init 0xFFFF) over the frame from the
0x9A byte up to (not including) the CRC, stored big-endian. There is no byte
stuffing; frames are delimited by the LEN field, never by scanning for 0x9D.

This module never builds anything but a 6-byte all-zero read request for a
command in :data:`ALLOWED_CMDS`. It has no notion of a "write" frame at all.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

SOF, EOF = 0x9A, 0x9D
HOST_ADDR, BCU_ADDR = 0x01, 0x21

CMD_PROTOCOL, CMD_FIRMWARE, CMD_BMS_SERIAL, CMD_PACK_SERIAL = 0x0001, 0x0002, 0x0005, 0x0007
CMD_SUMMARY, CMD_STATUS, CMD_CELLS, CMD_TEMPS = 0x000A, 0x000B, 0x000C, 0x000D

# Current field in PackSummary is a signed i32 in units of 0.01 A. The scale is
# not independently confirmed (current was 0 in every captured frame, pack in
# Standby) so it is kept as a named constant rather than buried in the decoder.
CURRENT_SCALE = 0.01

# Protection parameter blocks: cmd -> (key, unit). Every id here is an ODD
# 0x02xx "read" command observed in the captures. Even ids are presumed to be
# the matching writes and must never appear here or anywhere else in this file.
PARAM_CMDS: dict[int, tuple[str, str]] = {
    0x0201: ("cell_over_voltage", "mV"),
    0x0203: ("cell_under_voltage", "mV"),
    0x0209: ("charge_over_current", "A"),
    0x020B: ("discharge_over_current", "A"),
    0x020D: ("charge_high_temperature", "C"),
    0x020F: ("discharge_high_temperature", "C"),
    0x0211: ("charge_low_temperature", "C"),
    0x0213: ("discharge_low_temperature", "C"),
    0x0215: ("ambient_high_temperature", "C"),
    0x0217: ("ambient_low_temperature", "C"),
    0x021F: ("soc_high", "%"),
    0x0221: ("soc_low", "%"),
    0x0223: ("insulation_positive", "ohm/V"),
    0x0225: ("insulation_negative", "ohm/V"),
    0x0227: ("cell_delta_charge", "mV"),
    0x0229: ("cell_delta_discharge", "mV"),
    0x022B: ("temperature_delta_charge", "C"),
    0x022D: ("temperature_delta_discharge", "C"),
    0x0239: ("heating_start_stop", "C"),
    0x023D: ("relay_high_temperature", "C"),
}

# The complete whitelist of command ids this library will ever transmit.
# build_request() raises ValueError for anything outside this set - including
# every even 0x02xx id, which would be a write.
ALLOWED_CMDS: frozenset[int] = frozenset(
    {CMD_PROTOCOL, CMD_FIRMWARE, CMD_BMS_SERIAL, CMD_PACK_SERIAL,
     CMD_SUMMARY, CMD_STATUS, CMD_CELLS, CMD_TEMPS}
    | set(PARAM_CMDS.keys())
)


def crc16(data: bytes) -> int:
    """CRC-16/MODBUS (poly 0xA001 reflected, init 0xFFFF)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_request(cmd: int) -> bytes:
    """Build a 6-byte-zero-payload read request frame for ``cmd``.

    Raises ValueError if ``cmd`` is not in ALLOWED_CMDS. This is the only
    frame-building function in this module; there is no way to construct a
    write frame through this API.
    """
    if cmd not in ALLOWED_CMDS:
        raise ValueError(f"cmd 0x{cmd:04X} is not in the allowed command whitelist")
    payload = b"\x00" * 6
    header = (
        bytes([SOF, HOST_ADDR, BCU_ADDR])
        + cmd.to_bytes(2, "big")
        + b"\x00"
        + len(payload).to_bytes(2, "big")
    )
    frame = header + payload
    frame += crc16(frame).to_bytes(2, "big") + bytes([EOF])
    return frame


class FrameError(Exception):
    """Raised for a malformed or CRC-invalid frame."""


@dataclass(frozen=True)
class Frame:
    src: int
    dst: int
    cmd: int
    payload: bytes


def parse_frame(buf: bytes) -> Frame:
    """Strict single-frame parse. Raises FrameError on any problem."""
    if len(buf) < 11:
        raise FrameError(f"frame too short: {len(buf)} bytes")
    if buf[0] != SOF:
        raise FrameError(f"bad start-of-frame byte: 0x{buf[0]:02X}")
    if buf[-1] != EOF:
        raise FrameError(f"bad end-of-frame byte: 0x{buf[-1]:02X}")
    length = struct.unpack(">H", buf[6:8])[0]
    expected_len = 11 + length
    if len(buf) != expected_len:
        raise FrameError(f"length mismatch: expected {expected_len} bytes, got {len(buf)}")
    calc_crc = crc16(buf[:-3])
    recv_crc = struct.unpack(">H", buf[-3:-1])[0]
    if calc_crc != recv_crc:
        raise FrameError(f"CRC mismatch: expected 0x{calc_crc:04X}, got 0x{recv_crc:04X}")
    cmd = struct.unpack(">H", buf[3:5])[0]
    return Frame(src=buf[1], dst=buf[2], cmd=cmd, payload=bytes(buf[8:8 + length]))


class FrameParser:
    """Incremental, resyncing frame parser for the 0x9A/0x9D wire protocol.

    Feed it raw bytes as they arrive from the transport; ``feed()`` returns any
    complete, CRC-valid frames found so far and keeps the remainder buffered
    for the next call (handles frames split across reads). Garbage bytes
    (noise, misaligned data, a header whose CRC does not check out) are
    discarded one byte at a time until a plausible 0x9A header is found whose
    LEN fits the buffered data and whose CRC checks. The buffer is bounded and
    dropped outright if it grows past 4096 bytes, so a stream of pure noise
    cannot grow it without limit.
    """

    _MAX_BUFFER = 4096

    def __init__(self) -> None:
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        frames: list[Frame] = []

        while True:
            if len(self._buf) > self._MAX_BUFFER:
                self._buf.clear()
                break

            if not self._buf:
                break

            if self._buf[0] != SOF:
                idx = self._buf.find(SOF, 1)
                if idx == -1:
                    self._buf.clear()
                else:
                    del self._buf[:idx]
                continue

            if len(self._buf) < 8:
                break  # header not fully buffered yet

            length = (self._buf[6] << 8) | self._buf[7]
            frame_len = 11 + length

            if frame_len > self._MAX_BUFFER:
                # implausible LEN field - this 0x9A byte was not a real header
                del self._buf[0:1]
                continue

            if len(self._buf) < frame_len:
                break  # wait for more data

            candidate = bytes(self._buf[:frame_len])

            if candidate[-1] != EOF:
                del self._buf[0:1]
                continue

            calc_crc = crc16(candidate[:-3])
            recv_crc = (candidate[-3] << 8) | candidate[-2]
            if calc_crc != recv_crc:
                del self._buf[0:1]
                continue

            cmd = (candidate[3] << 8) | candidate[4]
            frames.append(Frame(src=candidate[1], dst=candidate[2], cmd=cmd,
                                 payload=candidate[8:8 + length]))
            del self._buf[:frame_len]

        return frames


def decode_string(payload: bytes) -> str:
    """NUL-trimmed ASCII string, for the 0x0001/0x0002/0x0005/0x0007 replies."""
    return payload.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def cell_label(index: int) -> str:
    """0-based pack cell index -> 'BMU<n> C<m>' (module = index//32+1)."""
    module = index // 32 + 1
    cell = index % 32 + 1
    return f"BMU{module} C{cell}"


def temp_label(index: int) -> str:
    """0-based pack temperature index -> 'BMU<n> T<m>' (module = index//64+1)."""
    module = index // 64 + 1
    sensor = index % 64 + 1
    return f"BMU{module} T{sensor}"


def kelvin10_to_c(raw: int) -> float:
    """Convert tenths-of-a-kelvin (as used throughout this protocol) to Celsius."""
    return (raw - 2731) / 10


@dataclass
class PackSummary:
    v_pack: float
    v_collect: float
    v_load: float
    current: float
    soc: int
    soh: int
    remaining_ah: float
    full_ah: float
    design_ah: float
    limits: list[int]
    raw_u32: dict[int, int]
    cell_max_mv: int
    cell_max_index: int
    cell_min_mv: int
    cell_min_index: int
    max_temp_c: float
    max_temp_index: int
    min_temp_c: float
    min_temp_index: int

    @property
    def cell_spread_mv(self) -> int:
        return self.cell_max_mv - self.cell_min_mv

    @property
    def cell_max_label(self) -> str:
        return cell_label(self.cell_max_index)

    @property
    def cell_min_label(self) -> str:
        return cell_label(self.cell_min_index)

    @property
    def max_temp_label(self) -> str:
        return temp_label(self.max_temp_index)

    @property
    def min_temp_label(self) -> str:
        return temp_label(self.min_temp_index)

    def as_dict(self) -> dict:
        return {
            "v_pack": self.v_pack,
            "v_collect": self.v_collect,
            "v_load": self.v_load,
            "current": self.current,
            "soc": self.soc,
            "soh": self.soh,
            "remaining_ah": self.remaining_ah,
            "full_ah": self.full_ah,
            "design_ah": self.design_ah,
            "limits": list(self.limits),
            "raw_u32": dict(self.raw_u32),
            "cell_max_mv": self.cell_max_mv,
            "cell_max_index": self.cell_max_index,
            "cell_max_label": self.cell_max_label,
            "cell_min_mv": self.cell_min_mv,
            "cell_min_index": self.cell_min_index,
            "cell_min_label": self.cell_min_label,
            "cell_spread_mv": self.cell_spread_mv,
            "max_temp_c": self.max_temp_c,
            "max_temp_index": self.max_temp_index,
            "max_temp_label": self.max_temp_label,
            "min_temp_c": self.min_temp_c,
            "min_temp_index": self.min_temp_index,
            "min_temp_label": self.min_temp_label,
        }


def decode_summary(payload: bytes) -> PackSummary:
    """Decode the 0x000A pack summary reply (82 bytes)."""
    if len(payload) < 82:
        raise FrameError(f"summary payload too short: {len(payload)} bytes, need 82")
    v_pack = struct.unpack(">H", payload[0:2])[0] / 10
    v_collect = struct.unpack(">H", payload[2:4])[0] / 10
    v_load = struct.unpack(">H", payload[4:6])[0] / 10
    current = struct.unpack(">i", payload[6:10])[0] * CURRENT_SCALE
    soc = payload[12]
    soh = payload[16]
    remaining_ah = struct.unpack(">I", payload[18:22])[0] / 100
    full_ah = struct.unpack(">I", payload[30:34])[0] / 100
    design_ah = struct.unpack(">I", payload[38:42])[0] / 100
    raw_u32 = {off: struct.unpack(">I", payload[off:off + 4])[0] for off in (22, 26, 34, 42, 46)}
    limits = [struct.unpack(">I", payload[50 + 4 * i:54 + 4 * i])[0] for i in range(4)]
    cell_max_mv, cell_max_index, cell_min_mv, cell_min_index = struct.unpack(">HHHH", payload[66:74])
    max_temp_raw, max_temp_index, min_temp_raw, min_temp_index = struct.unpack(">HHHH", payload[74:82])
    return PackSummary(
        v_pack=v_pack, v_collect=v_collect, v_load=v_load, current=current,
        soc=soc, soh=soh, remaining_ah=remaining_ah, full_ah=full_ah, design_ah=design_ah,
        limits=limits, raw_u32=raw_u32,
        cell_max_mv=cell_max_mv, cell_max_index=cell_max_index,
        cell_min_mv=cell_min_mv, cell_min_index=cell_min_index,
        max_temp_c=kelvin10_to_c(max_temp_raw), max_temp_index=max_temp_index,
        min_temp_c=kelvin10_to_c(min_temp_raw), min_temp_index=min_temp_index,
    )


@dataclass
class Status:
    relay_word: int
    current_limiting: bool
    charge_relay: bool
    discharge_relay: bool
    precharge_relay: bool
    negative_relay: bool
    heating_relay: bool
    raw: bytes
    byte27: int
    byte30: int
    byte36: int


def decode_status(payload: bytes) -> Status:
    """Decode the 0x000B status/relay reply. Relay word is a u32 BE at bytes 16..19."""
    if len(payload) < 37:
        raise FrameError(f"status payload too short: {len(payload)} bytes, need >=37")
    relay_word = struct.unpack(">I", payload[16:20])[0]
    return Status(
        relay_word=relay_word,
        current_limiting=bool(relay_word & (1 << 0)),
        charge_relay=bool(relay_word & (1 << 1)),
        discharge_relay=bool(relay_word & (1 << 2)),
        precharge_relay=bool(relay_word & (1 << 3)),
        negative_relay=bool(relay_word & (1 << 4)),
        heating_relay=bool(relay_word & (1 << 5)),
        raw=bytes(payload),
        byte27=payload[27],
        byte30=payload[30],
        byte36=payload[36],
    )


@dataclass
class ModuleCells:
    module_id: int
    cells_mv: list[int]

    @property
    def min_mv(self) -> int:
        return min(self.cells_mv) if self.cells_mv else 0

    @property
    def max_mv(self) -> int:
        return max(self.cells_mv) if self.cells_mv else 0

    @property
    def spread_mv(self) -> int:
        return self.max_mv - self.min_mv

    @property
    def min_index(self) -> int:
        return self.cells_mv.index(self.min_mv) if self.cells_mv else -1

    @property
    def max_index(self) -> int:
        return self.cells_mv.index(self.max_mv) if self.cells_mv else -1


def decode_cells(payload: bytes) -> list[ModuleCells]:
    """Decode the 0x000C cell-voltage reply: u16 module_count then per module."""
    if len(payload) < 2:
        raise FrameError(f"cells payload too short: {len(payload)} bytes")
    module_count = struct.unpack(">H", payload[0:2])[0]
    modules: list[ModuleCells] = []
    offset = 2
    for _ in range(module_count):
        if offset + 2 > len(payload):
            raise FrameError("truncated cells payload (module header)")
        module_id = payload[offset]
        cell_count = payload[offset + 1]
        offset += 2
        end = offset + 2 * cell_count
        if end > len(payload):
            raise FrameError("truncated cells payload (cell data)")
        cells_mv = list(struct.unpack(f">{cell_count}H", payload[offset:end]))
        offset = end
        modules.append(ModuleCells(module_id=module_id, cells_mv=cells_mv))
    return modules


@dataclass
class ModuleTemps:
    module_id: int
    sensors_c: list[float]
    extra_raw: list[int]


@dataclass
class Temperatures:
    bcu_c: list[float]
    modules: list[ModuleTemps]


def decode_temps(payload: bytes) -> Temperatures:
    """Decode the 0x000D temperatures reply.

    u8 module_count; u8 bcu_count; bcu_count x u16 (0.1 K); per module: u8 id,
    u8 sensor_count, sensor_count x u16 (0.1 K), u8 extra_count, extra_count x
    u16 (raw, meaning not established - kept unconverted).
    """
    if len(payload) < 2:
        raise FrameError(f"temps payload too short: {len(payload)} bytes")
    module_count = payload[0]
    bcu_count = payload[1]
    offset = 2
    end = offset + 2 * bcu_count
    if end > len(payload):
        raise FrameError("truncated temps payload (bcu sensors)")
    bcu_c = [kelvin10_to_c(v) for v in struct.unpack(f">{bcu_count}H", payload[offset:end])]
    offset = end

    modules: list[ModuleTemps] = []
    for _ in range(module_count):
        if offset + 2 > len(payload):
            raise FrameError("truncated temps payload (module header)")
        module_id = payload[offset]
        sensor_count = payload[offset + 1]
        offset += 2
        s_end = offset + 2 * sensor_count
        if s_end > len(payload):
            raise FrameError("truncated temps payload (sensors)")
        sensors_c = [kelvin10_to_c(v) for v in struct.unpack(f">{sensor_count}H", payload[offset:s_end])]
        offset = s_end
        if offset >= len(payload):
            raise FrameError("truncated temps payload (extra_count)")
        extra_count = payload[offset]
        offset += 1
        e_end = offset + 2 * extra_count
        if e_end > len(payload):
            raise FrameError("truncated temps payload (extra)")
        extra_raw = list(struct.unpack(f">{extra_count}H", payload[offset:e_end]))
        offset = e_end
        modules.append(ModuleTemps(module_id=module_id, sensors_c=sensors_c, extra_raw=extra_raw))

    return Temperatures(bcu_c=bcu_c, modules=modules)


@dataclass
class ParamLevel:
    trip: float
    trip_delay_s: float
    recover: float
    recover_delay_s: float


@dataclass
class ParamBlock:
    key: str
    unit: str
    levels: list[ParamLevel] = field(default_factory=list)


def _convert_param_value(raw: int, unit: str) -> float:
    if unit == "C":
        return kelvin10_to_c(raw)
    if unit == "A":
        signed = raw - 0x10000 if raw >= 0x8000 else raw
        return float(signed)
    return float(raw)


def decode_params(cmd: int, payload: bytes) -> ParamBlock:
    """Decode a 0x02xx protection-parameter reply.

    The documented, common shape is 24 bytes = 12 x u16 BE = three levels of
    (trip, trip_delay, recover, recover_delay); that holds for 18 of the 20
    whitelisted parameter commands. Two are documented exceptions, confirmed
    against the fixtures rather than assumed:

    - 0x0209 (charge over-current) and 0x020B (discharge over-current) reply
      with 16 bytes (8 x u16 = two quads, not three). protocol-reference.md
      flags this layout as "not yet confidently mapped" - decoded here with
      the same (trip, trip_delay, recover, recover_delay) grouping as the
      standard blocks, best-effort.
    - 0x0239 (heating film start/stop) replies with only 4 bytes (2 x u16):
      a bare (trip, recover) pair with no delay fields at all. Delays are
      reported as 0.0 in that case.

    Delay unit is 100 ms (raw x 0.1 = seconds); the unit for trip/recover
    comes from PARAM_CMDS and drives the C/A conversions.
    """
    if cmd not in PARAM_CMDS:
        raise ValueError(f"cmd 0x{cmd:04X} is not a known parameter command")
    if len(payload) < 4:
        raise FrameError(f"param payload too short: {len(payload)} bytes, need at least 4")
    key, unit = PARAM_CMDS[cmd]
    n_u16 = len(payload) // 2
    raw = struct.unpack(f">{n_u16}H", payload[:n_u16 * 2])

    levels: list[ParamLevel] = []
    i = 0
    while i + 4 <= len(raw):
        trip, trip_delay, recover, recover_delay = raw[i:i + 4]
        levels.append(ParamLevel(
            trip=_convert_param_value(trip, unit),
            trip_delay_s=trip_delay * 0.1,
            recover=_convert_param_value(recover, unit),
            recover_delay_s=recover_delay * 0.1,
        ))
        i += 4

    if not levels:
        # Shorter than one full quad (observed for 0x0239: 4 bytes). No delay
        # fields are present - just a bare (trip, recover) pair.
        trip, recover = raw[0], raw[1]
        levels.append(ParamLevel(
            trip=_convert_param_value(trip, unit),
            trip_delay_s=0.0,
            recover=_convert_param_value(recover, unit),
            recover_delay_s=0.0,
        ))

    return ParamBlock(key=key, unit=unit, levels=levels)
