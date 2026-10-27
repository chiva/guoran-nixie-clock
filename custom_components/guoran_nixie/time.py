"""Alarm times and the scheduled power-on and power-off times."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import GuoranConfigEntry, GuoranCoordinator
from .entity import GuoranEntity
from .protocol import TimeSetting, encode_time

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GuoranConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(GuoranTime(coordinator, setting) for setting in TimeSetting)


class GuoranTime(GuoranEntity, TimeEntity):
    def __init__(self, coordinator: GuoranCoordinator, setting: TimeSetting) -> None:
        super().__init__(coordinator, f"{setting.value}_time")
        self.setting = setting

    @property
    def native_value(self) -> time:
        return self.coordinator.data.times[self.setting]

    async def async_set_value(self, value: time) -> None:
        # The clock keeps minutes only.
        value = value.replace(second=0, microsecond=0)
        await self.coordinator.async_command(
            encode_time(self.setting, value), self.coordinator.data.with_time(self.setting, value)
        )
