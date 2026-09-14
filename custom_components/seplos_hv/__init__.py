"""The Seplos HV BMS integration (read-only)."""

from __future__ import annotations

import re

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler

from .const import CONF_ENABLE_CELL_SENSORS, DEFAULT_ENABLE_CELL_SENSORS
from .coordinator import SeplosHvConfigEntry, SeplosHvCoordinator

# unique_id of a per-cell voltage sensor: "<serial>_m<module>_c<cell>"
_CELL_UNIQUE_ID_RE = re.compile(r"_m\d+_c\d+$")


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

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SeplosHvConfigEntry) -> bool:
    """Set up Seplos HV BMS from a config entry."""
    coordinator = SeplosHvCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    _async_apply_cell_sensor_option(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SeplosHvConfigEntry) -> bool:
    """Unload a Seplos HV BMS config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.close()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: SeplosHvConfigEntry) -> None:
    """Reload the entry when its options change (intervals, cell sensors)."""
    await hass.config_entries.async_reload(entry.entry_id)
