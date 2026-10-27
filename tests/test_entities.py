"""Entities change the clock: each service call must reach the fake clock's state."""

from __future__ import annotations

from datetime import time

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.light import ATTR_HS_COLOR
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.time import ATTR_TIME, SERVICE_SET_VALUE
from homeassistant.components.time import DOMAIN as TIME_DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guoran_nixie.const import DOMAIN
from custom_components.guoran_nixie.protocol import (
    BLACK,
    STATUS_REQUEST,
    TimeSetting,
    Toggle,
    encode_time_sync,
)

from .fake_clock import FakeClock

PREFIX = "xggf_1234"
BACKLIGHT = f"light.{PREFIX}_backlight"


def last_command(clock: FakeClock) -> str:
    """The command a service call sent, checking it was followed by the confirming refresh."""
    command, refresh = clock.commands[-2:]
    assert refresh == STATUS_REQUEST
    return command


async def _call(
    hass: HomeAssistant, domain: str, service: str, entity_id: str, **data: object
) -> None:
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )


@pytest.mark.parametrize(
    ("entity_id", "toggle", "command"),
    [
        (f"switch.{PREFIX}_tube_3_backlight", Toggle.TUBE_3, "S31"),
        (f"switch.{PREFIX}_alarm_2", Toggle.ALARM_2, "S91"),
        (f"switch.{PREFIX}_12_hour_format", Toggle.HOUR_12, "SB1"),
        (f"switch.{PREFIX}_sensor_switch", Toggle.SENSOR_SWITCH, "SF1"),
    ],
)
async def test_switch_turns_the_setting_on_and_off(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    clock: FakeClock,
    entity_id: str,
    toggle: Toggle,
    command: str,
) -> None:
    assert hass.states.get(entity_id).state == "off"

    await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, entity_id)
    print("commands:", clock.commands)
    assert last_command(clock) == command
    assert clock.toggles[toggle] is True
    assert hass.states.get(entity_id).state == "on"

    await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, entity_id)
    assert clock.toggles[toggle] is False
    assert hass.states.get(entity_id).state == "off"


@pytest.mark.parametrize(
    ("entity_id", "setting", "command"),
    [
        (f"time.{PREFIX}_alarm_1_time", TimeSetting.ALARM_1, "A06:30A"),
        (f"time.{PREFIX}_alarm_2_time", TimeSetting.ALARM_2, "A06:30B"),
        (f"time.{PREFIX}_power_on_time", TimeSetting.POWER_ON, "T06:30O"),
        (f"time.{PREFIX}_power_off_time", TimeSetting.POWER_OFF, "T06:30C"),
    ],
)
async def test_time_sets_the_clock_to_the_minute(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    clock: FakeClock,
    entity_id: str,
    setting: TimeSetting,
    command: str,
) -> None:
    assert hass.states.get(entity_id).state == "07:00:00"

    await _call(hass, TIME_DOMAIN, SERVICE_SET_VALUE, entity_id, **{ATTR_TIME: "06:30:45"})

    assert last_command(clock) == command
    assert clock.times[setting] == time(6, 30)
    assert hass.states.get(entity_id).state == "06:30:00"


async def test_backlight_follows_the_hue_and_remembers_it_when_off(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, clock: FakeClock
) -> None:
    state = hass.states.get(BACKLIGHT)
    assert state.state == "on"
    assert state.attributes[ATTR_HS_COLOR] == (180.0, 100.0)

    await _call(hass, LIGHT_DOMAIN, SERVICE_TURN_ON, BACKLIGHT, **{ATTR_HS_COLOR: (120, 40)})
    assert last_command(clock) == "R000-G255-B000"
    assert clock.rgb == (0, 255, 0)
    assert hass.states.get(BACKLIGHT).attributes[ATTR_HS_COLOR] == (120.0, 100.0)

    await _call(hass, LIGHT_DOMAIN, SERVICE_TURN_OFF, BACKLIGHT)
    assert clock.rgb == BLACK
    state = hass.states.get(BACKLIGHT)
    assert state.state == "off"

    await _call(hass, LIGHT_DOMAIN, SERVICE_TURN_ON, BACKLIGHT)
    print("commands:", clock.commands)
    assert clock.rgb == (0, 255, 0)
    assert hass.states.get(BACKLIGHT).state == "on"


async def test_backlight_turned_on_from_black_uses_the_original_default(
    hass: HomeAssistant, clock: FakeClock, loaded_entry: MockConfigEntry
) -> None:
    clock.rgb = BLACK
    await hass.config_entries.async_reload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(BACKLIGHT).state == "off"

    await _call(hass, LIGHT_DOMAIN, SERVICE_TURN_ON, BACKLIGHT)
    assert clock.rgb == (0, 255, 255)


@pytest.mark.parametrize(
    ("name", "command"),
    [
        ("backlight_previous_colour", "K01"),
        ("backlight_mode", "K02"),
        ("backlight_next_colour", "K03"),
        ("power", "K04"),
        ("ok", "K08"),
        ("minus", "K09"),
    ],
)
async def test_key_buttons_press_the_clock_buttons(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, clock: FakeClock, name: str, command: str
) -> None:
    await _call(hass, BUTTON_DOMAIN, SERVICE_PRESS, f"button.{PREFIX}_{name}")
    assert last_command(clock) == command


async def test_sync_time_button_sets_the_clock_now(
    hass: HomeAssistant,
    loaded_entry: MockConfigEntry,
    clock: FakeClock,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-12-31T23:59:30+00:00")
    await _call(hass, BUTTON_DOMAIN, SERVICE_PRESS, f"button.{PREFIX}_sync_time_now")
    assert clock.commands[-1] == encode_time_sync(dt_util.now())


async def test_command_to_an_unreachable_clock_raises(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, clock: FakeClock
) -> None:
    clock.drop_connection()
    clock.refuse_connections = True
    with pytest.raises(HomeAssistantError, match="Could not talk to the clock"):
        await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, f"switch.{PREFIX}_alarm_1")
    assert hass.states.get(f"switch.{PREFIX}_alarm_1").state == "off"


async def test_command_with_a_changed_password_raises_and_starts_reauth(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, clock: FakeClock
) -> None:
    clock.drop_connection()
    clock.password = "222222"
    with pytest.raises(HomeAssistantError, match="refused the password"):
        await _call(hass, BUTTON_DOMAIN, SERVICE_PRESS, f"button.{PREFIX}_backlight_mode")
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
