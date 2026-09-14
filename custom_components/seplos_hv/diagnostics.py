"""Diagnostics support for the Seplos HV BMS integration.

Nothing is redacted: the integration is read-only and stores no secrets (a
host/IP or serial device path is not considered sensitive here, matching
other local-polling integrations).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import SeplosHvConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SeplosHvConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data

    diagnostics: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "last_update_success": coordinator.last_update_success,
    }

    if data is None:
        diagnostics["data"] = None
        return diagnostics

    diagnostics["data"] = {
        "identity": data.identity,
        "summary": data.summary.as_dict(),
        "status": {
            "relay_word": f"0x{data.status.relay_word:08X}",
            "current_limiting": data.status.current_limiting,
            "charge_relay": data.status.charge_relay,
            "discharge_relay": data.status.discharge_relay,
            "precharge_relay": data.status.precharge_relay,
            "negative_relay": data.status.negative_relay,
            "heating_relay": data.status.heating_relay,
            "byte27": data.status.byte27,
            "byte30": data.status.byte30,
            "byte36": data.status.byte36,
            # The raw status frame payload, as captured by the client. There
            # is no equivalent raw capture for the summary frame: the client
            # contract only exposes it pre-decoded (see PackSummary.as_dict
            # above for its full decoded content instead).
            "raw_hex": data.status.raw.hex(),
        },
        "cells": [
            {"module_id": module.module_id, "cells_mv": module.cells_mv}
            for module in data.cells
        ],
        "temps": {
            "bcu_c": data.temps.bcu_c,
            "modules": [
                {
                    "module_id": module.module_id,
                    "sensors_c": module.sensors_c,
                    "extra_raw": module.extra_raw,
                }
                for module in data.temps.modules
            ],
        },
        "params": {
            key: {
                "unit": block.unit,
                "levels": [asdict(level) for level in block.levels],
            }
            for key, block in data.params.items()
        },
        "derived": {
            "module_ids": data.module_ids,
            "cells_online": data.cells_online,
            "pack_spread_mv": data.pack_spread_mv,
            "modules_with_no_temp_sensors": data.modules_with_no_temp_sensors,
            "headroom_spread_to_l1_mv": data.headroom_spread_to_l1_mv,
            "headroom_min_temp_to_charge_inhibit_c": (
                data.headroom_min_temp_to_charge_inhibit_c
            ),
            "last_medium_update": (
                data.last_medium_update.isoformat() if data.last_medium_update else None
            ),
            "last_slow_update": (
                data.last_slow_update.isoformat() if data.last_slow_update else None
            ),
        },
    }
    return diagnostics
