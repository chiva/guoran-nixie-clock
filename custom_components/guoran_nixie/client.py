"""Bluetooth session with the clock: connect on demand, log in, send commands, read the status.

The clock's Bluetooth module accepts a single central at a time, so the session is dropped after a
short idle period: holding it would lock the phone app out for as long as Home Assistant runs.
Connections go through `bleak_retry_connector`, which lets Home Assistant pick the local adapter or
whichever ESPHome Bluetooth proxy has a free connection slot.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .protocol import (
    AUTH_ACCEPTED,
    AUTH_NOTIFY_CHAR,
    AUTH_SERVICE,
    AUTH_WRITE_CHAR,
    FACTORY_PASSWORD,
    STATUS_REQUEST,
    UART_RX_CHAR,
    UART_TX_CHAR,
    AuthResult,
    AuthResultError,
    ClockStatus,
    StatusAssembler,
    StatusFrameError,
    encode_auth,
    parse_auth_result,
    parse_status,
)

_LOGGER = logging.getLogger(__name__)

IDLE_DISCONNECT_S = 30.0
"""Seconds without traffic before the session is dropped and the phone can connect again."""
AUTH_TIMEOUT_S = 5.0
STATUS_TIMEOUT_S = 5.0
STATUS_ATTEMPTS = 2
"""The original app asked again whenever a status reply went missing or arrived garbled."""
COMMAND_GAP_S = 0.15
"""Minimum pause between two commands (status requests included). The module forwards bytes to
the clock's serial port without delimiters, so commands sent back to back could merge into one the
clock cannot parse."""


class GuoranError(Exception):
    """Base class for errors talking to the clock."""


class GuoranConnectionError(GuoranError):
    """The clock is unreachable, dropped the connection, or did not answer in time."""


class GuoranAuthError(GuoranError):
    """The clock's Bluetooth module rejected the password."""


def chunked(payload: bytes, size: int) -> Iterator[bytes]:
    """Split a write to fit one ATT packet: a 23-character time sync exceeds the default 20."""
    for start in range(0, len(payload), size):
        yield payload[start : start + size]


class GuoranClient:
    """One clock, reached by address through whichever adapter or proxy can see it."""

    def __init__(
        self, ble_device: BLEDevice, password: str, *, idle_timeout: float = IDLE_DISCONNECT_S
    ) -> None:
        self._ble_device = ble_device
        self._password = password
        self._idle_timeout = idle_timeout
        self._lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._assembler = StatusAssembler()
        self._status_waiter: asyncio.Future[str] | None = None
        self._auth_waiter: asyncio.Future[bytes] | None = None
        self._last_activity = 0.0
        self._last_command_at: float | None = None
        self._idle_timer: asyncio.TimerHandle | None = None
        self._idle_task: asyncio.Task[None] | None = None

    @property
    def address(self) -> str:
        return self._ble_device.address

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """The freshest advertisement's device: it names the adapter or proxy that heard it."""
        self._ble_device = ble_device

    async def async_send(self, *commands: str) -> None:
        """Send commands in order, spaced so the clock reads each one on its own."""
        async with self._lock:
            client = await self._ensure_connected()
            try:
                for command in commands:
                    await self._send_command(client, command)
            except BleakError as err:
                raise GuoranConnectionError(f"sending {commands}: {err}") from err
            finally:
                self._touch()

    async def async_fetch_status(self) -> ClockStatus:
        """Ask for the status frame and decode it.

        Raises:
            GuoranConnectionError: unreachable, or no complete frame after every attempt.
            GuoranAuthError: wrong password.
            StatusFrameError: every attempt got a complete frame whose fields make no sense.
        """
        bad_frame: StatusFrameError | None = None
        async with self._lock:
            client = await self._ensure_connected()
            try:
                for attempt in range(1, STATUS_ATTEMPTS + 1):
                    self._assembler.reset()
                    self._status_waiter = asyncio.get_running_loop().create_future()
                    await self._send_command(client, STATUS_REQUEST)
                    try:
                        async with asyncio.timeout(STATUS_TIMEOUT_S):
                            return parse_status(await self._status_waiter)
                    except TimeoutError:
                        _LOGGER.debug("No status from %s (attempt %d)", self.address, attempt)
                    except StatusFrameError as err:
                        _LOGGER.debug(
                            "Bad status from %s (attempt %d): %s", self.address, attempt, err
                        )
                        bad_frame = err
            except BleakError as err:
                raise GuoranConnectionError(f"requesting status: {err}") from err
            finally:
                self._status_waiter = None
                self._touch()
        if bad_frame is not None:
            raise bad_frame
        raise GuoranConnectionError(f"no status frame from {self.address}")

    async def async_disconnect(self) -> None:
        self._cancel_idle_timer()
        async with self._lock:
            await self._disconnect()

    async def _ensure_connected(self) -> BleakClientWithServiceCache:
        if self._client is not None and self._client.is_connected:
            return self._client
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self._ble_device.name or self.address,
                disconnected_callback=self._on_disconnected,
                ble_device_callback=lambda: self._ble_device,
            )
        except (BleakError, TimeoutError) as err:
            raise GuoranConnectionError(f"connecting to {self.address}: {err}") from err
        try:
            await self._authenticate(client)
            await client.start_notify(UART_RX_CHAR, self._on_uart)
        except BleakError as err:
            await self._safe_disconnect(client)
            raise GuoranConnectionError(f"setting up {self.address}: {err}") from err
        except GuoranError:
            await self._safe_disconnect(client)
            raise
        self._client = client
        return client

    async def _authenticate(self, client: BleakClientWithServiceCache) -> None:
        """Log in to the module's anti-hijack service, when it has one.

        The configured password first; if refused, the module may still have its factory password
        (never used with the original app), so change that to the configured one.
        """
        if client.services.get_service(AUTH_SERVICE) is None:
            _LOGGER.debug("%s has no password service; skipping login", self.address)
            return
        await client.start_notify(AUTH_NOTIFY_CHAR, self._on_auth)
        current_passwords = dict.fromkeys((self._password, FACTORY_PASSWORD))
        result = AuthResult.WRONG_PASSWORD
        for current in current_passwords:
            result = await self._write_auth(client, encode_auth(current, self._password))
            if result in AUTH_ACCEPTED:
                return
            if result is not AuthResult.WRONG_PASSWORD:
                break
        raise GuoranAuthError(f"{self.address} refused the password ({result.name})")

    async def _write_auth(self, client: BleakClientWithServiceCache, payload: str) -> AuthResult:
        self._auth_waiter = asyncio.get_running_loop().create_future()
        try:
            await self._write(client, AUTH_WRITE_CHAR, payload.encode("ascii"))
            async with asyncio.timeout(AUTH_TIMEOUT_S):
                answer = await self._auth_waiter
        except TimeoutError as err:
            raise GuoranConnectionError(f"{self.address} did not answer the login") from err
        finally:
            self._auth_waiter = None
        try:
            return parse_auth_result(answer)
        except AuthResultError as err:
            raise GuoranAuthError(str(err)) from err

    async def _send_command(self, client: BleakClientWithServiceCache, command: str) -> None:
        loop = asyncio.get_running_loop()
        if (
            self._last_command_at is not None
            and (wait := self._last_command_at + COMMAND_GAP_S - loop.time()) > 0
        ):
            await asyncio.sleep(wait)
        await self._write(client, UART_TX_CHAR, command.encode("ascii"))
        self._last_command_at = loop.time()

    async def _write(self, client: BleakClientWithServiceCache, uuid: str, payload: bytes) -> None:
        char = client.services.get_characteristic(uuid)
        if char is None:
            raise GuoranConnectionError(f"{self.address} has no characteristic {uuid}")
        with_response = "write" in char.properties
        for chunk in chunked(payload, char.max_write_without_response_size):
            await client.write_gatt_char(char, chunk, response=with_response)

    def _on_uart(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        for frame in self._assembler.feed(bytes(data)):
            if self._status_waiter is not None and not self._status_waiter.done():
                self._status_waiter.set_result(frame)

    def _on_auth(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        if self._auth_waiter is not None and not self._auth_waiter.done():
            self._auth_waiter.set_result(bytes(data))

    def _on_disconnected(self, _client: BleakClientWithServiceCache) -> None:
        _LOGGER.debug("%s disconnected", self.address)
        self._client = None
        self._cancel_idle_timer()
        for waiter in (self._status_waiter, self._auth_waiter):
            if waiter is not None and not waiter.done():
                waiter.set_exception(GuoranConnectionError(f"{self.address} disconnected"))

    def _touch(self) -> None:
        """Note activity and restart the idle countdown."""
        loop = asyncio.get_running_loop()
        self._last_activity = loop.time()
        self._cancel_idle_timer()
        if self._client is not None:
            self._idle_timer = loop.call_later(self._idle_timeout, self._on_idle)

    def _on_idle(self) -> None:
        self._idle_timer = None
        self._idle_task = asyncio.get_running_loop().create_task(self._disconnect_if_idle())

    async def _disconnect_if_idle(self) -> None:
        async with self._lock:
            # A command may have run while this task waited for the lock.
            idle_for = asyncio.get_running_loop().time() - self._last_activity
            if idle_for >= self._idle_timeout:
                await self._disconnect()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await self._safe_disconnect(client)

    async def _safe_disconnect(self, client: BleakClientWithServiceCache) -> None:
        try:
            await client.disconnect()
        except BleakError as err:
            _LOGGER.debug("Disconnecting %s: %s", self.address, err)
