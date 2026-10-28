"""A stand-in for the clock's Bluetooth module, at the level bleak sees it.

It behaves as the protocol says the real one does: a password service answering one byte, a UART
characteristic that receives commands as an undelimited byte stream (so chunked writes must
reassemble), and status frames sent back in MTU-sized notifications. Commands change its state, so
a status read after a command shows the effect, as on the real clock.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import time
from typing import Any

from bleak.exc import BleakError

from custom_components.guoran_nixie.protocol import (
    AUTH_NOTIFY_CHAR,
    AUTH_SERVICE,
    AUTH_WRITE_CHAR,
    DEFAULT_PASSWORD,
    PASSWORD_LENGTH,
    STATUS_REQUEST,
    STATUS_TERMINATOR,
    TIME_FORMATS,
    TOGGLE_IDS,
    UART_RX_CHAR,
    UART_RX_SERVICE,
    UART_TX_CHAR,
    UART_TX_SERVICE,
    AuthResult,
    TimeSetting,
    Toggle,
    encode_color,
    encode_time,
    encode_toggle,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
NAME = "XGGF-1234"
ATT_PAYLOAD = 20
"""Default MTU (23) minus the ATT header: what one write or notification carries."""

COMMAND_PATTERNS: dict[str, re.Pattern[str]] = {
    "color": re.compile(r"R(\d{3})-G(\d{3})-B(\d{3})"),
    "toggle": re.compile(r"S([0-9A-F])([01])"),
    "key": re.compile(r"K0([1-9])"),
    "time": re.compile(r"([AT])(\d{2}):(\d{2})([ABOC])"),
    "time_sync": re.compile(r"\$\d{4}-\d{2}-\d{2};\d{2}:\d{2}:\d{2};0[0-6]"),
    "status": re.compile(re.escape(STATUS_REQUEST)),
}


@dataclass
class FakeCharacteristic:
    uuid: str
    properties: list[str]
    max_write_without_response_size: int = ATT_PAYLOAD


@dataclass
class FakeServices:
    services: set[str]
    characteristics: dict[str, FakeCharacteristic]

    def get_service(self, uuid: str) -> str | None:
        return uuid if uuid in self.services else None

    def get_characteristic(self, uuid: str) -> FakeCharacteristic | None:
        return self.characteristics.get(uuid)


UNSET_TIME = "44:29"


@dataclass
class FakeClock:
    password: str = DEFAULT_PASSWORD
    has_auth_service: bool = True
    rgb: tuple[int, int, int] = (0, 255, 255)
    toggles: dict[Toggle, bool] = field(default_factory=lambda: dict.fromkeys(Toggle, False))
    times: dict[TimeSetting, time] = field(
        default_factory=lambda: dict.fromkeys(TimeSetting, time(7, 0))
    )
    unset_times: set[TimeSetting] = field(default_factory=set)
    """Times the clock reports as `UNSET_TIME`, as a real one did for a never-set power-off time."""
    ignore_status_requests: int = 0
    """Status requests to leave unanswered, as a flaky link would."""
    status_once_per_connection: bool = True
    """As a real IN1204M: only the first status request of each connection gets an answer."""
    garble_status_replies: int = 0
    """Status replies to corrupt (a valid frame shape with nonsense fields)."""
    refuse_connections: bool = False

    commands: list[str] = field(default_factory=list)
    auth_writes: list[str] = field(default_factory=list)
    writes: list[tuple[str, bytes, bool]] = field(default_factory=list)
    connections: int = 0
    connected: bool = False
    ble_device_callback: Callable[[], Any] | None = None
    _uart: str = ""
    _status_answered: bool = False
    _notify: dict[str, Callable[[Any, bytearray], None]] = field(default_factory=dict)
    _disconnected_callback: Callable[[Any], None] | None = None

    # bleak_retry_connector.establish_connection
    async def establish_connection(
        self,
        _client_class: type,
        _device: Any,
        _name: str,
        disconnected_callback: Callable[[Any], None] | None = None,
        **_kwargs: Any,
    ) -> FakeClock:
        if self.refuse_connections:
            raise BleakError("out of connection slots")
        self.connections += 1
        self.connected = True
        self._uart = ""
        self._status_answered = False
        self._notify = {}
        self._disconnected_callback = disconnected_callback
        self.ble_device_callback = _kwargs.get("ble_device_callback")
        return self

    # BleakClient
    @property
    def is_connected(self) -> bool:
        return self.connected

    @property
    def services(self) -> FakeServices:
        characteristics = {
            UART_TX_CHAR: FakeCharacteristic(UART_TX_CHAR, ["write-without-response"]),
            UART_RX_CHAR: FakeCharacteristic(UART_RX_CHAR, ["notify"]),
        }
        services = {UART_TX_SERVICE, UART_RX_SERVICE}
        if self.has_auth_service:
            services.add(AUTH_SERVICE)
            characteristics[AUTH_WRITE_CHAR] = FakeCharacteristic(AUTH_WRITE_CHAR, ["write"])
            characteristics[AUTH_NOTIFY_CHAR] = FakeCharacteristic(AUTH_NOTIFY_CHAR, ["notify"])
        return FakeServices(services, characteristics)

    async def start_notify(self, uuid: str, callback: Callable[[Any, bytearray], None]) -> None:
        self._notify[uuid] = callback

    async def write_gatt_char(self, char: FakeCharacteristic, data: bytes, response: bool) -> None:
        if not self.connected:
            raise BleakError("not connected")
        if len(data) > char.max_write_without_response_size:
            raise BleakError(f"write of {len(data)} bytes exceeds the MTU")
        self.writes.append((char.uuid, bytes(data), response))
        if char.uuid == AUTH_WRITE_CHAR:
            self._on_auth_write(data.decode("ascii"))
        elif char.uuid == UART_TX_CHAR:
            self._uart += data.decode("ascii")
            self._parse_uart()

    async def disconnect(self) -> None:
        self.connected = False

    # Test controls
    def drop_connection(self) -> None:
        """The link goes away on the clock's side (out of range, power cut)."""
        self.connected = False
        if self._disconnected_callback is not None:
            self._disconnected_callback(self)

    def status_frame(self) -> str:
        return (
            encode_color(self.rgb)
            + "".join(encode_toggle(toggle, self.toggles[toggle]) for toggle in Toggle)
            + "".join(self._time_field(setting) for setting in TimeSetting)
            + STATUS_TERMINATOR
        )

    def _time_field(self, setting: TimeSetting) -> str:
        if setting not in self.unset_times:
            return encode_time(setting, self.times[setting])
        prefix, suffix = TIME_FORMATS[setting]
        return f"{prefix}{UNSET_TIME}{suffix}"

    # The module and the clock's firmware
    def _on_auth_write(self, payload: str) -> None:
        self.auth_writes.append(payload)
        old, new = payload[:PASSWORD_LENGTH], payload[PASSWORD_LENGTH:]
        if old != self.password:
            result = AuthResult.WRONG_PASSWORD
        elif new != old:
            self.password = new
            result = AuthResult.PASSWORD_CHANGED
        else:
            result = AuthResult.OK
        self._send(AUTH_NOTIFY_CHAR, bytes([result]))

    def _parse_uart(self) -> None:
        while self._uart:
            for kind, pattern in COMMAND_PATTERNS.items():
                if match := pattern.match(self._uart):
                    self._uart = self._uart[match.end() :]
                    self.commands.append(match[0])
                    self._apply(kind, match)
                    break
            else:
                return  # an incomplete command: wait for the next chunk

    def _apply(self, kind: str, match: re.Match[str]) -> None:
        if kind == "color":
            # The colour the LEDs show: the clock lights green from "R" and red from "G".
            self.rgb = (int(match[2]), int(match[1]), int(match[3]))
        elif kind == "toggle":
            toggle = next(t for t, char in TOGGLE_IDS.items() if char == match[1])
            self.toggles[toggle] = match[2] == "1"
        elif kind == "time":
            affixes = (match[1], match[4])
            setting = next(s for s, fmt in TIME_FORMATS.items() if fmt == affixes)
            self.times[setting] = time(int(match[2]), int(match[3]))
        elif kind == "status":
            self._answer_status()

    def _answer_status(self) -> None:
        if self.ignore_status_requests:
            self.ignore_status_requests -= 1
            return
        if self.status_once_per_connection and self._status_answered:
            return
        self._status_answered = True
        frame = self.status_frame()
        if self.garble_status_replies:
            self.garble_status_replies -= 1
            frame = "R999" + frame[4:]
        payload = frame.encode("ascii")
        for start in range(0, len(payload), ATT_PAYLOAD):
            self._send(UART_RX_CHAR, payload[start : start + ATT_PAYLOAD])

    def _send(self, uuid: str, data: bytes) -> None:
        if callback := self._notify.get(uuid):
            asyncio.get_running_loop().call_soon(callback, None, bytearray(data))
