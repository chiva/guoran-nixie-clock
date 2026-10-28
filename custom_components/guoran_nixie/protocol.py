"""Wire protocol of the Guoran nixie clock, decoded from the original app (see docs/PROTOCOL.md).

Pure functions and types, no Bluetooth or Home Assistant: the client moves these bytes, this module
gives them meaning. Every encoder and the status parser are checked against
docs/fixtures/protocol-vectors.json, the same vectors web/protocol.js is tested with.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, time
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Final

NAME_PREFIX: Final = "XGGF"
"""Advertised local name of every clock; the original app ignored any other device."""


def _uuid16(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


UART_RX_SERVICE: Final = _uuid16(0xFFE0)
UART_RX_CHAR: Final = _uuid16(0xFFE4)
"""Notifications from the clock (status frames)."""
UART_TX_SERVICE: Final = _uuid16(0xFFE5)
UART_TX_CHAR: Final = _uuid16(0xFFE9)
"""Commands to the clock."""
AUTH_SERVICE: Final = _uuid16(0xFFC0)
AUTH_WRITE_CHAR: Final = _uuid16(0xFFC1)
AUTH_NOTIFY_CHAR: Final = _uuid16(0xFFC2)

DEFAULT_PASSWORD: Final = "210709"
"""The original app changes the Bluetooth module's password to this on first connection."""
FACTORY_PASSWORD: Final = "000000"
"""The Bluetooth module's password before any app has changed it."""
PASSWORD_LENGTH: Final = 6

STATUS_REQUEST: Final = "OSC"
STATUS_PREFIX: Final = "R"
STATUS_TERMINATOR: Final = "CSS"
STATUS_LENGTH: Final = 93

CHANNEL_MAX: Final = 255
HUE_SEGMENT: Final = CHANNEL_MAX
HUE_POSITIONS: Final = 6 * HUE_SEGMENT
"""The original colour slider runs 0…1530 around the hue wheel: six segments of 255 steps."""

type RGB = tuple[int, int, int]
BLACK: Final[RGB] = (0, 0, 0)


class ProtocolError(ValueError):
    """Bytes from the clock that do not follow the protocol."""


class StatusFrameError(ProtocolError):
    """A status frame with the wrong length, delimiters or field contents."""


class AuthResultError(ProtocolError):
    """An unknown answer from the password characteristic."""


class Toggle(StrEnum):
    """On/off settings, in the order the status frame reports them."""

    TUBE_1 = "tube_1"
    TUBE_2 = "tube_2"
    TUBE_3 = "tube_3"
    TUBE_4 = "tube_4"
    TUBE_5 = "tube_5"
    TUBE_6 = "tube_6"
    TIME_SYNC = "time_sync"
    ALARM_1 = "alarm_1"
    ALARM_2 = "alarm_2"
    SCHEDULE = "schedule"
    AUTO_BRIGHTNESS = "auto_brightness"
    HOUR_12 = "hour_12"
    AMBIENT_LIGHT = "ambient_light"
    HOURLY_CHIME = "hourly_chime"
    ENGLISH_VOICE = "english_voice"
    SENSOR_SWITCH = "sensor_switch"


TOGGLE_IDS: Final[Mapping[Toggle, str]] = MappingProxyType(
    dict(zip(Toggle, "1234567890ABCDEF", strict=True))
)
"""The character after "S" in each toggle command."""


class Key(StrEnum):
    """The buttons of the clock's IR remote, pressed over Bluetooth.

    PREVIOUS, MODE and NEXT sit under the colour slider in the original app: by the remote's
    layout (keys 1, 2, 3 control the backlight) they step the backlight colour and its mode
    (inferred from the manual, unverified). The rest match the remote's ON-OFF, BACK, +, SET, OK, −.
    """

    PREVIOUS = "previous"
    MODE = "mode"
    NEXT = "next"
    POWER = "power"
    BACK = "back"
    PLUS = "plus"
    SET = "set"
    OK = "ok"
    MINUS = "minus"


KEY_CODES: Final[Mapping[Key, str]] = MappingProxyType(
    {key: f"K{index:02d}" for index, key in enumerate(Key, start=1)}
)


class TimeSetting(StrEnum):
    """Times the clock stores, in the order the status frame reports them."""

    ALARM_1 = "alarm_1"
    ALARM_2 = "alarm_2"
    POWER_ON = "power_on"
    POWER_OFF = "power_off"


TIME_FORMATS: Final[Mapping[TimeSetting, tuple[str, str]]] = MappingProxyType(
    {
        TimeSetting.ALARM_1: ("A", "A"),
        TimeSetting.ALARM_2: ("A", "B"),
        TimeSetting.POWER_ON: ("T", "O"),
        TimeSetting.POWER_OFF: ("T", "C"),
    }
)
"""(prefix, suffix) around HH:MM in each time command."""


class AuthResult(IntEnum):
    """The single byte the password characteristic notifies after a password is written."""

    OK = 0
    WRONG_PASSWORD = 1
    PASSWORD_CHANGED = 2
    PASSWORD_REMOVED = 3


AUTH_ACCEPTED: Final = frozenset({AuthResult.OK, AuthResult.PASSWORD_CHANGED})


@dataclass(frozen=True, slots=True)
class ClockStatus:
    """Everything one status frame reports."""

    rgb: RGB
    toggles: Mapping[Toggle, bool]
    #: None for a time the clock holds but no clock could show (see `parse_status`).
    times: Mapping[TimeSetting, time | None]

    def with_rgb(self, rgb: RGB) -> ClockStatus:
        return replace(self, rgb=rgb)

    def with_toggle(self, toggle: Toggle, on: bool) -> ClockStatus:
        return replace(self, toggles=MappingProxyType({**self.toggles, toggle: on}))

    def with_time(self, setting: TimeSetting, value: time) -> ClockStatus:
        return replace(self, times=MappingProxyType({**self.times, setting: value}))


def _swap_red_green(rgb: RGB) -> RGB:
    """The clock lights green from the "R" field and red from the "G" field (observed on an
    IN1204M; the original app sent them unswapped, so its slider showed the wrong colours too).
    Swapping is its own inverse: it turns a colour into the fields, and the fields into a colour.
    """
    first, second, third = rgb
    return (second, first, third)


def encode_color(rgb: RGB) -> str:
    """Red `(255, 0, 0)` → `R000-G255-B000`: see `_swap_red_green`."""
    if any(not 0 <= channel <= CHANNEL_MAX for channel in rgb):
        raise ValueError(f"colour channels must be 0…{CHANNEL_MAX}: {rgb}")
    r_field, g_field, b_field = _swap_red_green(rgb)
    return f"R{r_field:03d}-G{g_field:03d}-B{b_field:03d}"


def encode_toggle(toggle: Toggle, on: bool) -> str:
    return f"S{TOGGLE_IDS[toggle]}{int(on)}"


def encode_key(key: Key) -> str:
    return KEY_CODES[key]


def encode_time(setting: TimeSetting, value: time) -> str:
    """Alarm 1 at 07:05 → `A07:05A`; power-off at 18:30 → `T18:30C`."""
    prefix, suffix = TIME_FORMATS[setting]
    return f"{prefix}{value.hour:02d}:{value.minute:02d}{suffix}"


def encode_time_sync(now: datetime) -> str:
    """`$YYYY-MM-DD;HH:MM:SS;0W`, W the weekday with Sunday as 0 (JavaScript's getDay)."""
    weekday = now.isoweekday() % 7
    return f"${now:%Y-%m-%d;%H:%M:%S};0{weekday}"


def encode_auth(old_password: str, new_password: str) -> str:
    """The password write: the current password, then the one to keep (the same to just log in)."""
    for password in (old_password, new_password):
        validate_password(password)
    return old_password + new_password


def validate_password(password: str) -> None:
    if len(password) != PASSWORD_LENGTH or not password.isascii() or not password.isdigit():
        raise ValueError(f"the password must be {PASSWORD_LENGTH} digits")


def parse_auth_result(data: bytes) -> AuthResult:
    if len(data) != 1:
        raise AuthResultError(f"expected one byte, got {data!r}")
    try:
        return AuthResult(data[0])
    except ValueError as err:
        raise AuthResultError(f"unknown password result {data[0]}") from err


def hue_to_rgb(position: int) -> RGB:
    """A position on the original 0…1530 colour slider → the fully saturated colour it sends."""
    if not 0 <= position <= HUE_POSITIONS:
        raise ValueError(f"hue position must be 0…{HUE_POSITIONS}: {position}")
    segment = min(position // HUE_SEGMENT, 5)
    offset = position - segment * HUE_SEGMENT
    falling = CHANNEL_MAX - offset
    return (
        (CHANNEL_MAX, offset, 0),
        (falling, CHANNEL_MAX, 0),
        (0, CHANNEL_MAX, offset),
        (0, falling, CHANNEL_MAX),
        (offset, 0, CHANNEL_MAX),
        (CHANNEL_MAX, 0, falling),
    )[segment]


_COLOR_FIELD = re.compile(r"R(\d{3})-G(\d{3})-B(\d{3})")
_TIME_FIELD = re.compile(r"[AT](\d{2}):(\d{2})[ABOC]")
_COLOR_LENGTH: Final = 14
_TOGGLE_LENGTH: Final = 3
_TIME_LENGTH: Final = 7
_TOGGLES_START: Final = _COLOR_LENGTH
_TIMES_START: Final = _TOGGLES_START + len(Toggle) * _TOGGLE_LENGTH


def parse_status(frame: str) -> ClockStatus:
    """Decode a complete status frame: the clock's current settings, written as the commands that
    would set them (`R…-G…-B…`, sixteen `S<id><0|1>`, four times) and closed by `CSS`.

    A time no clock could show is None: a never-set power-off time read `T44:29C` on an IN1204M.
    """
    if (
        len(frame) != STATUS_LENGTH
        or not frame.startswith(STATUS_PREFIX)
        or not frame.endswith(STATUS_TERMINATOR)
    ):
        raise StatusFrameError(f"not a status frame: {frame!r}")

    color = _COLOR_FIELD.fullmatch(frame[:_COLOR_LENGTH])
    if not color:
        raise StatusFrameError(f"bad colour field: {frame[:_COLOR_LENGTH]!r}")
    fields: RGB = (int(color[1]), int(color[2]), int(color[3]))
    if any(channel > CHANNEL_MAX for channel in fields):
        raise StatusFrameError(f"bad colour field: {frame[:_COLOR_LENGTH]!r}")
    rgb = _swap_red_green(fields)

    toggles: dict[Toggle, bool] = {}
    for index, toggle in enumerate(Toggle):
        start = _TOGGLES_START + index * _TOGGLE_LENGTH
        # As the original app: the third character, "0" for off and anything else for on.
        toggles[toggle] = frame[start + 2] != "0"

    times: dict[TimeSetting, time | None] = {}
    for index, setting in enumerate(TimeSetting):
        start = _TIMES_START + index * _TIME_LENGTH
        field = frame[start : start + _TIME_LENGTH]
        match = _TIME_FIELD.fullmatch(field)
        if not match:
            raise StatusFrameError(f"bad {setting} field: {field!r}")
        try:
            times[setting] = time(int(match[1]), int(match[2]))
        except ValueError:
            times[setting] = None

    return ClockStatus(
        rgb=rgb,
        toggles=MappingProxyType(toggles),
        times=MappingProxyType(times),
    )


class StatusAssembler:
    """Rebuilds status frames from notifications.

    A frame is 93 bytes, more than one notification carries at the default MTU (20 bytes), so it
    arrives in pieces. Bytes before a frame's "R", and any 93 bytes from an "R" that do not end in
    "CSS", are dropped so one corrupted piece cannot wedge the stream.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[str]:
        """Add one notification; return the frames it completes (usually none or one)."""
        self._buffer += chunk.decode("ascii", errors="replace")
        frames: list[str] = []
        while (start := self._buffer.find(STATUS_PREFIX)) != -1:
            self._buffer = self._buffer[start:]
            if len(self._buffer) < STATUS_LENGTH:
                return frames
            candidate = self._buffer[:STATUS_LENGTH]
            if candidate.endswith(STATUS_TERMINATOR):
                frames.append(candidate)
                self._buffer = self._buffer[STATUS_LENGTH:]
            else:
                self._buffer = self._buffer[1:]
        self._buffer = ""
        return frames

    def reset(self) -> None:
        self._buffer = ""
