"""Number platform for the Seplos HV BMS integration - guarded parameter writes.

Only created at all when the ``enable_writes`` option is on (see const.py/config_flow.py).
One entity per (parameter, level, trip/recover) - mirrors SeplosHvParamSensor's
enumeration in sensor.py. Every entity is disabled by default in the registry: turning on
``enable_writes`` makes the entities *exist*, not appear enabled - the user still has to
opt in per-entity, same two-step pattern as the per-cell voltage sensors.

Every ``async_set_native_value`` call goes through ``SeplosHvCoordinator.async_write_param``
with ``dry_run=False`` and ``force=False`` - the 20% single-step guard always applies here;
a wide swing after review belongs behind the ``write_param`` service's ``force`` field, not
a number entity's slider.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ENABLE_WRITES, DEFAULT_ENABLE_WRITES
from .coordinator import SeplosHvConfigEntry, SeplosHvCoordinator
from .entity import SeplosHvEntity
from .writes import DELAY_RANGE_S, UNIT_RANGES

_LOGGER = logging.getLogger(__name__)


def _humanize(key: str) -> str:
    return key.replace("_", " ")


# step per unit - 1 for the integer-ish units, 0.1 for the ones with meaningful tenths.
_UNIT_STEP: dict[str, float] = {
    "mV": 1,
    "V": 1,
    "A": 0.1,
    "C": 0.1,
    "%": 1,
    "ohm/V": 1,
}
_DELAY_STEP = 0.1


class SeplosHvParamNumber(SeplosHvEntity, NumberEntity):
    """One trip or recover threshold of a protection parameter, at one level - writable.

    Disabled by default in the entity registry, same rationale as SeplosHvParamSensor:
    up to 20 parameters x 3 levels x 2 (trip/recover) is well over a hundred entities.
    """

    _attr_entity_category = EntityCategory.CONFIG
    # Numbers only exist when enable_writes is on; the user chose that, so show them.
    _attr_entity_registry_enabled_default = True
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: SeplosHvCoordinator,
        param_key: str,
        unit: str,
        level_index: int,
        kind: str,
    ) -> None:
        """Set up one param threshold number. level_index is 0-based; kind is 'trip' or 'recover'."""
        super().__init__(coordinator, f"{param_key}_l{level_index + 1}_{kind}_set")
        self._param_key = param_key
        self._level_index = level_index
        self._kind = kind
        self._attr_translation_key = f"param_{kind}_set"
        self._attr_translation_placeholders = {
            "param": _humanize(param_key),
            "level": str(level_index + 1),
        }
        self._attr_native_unit_of_measurement = unit or None

        lo, hi = UNIT_RANGES.get(unit, (0, 1_000_000))
        if hi == float("inf"):
            hi = 1_000_000
        if unit == "A":
            # check_value_range checks abs(value) against (0, hi); a current threshold can
            # itself be entered negative (discharge), so mirror the range around zero.
            lo = -hi
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_step = _UNIT_STEP.get(unit, 1)

    @property
    def native_value(self) -> float | None:
        """Return the current trip or recover threshold, or None if this level is absent."""
        block = self.coordinator.data.params.get(self._param_key)
        if block is None or len(block.levels) <= self._level_index:
            return None
        level = block.levels[self._level_index]
        return level.trip if self._kind == "trip" else level.recover

    async def async_set_native_value(self, value: float) -> None:
        """Write the new threshold to the BCU (real write, force=False - 20% guard applies)."""
        await self.coordinator.async_write_param(
            self._param_key, self._level_index, self._kind, value,
            dry_run=False, force=False,
        )


class SeplosHvParamDelayNumber(SeplosHvEntity, NumberEntity):
    """One trip or recover DELAY of a protection parameter, at one level - writable.

    Not created for heating_start_stop (0x0239), which has no delay fields at all.
    """

    _attr_entity_category = EntityCategory.CONFIG
    # Numbers only exist when enable_writes is on; the user chose that, so show them.
    _attr_entity_registry_enabled_default = True
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "s"
    _attr_native_min_value = DELAY_RANGE_S[0]
    _attr_native_max_value = DELAY_RANGE_S[1]
    _attr_native_step = _DELAY_STEP

    def __init__(
        self,
        coordinator: SeplosHvCoordinator,
        param_key: str,
        level_index: int,
        kind: str,
    ) -> None:
        """Set up one param delay number. level_index is 0-based; kind is 'trip' or 'recover'."""
        super().__init__(coordinator, f"{param_key}_l{level_index + 1}_{kind}_delay_set")
        self._param_key = param_key
        self._level_index = level_index
        self._kind = kind
        self._field = "trip_delay" if kind == "trip" else "recover_delay"
        self._attr_translation_key = f"param_{kind}_delay_set"
        self._attr_translation_placeholders = {
            "param": _humanize(param_key),
            "level": str(level_index + 1),
        }

    @property
    def native_value(self) -> float | None:
        """Return the current trip or recover delay, in seconds, or None if absent."""
        block = self.coordinator.data.params.get(self._param_key)
        if block is None or len(block.levels) <= self._level_index:
            return None
        level = block.levels[self._level_index]
        return level.trip_delay_s if self._kind == "trip" else level.recover_delay_s

    async def async_set_native_value(self, value: float) -> None:
        """Write the new delay to the BCU (real write; delays are not step-guarded)."""
        await self.coordinator.async_write_param(
            self._param_key, self._level_index, self._field, value,
            dry_run=False, force=False,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SeplosHvConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Seplos HV BMS number entities - only when enable_writes is on."""
    if not entry.options.get(CONF_ENABLE_WRITES, DEFAULT_ENABLE_WRITES):
        return

    coordinator = entry.runtime_data
    data = coordinator.data

    entities: list[NumberEntity] = []
    for param_key, block in data.params.items():
        for level_index in range(len(block.levels)):
            entities.append(
                SeplosHvParamNumber(coordinator, param_key, block.unit, level_index, "trip")
            )
            entities.append(
                SeplosHvParamNumber(coordinator, param_key, block.unit, level_index, "recover")
            )
            if param_key != "heating_start_stop":
                entities.append(
                    SeplosHvParamDelayNumber(coordinator, param_key, level_index, "trip")
                )
                entities.append(
                    SeplosHvParamDelayNumber(coordinator, param_key, level_index, "recover")
                )

    async_add_entities(entities)
