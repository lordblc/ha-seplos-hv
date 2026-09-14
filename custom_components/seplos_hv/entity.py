"""Common entity base for the Seplos HV BMS integration.

All entities for a config entry share one device: the BCU-1002C master
control box. Per-module, per-cell and per-temp-sensor entities also belong
to this single device (the BCU is the thing HA talks to; the modules are not
separately addressable), per SPEC-HA.md.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, NAME
from .coordinator import SeplosHvCoordinator


def build_device_info(coordinator: SeplosHvCoordinator) -> DeviceInfo:
    """Build the single device that every entity of this entry belongs to."""
    entry = coordinator.config_entry
    identity = coordinator.data.identity if coordinator.data else {}
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer=MANUFACTURER,
        name=NAME,
        model=identity.get("protocol") or None,
        sw_version=identity.get("firmware") or None,
        serial_number=identity.get("bms_serial") or None,
    )


class SeplosHvEntity(CoordinatorEntity[SeplosHvCoordinator]):
    """Base class for all Seplos HV BMS entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SeplosHvCoordinator, unique_id_suffix: str) -> None:
        """Set up the shared unique id prefix and device info."""
        super().__init__(coordinator)
        serial = coordinator.config_entry.unique_id or coordinator.config_entry.entry_id
        self._attr_unique_id = f"{serial}_{unique_id_suffix}"
        self._attr_device_info = build_device_info(coordinator)
