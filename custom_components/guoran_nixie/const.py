"""Constants for the Guoran nixie clock integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "guoran_nixie"

CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_SYNC_TIME: Final = "sync_time"

DEFAULT_SCAN_INTERVAL_S: Final = 300
"""Each refresh opens a Bluetooth connection; the clock's state rarely changes on its own."""
MIN_SCAN_INTERVAL_S: Final = 60
MAX_SCAN_INTERVAL_S: Final = 3600
DEFAULT_SYNC_TIME: Final = True
TIME_SYNC_INTERVAL: Final = timedelta(hours=1)
"""How often the clock is set to Home Assistant's time (the original app sent it every second)."""

DEFAULT_HUE_POSITION: Final = 765
"""Cyan, the original app's default colour: used when the light is turned on from black."""

MANUFACTURER: Final = "Guoran"
MODEL: Final = "Nixie tube clock"
