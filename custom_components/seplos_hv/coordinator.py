"""DataUpdateCoordinator for the Seplos HV BMS integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import SeplosConnectionError, SeplosHvClient, SeplosTimeout
from .const import (
    CONF_BAUDRATE,
    CONF_FAST_INTERVAL,
    CONF_HOST,
    CONF_MEDIUM_INTERVAL,
    CONF_PORT,
    CONF_SERIAL_PORT,
    CONF_SLOW_INTERVAL,
    CONF_TRANSPORT,
    DEFAULT_BAUDRATE,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_MEDIUM_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SLOW_INTERVAL,
    DOMAIN,
    REQUEST_TIMEOUT,
    TRANSPORT_SERIAL,
)
from .protocol import ModuleCells, PackSummary, ParamBlock, Status, Temperatures

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
