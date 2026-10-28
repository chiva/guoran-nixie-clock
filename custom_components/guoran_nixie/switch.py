"""The clock's sixteen on/off settings, less the tube backlights a 4-tube model lacks."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import GuoranConfigEntry, GuoranCoordinator
from .entity import GuoranEntity, entity_unique_id
from .protocol import TUBES, Toggle, encode_toggle, tube_count

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
    # The advertised name tells the model; the entry's title is only a fallback, as users rename it.
    missing = TUBES[tube_count(coordinator.client.name or entry.title) :]
    registry = er.async_get(hass)
    for toggle in missing:
        # Created by a version that gave every clock six tubes.
        unique_id = entity_unique_id(coordinator.client.address, toggle.value)
        if entity_id := registry.async_get_entity_id(Platform.SWITCH, DOMAIN, unique_id):
            registry.async_remove(entity_id)
    async_add_entities(
        GuoranSwitch(coordinator, toggle) for toggle in Toggle if toggle not in missing
    )


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
