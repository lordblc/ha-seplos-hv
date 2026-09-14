"""Binary sensor platform for the Seplos HV BMS integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SeplosHvConfigEntry, SeplosHvCoordinator, SeplosHvData
from .entity import SeplosHvEntity


def _is_bcu_standby(data: SeplosHvData) -> bool:
    """True when the BCU reports system state 1 in status byte 36.

    Observed: byte 36 == 1 while the vendor UI showed "Standby" and the BCU
    advertised 0 A limits to the inverter; byte 36 == 2 once it advertised
    +/-32 A and the inverter charged/discharged normally. The four u32 values
    at summary offsets 50..65 stay 0 in both states, so they are NOT the
    limits and are not used here.
    """
    return data.status.byte36 == 1


def _is_cell_spread_warning(data: SeplosHvData) -> bool:
    """True when the pack spread has reached the L1 trip of cell_delta_charge."""
    block = data.params.get("cell_delta_charge")
    if block is None or not block.levels:
        return False
    return data.pack_spread_mv >= block.levels[0].trip


@dataclass(frozen=True, kw_only=True)
class SeplosHvBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes one Seplos HV binary sensor."""

    value_fn: Callable[[SeplosHvData], bool]


BINARY_SENSOR_DESCRIPTIONS: tuple[SeplosHvBinarySensorEntityDescription, ...] = (
    SeplosHvBinarySensorEntityDescription(
        key="charge_relay",
        translation_key="charge_relay",
        device_class=BinarySensorDeviceClass.POWER,
        value_fn=lambda d: d.status.charge_relay,
    ),
    SeplosHvBinarySensorEntityDescription(
        key="discharge_relay",
        translation_key="discharge_relay",
        device_class=BinarySensorDeviceClass.POWER,
        value_fn=lambda d: d.status.discharge_relay,
    ),
    SeplosHvBinarySensorEntityDescription(
        key="precharge_relay",
        translation_key="precharge_relay",
        value_fn=lambda d: d.status.precharge_relay,
    ),
    SeplosHvBinarySensorEntityDescription(
        key="negative_relay",
        translation_key="negative_relay",
        value_fn=lambda d: d.status.negative_relay,
    ),
    SeplosHvBinarySensorEntityDescription(
        key="heating_relay",
        translation_key="heating_relay",
        value_fn=lambda d: d.status.heating_relay,
    ),
    SeplosHvBinarySensorEntityDescription(
        key="current_limiting",
        translation_key="current_limiting",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: d.status.current_limiting,
    ),
    SeplosHvBinarySensorEntityDescription(
        key="bcu_standby",
        translation_key="bcu_standby",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_is_bcu_standby,
    ),
    SeplosHvBinarySensorEntityDescription(
        key="module_temp_sensors_missing",
        translation_key="module_temp_sensors_missing",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda d: bool(d.modules_with_no_temp_sensors),
    ),
    SeplosHvBinarySensorEntityDescription(
        key="cell_spread_warning",
        translation_key="cell_spread_warning",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_is_cell_spread_warning,
    ),
)


class SeplosHvBinarySensor(SeplosHvEntity, BinarySensorEntity):
    """A binary sensor backed by the shared coordinator data."""

    entity_description: SeplosHvBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: SeplosHvCoordinator,
        description: SeplosHvBinarySensorEntityDescription,
    ) -> None:
        """Set up a binary sensor from its description."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        """Return the derived boolean state from the latest coordinator data."""
        return self.entity_description.value_fn(self.coordinator.data)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SeplosHvConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Seplos HV BMS binary sensors from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        SeplosHvBinarySensor(coordinator, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )
