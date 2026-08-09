"""iNELS cover entity."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import time
from typing import Any

from inelsmqtt.const import JA3_014M, Shutter_state
from inelsmqtt.devices import Device

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityDescription,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import (
    DEVICES,
    DOMAIN,
    ICON_SHUTTER_CLOSED,
    ICON_SHUTTER_OPEN,
    LOGGER,
    OLD_ENTITIES,
)
from .entity import InelsBaseEntity
from .shutter_runtime import (
    ShutterRuntimeManager,
    async_get_shutter_manager,
)


@dataclass
class InelsShutterType:
    """Shutter type property description."""

    name: str
    supported_features: CoverEntityFeature


INELS_SHUTTERS_TYPES: dict[str, InelsShutterType] = {
    "simple_shutters": InelsShutterType(
        "Shutter",
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP,
    ),
    "shutters": InelsShutterType(
        "Shutter",
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP,
    ),
    "shutters_with_pos": InelsShutterType(
        "Shutter",
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION,
    ),
}


JA3_TIMED_FEATURES = (
    CoverEntityFeature.OPEN
    | CoverEntityFeature.CLOSE
    | CoverEntityFeature.STOP
    | CoverEntityFeature.SET_POSITION
    | CoverEntityFeature.OPEN_TILT
    | CoverEntityFeature.CLOSE_TILT
    | CoverEntityFeature.STOP_TILT
    | CoverEntityFeature.SET_TILT_POSITION
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Load iNELS covers."""

    device_list: list[Device] = hass.data[DOMAIN][config_entry.entry_id][DEVICES]
    old_entities: list[str] = (
        hass.data[DOMAIN][config_entry.entry_id][OLD_ENTITIES].get(Platform.COVER)
        or []
    )

    manager = await async_get_shutter_manager(hass)
    entities: list[InelsBaseEntity] = []

    for device in device_list:
        for key, type_dict in INELS_SHUTTERS_TYPES.items():
            if not hasattr(device.state, key):
                continue

            values = device.state.__dict__[key]

            for index in range(len(values)):
                name = (
                    type_dict.name
                    if len(values) == 1
                    else f"{type_dict.name} {index + 1}"
                )
                description_key = key if len(values) == 1 else f"{key}{index}"

                description = InelsCoverEntityDescription(
                    key=description_key,
                    name=name,
                    supported_features=type_dict.supported_features,
                )

                if device.inels_type == JA3_014M and key == "simple_shutters":
                    description.supported_features = JA3_TIMED_FEATURES
                    entities.append(
                        InelsTimedCover(
                            device=device,
                            key=key,
                            index=index,
                            description=description,
                            manager=manager,
                        )
                    )
                else:
                    entities.append(
                        InelsCover(
                            device=device,
                            key=key,
                            index=index,
                            description=description,
                        )
                    )

    async_add_entities(entities, False)

    for entity in entities:
        if entity.entity_id in old_entities:
            old_entities.remove(entity.entity_id)

    hass.data[DOMAIN][config_entry.entry_id][Platform.COVER] = old_entities


@dataclass
class InelsCoverEntityDescription(CoverEntityDescription):
    """Description for iNELS covers."""

    supported_features: CoverEntityFeature | None = None


class InelsCover(InelsBaseEntity, CoverEntity):
    """Standard iNELS cover."""

    entity_description: InelsCoverEntityDescription

    def __init__(
        self,
        device: Device,
        key: str,
        index: int,
        description: InelsCoverEntityDescription,
    ) -> None:
        super().__init__(device=device, key=key, index=index)

        self.entity_description = description
        self._attr_device_class = CoverDeviceClass.SHUTTER
        self._attr_unique_id = slugify(f"{self._attr_unique_id}_{description.key}")
        self.entity_id = f"{Platform.COVER}.{self._attr_unique_id}"
        self._attr_name = f"{self._attr_name} {description.name}"
        self._attr_supported_features = description.supported_features

    @property
    def icon(self) -> str | None:
        """Return cover icon."""
        return ICON_SHUTTER_CLOSED if self.is_closed is True else ICON_SHUTTER_OPEN

    @property
    def is_opening(self) -> bool | None:
        """Return whether the cover is opening."""
        if self.key not in ["shutters", "shutters_with_pos"]:
            return (
                self._device.state.__dict__[self.key][self.index].state
                == Shutter_state.Open
            )
        return None

    @property
    def is_closing(self) -> bool | None:
        """Return whether the cover is closing."""
        if self.key not in ["shutters", "shutters_with_pos"]:
            return (
                self._device.state.__dict__[self.key][self.index].state
                == Shutter_state.Closed
            )
        return None

    @property
    def is_closed(self) -> bool | None:
        """Return whether the cover is closed."""
        return self._device.state.__dict__[self.key][self.index].is_closed

    @property
    def current_cover_position(self) -> int | None:
        """Return current cover position if the device reports it."""
        value = self._device.state.__dict__[self.key][self.index]
        if hasattr(value, "position"):
            return value.position
        return None

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Set cover position for devices which report a real position."""
        value = self._device.state.__dict__[self.key][self.index]

        if not hasattr(value, "position"):
            return

        ha_val = self._device.state
        ha_val.__dict__[self.key][self.index].position = kwargs[ATTR_POSITION]
        ha_val.__dict__[self.key][self.index].set_pos = True
        await self.hass.async_add_executor_job(self._device.set_ha_value, ha_val)

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open cover."""
        ha_val = self._device.state
        ha_val.__dict__[self.key][self.index].state = Shutter_state.Open
        await self.hass.async_add_executor_job(self._device.set_ha_value, ha_val)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close cover."""
        ha_val = self._device.state
        ha_val.__dict__[self.key][self.index].state = Shutter_state.Closed
        await self.hass.async_add_executor_job(self._device.set_ha_value, ha_val)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop cover."""
        ha_val = self._device.state
        stop_state = Shutter_state.Stop_up if self.is_opening else Shutter_state.Stop_down
        ha_val.__dict__[self.key][self.index].state = stop_state
        await self.hass.async_add_executor_job(self._device.set_ha_value, ha_val)


class InelsTimedCover(InelsCover):
    """JA3-014M cover with time-based position and tilt estimation."""

    def __init__(
        self,
        device: Device,
        key: str,
        index: int,
        description: InelsCoverEntityDescription,
        manager: ShutterRuntimeManager,
    ) -> None:
        super().__init__(
            device=device,
            key=key,
            index=index,
            description=description,
        )

        # JA3-014M has no absolute position feedback. The percentage is only
        # a time-based estimate, so Home Assistant must treat the cover state
        # as assumed. This keeps both UP and DOWN controls available even when
        # the estimate is currently 0 % or 100 %.
        self._attr_assumed_state = True

        self._manager = manager
        self._motion_task: asyncio.Task[None] | None = None
        self._motion_direction: str | None = None
        self._motion_mode: str | None = None
        self._motion_started: float | None = None
        self._start_position: float | None = None
        self._start_tilt: float | None = None
        self._target_position: float | None = None
        self._target_tilt: float | None = None
        self._active_command = False
        self._external_motion_seen = False

    def _stored(self, key: str) -> float | None:
        return self._manager.get_value(self._device.unique_id, self.index, key)

    def _set_stored(
        self,
        key: str,
        value: float | None,
        *,
        persist: bool = True,
    ) -> None:
        self._manager.set_value(
            self._device.unique_id,
            self.index,
            key,
            value,
            persist=persist,
        )

    def _time_setting(self, key: str) -> float:
        value = self._stored(key)
        return float(value or 0.0)

    @property
    def current_cover_position(self) -> int | None:
        """Return estimated position: 0 closed, 100 open."""
        value = self._stored("position")
        if value is None:
            return None
        return int(round(max(0.0, min(100.0, float(value)))))

    @property
    def current_cover_tilt_position(self) -> int | None:
        """Return estimated tilt: 0 closed, 100 open."""
        value = self._stored("tilt")
        if value is None:
            return None
        return int(round(max(0.0, min(100.0, float(value)))))

    @property
    def is_closed(self) -> bool | None:
        """Use the time-based estimate only when it is known."""
        position = self.current_cover_position
        if position is None:
            return None
        return position <= 0

    def _callback(self) -> None:
        """Handle MQTT updates and invalidate estimates after external movement."""
        super()._callback()

        if not hasattr(self, "hass"):
            return

        try:
            self.hass.loop.call_soon_threadsafe(self._handle_device_state_update)
        except RuntimeError:
            return

    def _handle_device_state_update(self) -> None:
        """Handle the device update on the Home Assistant event loop."""
        try:
            state = self._device.state.__dict__[self.key][self.index].state
        except (AttributeError, IndexError, KeyError, TypeError):
            return

        moving = state in (Shutter_state.Open, Shutter_state.Closed)

        # If the blind was moved by a wall button / iDM logic / another client,
        # we cannot know how long it moved. Do not lie about the position.
        if moving and not self._active_command:
            if not self._external_motion_seen:
                LOGGER.info(
                    "External movement detected on %s; invalidating estimated position and tilt",
                    self.name,
                )
                self._set_stored("position", None, persist=False)
                self._set_stored("tilt", None, persist=False)
                self._manager.async_schedule_save()
                self._external_motion_seen = True

        elif not moving:
            self._external_motion_seen = False

        self.async_write_ha_state()

    def _actual_direction(self) -> str | None:
        """Read the currently active direction from the relay pair."""
        try:
            state = self._device.state.__dict__[self.key][self.index].state
        except (AttributeError, IndexError, KeyError, TypeError):
            return None

        if state == Shutter_state.Open:
            return "up"
        if state == Shutter_state.Closed:
            return "down"
        return None

    async def _send_direction(self, direction: str) -> None:
        """Start the physical relay pair."""
        ha_val = self._device.state
        ha_val.__dict__[self.key][self.index].state = (
            Shutter_state.Open if direction == "up" else Shutter_state.Closed
        )
        await self.hass.async_add_executor_job(self._device.set_ha_value, ha_val)

    async def _send_stop(self) -> None:
        """Stop both physical relays."""
        direction = self._motion_direction or self._actual_direction()
        ha_val = self._device.state
        ha_val.__dict__[self.key][self.index].state = (
            Shutter_state.Stop_up if direction == "up" else Shutter_state.Stop_down
        )
        await self.hass.async_add_executor_job(self._device.set_ha_value, ha_val)

    def _update_estimate(self) -> None:
        """Update position or tilt from elapsed time."""
        if self._motion_started is None or self._motion_direction is None:
            return

        elapsed = max(0.0, time.monotonic() - self._motion_started)

        if self._motion_mode == "travel":
            if self._start_position is None:
                return

            full_time = self._time_setting(
                "travel_up" if self._motion_direction == "up" else "travel_down"
            )
            if full_time <= 0:
                return

            delta = elapsed / full_time * 100.0
            value = (
                self._start_position + delta
                if self._motion_direction == "up"
                else self._start_position - delta
            )
            self._set_stored(
                "position",
                max(0.0, min(100.0, value)),
                persist=False,
            )

        elif self._motion_mode == "tilt":
            if self._start_tilt is None:
                return

            full_time = self._time_setting(
                "tilt_up" if self._motion_direction == "up" else "tilt_down"
            )
            if full_time <= 0:
                return

            delta = elapsed / full_time * 100.0
            value = (
                self._start_tilt + delta
                if self._motion_direction == "up"
                else self._start_tilt - delta
            )
            self._set_stored(
                "tilt",
                max(0.0, min(100.0, value)),
                persist=False,
            )

    def _clear_motion_state(self) -> None:
        self._motion_task = None
        self._motion_direction = None
        self._motion_mode = None
        self._motion_started = None
        self._start_position = None
        self._start_tilt = None
        self._target_position = None
        self._target_tilt = None
        self._active_command = False

    async def _cancel_active_motion(self, *, send_stop: bool) -> None:
        """Cancel active timer and optionally stop the relays."""
        task = self._motion_task

        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        self._update_estimate()

        if send_stop and (
            self._motion_direction is not None or self._actual_direction() is not None
        ):
            await self._send_stop()

        self._manager.async_schedule_save()
        self._clear_motion_state()
        self.async_write_ha_state()

    async def _run_timed_motion(self, duration: float) -> None:
        """Update HA estimate while the relays are active, then stop."""
        completed = False

        try:
            remaining = duration

            while remaining > 0:
                step = min(0.25, remaining)
                await asyncio.sleep(step)
                remaining -= step

                self._update_estimate()
                self.async_write_ha_state()

            if self._motion_mode == "travel" and self._target_position is not None:
                self._set_stored(
                    "position",
                    self._target_position,
                    persist=False,
                )

            if self._motion_mode == "tilt" and self._target_tilt is not None:
                self._set_stored(
                    "tilt",
                    self._target_tilt,
                    persist=False,
                )

            await self._send_stop()
            self._manager.async_schedule_save()
            completed = True

        except asyncio.CancelledError:
            raise

        finally:
            current = asyncio.current_task()
            if completed and self._motion_task is current:
                self._clear_motion_state()
                self.async_write_ha_state()

    async def _start_motion(
        self,
        *,
        direction: str,
        mode: str,
        duration: float,
        target_position: float | None = None,
        target_tilt: float | None = None,
    ) -> None:
        """Start one cancellable timed motion."""
        await self._cancel_active_motion(send_stop=True)

        self._motion_direction = direction
        self._motion_mode = mode
        self._motion_started = time.monotonic()
        self._start_position = self._stored("position")
        self._start_tilt = self._stored("tilt")
        self._target_position = target_position
        self._target_tilt = target_tilt
        self._active_command = True

        await self._send_direction(direction)
        self.async_write_ha_state()

        self._motion_task = self.hass.async_create_task(
            self._run_timed_motion(duration),
            f"iNELS JA3 shutter {self._device.unique_id}/{self.index + 1}",
        )

    async def _move_to_position(self, target: float, *, force_full: bool = False) -> None:
        """Move to an estimated travel position."""
        target = max(0.0, min(100.0, target))
        current = self._stored("position")

        if current is not None and abs(target - float(current)) < 0.5 and not force_full:
            self._set_stored("position", target)
            self.async_write_ha_state()
            return

        if current is None:
            if target not in (0.0, 100.0):
                LOGGER.warning(
                    "%s position is unknown. First fully open or close it to calibrate.",
                    self.name,
                )
                return
            direction = "up" if target == 100.0 else "down"
        else:
            direction = "up" if target > float(current) else "down"
            if force_full:
                direction = "up" if target == 100.0 else "down"

        full_time = self._time_setting(
            "travel_up" if direction == "up" else "travel_down"
        )

        if full_time <= 0:
            LOGGER.warning(
                "%s command ignored: configure Travel time %s first",
                self.name,
                direction,
            )
            return

        if force_full or current is None:
            duration = full_time
        else:
            duration = full_time * abs(target - float(current)) / 100.0

        await self._start_motion(
            direction=direction,
            mode="travel",
            duration=max(0.05, duration),
            target_position=target,
        )

    async def _move_to_tilt(self, target: float, *, force_full: bool = False) -> None:
        """Move to an estimated tilt position."""
        target = max(0.0, min(100.0, target))
        current = self._stored("tilt")

        if current is not None and abs(target - float(current)) < 0.5 and not force_full:
            self._set_stored("tilt", target)
            self.async_write_ha_state()
            return

        if current is None:
            if target not in (0.0, 100.0):
                LOGGER.warning(
                    "%s tilt is unknown. First fully open or close tilt to calibrate.",
                    self.name,
                )
                return
            direction = "up" if target == 100.0 else "down"
        else:
            direction = "up" if target > float(current) else "down"
            if force_full:
                direction = "up" if target == 100.0 else "down"

        full_time = self._time_setting(
            "tilt_up" if direction == "up" else "tilt_down"
        )

        if full_time <= 0:
            LOGGER.warning(
                "%s command ignored: configure Tilt time %s first",
                self.name,
                direction,
            )
            return

        if force_full or current is None:
            duration = full_time
        else:
            duration = full_time * abs(target - float(current)) / 100.0

        await self._start_motion(
            direction=direction,
            mode="tilt",
            duration=max(0.05, duration),
            target_tilt=target,
        )

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Fully open using the complete configured up time and calibrate to 100%."""
        await self._move_to_position(100.0, force_full=True)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Fully close using the complete configured down time and calibrate to 0%."""
        await self._move_to_position(0.0, force_full=True)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop current movement, including movement started outside this entity."""
        await self._cancel_active_motion(send_stop=True)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Set estimated travel position."""
        await self._move_to_position(float(kwargs[ATTR_POSITION]))

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Tilt fully upward and calibrate tilt to 100%."""
        await self._move_to_tilt(100.0, force_full=True)

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Tilt fully downward and calibrate tilt to 0%."""
        await self._move_to_tilt(0.0, force_full=True)

    async def async_stop_cover_tilt(self, **kwargs: Any) -> None:
        """Stop tilt motion."""
        await self._cancel_active_motion(send_stop=True)

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Set estimated tilt position."""
        await self._move_to_tilt(float(kwargs[ATTR_TILT_POSITION]))

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending timer when the entity is removed."""
        if self._motion_task is not None and not self._motion_task.done():
            self._motion_task.cancel()

        await super().async_will_remove_from_hass()