"""The clock's sixteen on/off settings."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import GuoranConfigEntry, GuoranCoordinator
from .entity import GuoranEntity
from .protocol import Toggle, encode_toggle

PARALLEL_UPDATES = 1

PRIMARY_TOGGLES = frozenset(
    {
        Toggle.TUBE_1,
        Toggle.TUBE_2,
        Toggle.TUBE_3,
        Toggle.TUBE_4,
        Toggle.TUBE_5,
        Toggle.TUBE_6,
        Toggle.ALARM_1,
        Toggle.ALARM_2,
        Toggle.SCHEDULE,
    }
)
"""Shown as controls; the rest are configuration."""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GuoranConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(GuoranSwitch(coordinator, toggle) for toggle in Toggle)


class GuoranSwitch(GuoranEntity, SwitchEntity):
    def __init__(self, coordinator: GuoranCoordinator, toggle: Toggle) -> None:
        super().__init__(coordinator, toggle.value)
        self._toggle = toggle
        if toggle not in PRIMARY_TOGGLES:
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.toggles[self._toggle]

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:
        await self.coordinator.async_command(
            encode_toggle(self._toggle, on), self.coordinator.data.with_toggle(self._toggle, on)
        )
