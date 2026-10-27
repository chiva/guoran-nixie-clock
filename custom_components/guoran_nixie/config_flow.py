"""Config flow: Bluetooth discovery (or a pick from discovered clocks), the password, and options."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .client import GuoranAuthError, GuoranClient, GuoranConnectionError
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_SYNC_TIME,
    DEFAULT_SCAN_INTERVAL_S,
    DEFAULT_SYNC_TIME,
    DOMAIN,
    MAX_SCAN_INTERVAL_S,
    MIN_SCAN_INTERVAL_S,
)
from .coordinator import GuoranConfigEntry
from .protocol import DEFAULT_PASSWORD, NAME_PREFIX, StatusFrameError, validate_password

_LOGGER = logging.getLogger(__name__)

ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_INVALID_AUTH = "invalid_auth"
ERROR_INVALID_PASSWORD = "invalid_password"


def _password_schema(default: str) -> vol.Schema:
    return vol.Schema({vol.Required(CONF_PASSWORD, default=default): TextSelector()})


async def _async_validate(hass: HomeAssistant, address: str, password: str) -> str | None:
    """Log in and read one status frame: the error key, or None when the clock answered."""
    try:
        validate_password(password)
    except ValueError:
        return ERROR_INVALID_PASSWORD
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        return ERROR_CANNOT_CONNECT
    client = GuoranClient(ble_device, password)
    try:
        await client.async_fetch_status()
    except GuoranAuthError:
        return ERROR_INVALID_AUTH
    except (GuoranConnectionError, StatusFrameError) as err:
        _LOGGER.debug("Validating %s: %s", address, err)
        return ERROR_CANNOT_CONNECT
    finally:
        await client.async_disconnect()
    return None


class GuoranConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovery: bluetooth.BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, str] = {}
        """Address → name of the clocks offered in the user step."""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: GuoranConfigEntry) -> OptionsFlow:
        return GuoranOptionsFlow()

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            address = self._discovery.address
            password = user_input[CONF_PASSWORD]
            if (error := await _async_validate(self.hass, address, password)) is None:
                return self._create(self._discovery.name, address, password)
            errors["base"] = error
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=_password_schema(DEFAULT_PASSWORD),
            description_placeholders={"name": self._discovery.name},
            errors=errors,
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            password = user_input[CONF_PASSWORD]
            if (error := await _async_validate(self.hass, address, password)) is None:
                return self._create(self._discovered[address], address, password)
            errors["base"] = error
        else:
            configured = self._async_current_ids(include_ignore=False)
            self._discovered = {
                info.address: info.name
                for info in bluetooth.async_discovered_service_info(self.hass, connectable=True)
                if info.name.startswith(NAME_PREFIX) and info.address not in configured
            }
            if not self._discovered:
                return self.async_abort(reason="no_devices_found")
        clocks = {address: f"{name} ({address})" for address, name in self._discovered.items()}
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(clocks)}).extend(
                _password_schema(DEFAULT_PASSWORD).schema
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            if (
                error := await _async_validate(self.hass, entry.data[CONF_ADDRESS], password)
            ) is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: password}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_password_schema(entry.data[CONF_PASSWORD]),
            description_placeholders={"name": entry.title},
            errors=errors,
        )

    def _create(self, name: str, address: str, password: str) -> ConfigFlowResult:
        return self.async_create_entry(
            title=name, data={CONF_ADDRESS: address, CONF_PASSWORD: password}
        )


class GuoranOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_SYNC_TIME: user_input[CONF_SYNC_TIME],
                }
            )
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_S),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL_S,
                        max=MAX_SCAN_INTERVAL_S,
                        step=30,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_SYNC_TIME, default=options.get(CONF_SYNC_TIME, DEFAULT_SYNC_TIME)
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
