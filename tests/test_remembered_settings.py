"""Settings the clock forgets: an IN1204M loses times now and then, and lights blue after a
power cut while reporting black (2026-11-14, 2026-11-18). Home Assistant sets them again."""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.time import ATTR_TIME, SERVICE_SET_VALUE
from homeassistant.components.time import DOMAIN as TIME_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.guoran_nixie.const import DEFAULT_SCAN_INTERVAL_S, DOMAIN, STORAGE_VERSION
from custom_components.guoran_nixie.protocol import BLACK, STATUS_REQUEST, TimeSetting, encode_color

from .fake_clock import FakeClock

POWER_OFF = "time.xggf_1234_power_off_time"
BACKLIGHT = "light.xggf_1234_backlight"
OFF_COMMAND = encode_color(BLACK)
SCAN_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL_S)


def _storage_key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}.{entry.entry_id}"


def _commands_since_last_status(clock: FakeClock) -> list[str]:
    last = len(clock.commands) - 1 - clock.commands[::-1].index(STATUS_REQUEST)
    return clock.commands[last + 1 :]


def _time_commands(commands: list[str]) -> list[str]:
    return [command for command in commands if command[0] in "AT"]


def _stored(hass_storage: dict[str, Any], entry: MockConfigEntry) -> dict[str, Any]:
    stored: dict[str, Any] = hass_storage[_storage_key(entry)]["data"]
    return stored


def _seed(hass_storage: dict[str, Any], entry: MockConfigEntry, **data: Any) -> None:
    hass_storage[_storage_key(entry)] = {
        "version": STORAGE_VERSION,
        "key": _storage_key(entry),
        "data": data,
    }


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
    print("stored:", _stored(hass_storage, loaded_entry))
    assert _stored(hass_storage, loaded_entry)["times"]["power_off"] == "01:07"

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
        "times": {
            "alarm_1": "07:00",
            "alarm_2": "07:00",
            "power_on": "07:00",
            "power_off": "22:00",
        },
        "backlight_off": False,
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
    _seed(hass_storage, config_entry, times={"power_off": "01:07"})
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
    _seed(hass_storage, config_entry, times={"power_off": "01:07"})
    clock.times[TimeSetting.POWER_OFF] = time(23, 30)

    await _setup(hass, config_entry)

    assert _time_commands(clock.commands) == []
    assert hass.states.get(POWER_OFF).state == "23:30:00"
    assert _stored(hass_storage, config_entry)["times"]["power_off"] == "23:30"


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


async def test_a_backlight_turned_off_here_is_turned_off_again_after_a_power_cut(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    clock: FakeClock,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: BACKLIGHT}, blocking=True
    )
    await hass.async_block_till_done()
    assert _stored(hass_storage, loaded_entry)["backlight_off"] is True

    clock.power_cycle()
    await _poll(hass, freezer)

    sent = _commands_since_last_status(clock)
    print("commands after the power cut:", sent)
    assert OFF_COMMAND in sent
    assert hass.states.get(BACKLIGHT).state == "off"


async def test_a_backlight_left_on_gets_no_off_command(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    clock: FakeClock,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _poll(hass, freezer)

    assert OFF_COMMAND not in clock.commands
    assert hass.states.get(BACKLIGHT).state == "on"


async def test_a_backlight_turned_on_elsewhere_is_left_on(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    clock: FakeClock,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: BACKLIGHT}, blocking=True
    )
    clock.rgb = (255, 0, 0)  # the remote or the original app turned it on
    await _poll(hass, freezer)
    assert _stored(hass_storage, loaded_entry)["backlight_off"] is False

    clock.power_cycle()
    await _poll(hass, freezer)

    assert OFF_COMMAND not in _commands_since_last_status(clock)


async def test_turning_the_backlight_on_here_stops_keeping_it_off(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    clock: FakeClock,
    hass_storage: dict[str, Any],
) -> None:
    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: BACKLIGHT}, blocking=True
    )
    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: BACKLIGHT}, blocking=True
    )
    await hass.async_block_till_done()

    assert _stored(hass_storage, loaded_entry)["backlight_off"] is False


async def test_a_backlight_kept_off_survives_a_home_assistant_restart(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    clock: FakeClock,
    bluetooth_api: list[Any],
    hass_storage: dict[str, Any],
) -> None:
    _seed(hass_storage, config_entry, times={}, backlight_off=True)
    clock.rgb = BLACK

    await _setup(hass, config_entry)

    print("commands:", clock.commands)
    assert OFF_COMMAND in clock.commands
