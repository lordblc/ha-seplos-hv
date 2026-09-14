# HA integration spec — `custom_components/seplos_hv`  (HA 2026.9.1)

Model after the author's existing integration style (config-entry, DataUpdateCoordinator,
`entry.runtime_data`, translations en + nb). Domain `seplos_hv`, name "Seplos HV BMS",
iot_class `local_polling`, integration_type `device`, version `0.1.0`, codeowners `@lordblc`,
requirements: `["pyserial-asyncio-fast>=0.16"]` (HA core already ships it — it is used only for the
serial transport; TCP mode must work even if the import fails).

## Files
- `manifest.json`, `const.py`, `__init__.py`, `config_flow.py`, `coordinator.py`,
  `entity.py`, `sensor.py`, `binary_sensor.py`, `diagnostics.py`, `strings.json`,
  `translations/en.json`, `translations/nb.json`, `icons.json`, `protocol.py`, `client.py`
  (the last two per SPEC.md — same files, do not duplicate logic).
- Repo root: `hacs.json` (`{"name":"Seplos HV BMS","content_in_root":false,"render_readme":true,"homeassistant":"2025.6.0"}`), `README.md`, `LICENSE` (MIT, lordblc).

## Config flow
Step `user`: choose transport — `tcp` (host, port default 8899) or `serial` (device path,
baudrate default 57600). On submit: connect, `read_identity()`; use `bms_serial` as unique_id
(fallback host:port); abort if already configured; show errors `cannot_connect`, `timeout`.
Title: "Seplos HV BMS <serial>". Options flow: `fast_interval` (5–300 s, default 15),
`medium_interval` (15–900, default 60), `slow_interval` (300–86400, default 3600),
`enable_cell_sensors` (bool, default false → per-cell sensors created but disabled by default via
`entity_registry_enabled_default`; true → enabled). Reload entry on options change.

## Coordinator
One `DataUpdateCoordinator` at the fast interval. Each refresh: read summary + status always;
read cells + temps when `now - last_medium >= medium_interval`; read params when
`now - last_slow >= slow_interval` (and once at startup, together with identity). Keep the last
good medium/slow data in `coordinator.data` between refreshes. All requests go through the single
client lock. On `SeplosConnectionError`/`SeplosTimeout` → `UpdateFailed` (entities become
unavailable after HA's normal grace). Reconnect on next refresh. Log at debug, warn once on
first failure, info on recovery.

Data object (`SeplosHvData` dataclass): identity dict, summary: PackSummary, status: Status,
cells: list[ModuleCells], temps: Temperatures, params: dict[str, ParamBlock], plus derived:
`pack_spread_mv`, per-module spread, `module_ids`, `modules_with_no_temp_sensors: list[int]`,
`cells_online: int`, `headroom_spread_to_l1_mv` (L1 trip of cell_delta_charge − pack spread),
`headroom_min_temp_to_charge_inhibit_c` (min temp − L3 trip of charge_low_temp),
`last_medium_update`, `last_slow_update` (datetime).

## Device
One device per config entry: manufacturer "Seplos", model = protocol string, sw_version =
firmware string, serial_number = bms serial, name "Seplos HV BMS". Per-module entities belong
to the same device (keep it simple; use `translation_key` + `translation_placeholders` for
`{module}` / `{cell}`).

## Entities (all read-only). Use EntityDescription dataclasses with a `value_fn`.
### sensor (pack)
pack_voltage (V, voltage, measurement), collect_voltage, load_voltage (diagnostic, disabled by
default), current (A, current, measurement), power (W = v_pack × current, measurement),
soc (%, battery), soh (%, diagnostic), remaining_capacity (Ah), full_capacity (Ah, diagnostic),
design_capacity (Ah, diagnostic), remaining_energy (kWh = remaining_ah × v_pack/1000,
energy_storage), cell_max_voltage (mV), cell_max_label (text, e.g. "BMU1 C21"), cell_min_voltage,
cell_min_label, cell_spread (mV — the headline health number), temp_max (°C), temp_max_label,
temp_min, temp_min_label, limit_1..limit_4 (raw u32, diagnostic, disabled by default — believed
charge/discharge limits), relay_word (diagnostic hex), status_byte27/30/36 (diagnostic, disabled),
protocol / firmware / bms_serial (diagnostic text), modules_online (count), cells_online,
modules_without_temp_sensors (count; attributes: list), headroom_spread_to_l1 (mV),
headroom_min_temp_to_charge_inhibit (°C), last_medium_update / last_slow_update (timestamp,
diagnostic), bcu_temperature_N for each BCU sensor.
### sensor (per module, N = 1..module_count from live data)
module_N_min_voltage, _max_voltage, _spread, _min_cell (label), _max_cell, _sum_voltage (V),
_cell_count (diagnostic), _temp_sensor_count (diagnostic), _temp_min, _temp_max, _temp_avg.
### sensor (per cell, 128) — `entity_registry_enabled_default` follows option
module_N_cell_M_voltage (mV). Unique id `f"{serial}_m{N}_c{M}"`.
### sensor (per module temp sensor) — disabled by default
module_N_temp_S (°C).
### sensor (protection params) — diagnostic, disabled by default
For each ParamBlock: `<key>_l1_trip`, `<key>_l1_recover`, `<key>_l2_trip`, … `_l3_recover`
(unit per block), plus attributes with delays.
### binary_sensor
charge_relay, discharge_relay, precharge_relay, negative_relay, heating_relay
(device_class power/none), current_limiting (problem), bcu_standby (problem — true when all four
`limits` are 0 while relays charge+discharge are closed), module_temp_sensors_missing (problem),
cell_spread_warning (problem — true when pack spread ≥ L1 trip of cell_delta_charge).

## Diagnostics
`async_get_config_entry_diagnostics` returns the full data as dict + raw payload hex of the
last summary/status frames. Redact nothing (no secrets exist).

## Translations
`strings.json` + `translations/en.json` identical; `translations/nb.json` in Norwegian bokmål
(the author's HA is Norwegian). Entity names: "Pakkespenning", "Cellespredning", etc.

## Quality bar
- `python3 -m py_compile` clean; no HA imports in protocol.py/client.py.
- Async only; never block the event loop.
- Follow HA 2026 conventions: `ConfigEntry` typed alias with `runtime_data`, `CoordinatorEntity`,
  `EntityCategory.DIAGNOSTIC`, `SensorDeviceClass`, `SensorStateClass`, `has_entity_name=True`.
