"""Constants for the Seplos HV BMS integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "seplos_hv"
NAME: Final = "Seplos HV BMS"
MANUFACTURER: Final = "Seplos"

# Config entry data keys.
CONF_TRANSPORT: Final = "transport"
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_SERIAL_PORT: Final = "serial_port"
CONF_BAUDRATE: Final = "baudrate"

TRANSPORT_TCP: Final = "tcp"
TRANSPORT_SERIAL: Final = "serial"

# Options keys.
CONF_FAST_INTERVAL: Final = "fast_interval"
CONF_MEDIUM_INTERVAL: Final = "medium_interval"
CONF_SLOW_INTERVAL: Final = "slow_interval"
CONF_ENABLE_CELL_SENSORS: Final = "enable_cell_sensors"
# Guarded parameter-write support (v0.4.3). Off by default: with this off, no number
# entities are created and the seplos_hv.write_param service refuses every call.
CONF_ENABLE_WRITES: Final = "enable_writes"

# Defaults (seconds unless noted).
DEFAULT_PORT: Final = 8899
DEFAULT_BAUDRATE: Final = 57600
DEFAULT_FAST_INTERVAL: Final = 15
DEFAULT_MEDIUM_INTERVAL: Final = 60
DEFAULT_SLOW_INTERVAL: Final = 3600
DEFAULT_ENABLE_CELL_SENSORS: Final = True
DEFAULT_ENABLE_WRITES: Final = False

# Interval bounds enforced by the options flow.
MIN_FAST_INTERVAL: Final = 5
MAX_FAST_INTERVAL: Final = 300
MIN_MEDIUM_INTERVAL: Final = 15
MAX_MEDIUM_INTERVAL: Final = 900
MIN_SLOW_INTERVAL: Final = 300
MAX_SLOW_INTERVAL: Final = 86400

# Transport-level request timeout, in seconds. Not user-configurable: the fast
# interval's lower bound (5 s) already gives plenty of margin over this.
REQUEST_TIMEOUT: Final = 1.5

# Guarded parameter-write support (v0.4.3).
SERVICE_WRITE_PARAM: Final = "write_param"
EVENT_WRITE: Final = "seplos_hv_write"
STEP_GUARD_FRACTION: Final = 0.20

# Integration version (keep in sync with manifest.json).
VERSION: Final = "0.4.3"
