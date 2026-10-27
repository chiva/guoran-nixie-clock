"""The tubes' RGB backlight.

The original app only ever sent fully saturated colours (its slider walks the hue wheel), so the
light exposes hue only: saturation and brightness from Home Assistant are ignored. Black,
`R000-G000-B000`, stands for off.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_HS_COLOR, LightEntity
from homeassistant.components.light.const import ColorMode
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import color as color_util

from .const import DEFAULT_HUE_POSITION
from .coordinator import GuoranConfigEntry, GuoranCoordinator
from .entity import GuoranEntity
from .protocol import BLACK, HUE_POSITIONS, RGB, encode_color, hue_to_rgb

PARALLEL_UPDATES = 1

DEGREES: float = 360


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GuoranConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([GuoranBacklight(entry.runtime_data)])


def hue_degrees_to_rgb(hue: float) -> RGB:
    return hue_to_rgb(round(hue / DEGREES * HUE_POSITIONS))


class GuoranBacklight(GuoranEntity, LightEntity):
    _attr_color_mode = ColorMode.HS
    _attr_supported_color_modes = {ColorMode.HS}  # noqa: RUF012 (typed by LightEntity)
    _attr_brightness = 255

    def __init__(self, coordinator: GuoranCoordinator) -> None:
        super().__init__(coordinator, "backlight")
        self._last_rgb = hue_to_rgb(DEFAULT_HUE_POSITION)
        """The colour to restore when turned on without one."""
        self._remember_color()

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.rgb != BLACK

    @property
    def hs_color(self) -> tuple[float, float]:
        return color_util.color_RGB_to_hs(*self._last_rgb)

    async def async_turn_on(self, **kwargs: Any) -> None:
        rgb = (
            hue_degrees_to_rgb(kwargs[ATTR_HS_COLOR][0])
            if ATTR_HS_COLOR in kwargs
            else self._last_rgb
        )
        await self._set(rgb)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(BLACK)

    async def _set(self, rgb: RGB) -> None:
        await self.coordinator.async_command(encode_color(rgb), self.coordinator.data.with_rgb(rgb))

    @callback
    def _handle_coordinator_update(self) -> None:
        self._remember_color()
        super()._handle_coordinator_update()

    def _remember_color(self) -> None:
        if self.coordinator.data.rgb != BLACK:
            self._last_rgb = self.coordinator.data.rgb
