"""Setup, polling, time sync, failures and unload."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.bluetooth import BluetoothChange, BluetoothServiceInfoBleak
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.guoran_nixie.const import (
    CONF_SYNC_TIME,
    DEFAULT_SCAN_INTERVAL_S,
    DOMAIN,
    TIME_SYNC_INTERVAL,
)
from custom_components.guoran_nixie.protocol import STATUS_REQUEST, Toggle, encode_time_sync

from .conftest import service_info
from .fake_clock import ADDRESS, NAME, FakeClock

ALARM_1 = "switch.xggf_1234_alarm_1"
SCAN_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL_S)


def _time_syncs(clock: FakeClock) -> list[str]:
    return [command for command in clock.commands if command.startswith("$")]


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    """Let a scheduled refresh run to the end: it is a background task, so it needs waiting for."""
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_creates_one_device_with_all_entities(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    clock: FakeClock,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    assert loaded_entry.state is ConfigEntryState.LOADED
    device = device_registry.async_get_device_by_connection(
        (dr.CONNECTION_BLUETOOTH, ADDRESS), loaded_entry.entry_id
    )
    assert device is not None
    assert device.name == NAME
    assert device.manufacturer == "Guoran"

    entities = er.async_entries_for_config_entry(entity_registry, loaded_entry.entry_id)
    by_domain: dict[str, int] = {}
    for entity in entities:
        by_domain[entity.domain] = by_domain.get(entity.domain, 0) + 1
    print("entities:", sorted(e.entity_id for e in entities))
    assert by_domain == {"switch": 16, "time": 4, "button": 10, "light": 1}
    assert all(e.device_id == device.id for e in entities)
    assert hass.states.get(ALARM_1).state == "off"


async def test_a_4_tube_clock_gets_4_tube_switches_and_loses_older_ones(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    clock: FakeClock,
    bluetooth_api: list[Any],
    advertisements: list[BluetoothServiceInfoBleak],
    entity_registry: er.EntityRegistry,
) -> None:
    advertisements[:] = [service_info(name="XGGF-IN1204231115.1")]
    config_entry.add_to_hass(hass)
    for toggle in (Toggle.TUBE_5, Toggle.TUBE_6):
        entity_registry.async_get_or_create(
            "switch", DOMAIN, f"{ADDRESS}_{toggle.value}", config_entry=config_entry
        )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    tubes = sorted(
        entry.unique_id.removeprefix(f"{ADDRESS}_")
        for entry in er.async_entries_for_config_entry(entity_registry, config_entry.entry_id)
        if entry.unique_id.startswith(f"{ADDRESS}_tube_")
    )
    print("tube switches:", tubes)
    assert tubes == ["tube_1", "tube_2", "tube_3", "tube_4"]
    assert hass.states.get("switch.xggf_1234_tube_4_backlight") is not None


async def test_first_refresh_reads_status_then_sets_the_clock_time(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, clock: FakeClock
) -> None:
    assert clock.commands[:2] == [STATUS_REQUEST, encode_time_sync(dt_util.now())]
    assert clock.connections == 1, "the time sync reuses the connection the status read opened"


async def test_time_is_synced_hourly_not_every_poll(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    clock: FakeClock,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _poll(hass, freezer, SCAN_INTERVAL)
    assert len(_time_syncs(clock)) == 1
    assert clock.commands.count(STATUS_REQUEST) == 2

    await _poll(hass, freezer, TIME_SYNC_INTERVAL)
    print("commands:", clock.commands)
    assert len(_time_syncs(clock)) == 2


async def test_time_sync_can_be_disabled(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    clock: FakeClock,
    bluetooth_api: list[Any],
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_SYNC_TIME: False})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert _time_syncs(clock) == []


async def test_clock_out_of_range_retries_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    clock: FakeClock,
    bluetooth_api: list[Any],
    advertisements: list[BluetoothServiceInfoBleak],
) -> None:
    advertisements.clear()
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert clock.connections == 0


async def test_unreachable_clock_retries_setup(
    hass: HomeAssistant, config_entry: MockConfigEntry, clock: FakeClock, bluetooth_api: list[Any]
) -> None:
    clock.refuse_connections = True
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_changed_password_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, clock: FakeClock, bluetooth_api: list[Any]
) -> None:
    clock.password = "111111"
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_failed_poll_makes_entities_unavailable_until_the_clock_answers(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    clock: FakeClock,
    freezer: FrozenDateTimeFactory,
) -> None:
    clock.drop_connection()
    clock.refuse_connections = True
    await _poll(hass, freezer, SCAN_INTERVAL)
    assert hass.states.get(ALARM_1).state == STATE_UNAVAILABLE

    clock.refuse_connections = False
    await _poll(hass, freezer, SCAN_INTERVAL)
    assert hass.states.get(ALARM_1).state == "off"


async def test_garbled_status_makes_entities_unavailable(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    clock: FakeClock,
    freezer: FrozenDateTimeFactory,
) -> None:
    clock.garble_status_replies = 2
    await _poll(hass, freezer, SCAN_INTERVAL)
    assert hass.states.get(ALARM_1).state == STATE_UNAVAILABLE


async def test_state_changed_on_the_clock_shows_after_the_next_poll(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    clock: FakeClock,
    freezer: FrozenDateTimeFactory,
) -> None:
    clock.toggles[Toggle.ALARM_1] = True
    await _poll(hass, freezer, SCAN_INTERVAL)
    assert hass.states.get(ALARM_1).state == "on"


async def test_advertisements_update_the_device_used_to_connect(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, clock: FakeClock, bluetooth_api: list[Any]
) -> None:
    via_proxy = service_info()
    bluetooth_api[0](via_proxy, BluetoothChange.ADVERTISEMENT)
    assert clock.ble_device_callback is not None
    assert clock.ble_device_callback() is via_proxy.device


async def test_unload_disconnects_so_the_phone_can_connect(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, clock: FakeClock, bluetooth_api: list[Any]
) -> None:
    assert clock.connected
    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    assert loaded_entry.state is ConfigEntryState.NOT_LOADED
    assert not clock.connected
    assert bluetooth_api == []
