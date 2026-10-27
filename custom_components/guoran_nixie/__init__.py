"""Guoran nixie clock: the "果然" Bluetooth tube clock, controlled locally over BLE.

One config entry per clock, found by Bluetooth discovery (local name `XGGF…`). Home Assistant's
bluetooth stack picks the adapter or ESPHome proxy that hears it; see client.py for why the
connection is opened on demand rather than held.
"""

from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_PASSWORD,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .client import GuoranClient
from .const import DOMAIN
from .coordinator import GuoranConfigEntry, GuoranCoordinator

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.LIGHT, Platform.SWITCH, Platform.TIME]


async def async_setup_entry(hass: HomeAssistant, entry: GuoranConfigEntry) -> bool:
    address: str = entry.data[CONF_ADDRESS]
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="not_found",
            translation_placeholders={"address": address},
        )
    client = GuoranClient(ble_device, entry.data[CONF_PASSWORD])

    @callback
    def _on_advertisement(
        service_info: bluetooth.BluetoothServiceInfoBleak, _change: bluetooth.BluetoothChange
    ) -> None:
        client.set_ble_device(service_info.device)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=address, connectable=True),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )
    entry.async_on_unload(client.async_disconnect)

    async def _on_stop(_event: Event) -> None:
        await client.async_disconnect()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))

    coordinator = GuoranCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GuoranConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
