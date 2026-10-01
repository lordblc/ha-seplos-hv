"""DataUpdateCoordinator for the Seplos HV BMS integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import SeplosConnectionError, SeplosHvClient, SeplosTimeout, WriteResult
from .const import (
    CONF_BAUDRATE,
    CONF_ENABLE_WRITES,
    CONF_FAST_INTERVAL,
    CONF_HOST,
    CONF_MEDIUM_INTERVAL,
    CONF_PORT,
    CONF_SERIAL_PORT,
    CONF_SLOW_INTERVAL,
    CONF_TRANSPORT,
    DEFAULT_BAUDRATE,
    DEFAULT_ENABLE_WRITES,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_MEDIUM_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SLOW_INTERVAL,
    DOMAIN,
    EVENT_WRITE,
    REQUEST_TIMEOUT,
    TRANSPORT_SERIAL,
)
from .protocol import (
    PARAM_CMDS,
    ModuleCells,
    PackSummary,
    ParamBlock,
    Status,
    Temperatures,
    apply_param_change,
)
from .writes import check_step_guard, check_value_range

# Map the service/number "field" spelling (no _s suffix) to the ParamLevel/
# apply_param_change spelling used throughout protocol.py.
_FIELD_ALIASES: dict[str, str] = {
    "trip": "trip",
    "recover": "recover",
    "trip_delay": "trip_delay_s",
    "recover_delay": "recover_delay_s",
}

_LOGGER = logging.getLogger(__name__)

type SeplosHvConfigEntry = ConfigEntry[SeplosHvCoordinator]


@dataclass
class SeplosHvData:
    """A full snapshot of everything decoded from the BCU so far.

    ``cells``, ``temps`` and ``params`` are refreshed on their own tiered
    schedule; between refreshes the previous good value is carried over here
    by the coordinator so entities always have something to show.
    """

    identity: dict[str, str]
    summary: PackSummary
    status: Status
    cells: list[ModuleCells]
    temps: Temperatures
    params: dict[str, ParamBlock]
    last_medium_update: datetime | None
    last_slow_update: datetime | None

    @property
    def module_ids(self) -> list[int]:
        """Module ids as seen in the last cell-voltage read."""
        return [module.module_id for module in self.cells]

    @property
    def cells_online(self) -> int:
        """Total number of cell-voltage readings across all modules."""
        return sum(len(module.cells_mv) for module in self.cells)

    @property
    def pack_spread_mv(self) -> int:
        """Pack-wide max-min cell spread, in mV, from the fast summary read."""
        return self.summary.cell_spread_mv

    def module_spread_mv(self, module_id: int) -> int | None:
        """Per-module max-min cell spread, in mV, or None if unknown."""
        for module in self.cells:
            if module.module_id == module_id:
                return module.spread_mv
        return None

    @property
    def modules_with_no_temp_sensors(self) -> list[int]:
        """Module ids that reported zero temperature sensors."""
        return [module.module_id for module in self.temps.modules if not module.sensors_c]

    @property
    def headroom_spread_to_l1_mv(self) -> float | None:
        """Margin, in mV, between the pack spread and the L1 cell-delta trip."""
        block = self.params.get("cell_delta_charge")
        if block is None or not block.levels:
            return None
        return block.levels[0].trip - self.pack_spread_mv

    @property
    def headroom_min_temp_to_charge_inhibit_c(self) -> float | None:
        """Margin, in °C, between the pack min temp and the L3 charge-low-temperature trip."""
        block = self.params.get("charge_low_temperature")
        if block is None or len(block.levels) < 3:
            return None
        return self.summary.min_temp_c - block.levels[2].trip


def _build_client(entry: SeplosHvConfigEntry) -> SeplosHvClient:
    """Build the transport client from the config entry data."""
    data = entry.data
    if data[CONF_TRANSPORT] == TRANSPORT_SERIAL:
        return SeplosHvClient(
            serial_port=data[CONF_SERIAL_PORT],
            baudrate=data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
            timeout=REQUEST_TIMEOUT,
        )
    return SeplosHvClient(
        host=data[CONF_HOST],
        port=data.get(CONF_PORT, DEFAULT_PORT),
        timeout=REQUEST_TIMEOUT,
    )


class SeplosHvCoordinator(DataUpdateCoordinator[SeplosHvData]):
    """Poll the BCU at a fast interval with tiered medium/slow reads.

    A single asyncio lock inside ``client`` already serialises every request
    on the wire; this coordinator only decides *which* commands are due on
    a given refresh.
    """

    config_entry: SeplosHvConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: SeplosHvConfigEntry) -> None:
        """Initialise the coordinator from a config entry."""
        options = config_entry.options
        self.client = _build_client(config_entry)
        self._medium_interval = timedelta(
            seconds=options.get(CONF_MEDIUM_INTERVAL, DEFAULT_MEDIUM_INTERVAL)
        )
        self._slow_interval = timedelta(
            seconds=options.get(CONF_SLOW_INTERVAL, DEFAULT_SLOW_INTERVAL)
        )
        self._identity: dict[str, str] = {}
        self._last_medium: datetime | None = None
        self._last_slow: datetime | None = None
        self._warned = False
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=options.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)
            ),
        )

    async def _async_setup(self) -> None:
        """Connect and read identity once, before the first refresh."""
        try:
            await self.client.connect()
            self._identity = await self.client.read_identity()
        except (SeplosConnectionError, SeplosTimeout) as err:
            raise UpdateFailed(f"Could not reach Seplos BCU: {err}") from err

    async def _async_update_data(self) -> SeplosHvData:
        """Read the fast tier every refresh; medium/slow tiers when due."""
        now = dt_util.utcnow()
        previous = self.data

        try:
            summary = await self.client.read_summary()
            status = await self.client.read_status()

            if self._last_medium is None or now - self._last_medium >= self._medium_interval:
                cells = await self.client.read_cells()
                temps = await self.client.read_temps()
                self._last_medium = now
            elif previous is not None:
                cells, temps = previous.cells, previous.temps
            else:
                cells = await self.client.read_cells()
                temps = await self.client.read_temps()
                self._last_medium = now

            if self._last_slow is None or now - self._last_slow >= self._slow_interval:
                params = await self.client.read_params()
                self._last_slow = now
            elif previous is not None:
                params = previous.params
            else:
                params = await self.client.read_params()
                self._last_slow = now
        except (SeplosConnectionError, SeplosTimeout) as err:
            if not self._warned:
                _LOGGER.warning("Lost connection to Seplos BCU: %s", err)
                self._warned = True
            else:
                _LOGGER.debug("Still unable to reach Seplos BCU: %s", err)
            raise UpdateFailed(str(err)) from err

        if self._warned:
            _LOGGER.info("Connection to Seplos BCU restored")
            self._warned = False

        return SeplosHvData(
            identity=self._identity,
            summary=summary,
            status=status,
            cells=cells,
            temps=temps,
            params=params,
            last_medium_update=self._last_medium,
            last_slow_update=self._last_slow,
        )

    @property
    def writes_enabled(self) -> bool:
        """Whether the enable_writes option is on for this config entry."""
        return self.config_entry.options.get(CONF_ENABLE_WRITES, DEFAULT_ENABLE_WRITES)

    async def async_write_param(
        self,
        key: str,
        level_index: int,
        field: str,
        value: float,
        *,
        dry_run: bool,
        force: bool = False,
    ) -> WriteResult:
        """Validate and write one trip/recover/delay field of one parameter block.

        ``key`` is a PARAM_CMDS value name (e.g. "charge_over_current"); ``field`` is one
        of "trip", "recover", "trip_delay", "recover_delay" (no "_s" suffix - that is an
        internal protocol.py/ParamLevel spelling). Raises ParamValidationError if the
        value is out of range, or is a >20% single step on a trip/recover threshold and
        ``force`` is not True. Raises KeyError if ``key`` is not a known parameter.

        **Baseline freshness.** ``self.data.params[key]`` can be up to ``slow_interval``
        (default 3600 s) stale - long enough for the vendor tool or another client to have
        changed the value in between. So this always tries a FRESH single-group read
        (``client.read_param``, through the normal request()/lock path - never re-polling
        all ``len(PARAM_CMDS)`` groups) before validating, and uses that fresh block - not
        the cached one - for the 20% step guard, for ``apply_param_change``, and for the
        ``old_value`` reported in the event/result.

        - **dry_run=True**: a fresh read is still attempted (it is a read, always allowed).
          If it fails, falls back to the cached ``self.data.params[key]`` if one exists
          (``result.baseline`` is set to ``"cached"`` in that case, ``"fresh"``
          otherwise) - a dry run should still report *something* useful when the device is
          briefly unreachable. Raises KeyError if there is no cached block either (nothing
          to validate against at all).
        - **dry_run=False**: a failed fresh read ABORTS the write outright (the original
          SeplosConnectionError/SeplosTimeout propagates) - a real write never falls back
          to a baseline it cannot confirm is current.

        On success, the freshly-read (or, for dry runs, freshly re-read) block always
        replaces ``self.data.params[key]``. After a REAL write, the read-back block from
        ``client.write_params`` (not a full re-poll) is merged into ``self.data.params``
        and pushed to entities via ``async_set_updated_data`` - deliberately not a full
        ``async_request_refresh()``, which would re-read all 20 parameter groups for a
        change to just one. Always fires a ``seplos_hv_write`` event with the outcome and
        always logs the frame hex at INFO.
        """
        read_cmd = next((cmd for cmd, (k, _unit) in PARAM_CMDS.items() if k == key), None)
        if read_cmd is None:
            raise KeyError(f"{key!r} is not a known protection parameter")
        if field not in _FIELD_ALIASES:
            raise ValueError(f"field must be one of {sorted(_FIELD_ALIASES)}, got {field!r}")
        internal_field = _FIELD_ALIASES[field]

        baseline_source = "fresh"
        try:
            block = await self.client.read_param(read_cmd)
        except (SeplosConnectionError, SeplosTimeout):
            if not dry_run:
                raise  # real write: never fall back to a baseline that might be stale
            cached = self.data.params.get(key) if self.data is not None else None
            if cached is None:
                raise KeyError(
                    f"fresh read for {key!r} failed and no cached baseline exists either"
                ) from None
            _LOGGER.warning(
                "seplos_hv write %s: fresh baseline read failed, dry-run falling back "
                "to the cached (possibly stale) value", key,
            )
            block, baseline_source = cached, "cached"
        else:
            if self.data is not None:
                self.data.params[key] = block

        if not 0 <= level_index < len(block.levels):
            raise ValueError(
                f"level_index {level_index} out of range for {len(block.levels)}-level block"
            )
        old_level = block.levels[level_index]
        old_value = getattr(old_level, internal_field)

        check_value_range(block.unit, internal_field, value, key=key)
        check_step_guard(internal_field, old_value, value, force=force)

        new_block = apply_param_change(block, level_index, internal_field, value)

        result = await self.client.write_params(read_cmd, new_block, dry_run=dry_run)
        result.baseline = baseline_source

        _LOGGER.info(
            "seplos_hv write %s L%d %s: %s -> %s (dry_run=%s, baseline=%s) frame=%s",
            key, level_index + 1, field, old_value, value, dry_run, baseline_source,
            result.frame_hex,
        )

        if not dry_run:
            if not result.verified:
                _LOGGER.warning(
                    "seplos_hv write %s L%d %s not verified by read-back "
                    "(intended %s, read back %r)",
                    key, level_index + 1, field, value, result.readback,
                )
            if self.data is not None and result.readback is not None:
                self.data.params[key] = result.readback
                self.async_set_updated_data(self.data)

        self.hass.bus.async_fire(
            EVENT_WRITE,
            {
                "key": key,
                "level": level_index + 1,
                "field": field,
                "old_value": old_value,
                "new_value": value,
                "dry_run": dry_run,
                "verified": result.verified,
                "ack_ok": result.ack_ok,
                "frame_hex": result.frame_hex,
                "baseline": baseline_source,
            },
        )
        return result
