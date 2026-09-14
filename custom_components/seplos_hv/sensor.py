"""Sensor platform for the Seplos HV BMS integration.

Pack-level sensors are a static tuple of descriptions. Per-module, per-cell,
per-module-temp-sensor and per-protection-parameter sensors are dynamic:
their count comes from the first successful coordinator data at setup time
(SPEC-HA.md), never hard-coded.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import CONF_ENABLE_CELL_SENSORS, DEFAULT_ENABLE_CELL_SENSORS
from .coordinator import SeplosHvConfigEntry, SeplosHvCoordinator, SeplosHvData
from .entity import SeplosHvEntity
from .protocol import BATT_STATUS, SYS_STATUS, ModuleCells, ModuleTemps, ParamLevel

# Ampere-hour has no dedicated HA unit constant; used as a plain string.
UNIT_AH = "Ah"


def _module_cells(data: SeplosHvData, module_id: int) -> ModuleCells | None:
    """Find one module's cell-voltage block by module id."""
    for module in data.cells:
        if module.module_id == module_id:
            return module
    return None


def _module_temps(data: SeplosHvData, module_id: int) -> ModuleTemps | None:
    """Find one module's temperature block by module id."""
    for module in data.temps.modules:
        if module.module_id == module_id:
            return module
    return None


def _module_cell_label(module_id: int, local_index: int) -> str:
    """Format a per-module cell label, e.g. 'BMU3 C7' (local_index is 0-based)."""
    return f"BMU{module_id} C{local_index + 1}"


def _humanize(key: str) -> str:
    """Turn a snake_case protocol key into a readable label for a placeholder."""
    return key.replace("_", " ")


# -- pack-level sensors ------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SeplosHvSensorEntityDescription(SensorEntityDescription):
    """Describes one pack-level Seplos HV sensor."""

    value_fn: Callable[[SeplosHvData], StateType | datetime]
    attributes_fn: Callable[[SeplosHvData], dict[str, Any]] | None = None


PACK_SENSOR_DESCRIPTIONS: tuple[SeplosHvSensorEntityDescription, ...] = (
    SeplosHvSensorEntityDescription(
        key="pack_voltage",
        translation_key="pack_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.summary.v_pack,
    ),
    SeplosHvSensorEntityDescription(
        key="collect_voltage",
        translation_key="collect_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.summary.v_collect,
    ),
    SeplosHvSensorEntityDescription(
        key="load_voltage",
        translation_key="load_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.summary.v_load,
    ),
    SeplosHvSensorEntityDescription(
        key="current",
        translation_key="current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.summary.current,
    ),
    SeplosHvSensorEntityDescription(
        key="power",
        translation_key="power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.summary.v_pack * d.summary.current,
    ),
    SeplosHvSensorEntityDescription(
        key="soc",
        translation_key="soc",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.summary.soc,
    ),
    SeplosHvSensorEntityDescription(
        key="soc_coulomb",
        translation_key="soc_coulomb",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.summary.soc_coulomb,
    ),
    SeplosHvSensorEntityDescription(
        key="soc_usable",
        translation_key="soc_usable",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.summary.soc_usable,
    ),
    SeplosHvSensorEntityDescription(
        key="soh",
        translation_key="soh",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.summary.soh,
    ),
    SeplosHvSensorEntityDescription(
        key="remaining_capacity",
        translation_key="remaining_capacity",
        native_unit_of_measurement=UNIT_AH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.summary.remaining_ah,
    ),
    SeplosHvSensorEntityDescription(
        key="remaining_capacity_reported",
        translation_key="remaining_capacity_reported",
        native_unit_of_measurement=UNIT_AH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.summary.remaining_reported_ah,
    ),
    SeplosHvSensorEntityDescription(
        key="usable_remaining_capacity",
        translation_key="usable_remaining_capacity",
        native_unit_of_measurement=UNIT_AH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.summary.usable_remaining_ah,
    ),
    SeplosHvSensorEntityDescription(
        key="usable_full_capacity",
        translation_key="usable_full_capacity",
        native_unit_of_measurement=UNIT_AH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.summary.usable_full_ah,
    ),
    SeplosHvSensorEntityDescription(
        key="full_capacity",
        translation_key="full_capacity",
        native_unit_of_measurement=UNIT_AH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.summary.full_ah,
    ),
    SeplosHvSensorEntityDescription(
        key="design_capacity",
        translation_key="design_capacity",
        native_unit_of_measurement=UNIT_AH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.summary.design_ah,
    ),
    SeplosHvSensorEntityDescription(
        key="remaining_energy",
        translation_key="remaining_energy",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.summary.remaining_reported_ah * d.summary.v_pack / 1000,
    ),
    SeplosHvSensorEntityDescription(
        key="cell_max_voltage",
        translation_key="cell_max_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.summary.cell_max_mv,
    ),
    SeplosHvSensorEntityDescription(
        key="cell_max_label",
        translation_key="cell_max_label",
        value_fn=lambda d: d.summary.cell_max_label,
    ),
    SeplosHvSensorEntityDescription(
        key="cell_min_voltage",
        translation_key="cell_min_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.summary.cell_min_mv,
    ),
    SeplosHvSensorEntityDescription(
        key="cell_min_label",
        translation_key="cell_min_label",
        value_fn=lambda d: d.summary.cell_min_label,
    ),
    SeplosHvSensorEntityDescription(
        key="cell_spread",
        translation_key="cell_spread",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.pack_spread_mv,
    ),
    SeplosHvSensorEntityDescription(
        key="temp_max",
        translation_key="temp_max",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.summary.max_temp_c,
    ),
    SeplosHvSensorEntityDescription(
        key="temp_max_label",
        translation_key="temp_max_label",
        value_fn=lambda d: d.summary.max_temp_label,
    ),
    SeplosHvSensorEntityDescription(
        key="temp_min",
        translation_key="temp_min",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.summary.min_temp_c,
    ),
    SeplosHvSensorEntityDescription(
        key="temp_min_label",
        translation_key="temp_min_label",
        value_fn=lambda d: d.summary.min_temp_label,
    ),
    SeplosHvSensorEntityDescription(
        key="system_state",
        translation_key="system_state",
        device_class=SensorDeviceClass.ENUM,
        options=[*SYS_STATUS.values(), "unknown"],
        value_fn=lambda d: d.status.system_state,
        attributes_fn=lambda d: {"code": d.status.sys_status},
    ),
    SeplosHvSensorEntityDescription(
        key="battery_mode",
        translation_key="battery_mode",
        device_class=SensorDeviceClass.ENUM,
        options=[*BATT_STATUS.values(), "unknown"],
        value_fn=lambda d: d.status.battery_mode,
        attributes_fn=lambda d: {"code": d.status.batt_status},
    ),
    SeplosHvSensorEntityDescription(
        key="status_summary",
        translation_key="status_summary",
        value_fn=lambda d: d.status.active_summary[:255],
        attributes_fn=lambda d: {
            "protect_l1": d.status.protect_l1_names,
            "protect_l2": d.status.protect_l2_names,
            "protect_l3": d.status.protect_l3_names,
            "fault": d.status.fault_names,
        },
    ),
    SeplosHvSensorEntityDescription(
        key="protection_l1",
        translation_key="protection_l1",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (", ".join(d.status.protect_l1_names) or "none")[:255],
        attributes_fn=lambda d: {"raw": f"0x{d.status.protect_l1:08X}"},
    ),
    SeplosHvSensorEntityDescription(
        key="protection_l2",
        translation_key="protection_l2",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (", ".join(d.status.protect_l2_names) or "none")[:255],
        attributes_fn=lambda d: {"raw": f"0x{d.status.protect_l2:08X}"},
    ),
    SeplosHvSensorEntityDescription(
        key="protection_l3",
        translation_key="protection_l3",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (", ".join(d.status.protect_l3_names) or "none")[:255],
        attributes_fn=lambda d: {"raw": f"0x{d.status.protect_l3:08X}"},
    ),
    SeplosHvSensorEntityDescription(
        key="fault_status",
        translation_key="fault_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (", ".join(d.status.fault_names) or "none")[:255],
        attributes_fn=lambda d: {"raw": f"0x{d.status.fault:08X}"},
    ),
    SeplosHvSensorEntityDescription(
        key="sensor_status_word",
        translation_key="sensor_status_word",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: f"0x{d.status.sensor_status:08X}",
    ),
    SeplosHvSensorEntityDescription(
        key="relay_word",
        translation_key="relay_word",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: f"0x{d.status.relay_word:08X}",
    ),
    SeplosHvSensorEntityDescription(
        key="status_byte27",
        translation_key="status_byte27",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.status.byte27,
    ),
    SeplosHvSensorEntityDescription(
        key="status_byte30",
        translation_key="status_byte30",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.status.byte30,
    ),
    SeplosHvSensorEntityDescription(
        key="status_byte36",
        translation_key="status_byte36",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.status.byte36,
    ),
    SeplosHvSensorEntityDescription(
        key="protocol",
        translation_key="protocol",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.identity.get("protocol"),
    ),
    SeplosHvSensorEntityDescription(
        key="firmware",
        translation_key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.identity.get("firmware"),
    ),
    SeplosHvSensorEntityDescription(
        key="bms_serial",
        translation_key="bms_serial",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.identity.get("bms_serial"),
    ),
    SeplosHvSensorEntityDescription(
        key="modules_online",
        translation_key="modules_online",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(d.module_ids),
    ),
    SeplosHvSensorEntityDescription(
        key="cells_online",
        translation_key="cells_online",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.cells_online,
    ),
    SeplosHvSensorEntityDescription(
        key="modules_without_temp_sensors",
        translation_key="modules_without_temp_sensors",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(d.modules_with_no_temp_sensors),
        attributes_fn=lambda d: {"modules": d.modules_with_no_temp_sensors},
    ),
    SeplosHvSensorEntityDescription(
        key="headroom_spread_to_l1",
        translation_key="headroom_spread_to_l1",
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.headroom_spread_to_l1_mv,
    ),
    SeplosHvSensorEntityDescription(
        key="headroom_min_temp_to_charge_inhibit",
        translation_key="headroom_min_temp_to_charge_inhibit",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.headroom_min_temp_to_charge_inhibit_c,
    ),
    SeplosHvSensorEntityDescription(
        key="last_medium_update",
        translation_key="last_medium_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.last_medium_update,
    ),
    SeplosHvSensorEntityDescription(
        key="last_slow_update",
        translation_key="last_slow_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.last_slow_update,
    ),
) + tuple(
    SeplosHvSensorEntityDescription(
        key=f"limit_{i}",
        translation_key="limit",
        translation_placeholders={"index": str(i)},
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=(lambda idx: (lambda d: d.summary.limits[idx]))(i - 1),
    )
    for i in range(1, 5)
)


class SeplosHvSensor(SeplosHvEntity, SensorEntity):
    """A pack-level sensor backed by the shared coordinator data."""

    entity_description: SeplosHvSensorEntityDescription

    def __init__(
        self,
        coordinator: SeplosHvCoordinator,
        description: SeplosHvSensorEntityDescription,
    ) -> None:
        """Set up a pack-level sensor from its description."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | datetime:
        """Return the derived value from the latest coordinator data."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes when the description defines them."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator.data)


# -- per-module sensors -------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SeplosHvModuleSensorEntityDescription(SensorEntityDescription):
    """Describes one per-module Seplos HV sensor."""

    value_fn: Callable[[SeplosHvData, int], StateType]


MODULE_SENSOR_DESCRIPTIONS: tuple[SeplosHvModuleSensorEntityDescription, ...] = (
    SeplosHvModuleSensorEntityDescription(
        key="min_voltage",
        translation_key="module_min_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, mid: m.min_mv if (m := _module_cells(d, mid)) else None,
    ),
    SeplosHvModuleSensorEntityDescription(
        key="max_voltage",
        translation_key="module_max_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, mid: m.max_mv if (m := _module_cells(d, mid)) else None,
    ),
    SeplosHvModuleSensorEntityDescription(
        key="spread",
        translation_key="module_spread",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, mid: m.spread_mv if (m := _module_cells(d, mid)) else None,
    ),
    SeplosHvModuleSensorEntityDescription(
        key="min_cell",
        translation_key="module_min_cell",
        value_fn=lambda d, mid: (
            _module_cell_label(mid, m.min_index) if (m := _module_cells(d, mid)) else None
        ),
    ),
    SeplosHvModuleSensorEntityDescription(
        key="max_cell",
        translation_key="module_max_cell",
        value_fn=lambda d, mid: (
            _module_cell_label(mid, m.max_index) if (m := _module_cells(d, mid)) else None
        ),
    ),
    SeplosHvModuleSensorEntityDescription(
        key="sum_voltage",
        translation_key="module_sum_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d, mid: (
            sum(m.cells_mv) / 1000 if (m := _module_cells(d, mid)) else None
        ),
    ),
    SeplosHvModuleSensorEntityDescription(
        key="cell_count",
        translation_key="module_cell_count",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, mid: (
            len(m.cells_mv) if (m := _module_cells(d, mid)) else None
        ),
    ),
    SeplosHvModuleSensorEntityDescription(
        key="temp_sensor_count",
        translation_key="module_temp_sensor_count",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, mid: (
            len(t.sensors_c) if (t := _module_temps(d, mid)) else None
        ),
    ),
    SeplosHvModuleSensorEntityDescription(
        key="temp_min",
        translation_key="module_temp_min",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d, mid: (
            min(t.sensors_c) if (t := _module_temps(d, mid)) and t.sensors_c else None
        ),
    ),
    SeplosHvModuleSensorEntityDescription(
        key="temp_max",
        translation_key="module_temp_max",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d, mid: (
            max(t.sensors_c) if (t := _module_temps(d, mid)) and t.sensors_c else None
        ),
    ),
    SeplosHvModuleSensorEntityDescription(
        key="temp_avg",
        translation_key="module_temp_avg",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d, mid: (
            sum(t.sensors_c) / len(t.sensors_c)
            if (t := _module_temps(d, mid)) and t.sensors_c
            else None
        ),
    ),
)


class SeplosHvModuleSensor(SeplosHvEntity, SensorEntity):
    """A per-module sensor (voltage/spread/temperature summaries)."""

    entity_description: SeplosHvModuleSensorEntityDescription

    def __init__(
        self,
        coordinator: SeplosHvCoordinator,
        description: SeplosHvModuleSensorEntityDescription,
        module_id: int,
    ) -> None:
        """Set up a per-module sensor for a specific module id."""
        super().__init__(coordinator, f"m{module_id}_{description.key}")
        self.entity_description = description
        self._module_id = module_id
        self._attr_translation_placeholders = {"module": str(module_id)}

    @property
    def native_value(self) -> StateType:
        """Return the derived value for this module from the latest data."""
        return self.entity_description.value_fn(self.coordinator.data, self._module_id)


# -- per-cell sensors ----------------------------------------------------------


class SeplosHvCellSensor(SeplosHvEntity, SensorEntity):
    """A single cell's voltage.

    Disabled by default unless the ``enable_cell_sensors`` option is set
    (there can be well over a hundred of these).
    """

    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.MILLIVOLT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "cell_voltage"

    def __init__(
        self,
        coordinator: SeplosHvCoordinator,
        module_id: int,
        cell_index: int,
        *,
        enabled_default: bool,
    ) -> None:
        """Set up a single cell-voltage sensor. cell_index is 0-based."""
        # Unique id intentionally omits a "_voltage" suffix per SPEC-HA.md.
        super().__init__(coordinator, f"m{module_id}_c{cell_index + 1}")
        self._module_id = module_id
        self._cell_index = cell_index
        self._attr_translation_placeholders = {
            "module": str(module_id),
            "cell": str(cell_index + 1),
        }
        self._attr_entity_registry_enabled_default = enabled_default

    @property
    def native_value(self) -> StateType:
        """Return this cell's voltage in mV, or None if it dropped out."""
        module = _module_cells(self.coordinator.data, self._module_id)
        if module is None or self._cell_index >= len(module.cells_mv):
            return None
        return module.cells_mv[self._cell_index]


# -- per-module temperature-sensor entities ------------------------------------


class SeplosHvModuleTempSensor(SeplosHvEntity, SensorEntity):
    """A single module's individual temperature-sensor reading. Disabled by default."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_translation_key = "module_temp_sensor"
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: SeplosHvCoordinator, module_id: int, sensor_index: int
    ) -> None:
        """Set up a single module temperature-sensor entity. sensor_index is 0-based."""
        super().__init__(coordinator, f"m{module_id}_t{sensor_index + 1}")
        self._module_id = module_id
        self._sensor_index = sensor_index
        self._attr_translation_placeholders = {
            "module": str(module_id),
            "sensor": str(sensor_index + 1),
        }

    @property
    def native_value(self) -> StateType:
        """Return this sensor's temperature in °C, or None if it dropped out."""
        module = _module_temps(self.coordinator.data, self._module_id)
        if module is None or self._sensor_index >= len(module.sensors_c):
            return None
        return module.sensors_c[self._sensor_index]


# -- BCU onboard temperature sensors -------------------------------------------


class SeplosHvBcuTempSensor(SeplosHvEntity, SensorEntity):
    """One of the BCU's own onboard temperature sensors."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_translation_key = "bcu_temperature"

    def __init__(self, coordinator: SeplosHvCoordinator, index: int) -> None:
        """Set up one BCU onboard temperature sensor. index is 0-based."""
        super().__init__(coordinator, f"bcu_t{index + 1}")
        self._index = index
        self._attr_translation_placeholders = {"sensor": str(index + 1)}

    @property
    def native_value(self) -> StateType:
        """Return this BCU sensor's temperature in °C, or None if it dropped out."""
        bcu_c = self.coordinator.data.temps.bcu_c
        if self._index >= len(bcu_c):
            return None
        return bcu_c[self._index]


# -- protection-parameter sensors ----------------------------------------------

_PARAM_UNIT_NATIVE: dict[str, str] = {
    "V": UnitOfElectricPotential.VOLT,
    "mV": UnitOfElectricPotential.MILLIVOLT,
    "A": UnitOfElectricCurrent.AMPERE,
    "C": UnitOfTemperature.CELSIUS,
    "%": PERCENTAGE,
}
_PARAM_UNIT_DEVICE_CLASS: dict[str, SensorDeviceClass] = {
    "V": SensorDeviceClass.VOLTAGE,
    "mV": SensorDeviceClass.VOLTAGE,
    "A": SensorDeviceClass.CURRENT,
    "C": SensorDeviceClass.TEMPERATURE,
}


class SeplosHvParamSensor(SeplosHvEntity, SensorEntity):
    """One trip or recover threshold of a protection parameter, at one level.

    Disabled by default: with up to 20 parameters x 3 levels x 2 (trip/
    recover), this tier can add well over a hundred entities.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: SeplosHvCoordinator,
        param_key: str,
        unit: str,
        level_index: int,
        kind: str,
    ) -> None:
        """Set up one param threshold sensor. level_index is 0-based; kind is 'trip' or 'recover'."""
        super().__init__(coordinator, f"{param_key}_l{level_index + 1}_{kind}")
        self._param_key = param_key
        self._level_index = level_index
        self._kind = kind
        self._attr_translation_key = f"param_{kind}"
        self._attr_translation_placeholders = {
            "param": _humanize(param_key),
            "level": str(level_index + 1),
        }
        self._attr_native_unit_of_measurement = _PARAM_UNIT_NATIVE.get(unit, unit or None)
        device_class = _PARAM_UNIT_DEVICE_CLASS.get(unit)
        if device_class is not None:
            self._attr_device_class = device_class

    @property
    def _level(self) -> ParamLevel | None:
        block = self.coordinator.data.params.get(self._param_key)
        if block is None or len(block.levels) <= self._level_index:
            return None
        return block.levels[self._level_index]

    @property
    def native_value(self) -> StateType:
        """Return the trip or recover threshold, or None if this level is absent."""
        level = self._level
        if level is None:
            return None
        return level.trip if self._kind == "trip" else level.recover

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the matching delay, in seconds, as an attribute."""
        level = self._level
        if level is None:
            return None
        delay = level.trip_delay_s if self._kind == "trip" else level.recover_delay_s
        return {"delay_s": delay}


# -- platform setup -------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SeplosHvConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Seplos HV BMS sensors from a config entry.

    Per-module/per-cell/per-temp-sensor/per-param entities are sized from the
    first successful coordinator data (already available: the coordinator's
    first refresh has completed by the time platforms are forwarded).
    """
    coordinator = entry.runtime_data
    data = coordinator.data
    enable_cell_sensors = entry.options.get(
        CONF_ENABLE_CELL_SENSORS, DEFAULT_ENABLE_CELL_SENSORS
    )

    entities: list[SensorEntity] = [
        SeplosHvSensor(coordinator, description) for description in PACK_SENSOR_DESCRIPTIONS
    ]

    entities.extend(
        SeplosHvBcuTempSensor(coordinator, index) for index in range(len(data.temps.bcu_c))
    )

    for module in data.cells:
        module_id = module.module_id
        entities.extend(
            SeplosHvModuleSensor(coordinator, description, module_id)
            for description in MODULE_SENSOR_DESCRIPTIONS
        )
        entities.extend(
            SeplosHvCellSensor(
                coordinator, module_id, cell_index, enabled_default=enable_cell_sensors
            )
            for cell_index in range(len(module.cells_mv))
        )

    for module_temps in data.temps.modules:
        entities.extend(
            SeplosHvModuleTempSensor(coordinator, module_temps.module_id, sensor_index)
            for sensor_index in range(len(module_temps.sensors_c))
        )

    for param_key, block in data.params.items():
        for level_index in range(len(block.levels)):
            entities.append(
                SeplosHvParamSensor(coordinator, param_key, block.unit, level_index, "trip")
            )
            entities.append(
                SeplosHvParamSensor(coordinator, param_key, block.unit, level_index, "recover")
            )

    async_add_entities(entities)
