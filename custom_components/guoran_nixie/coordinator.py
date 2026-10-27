"""Polls the clock's status and carries commands, keeping the entities' state in step."""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import GuoranAuthError, GuoranClient, GuoranConnectionError
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_SYNC_TIME,
    DEFAULT_SCAN_INTERVAL_S,
    DEFAULT_SYNC_TIME,
    DOMAIN,
    TIME_SYNC_INTERVAL,
)
from .protocol import ClockStatus, StatusFrameError, encode_time_sync

_LOGGER = logging.getLogger(__name__)

type GuoranConfigEntry = ConfigEntry[GuoranCoordinator]


class GuoranCoordinator(DataUpdateCoordinator[ClockStatus]):
    """One clock: its last status frame, refreshed every scan interval and after each command."""

    config_entry: GuoranConfigEntry

    def __init__(self, hass: HomeAssistant, entry: GuoranConfigEntry, client: GuoranClient) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_S)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=interval),
            always_update=False,
        )
        self.client = client
        self.sync_time: bool = entry.options.get(CONF_SYNC_TIME, DEFAULT_SYNC_TIME)
        self._time_synced_at: datetime | None = None

    async def _async_update_data(self) -> ClockStatus:
        try:
            if self.sync_time and self._time_sync_due():
                await self._send_time()
            return await self.client.async_fetch_status()
        except GuoranAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="invalid_auth",
                translation_placeholders={"detail": str(err)},
            ) from err
        except GuoranConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"detail": str(err)},
            ) from err
        except StatusFrameError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="bad_status",
                translation_placeholders={"detail": str(err)},
            ) from err

    async def async_command(self, command: str, expected: ClockStatus | None = None) -> None:
        """Send a command; show `expected` straight away, then confirm with a refresh."""
        await self._run(self.client.async_send(command))
        if expected is not None:
            self.async_set_updated_data(expected)
        await self.async_request_refresh()

    async def async_sync_time(self) -> None:
        await self._run(self._send_time())

    async def _send_time(self) -> None:
        now = dt_util.now()
        await self.client.async_send(encode_time_sync(now))
        self._time_synced_at = now

    def _time_sync_due(self) -> bool:
        return (
            self._time_synced_at is None
            or dt_util.now() - self._time_synced_at >= TIME_SYNC_INTERVAL
        )

    async def _run[T](self, operation: Awaitable[T]) -> T:
        """An action the user asked for: failures become errors shown in the frontend."""
        try:
            return await operation
        except GuoranAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="invalid_auth",
                translation_placeholders={"detail": str(err)},
            ) from err
        except GuoranConnectionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"detail": str(err)},
            ) from err
