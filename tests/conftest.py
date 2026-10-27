"""Fixtures: a fake clock behind a patched bleak connection, and Home Assistant's bluetooth API
answering as if one adapter could hear it.

Home Assistant's bluetooth integration needs real adapters (BlueZ, CoreBluetooth), so it is marked
as loaded and the few API calls the integration makes are patched instead.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guoran_nixie import client as client_module
from custom_components.guoran_nixie.const import DOMAIN
from custom_components.guoran_nixie.protocol import DEFAULT_PASSWORD

from .fake_clock import ADDRESS, NAME, FakeClock

VECTORS_PATH = Path(__file__).parents[1] / "docs" / "fixtures" / "protocol-vectors.json"


def load_vectors() -> dict[str, Any]:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


VECTORS = load_vectors()
"""The protocol vectors web/protocol.js is tested against, too."""


def service_info(address: str = ADDRESS, name: str = NAME) -> BluetoothServiceInfoBleak:
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        source="local",
        device=BLEDevice(address, name, None),
        advertisement=None,
        connectable=True,
        time=0,
        tx_power=None,
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/ in every test."""


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake clock answers at once: waiting for silence need not take seconds."""
    monkeypatch.setattr(client_module, "STATUS_TIMEOUT_S", 0.05)
    monkeypatch.setattr(client_module, "AUTH_TIMEOUT_S", 0.05)
    monkeypatch.setattr(client_module, "COMMAND_GAP_S", 0)


@pytest.fixture(autouse=True)
def bluetooth_loaded(hass: HomeAssistant) -> None:
    hass.config.components.update({"bluetooth", "bluetooth_adapters", "usb"})


@pytest.fixture
def clock() -> Iterator[FakeClock]:
    fake = FakeClock()
    with patch(
        "custom_components.guoran_nixie.client.establish_connection",
        side_effect=fake.establish_connection,
    ):
        yield fake


@pytest.fixture
def advertisements() -> list[BluetoothServiceInfoBleak]:
    """What the bluetooth stack has heard; tests may add or remove entries."""
    return [service_info()]


@pytest.fixture
def bluetooth_api(advertisements: list[BluetoothServiceInfoBleak]) -> Iterator[list[Any]]:
    """The bluetooth API, answering from `advertisements`. Yields the registered callbacks."""
    callbacks: list[Any] = []

    def ble_device_from_address(
        _hass: HomeAssistant, address: str, connectable: bool = True
    ) -> BLEDevice | None:
        return next((i.device for i in advertisements if i.address == address), None)

    def register_callback(_hass: HomeAssistant, callback: Any, *_args: Any, **_kw: Any) -> Any:
        callbacks.append(callback)
        return lambda: callbacks.remove(callback)

    with (
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address",
            side_effect=ble_device_from_address,
        ),
        patch(
            "homeassistant.components.bluetooth.async_discovered_service_info",
            side_effect=lambda _hass, connectable=True: list(advertisements),
        ),
        patch(
            "homeassistant.components.bluetooth.async_register_callback",
            side_effect=register_callback,
        ),
    ):
        yield callbacks


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=NAME,
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_PASSWORD: DEFAULT_PASSWORD},
    )


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry, clock: FakeClock, bluetooth_api: list[Any]
) -> MockConfigEntry:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
