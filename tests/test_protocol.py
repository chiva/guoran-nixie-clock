"""Encoders and the status parser, against the vectors shared with web/protocol.js."""

from __future__ import annotations

from datetime import datetime, time

import pytest

from custom_components.guoran_nixie.protocol import (
    STATUS_LENGTH,
    AuthResult,
    AuthResultError,
    Key,
    StatusAssembler,
    StatusFrameError,
    TimeSetting,
    Toggle,
    encode_auth,
    encode_color,
    encode_key,
    encode_time,
    encode_time_sync,
    encode_toggle,
    hue_to_rgb,
    parse_auth_result,
    parse_status,
)

from .conftest import VECTORS

STATUS = VECTORS["status"]


@pytest.mark.parametrize("vector", VECTORS["color"], ids=lambda v: v["command"])
def test_encode_color(vector: dict) -> None:
    assert encode_color(tuple(vector["rgb"])) == vector["command"]


@pytest.mark.parametrize("rgb", VECTORS["color_invalid"], ids=str)
def test_encode_color_rejects_out_of_range(rgb: list[int]) -> None:
    with pytest.raises(ValueError, match="colour channels"):
        encode_color((rgb[0], rgb[1], rgb[2]))


@pytest.mark.parametrize("vector", VECTORS["toggle"], ids=lambda v: v["command"])
def test_encode_toggle(vector: dict) -> None:
    assert encode_toggle(Toggle(vector["toggle"]), vector["on"]) == vector["command"]


def test_every_toggle_has_a_distinct_id() -> None:
    commands = {encode_toggle(toggle, True) for toggle in Toggle}
    assert len(commands) == len(Toggle) == 16


@pytest.mark.parametrize("vector", VECTORS["key"], ids=lambda v: v["key"])
def test_encode_key(vector: dict) -> None:
    assert encode_key(Key(vector["key"])) == vector["command"]


@pytest.mark.parametrize("vector", VECTORS["time"], ids=lambda v: v["command"])
def test_encode_time(vector: dict) -> None:
    value = time.fromisoformat(vector["time"])
    assert encode_time(TimeSetting(vector["setting"]), value) == vector["command"]


@pytest.mark.parametrize("vector", VECTORS["time_sync"], ids=lambda v: v["local"])
def test_encode_time_sync_uses_sunday_as_zero(vector: dict) -> None:
    assert encode_time_sync(datetime.fromisoformat(vector["local"])) == vector["command"]


@pytest.mark.parametrize("vector", VECTORS["auth"], ids=lambda v: v["command"])
def test_encode_auth(vector: dict) -> None:
    assert encode_auth(vector["old"], vector["new"]) == vector["command"]


@pytest.mark.parametrize("passwords", VECTORS["auth_invalid"], ids=str)
def test_encode_auth_rejects_anything_but_six_digits(passwords: list[str]) -> None:
    with pytest.raises(ValueError, match="6 digits"):
        encode_auth(*passwords)


@pytest.mark.parametrize("vector", VECTORS["auth_result"], ids=lambda v: v["result"])
def test_parse_auth_result(vector: dict) -> None:
    result = parse_auth_result(bytes([vector["byte"]]))
    assert result is AuthResult[vector["result"].upper()]


@pytest.mark.parametrize("data", VECTORS["auth_result_invalid"], ids=str)
def test_parse_auth_result_rejects_unknown_answers(data: list[int]) -> None:
    with pytest.raises(AuthResultError):
        parse_auth_result(bytes(data))


@pytest.mark.parametrize("vector", VECTORS["hue"], ids=lambda v: str(v["position"]))
def test_hue_to_rgb_matches_the_original_slider(vector: dict) -> None:
    assert hue_to_rgb(vector["position"]) == tuple(vector["rgb"])


@pytest.mark.parametrize("position", [-1, 1531])
def test_hue_to_rgb_rejects_positions_off_the_slider(position: int) -> None:
    with pytest.raises(ValueError, match="hue position"):
        hue_to_rgb(position)


def test_parse_status() -> None:
    status = parse_status(STATUS["frame"])
    expected = STATUS["expected"]
    assert status.rgb == tuple(expected["rgb"])
    assert {toggle.value: on for toggle, on in status.toggles.items()} == expected["toggles"]
    assert {
        setting.value: value.strftime("%H:%M") for setting, value in status.times.items()
    } == expected["times"]


def test_parse_status_reports_a_time_no_clock_could_show_as_unknown() -> None:
    vector = VECTORS["status_unset_time"]
    status = parse_status(vector["frame"])
    assert {
        setting.value: value and value.strftime("%H:%M") for setting, value in status.times.items()
    } == vector["times"]


@pytest.mark.parametrize("vector", VECTORS["status_invalid"], ids=lambda v: v["reason"])
def test_parse_status_rejects_malformed_frames(vector: dict) -> None:
    with pytest.raises(StatusFrameError):
        parse_status(vector["frame"])


def test_status_is_immutable_and_updates_by_copy() -> None:
    status = parse_status(STATUS["frame"])
    changed = (
        status.with_rgb((255, 0, 0))
        .with_toggle(Toggle.TUBE_3, True)
        .with_time(TimeSetting.POWER_OFF, time(22, 0))
    )
    assert changed.rgb == (255, 0, 0)
    assert changed.toggles[Toggle.TUBE_3] is True
    assert changed.times[TimeSetting.POWER_OFF] == time(22, 0)
    assert status == parse_status(STATUS["frame"])
    with pytest.raises(TypeError):
        status.toggles[Toggle.TUBE_3] = True  # type: ignore[index]


def _chunks(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


@pytest.mark.parametrize("size", [1, 7, 20, 93, 200])
def test_assembler_rebuilds_a_frame_from_notifications(size: int) -> None:
    assembler = StatusAssembler()
    frames = [
        frame
        for chunk in _chunks(STATUS["frame"].encode(), size)
        for frame in assembler.feed(chunk)
    ]
    assert frames == [STATUS["frame"]]


def test_assembler_skips_noise_and_a_truncated_frame() -> None:
    assembler = StatusAssembler()
    frame = STATUS["frame"]
    truncated = frame[:40]
    stream = f"garbage{truncated}{frame}{frame}".encode()
    frames = [f for chunk in _chunks(stream, 20) for f in assembler.feed(chunk)]
    assert frames == [frame, frame]


def test_assembler_reset_drops_a_partial_frame() -> None:
    assembler = StatusAssembler()
    assert assembler.feed(STATUS["frame"][:50].encode()) == []
    assembler.reset()
    assert assembler.feed(STATUS["frame"][50:].encode()) == []
    assert assembler.feed(STATUS["frame"].encode()) == [STATUS["frame"]]


def test_assembler_never_mistakes_93_bytes_without_terminator() -> None:
    assembler = StatusAssembler()
    assert assembler.feed(("R" + "x" * (STATUS_LENGTH - 1)).encode()) == []
