"""Runtime storage for time-controlled iNELS shutters."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

DATA_KEY = f"{DOMAIN}_shutter_runtime_manager"
STORAGE_KEY = f"{DOMAIN}.shutter_runtime"
STORAGE_VERSION = 1

# Zero is intentional: until the installer fills in the real times,
# automatic timed movement is blocked instead of guessing a duration.
DEFAULT_CHANNEL: dict[str, float | None] = {
    "travel_up": 0.0,
    "travel_down": 0.0,
    "tilt_up": 0.0,
    "tilt_down": 0.0,
    "position": None,
    "tilt": None,
}


class ShutterRuntimeManager:
    """Persist JA3 timing configuration and estimated positions."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store = Store(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY,
            atomic_writes=True,
        )
        self.data: dict[str, Any] = {"devices": {}}
        self._loaded = False
        self._load_lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load persisted data once."""
        if self._loaded:
            return

        async with self._load_lock:
            if self._loaded:
                return

            stored = await self.store.async_load()
            if isinstance(stored, dict):
                self.data = stored

            self.data.setdefault("devices", {})
            self._loaded = True

    def _channel(self, device_id: str, index: int) -> dict[str, Any]:
        devices = self.data.setdefault("devices", {})
        device = devices.setdefault(device_id, {})
        channel = device.setdefault(str(index), deepcopy(DEFAULT_CHANNEL))

        for key, value in DEFAULT_CHANNEL.items():
            channel.setdefault(key, value)

        return channel

    def get_value(self, device_id: str, index: int, key: str) -> float | None:
        """Return one channel value."""
        return self._channel(device_id, index).get(key)

    def set_value(
        self,
        device_id: str,
        index: int,
        key: str,
        value: float | None,
        *,
        persist: bool = True,
    ) -> None:
        """Set one channel value."""
        self._channel(device_id, index)[key] = value

        if persist:
            self.async_schedule_save()

    def async_schedule_save(self) -> None:
        """Debounce writes to Home Assistant storage."""
        self.store.async_delay_save(
            lambda: deepcopy(self.data),
            1.0,
        )


async def async_get_shutter_manager(hass: HomeAssistant) -> ShutterRuntimeManager:
    """Return the shared shutter runtime manager."""
    manager = hass.data.get(DATA_KEY)

    if manager is None:
        manager = ShutterRuntimeManager(hass)
        hass.data[DATA_KEY] = manager

    await manager.async_load()
    return manager