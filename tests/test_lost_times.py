"""Times the clock forgets: an IN1204M loses its power-off time on a power cut (observed
2026-11-14), and Home Assistant sets the last known one again."""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.time import ATTR_TIME, SERVICE_SET_VALUE
from homeassistant.components.time import DOMAIN as TIME_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.guoran_nixie.const import DEFAULT_SCAN_INTERVAL_S, DOMAIN, STORAGE_VERSION
from custom_components.guoran_nixie.protocol import STATUS_REQUEST, TimeSetting

from .fake_clock import FakeClock

POWER_OFF = "time.xggf_1234_power_off_time"
SCAN_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL_S)


def _storage_key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}.{entry.entry_id}"


def _commands_since_last_status(clock: FakeClock) -> list[str]:
    last = len(clock.commands) - 1 - clock.commands[::-1].index(STATUS_REQUEST)
    return clock.commands[last + 1 :]


def _time_commands(commands: list[str]) -> list[str]:
    return [command for command in commands if command[0] in "AT"]


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(SCAN_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_power_off_time_set_here_comes_back_after_a_power_cut(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    clock: FakeClock,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    await hass.services.async_call(
        TIME_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: POWER_OFF, ATTR_TIME: "01:07:00"},
        blocking=True,
    )
    await hass.async_block_till_done()
    print("stored:", hass_storage[_storage_key(loaded_entry)])
    assert hass_storage[_storage_key(loaded_entry)]["data"]["power_off"] == "01:07"

    clock.power_cycle()
    await _poll(hass, freezer)

    restored = _commands_since_last_status(clock)
    print("commands after the power cut:", restored)
    assert _time_commands(restored) == ["T01:07C"]
    assert clock.times[TimeSetting.POWER_OFF] == time(1, 7)
    assert not clock.unset_times
    assert hass.states.get(POWER_OFF).state == "01:07:00"


async def test_a_time_read_from_the_clock_is_remembered_and_restored(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    clock: FakeClock,
    bluetooth_api: list[Any],
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    clock.times[TimeSetting.POWER_OFF] = time(22, 0)
    await _setup(hass, config_entry)
    stored = hass_storage[_storage_key(config_entry)]
    print("stored after the first read:", stored)
    assert stored["version"] == STORAGE_VERSION
    assert stored["data"] == {
        "alarm_1": "07:00",
        "alarm_2": "07:00",
        "power_on": "07:00",
        "power_off": "22:00",
    }

    clock.power_cycle()
    await _poll(hass, freezer)

    assert _time_commands(_commands_since_last_status(clock)) == ["T22:00C"]
    assert hass.states.get(POWER_OFF).state == "22:00:00"


async def test_remembered_times_survive_a_home_assistant_restart(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    clock: FakeClock,
    bluetooth_api: list[Any],
    hass_storage: dict[str, Any],
) -> None:
    hass_storage[_storage_key(config_entry)] = {
        "version": STORAGE_VERSION,
        "key": _storage_key(config_entry),
        "data": {"power_off": "01:07"},
    }
    clock.unset_times.add(TimeSetting.POWER_OFF)

    await _setup(hass, config_entry)

    print("commands:", clock.commands)
    assert _time_commands(clock.commands) == ["T01:07C"]
    assert hass.states.get(POWER_OFF).state == "01:07:00"


async def test_a_time_the_clock_reports_wins_over_a_remembered_one(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    clock: FakeClock,
    bluetooth_api: list[Any],
    hass_storage: dict[str, Any],
) -> None:
    hass_storage[_storage_key(config_entry)] = {
        "version": STORAGE_VERSION,
        "key": _storage_key(config_entry),
        "data": {"power_off": "01:07"},
    }
    clock.times[TimeSetting.POWER_OFF] = time(23, 30)

    await _setup(hass, config_entry)

    assert _time_commands(clock.commands) == []
    assert hass.states.get(POWER_OFF).state == "23:30:00"
    assert hass_storage[_storage_key(config_entry)]["data"]["power_off"] == "23:30"


async def test_a_lost_time_never_known_stays_unknown(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    clock: FakeClock,
    bluetooth_api: list[Any],
) -> None:
    clock.unset_times.add(TimeSetting.POWER_OFF)

    await _setup(hass, config_entry)

    assert _time_commands(clock.commands) == []
    assert hass.states.get(POWER_OFF).state == STATE_UNKNOWN


async def test_removing_the_clock_forgets_its_times(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    assert _storage_key(loaded_entry) in hass_storage

    assert await hass.config_entries.async_remove(loaded_entry.entry_id)
    await hass.async_block_till_done()

    assert _storage_key(loaded_entry) not in hass_storage
