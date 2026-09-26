"""The Seplos HV BMS integration.

Read-only by default. Since v0.4.0, an ``enable_writes`` option (off by default) gates a
guarded, dry-run-first parameter-write path - see writes.py, client.py's write_params()
and coordinator.py's async_write_param() for the actual write plumbing; this module only
wires up the ``write_param`` service on top of it.
"""

from __future__ import annotations

import re

import voluptuous as vol

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler

from .const import (
    CONF_ENABLE_CELL_SENSORS,
    CONF_ENABLE_WRITES,
    DEFAULT_ENABLE_CELL_SENSORS,
    DEFAULT_ENABLE_WRITES,
    DOMAIN,
    SERVICE_WRITE_PARAM,
)
from .coordinator import SeplosHvConfigEntry, SeplosHvCoordinator
from .writes import ParamValidationError

# unique_id of a per-cell voltage sensor: "<serial>_m<module>_c<cell>"
_CELL_UNIQUE_ID_RE = re.compile(r"_m\d+_c\d+$")

_WRITE_PARAM_SCHEMA = vol.Schema(
    {
        vol.Required("param"): cv.string,
        vol.Required("level"): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
        vol.Required("field"): vol.In(["trip", "recover", "trip_delay", "recover_delay"]),
        vol.Required("value"): vol.Coerce(float),
        vol.Optional("dry_run", default=True): cv.boolean,
        vol.Optional("force", default=False): cv.boolean,
    }
)


@callback
def _async_apply_cell_sensor_option(hass: HomeAssistant, entry: SeplosHvConfigEntry) -> None:
    """Honour ``enable_cell_sensors`` for cell entities that already exist.

    ``entity_registry_enabled_default`` only applies when an entity is first
    registered. If the option is on, clear the integration-set disabled flag on
    any per-cell sensor so it is added enabled on this setup. Entities the user
    disabled themselves are left alone.
    """
    if not entry.options.get(CONF_ENABLE_CELL_SENSORS, DEFAULT_ENABLE_CELL_SENSORS):
        return
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            reg_entry.disabled_by is RegistryEntryDisabler.INTEGRATION
            and _CELL_UNIQUE_ID_RE.search(reg_entry.unique_id or "")
        ):
            registry.async_update_entity(reg_entry.entity_id, disabled_by=None)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.NUMBER]


async def async_setup_entry(hass: HomeAssistant, entry: SeplosHvConfigEntry) -> bool:
    """Set up Seplos HV BMS from a config entry."""
    coordinator = SeplosHvCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    _async_apply_cell_sensor_option(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    domain_entries = hass.data.setdefault(DOMAIN, {})
    domain_entries[entry.entry_id] = coordinator
    if not hass.services.has_service(DOMAIN, SERVICE_WRITE_PARAM):
        _async_register_write_param_service(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SeplosHvConfigEntry) -> bool:
    """Unload a Seplos HV BMS config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.close()
        domain_entries = hass.data.get(DOMAIN, {})
        domain_entries.pop(entry.entry_id, None)
        if not domain_entries and hass.services.has_service(DOMAIN, SERVICE_WRITE_PARAM):
            hass.services.async_remove(DOMAIN, SERVICE_WRITE_PARAM)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: SeplosHvConfigEntry) -> None:
    """Reload the entry when its options change (intervals, cell sensors, enable_writes)."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_pick_coordinator(hass: HomeAssistant) -> SeplosHvCoordinator:
    """Pick the coordinator a write_param call applies to.

    This integration has no per-call device/entity target selector (see services.yaml) -
    with exactly one loaded config entry (the common case: one BCU) that entry is used
    unconditionally. With more than one, there is no way to disambiguate, so the call is
    refused rather than guessing which battery to write to.
    """
    domain_entries: dict[str, SeplosHvCoordinator] = hass.data.get(DOMAIN, {})
    if not domain_entries:
        raise ServiceValidationError("No Seplos HV BMS config entry is loaded")
    if len(domain_entries) > 1:
        raise ServiceValidationError(
            "More than one Seplos HV BMS config entry is loaded; "
            f"the {SERVICE_WRITE_PARAM} service cannot pick one automatically"
        )
    return next(iter(domain_entries.values()))


@callback
def _async_register_write_param_service(hass: HomeAssistant) -> None:
    """Register the seplos_hv.write_param service (once, shared by every config entry)."""

    async def _async_write_param(call: ServiceCall) -> ServiceResponse:
        coordinator = _async_pick_coordinator(hass)
        if not coordinator.writes_enabled:
            raise ServiceValidationError(
                "Parameter writes are disabled for this Seplos HV BMS entry - "
                "turn on 'Enable parameter writes' in the integration's options first"
            )
        try:
            result = await coordinator.async_write_param(
                call.data["param"],
                call.data["level"] - 1,
                call.data["field"],
                call.data["value"],
                dry_run=call.data["dry_run"],
                force=call.data["force"],
            )
        except (KeyError, ValueError, ParamValidationError) as err:
            raise ServiceValidationError(str(err)) from err

        return {
            "frame_hex": result.frame_hex,
            "sent": result.sent,
            "verified": result.verified,
            "reply_cmd": f"0x{result.reply.cmd:04X}" if result.reply is not None else None,
            "reply_payload_hex": (
                result.reply.payload.hex(" ").upper() if result.reply is not None else None
            ),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_PARAM,
        _async_write_param,
        schema=_WRITE_PARAM_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
