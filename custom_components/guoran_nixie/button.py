"""The clock's front-panel buttons, and setting its time from Home Assistant."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import GuoranConfigEntry, GuoranCoordinator
from .entity import GuoranEntity
from .protocol import Key

PARALLEL_UPDATES = 1

DUPLICATE_KEYS = frozenset({Key.PREVIOUS})
"""Keys that do what another does (PREVIOUS steps forward through the colours, as NEXT does):
disabled by default, so the device page shows each action once."""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GuoranConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            *(GuoranKeyButton(coordinator, key) for key in Key),
            GuoranSyncTimeButton(coordinator, "sync_time"),
        ]
    )


class GuoranKeyButton(GuoranEntity, ButtonEntity):
    def __init__(self, coordinator: GuoranCoordinator, key: Key) -> None:
        super().__init__(coordinator, f"key_{key.value}")
        self.key = key
        self._attr_entity_registry_enabled_default = key not in DUPLICATE_KEYS

    async def async_press(self) -> None:
        await self.coordinator.async_press_key(self.key)


class GuoranSyncTimeButton(GuoranEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        await self.coordinator.async_sync_time()
