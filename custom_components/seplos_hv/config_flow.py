"""Config and options flow for the Seplos HV BMS integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .client import SeplosConnectionError, SeplosHvClient, SeplosTimeout
from .const import (
    CONF_BAUDRATE,
    CONF_ENABLE_CELL_SENSORS,
    CONF_FAST_INTERVAL,
    CONF_HOST,
    CONF_MEDIUM_INTERVAL,
    CONF_PORT,
    CONF_SERIAL_PORT,
    CONF_SLOW_INTERVAL,
    CONF_TRANSPORT,
    DEFAULT_BAUDRATE,
    DEFAULT_ENABLE_CELL_SENSORS,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_MEDIUM_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SLOW_INTERVAL,
    DOMAIN,
    MAX_FAST_INTERVAL,
    MAX_MEDIUM_INTERVAL,
    MAX_SLOW_INTERVAL,
    MIN_FAST_INTERVAL,
    MIN_MEDIUM_INTERVAL,
    MIN_SLOW_INTERVAL,
    REQUEST_TIMEOUT,
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
)

_LOGGER = logging.getLogger(__name__)


class SeplosHvConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Seplos HV BMS configuration flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state."""
        self._identity: dict[str, str] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which transport connects to the BCU."""
        return self.async_show_menu(step_id="user", menu_options=["tcp", "serial"])

    async def async_step_tcp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """TCP transport: a Waveshare gateway in transparent mode, or similar."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = SeplosHvClient(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
                timeout=REQUEST_TIMEOUT,
            )
            errors = await self._async_try_connect(client)
            if not errors:
                fallback_id = f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
                return await self._async_finish(
                    data={
                        CONF_TRANSPORT: TRANSPORT_TCP,
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                    },
                    fallback_id=fallback_id,
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")
                ): str,
                vol.Required(
                    CONF_PORT, default=(user_input or {}).get(CONF_PORT, DEFAULT_PORT)
                ): int,
            }
        )
        return self.async_show_form(step_id="tcp", data_schema=schema, errors=errors)

    async def async_step_serial(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Serial transport: a local USB RS485 adapter."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = SeplosHvClient(
                serial_port=user_input[CONF_SERIAL_PORT],
                baudrate=user_input[CONF_BAUDRATE],
                timeout=REQUEST_TIMEOUT,
            )
            errors = await self._async_try_connect(client)
            if not errors:
                return await self._async_finish(
                    data={
                        CONF_TRANSPORT: TRANSPORT_SERIAL,
                        CONF_SERIAL_PORT: user_input[CONF_SERIAL_PORT],
                        CONF_BAUDRATE: user_input[CONF_BAUDRATE],
                    },
                    fallback_id=user_input[CONF_SERIAL_PORT],
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SERIAL_PORT,
                    default=(user_input or {}).get(CONF_SERIAL_PORT, ""),
                ): str,
                vol.Required(
                    CONF_BAUDRATE,
                    default=(user_input or {}).get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
                ): int,
            }
        )
        return self.async_show_form(step_id="serial", data_schema=schema, errors=errors)

    async def _async_try_connect(self, client: SeplosHvClient) -> dict[str, str]:
        """Connect, read identity for the unique id, then disconnect."""
        try:
            await client.connect()
            try:
                self._identity = await client.read_identity()
            finally:
                await client.close()
        except SeplosTimeout:
            return {"base": "timeout"}
        except SeplosConnectionError:
            return {"base": "cannot_connect"}
        return {}

    async def _async_finish(
        self, *, data: dict[str, Any], fallback_id: str
    ) -> ConfigFlowResult:
        """Set the unique id from the BMS serial (or a transport fallback) and create the entry."""
        unique_id = self._identity.get("bms_serial") or fallback_id
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f"Seplos HV BMS {unique_id}", data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SeplosHvOptionsFlow:
        """Return the options flow handler."""
        return SeplosHvOptionsFlow()


class SeplosHvOptionsFlow(OptionsFlow):
    """Handle Seplos HV BMS options (poll intervals and cell sensors)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the polling intervals and the per-cell sensor toggle."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_FAST_INTERVAL,
                    default=options.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_FAST_INTERVAL,
                        max=MAX_FAST_INTERVAL,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                vol.Required(
                    CONF_MEDIUM_INTERVAL,
                    default=options.get(CONF_MEDIUM_INTERVAL, DEFAULT_MEDIUM_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_MEDIUM_INTERVAL,
                        max=MAX_MEDIUM_INTERVAL,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                vol.Required(
                    CONF_SLOW_INTERVAL,
                    default=options.get(CONF_SLOW_INTERVAL, DEFAULT_SLOW_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SLOW_INTERVAL,
                        max=MAX_SLOW_INTERVAL,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                vol.Required(
                    CONF_ENABLE_CELL_SENSORS,
                    default=options.get(
                        CONF_ENABLE_CELL_SENSORS, DEFAULT_ENABLE_CELL_SENSORS
                    ),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
