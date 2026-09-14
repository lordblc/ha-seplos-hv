"""The Seplos HV BMS integration (read-only)."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import SeplosHvConfigEntry, SeplosHvCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SeplosHvConfigEntry) -> bool:
    """Set up Seplos HV BMS from a config entry."""
    coordinator = SeplosHvCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
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
