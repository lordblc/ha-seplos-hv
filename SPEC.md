# ha-seplos-hv — orchestration spec (read this first)

Goal: a **read-only** Home Assistant custom integration (`custom_components/seplos_hv`) that
polls a Seplos HV Master Control Box (BCU-1002C) over its proprietary RS485-1 protocol and
exposes everything decodable as entities. Transport #1 = TCP socket to a Waveshare
RS232/485/422 TO POE ETH (B) gateway in **transparent** mode (Protocol "None", TCP Server).
Transport #2 = local serial device (USB RS485 adapter) via `pyserial-asyncio-fast` (optional).

Target HA: **2026.9.1**. Python 3.13 in HA; local dev machine has Python 3.12 and **no pip**,
so the protocol library and its tests must use ONLY the standard library (`unittest`, `asyncio`).

Authoritative protocol reference: `docs/protocol-reference.md` (reverse-engineered, CRC-verified
against 5809 captured frames). Real captures: `tests/fixtures/bcu_frames_1.txt` and
`bcu_frames_2.txt` (vendor tool log; each line `【Data】<hex bytes>` is one frame, `↓` = host→BCU
request, `↑` = BCU→host reply).

## HARD SAFETY RULES (non-negotiable)
1. The integration NEVER writes to the BCU. Only the command ids listed below may ever be sent,
   and only with the 6-byte zero payload.
2. NEVER send an even `0x02xx` id. Never send `0x0009`, `0x000E`, `0x0011`, `0x0039` or any
   "seen once" ids from the reference either — only the whitelist below.
3. One outstanding request at a time on the link (asyncio.Lock). Replies carry no transaction id.

## Command whitelist (poll set)
| id | name | tier |
|---|---|---|
| 0x0001 | protocol/model string | startup |
| 0x0002 | firmware string | startup |
| 0x0005 | BMS serial | startup |
| 0x0007 | pack serial | startup |
| 0x000A | pack summary (82 B) | fast |
| 0x000B | status/relay word (38 B) | fast |
| 0x000C | cell voltages | medium |
| 0x000D | temperatures | medium |
| 0x0201,0x0203,0x0209,0x020B,0x020D,0x020F,0x0211,0x0213,0x0215,0x0217,0x021F,0x0221,0x0223,0x0225,0x0227,0x0229,0x022B,0x022D,0x0239,0x023D | protection parameters (24 B, 12×u16) | slow |

Default intervals: fast 15 s, medium 60 s, slow 3600 s (all configurable in options flow).

## Frame format
```
9A | src | dst | CMD u16 BE | 00 | LEN u16 BE | payload[LEN] | CRC u16 BE | 9D
```
host=0x01, BCU=0x21; replies swap src/dst. CRC-16/MODBUS (poly 0xA001 reflected, init 0xFFFF)
over frame[0 : 8+LEN] (SOF inclusive), stored BIG-endian. No byte stuffing — parse by LEN, never
by hunting for 0x9D. Request payload = 6 zero bytes, LEN=6.

## Module `custom_components/seplos_hv/protocol.py` — PUBLIC API CONTRACT
(pure stdlib, no HA imports; importable standalone as `protocol.py`)

```python
SOF, EOF = 0x9A, 0x9D
HOST_ADDR, BCU_ADDR = 0x01, 0x21
CMD_PROTOCOL, CMD_FIRMWARE, CMD_BMS_SERIAL, CMD_PACK_SERIAL = 0x0001, 0x0002, 0x0005, 0x0007
CMD_SUMMARY, CMD_STATUS, CMD_CELLS, CMD_TEMPS = 0x000A, 0x000B, 0x000C, 0x000D
PARAM_CMDS: dict[int, tuple[str, str]]   # id -> (key, unit) e.g. 0x0201 -> ("cell_over_voltage", "mV")
ALLOWED_CMDS: frozenset[int]             # the whitelist above; build_request raises ValueError otherwise

def crc16(data: bytes) -> int
def build_request(cmd: int) -> bytes                     # raises ValueError if cmd not in ALLOWED_CMDS
class FrameError(Exception)
@dataclass(frozen=True) class Frame: src:int; dst:int; cmd:int; payload:bytes
class FrameParser:
    """Incremental parser. feed(bytes) -> list[Frame]. Resyncs on garbage: discards bytes until a
    plausible 0x9A header whose LEN fits and CRC checks. Bounded buffer (drop if > 4096)."""
    def feed(self, data: bytes) -> list[Frame]
    def reset(self) -> None
def parse_frame(buf: bytes) -> Frame                    # strict single-frame parse, raises FrameError

def decode_string(payload) -> str                         # NUL-trimmed ASCII (0x0001/2/5/7)
@dataclass class PackSummary: (see offsets below) + .cell_spread_mv, .as_dict()
def decode_summary(payload) -> PackSummary               # raises FrameError if len < 82
@dataclass class Status: relay_word:int, current_limiting:bool, charge_relay:bool, discharge_relay:bool,
                          precharge_relay:bool, negative_relay:bool, heating_relay:bool, raw:bytes,
                          byte27:int, byte30:int, byte36:int
def decode_status(payload) -> Status
@dataclass class ModuleCells: module_id:int; cells_mv:list[int]; + min/max/spread/min_index/max_index props
def decode_cells(payload) -> list[ModuleCells]
@dataclass class Temperatures: bcu_c: list[float]; modules: list[ModuleTemps]
@dataclass class ModuleTemps: module_id:int; sensors_c:list[float]; extra_raw:list[int]
def decode_temps(payload) -> Temperatures
@dataclass class ParamBlock: key:str; unit:str; levels: list[ParamLevel]  # 3 levels
@dataclass class ParamLevel: trip:float; trip_delay_s:float; recover:float; recover_delay_s:float
def decode_params(cmd:int, payload) -> ParamBlock         # converts units: C from 0.1K, A signed, delays ×0.1 s
def cell_label(index:int) -> str   # "BMU1 C21" : module = index//32+1, cell = index%32+1
def temp_label(index:int) -> str   # "BMU4 T12" : module = index//64+1, sensor = index%64+1
def kelvin10_to_c(raw:int) -> float  # (raw-2731)/10
```

PackSummary offsets (u16/u32 BE unless noted): 0 v_pack 0.1V; 2 v_collect 0.1V; 4 v_load 0.1V;
6 i32 current 0.01A (scale unverified — keep a module constant CURRENT_SCALE=0.01); 12 u8 soc %;
16 u8 soh %; 18 u32 remaining_ah (×0.01); 30 u32 full_ah; 38 u32 design_ah; 22,26,34,42,46 u32
raw undecoded (expose as `raw_u32` dict keyed by offset); 50..65 four u32 `limits` (believed
charge/discharge current or power limits, all 0 while BCU is in Standby); 66/68 max cell mV +
index; 70/72 min cell mV + index; 74/76 max temp 0.1K + index; 78/80 min temp 0.1K + index.

Status: relay word = u32 BE at bytes 16..19 (bit0 current-limiting, 1 charge, 2 discharge,
3 precharge, 4 negative, 5 heating). Keep bytes 27/30/36 raw.

Cells payload: u16 module_count; per module u8 id, u8 cell_count, cell_count×u16 mV.
Temps payload: u8 module_count; u8 bcu_count; bcu_count×u16; per module u8 id, u8 sensor_count,
sensor_count×u16, u8 extra_count, extra_count×u16. Temps in 0.1 K.
Params payload: 12×u16 = L1 trip, L1 trip delay, L1 recover, L1 recover delay, L2…, L3… ;
delay ×100 ms; unit "C" → kelvin10_to_c; unit "A" → signed i16; else raw int.

## Module `custom_components/seplos_hv/client.py` — transport (stdlib asyncio only)
```python
class SeplosHvClient:
    def __init__(self, *, host: str|None=None, port: int=8899, serial_port: str|None=None,
                 baudrate: int=57600, timeout: float=1.5)
    async def connect(self) / async def close(self)
    async def request(self, cmd: int) -> Frame      # serialised by an asyncio.Lock; raises
                                                     # SeplosTimeout / SeplosConnectionError; auto-reconnect once
    async def read_identity(self) -> dict[str,str]   # protocol, firmware, bms_serial, pack_serial
    async def read_summary() -> PackSummary; read_status() -> Status; read_cells() -> list[ModuleCells]
    async def read_temps() -> Temperatures; async def read_params() -> dict[str, ParamBlock]
```
TCP: asyncio.open_connection. Serial: `serial_asyncio_fast.open_serial_connection` imported lazily
(inside the serial branch) so TCP mode works without pyserial. Before each request drain/discard
any stale bytes and reset the FrameParser. Inter-request gap ≥ 30 ms.

## `tools/mock_bcu.py` — replay server for tests without hardware
Stdlib asyncio TCP server (default port 8899) that answers each whitelisted request with the
matching reply frame taken from the fixtures (last seen reply per cmd), rebuilt with correct CRC.
Also usable with `--serial /dev/ptsX` later (optional). Unknown/forbidden cmds → log + no reply.

## `tools/bcu_cli.py` — bench CLI
`python3 tools/bcu_cli.py --host 192.168.157.155 --port 8899 [--serial /dev/ttyUSB0]` prints
identity, summary, status, cells (per module table), temps, and with `--params` the protection
table. `--json` dumps everything as one JSON document. `--watch N` loops. Uses client.py.

## Tests (`tests/`, stdlib unittest, run with `python3 -m unittest discover -s tests -v`)
- test_protocol.py: every ↑ frame in both fixtures parses (CRC ok); every ↓ frame equals
  build_request(cmd) for whitelisted cmds; decode_summary on the fixture 0x000A reply gives
  v_pack=422.2, soc=50, soh=100, remaining=299.97 (approx), full=600.0, design=600.0, max cell
  3416 @ "BMU1 C21", min cell 3187 @ "BMU1 C19", max temp ≈19.8 °C; decode_string(0x0005) ==
  "304431553990004P"; firmware string starts "HVP-B1018"; decode_cells returns 4 modules × 32
  cells; decode_temps shows BMU1 sensor_count 0; FrameParser handles split/concatenated/garbage
  input; build_request(0x0202) raises ValueError.
- test_client.py: spins up mock_bcu on an ephemeral port, runs the client end-to-end.

## HA integration (`custom_components/seplos_hv/`) — see SPEC-HA.md
