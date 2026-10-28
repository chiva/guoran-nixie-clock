"""The Bluetooth session against the fake module: login, framing, retries, idle disconnect."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, time
from typing import Any

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from custom_components.guoran_nixie import client as client_module
from custom_components.guoran_nixie.client import (
    GuoranAuthError,
    GuoranClient,
    GuoranConnectionError,
    chunked,
)
from custom_components.guoran_nixie.protocol import (
    FACTORY_PASSWORD,
    STATUS_REQUEST,
    UART_TX_CHAR,
    StatusFrameError,
    TimeSetting,
    Toggle,
    encode_time_sync,
    encode_toggle,
)

from .fake_clock import ADDRESS, ATT_PAYLOAD, NAME, FakeClock

PASSWORD = "210709"


@pytest.fixture
async def session(clock: FakeClock) -> Iterator[GuoranClient]:
    client = GuoranClient(BLEDevice(ADDRESS, NAME, None), PASSWORD)
    yield client
    await client.async_disconnect()


async def test_fetch_status_logs_in_and_reads_the_whole_frame(
    session: GuoranClient, clock: FakeClock
) -> None:
    clock.toggles[Toggle.ALARM_1] = True
    clock.times[TimeSetting.ALARM_1] = time(6, 45)

    status = await session.async_fetch_status()

    print("auth writes:", clock.auth_writes, "commands:", clock.commands)
    assert clock.auth_writes == [PASSWORD * 2]
    assert clock.commands == [STATUS_REQUEST]
    assert status.rgb == clock.rgb
    assert status.toggles[Toggle.ALARM_1] is True
    assert status.times[TimeSetting.ALARM_1] == time(6, 45)
    assert session.is_connected


async def test_factory_module_gets_the_configured_password(
    session: GuoranClient, clock: FakeClock
) -> None:
    clock.password = FACTORY_PASSWORD

    await session.async_fetch_status()

    print("auth writes:", clock.auth_writes)
    assert clock.auth_writes == [PASSWORD * 2, FACTORY_PASSWORD + PASSWORD]
    assert clock.password == PASSWORD


async def test_wrong_password_raises_and_disconnects(
    session: GuoranClient, clock: FakeClock
) -> None:
    clock.password = "123456"

    with pytest.raises(GuoranAuthError, match="WRONG_PASSWORD"):
        await session.async_fetch_status()

    assert not clock.connected
    assert not session.is_connected
    assert clock.commands == []


async def test_module_without_password_service_skips_login(
    session: GuoranClient, clock: FakeClock
) -> None:
    clock.has_auth_service = False
    await session.async_fetch_status()
    assert clock.auth_writes == []


async def test_silent_login_is_a_connection_error(
    session: GuoranClient, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clock, "_on_auth_write", lambda payload: None)
    with pytest.raises(GuoranConnectionError, match="did not answer the login"):
        await session.async_fetch_status()


async def test_commands_are_chunked_to_the_mtu_and_kept_apart(
    session: GuoranClient, clock: FakeClock
) -> None:
    sync = encode_time_sync(datetime(2026, 10, 25, 9, 3, 7))
    assert len(sync) > ATT_PAYLOAD

    await session.async_send(sync, encode_toggle(Toggle.TUBE_2, False))

    uart_writes = [data for uuid, data, _ in clock.writes if uuid == UART_TX_CHAR]
    print("UART writes:", uart_writes)
    assert [len(w) for w in uart_writes] == [ATT_PAYLOAD, len(sync) - ATT_PAYLOAD, 3]
    assert clock.commands == [sync, "S20"]
    assert all(response is False for uuid, _, response in clock.writes if uuid == UART_TX_CHAR)


def test_chunked_splits_exactly() -> None:
    assert list(chunked(b"abcdefg", 3)) == [b"abc", b"def", b"g"]
    assert list(chunked(b"", 3)) == []


async def test_commands_share_the_connection_a_status_read_opened(
    session: GuoranClient, clock: FakeClock
) -> None:
    await session.async_fetch_status()
    await session.async_send("K02")
    await session.async_send("K03")
    assert clock.connections == 1
    assert len(clock.auth_writes) == 1


async def test_status_is_read_on_a_new_connection_as_the_clock_answers_only_once(
    session: GuoranClient, clock: FakeClock
) -> None:
    await session.async_fetch_status()
    await session.async_send(encode_toggle(Toggle.ALARM_2, True))

    status = await session.async_fetch_status()

    assert status.toggles[Toggle.ALARM_2] is True
    assert clock.connections == 2
    assert clock.commands == [STATUS_REQUEST, "S91", STATUS_REQUEST]


async def test_missing_status_reply_is_requested_again_on_a_new_connection(
    session: GuoranClient, clock: FakeClock
) -> None:
    clock.ignore_status_requests = 1
    status = await session.async_fetch_status()
    assert clock.commands == [STATUS_REQUEST, STATUS_REQUEST]
    assert clock.connections == 2
    assert status.rgb == clock.rgb


async def test_no_status_after_every_attempt(session: GuoranClient, clock: FakeClock) -> None:
    clock.ignore_status_requests = client_module.STATUS_ATTEMPTS
    with pytest.raises(GuoranConnectionError, match="no status frame"):
        await session.async_fetch_status()


async def test_garbled_status_is_requested_again(session: GuoranClient, clock: FakeClock) -> None:
    clock.garble_status_replies = 1
    status = await session.async_fetch_status()
    assert status.rgb == clock.rgb


async def test_garbled_status_every_time_raises_frame_error(
    session: GuoranClient, clock: FakeClock
) -> None:
    clock.garble_status_replies = client_module.STATUS_ATTEMPTS
    with pytest.raises(StatusFrameError, match="colour"):
        await session.async_fetch_status()


async def test_unreachable_clock(session: GuoranClient, clock: FakeClock) -> None:
    clock.refuse_connections = True
    with pytest.raises(GuoranConnectionError, match="out of connection slots"):
        await session.async_send("K01")


async def test_write_failure_is_a_connection_error(
    session: GuoranClient, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await session.async_fetch_status()

    async def failing_write(*_args: Any, **_kwargs: Any) -> None:
        raise BleakError("ATT error")

    monkeypatch.setattr(clock, "write_gatt_char", failing_write)
    with pytest.raises(GuoranConnectionError, match="ATT error"):
        await session.async_send("K01")
    with pytest.raises(GuoranConnectionError, match="ATT error"):
        await session.async_fetch_status()


async def test_link_lost_while_waiting_fails_fast(session: GuoranClient, clock: FakeClock) -> None:
    clock.ignore_status_requests = 1
    fetch = asyncio.create_task(session.async_fetch_status())
    for _ in range(100):
        if STATUS_REQUEST in clock.commands:
            break
        await asyncio.sleep(0)
    assert STATUS_REQUEST in clock.commands, "the request goes out before the link drops"
    clock.drop_connection()

    with pytest.raises(GuoranConnectionError, match="disconnected"):
        await fetch
    assert not session.is_connected

    await session.async_fetch_status()
    assert clock.connections == 2


async def test_idle_session_is_dropped_so_the_phone_can_connect(clock: FakeClock) -> None:
    session = GuoranClient(BLEDevice(ADDRESS, NAME, None), PASSWORD, idle_timeout=0.05)
    await session.async_fetch_status()
    assert clock.connected

    await asyncio.sleep(0.1)

    assert not clock.connected
    assert not session.is_connected
    await session.async_send("K08")
    assert clock.connections == 2
    await session.async_disconnect()


async def test_activity_postpones_the_idle_disconnect(clock: FakeClock) -> None:
    session = GuoranClient(BLEDevice(ADDRESS, NAME, None), PASSWORD, idle_timeout=0.1)
    await session.async_fetch_status()
    for _ in range(3):
        await asyncio.sleep(0.06)
        await session.async_send("K06")
    assert clock.connected
    assert clock.connections == 1
    await session.async_disconnect()
    assert not clock.connected


async def test_newer_advertisement_device_is_used_for_reconnects(
    session: GuoranClient, clock: FakeClock
) -> None:
    await session.async_fetch_status()
    via_proxy = BLEDevice(ADDRESS, NAME, {"source": "esphome-proxy"})
    session.set_ble_device(via_proxy)
    assert clock.ble_device_callback is not None
    assert clock.ble_device_callback() is via_proxy
