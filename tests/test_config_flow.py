"""Discovery, manual pick, password validation, reauth and options."""

from __future__ import annotations

from typing import Any

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.guoran_nixie.const import CONF_SCAN_INTERVAL, CONF_SYNC_TIME, DOMAIN
from custom_components.guoran_nixie.protocol import DEFAULT_PASSWORD, FACTORY_PASSWORD

from .conftest import service_info
from .fake_clock import ADDRESS, NAME, FakeClock

OTHER_ADDRESS = "11:22:33:44:55:66"


async def _discover(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )


async def test_discovery_asks_for_the_password_and_creates_the_entry(
    hass: HomeAssistant, clock: FakeClock, bluetooth_api: list[Any]
) -> None:
    result = await _discover(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    assert result["description_placeholders"] == {"name": NAME}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: DEFAULT_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_PASSWORD: DEFAULT_PASSWORD}
    assert result["result"].unique_id == ADDRESS
    assert result["result"].state is ConfigEntryState.LOADED


async def test_discovery_of_a_factory_module_sets_the_password(
    hass: HomeAssistant, clock: FakeClock, bluetooth_api: list[Any]
) -> None:
    clock.password = FACTORY_PASSWORD
    result = await _discover(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "424242"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert clock.password == "424242"


async def test_discovery_of_a_configured_clock_aborts(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    result = await _discover(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_password_errors_are_shown_and_can_be_corrected(
    hass: HomeAssistant, clock: FakeClock, bluetooth_api: list[Any]
) -> None:
    result = await _discover(hass)
    flow_id = result["flow_id"]

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_PASSWORD: "12ab"})
    assert result["errors"] == {"base": "invalid_password"}

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_PASSWORD: "999999"})
    assert result["errors"] == {"base": "invalid_auth"}
    assert not clock.connected

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_PASSWORD: DEFAULT_PASSWORD}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_unreachable_clock_cannot_connect(
    hass: HomeAssistant,
    clock: FakeClock,
    bluetooth_api: list[Any],
    advertisements: list[BluetoothServiceInfoBleak],
) -> None:
    result = await _discover(hass)
    clock.refuse_connections = True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: DEFAULT_PASSWORD}
    )
    assert result["errors"] == {"base": "cannot_connect"}

    advertisements.clear()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: DEFAULT_PASSWORD}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_lists_unconfigured_clocks_only(
    hass: HomeAssistant,
    clock: FakeClock,
    bluetooth_api: list[Any],
    advertisements: list[BluetoothServiceInfoBleak],
) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=OTHER_ADDRESS, data={}).add_to_hass(hass)
    advertisements.extend(
        [service_info(OTHER_ADDRESS, "XGGF-OTHER"), service_info("77:77:77:77:77:77", "Speaker")]
    )

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    clocks = result["data_schema"].schema[CONF_ADDRESS].container
    assert clocks == {ADDRESS: f"{NAME} ({ADDRESS})"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS, CONF_PASSWORD: "999999"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS, CONF_PASSWORD: DEFAULT_PASSWORD}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME


async def test_user_flow_without_clocks_aborts(
    hass: HomeAssistant, bluetooth_api: list[Any], advertisements: list[BluetoothServiceInfoBleak]
) -> None:
    advertisements[:] = [service_info("77:77:77:77:77:77", "Speaker")]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_reauth_stores_the_new_password(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, clock: FakeClock
) -> None:
    clock.password = "135790"
    result = await loaded_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "000001"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "135790"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert loaded_entry.data[CONF_PASSWORD] == "135790"
    assert loaded_entry.state is ConfigEntryState.LOADED


async def test_options_change_the_polling_and_reload(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120.0, CONF_SYNC_TIME: False}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded_entry.options == {CONF_SCAN_INTERVAL: 120, CONF_SYNC_TIME: False}
    coordinator = loaded_entry.runtime_data
    assert coordinator.update_interval.total_seconds() == 120
    assert coordinator.sync_time is False
