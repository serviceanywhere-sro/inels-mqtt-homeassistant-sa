"""iNELS light platform.

RGB/RGBW handling notes:
- iNELS uses native 0-100 values.
- For DA3-03M/RGBW (type 153), the physical R/G/B/W channel values themselves
  determine visible intensity. HA brightness therefore scales R/G/B/W.
- The Y value is kept at 100 while ON and 0 while OFF.
- OFF explicitly clears R/G/B/W and Y.
- A plain ON after OFF starts as pure white instead of restoring an old color.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from inelsmqtt.devices import Device

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ColorMode,
    LightEntity,
    LightEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .entity import InelsBaseEntity
from .const import (
    DEVICES,
    DOMAIN,
    ICON_FLASH,
    ICON_LIGHT,
    OLD_ENTITIES,
)

RGB_UI_DEBOUNCE_SECONDS = 0.03


@dataclass
class InelsLightAlert:
    """iNELS light alert property description."""

    key: str
    message: str


thermal_alert = InelsLightAlert(
    key="toa",
    message="Thermal overload",
)

current_alert = InelsLightAlert(
    key="coa",
    message="Current overload",
)

dali_comm = InelsLightAlert(
    key="alert_dali_communication",
    message="DALI communication error",
)

dali_power = InelsLightAlert(
    key="alert_dali_power",
    message="DALI power error",
)

aout_current = InelsLightAlert(
    key="aout_coa",
    message="Analog output current overload",
)


@dataclass
class InelsLightType:
    """iNELS light type."""

    name: str
    color_modes: list[ColorMode]
    icon: str = ICON_LIGHT
    alerts: list[InelsLightAlert] | None = None


INELS_LIGHT_TYPES: dict[str, InelsLightType] = {
    "simple_light": InelsLightType(
        name="Light",
        color_modes=[ColorMode.BRIGHTNESS],
    ),
    "light_coa_toa": InelsLightType(
        name="Light",
        color_modes=[ColorMode.BRIGHTNESS],
        alerts=[current_alert, thermal_alert],
    ),
    "dali": InelsLightType(
        name="DALI",
        color_modes=[ColorMode.BRIGHTNESS],
        alerts=[dali_comm, dali_power],
    ),
    "aout": InelsLightType(
        name="Analog output",
        icon=ICON_FLASH,
        color_modes=[ColorMode.BRIGHTNESS],
        alerts=[aout_current],
    ),
    "rgb": InelsLightType(
        name="RGB light",
        color_modes=[ColorMode.RGB],
    ),
    "rgbw": InelsLightType(
        name="RGBW light",
        color_modes=[ColorMode.RGBW],
    ),
    "warm_light": InelsLightType(
        name="Tunable white light",
        color_modes=[ColorMode.COLOR_TEMP],
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Load iNELS lights from config entry."""

    device_list: list[Device] = hass.data[DOMAIN][config_entry.entry_id][DEVICES]

    old_entities: list[str] = (
        hass.data[DOMAIN][config_entry.entry_id][OLD_ENTITIES].get(Platform.LIGHT)
        or []
    )

    entities: list[InelsBaseEntity] = []

    for device in device_list:
        for key, type_dict in INELS_LIGHT_TYPES.items():
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

                entities.append(
                    InelsLight(
                        device=device,
                        key=key,
                        index=index,
                        description=InelsLightDescription(
                            key=description_key,
                            name=name,
                            icon=type_dict.icon,
                            color_modes=type_dict.color_modes,
                            alerts=type_dict.alerts,
                        ),
                    )
                )

    async_add_entities(entities, True)

    for entity in entities:
        if entity.entity_id in old_entities:
            old_entities.remove(entity.entity_id)

    hass.data[DOMAIN][config_entry.entry_id][Platform.LIGHT] = old_entities


@dataclass
class InelsLightDescription(LightEntityDescription):
    """iNELS light description."""

    color_modes: list[ColorMode] = field(default_factory=list)
    alerts: list[InelsLightAlert] | None = None


class InelsLight(InelsBaseEntity, LightEntity):
    """iNELS light."""

    _entity_description: InelsLightDescription

    def __init__(
        self,
        device: Device,
        key: str,
        index: int,
        description: InelsLightDescription,
    ) -> None:
        """Initialize a light."""

        super().__init__(
            device=device,
            key=key,
            index=index,
        )

        self._entity_description = description
        self._last_nonzero_percent: int | None = None

        # Keep the last useful color separately from the device state.
        # Type 153 has to be physically cleared to 0/0/0/0 on OFF, so the
        # device itself cannot be used to remember the previous color.
        self._last_rgb_channels: tuple[int, ...] | None = None

        # RGB/RGBW controls in HA can issue a dense stream of service calls.
        self._rgb_command_revision = 0

        # All RGB/RGBW channels of one physical actuator share one MQTT SET.
        self._rgb_command_lock: asyncio.Lock | None = None
        if key in {"rgb", "rgbw"}:
            lock = getattr(device, "_sa_rgb_command_lock", None)
            if lock is None:
                lock = asyncio.Lock()
                setattr(device, "_sa_rgb_command_lock", lock)
            self._rgb_command_lock = lock

        self._attr_unique_id = slugify(
            f"{self._attr_unique_id}_{description.key}"
        )
        self.entity_id = f"{Platform.LIGHT}.{self._attr_unique_id}"
        self._attr_name = f"{self._attr_name} {description.name}"

        self._attr_supported_color_modes = set(description.color_modes)

        if len(self._attr_supported_color_modes) == 1:
            self._attr_color_mode = next(iter(self._attr_supported_color_modes))

        self._attr_min_color_temp_kelvin = 2700
        self._attr_max_color_temp_kelvin = 6500

        current = self._read_percent()
        if current is not None and current > 0:
            self._last_nonzero_percent = current

        self._remember_color_from_item(self._state_item())

    def _state_item(self) -> Any | None:
        """Return this channel's parsed state object."""

        try:
            state = self._device.state
            if state is None or not hasattr(state, self.key):
                return None

            values = state.__dict__[self.key]

            if self.index < 0 or self.index >= len(values):
                return None

            return values[self.index]

        except (AttributeError, IndexError, KeyError, TypeError):
            return None

    @staticmethod
    def _clamp_percent(value: Any) -> int:
        """Clamp one native channel to 0-100."""

        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 0

    def _read_percent(self) -> int | None:
        """Read brightness/Y on the native iNELS 0-100 scale."""

        item = self._state_item()

        if item is None or not hasattr(item, "brightness"):
            return None

        try:
            value = int(item.brightness)
        except (TypeError, ValueError):
            return None

        return max(0, min(100, value))

    def _remember_current_brightness(self) -> None:
        """Remember a usable ON level."""

        current = self._read_percent()

        if current is not None and current > 0:
            self._last_nonzero_percent = current

    def _restore_brightness(self) -> int:
        """Return brightness/Y to use when an OFF light becomes ON."""

        if self._last_nonzero_percent is not None and self._last_nonzero_percent > 0:
            return self._last_nonzero_percent

        return 100

    def _next_rgb_revision(self) -> int:
        """Invalidate any older queued RGB/RGBW command."""

        self._rgb_command_revision += 1
        return self._rgb_command_revision

    def _rgb_revision_is_current(self, revision: int) -> bool:
        """Return whether this command is still the newest UI request."""

        return revision == self._rgb_command_revision

    @staticmethod
    def _rgb_is_black(item: Any) -> bool:
        """Return True if an RGB item has no visible channel selected."""

        return (
            hasattr(item, "r")
            and hasattr(item, "g")
            and hasattr(item, "b")
            and int(item.r) == 0
            and int(item.g) == 0
            and int(item.b) == 0
        )

    @staticmethod
    def _rgbw_is_black(item: Any) -> bool:
        """Return True if an RGBW item has no visible channel selected."""

        return (
            hasattr(item, "r")
            and hasattr(item, "g")
            and hasattr(item, "b")
            and hasattr(item, "w")
            and int(item.r) == 0
            and int(item.g) == 0
            and int(item.b) == 0
            and int(item.w) == 0
        )

    def _remember_color_from_item(self, item: Any | None) -> None:
        """Remember the last non-zero RGB/RGBW channel values."""

        if item is None:
            return

        if hasattr(item, "w") and all(
            hasattr(item, attr) for attr in ("r", "g", "b")
        ):
            channels = tuple(
                self._clamp_percent(getattr(item, attr))
                for attr in ("r", "g", "b", "w")
            )
            if any(channels):
                self._last_rgb_channels = channels
            return

        if all(hasattr(item, attr) for attr in ("r", "g", "b")):
            channels = tuple(
                self._clamp_percent(getattr(item, attr))
                for attr in ("r", "g", "b")
            )
            if any(channels):
                self._last_rgb_channels = channels

    def _restore_or_default_color(self, item: Any) -> None:
        """Restore the previous color, falling back to white."""

        if hasattr(item, "w"):
            if not self._rgbw_is_black(item):
                return

            if self._last_rgb_channels is not None and len(self._last_rgb_channels) == 4:
                item.r, item.g, item.b, item.w = self._last_rgb_channels
            else:
                item.r = 0
                item.g = 0
                item.b = 0
                item.w = 100
            return

        if hasattr(item, "r") and self._rgb_is_black(item):
            if self._last_rgb_channels is not None and len(self._last_rgb_channels) == 3:
                item.r, item.g, item.b = self._last_rgb_channels
            else:
                item.r = 100
                item.g = 100
                item.b = 100

    @staticmethod
    def _clear_visible_channels(item: Any) -> None:
        """Force physical RGB/RGBW channels to zero."""

        for attr in ("r", "g", "b", "w"):
            if hasattr(item, attr):
                setattr(item, attr, 0)

    @staticmethod
    def _rgbw_channels(item: Any) -> tuple[int, int, int, int]:
        """Return physical RGBW channel levels on the native 0-100 scale."""

        def clamp(value: Any) -> int:
            try:
                return max(0, min(100, int(value)))
            except (TypeError, ValueError):
                return 0

        return (
            clamp(getattr(item, "r", 0)),
            clamp(getattr(item, "g", 0)),
            clamp(getattr(item, "b", 0)),
            clamp(getattr(item, "w", 0)),
        )

    @classmethod
    def _rgbw_level(cls, item: Any) -> int:
        """Return effective RGBW brightness from the strongest physical channel."""

        return max(cls._rgbw_channels(item))

    @classmethod
    def _rgbw_normalized_channels(
        cls,
        item: Any,
    ) -> tuple[int, int, int, int]:
        """Return the current RGBW colour normalized to a 100 % peak."""

        channels = cls._rgbw_channels(item)
        peak = max(channels)
        if peak <= 0:
            return (0, 0, 0, 0)

        return tuple(
            max(0, min(100, round(channel * 100 / peak)))
            for channel in channels
        )

    @staticmethod
    def _normalize_rgbw_values(
        values: tuple[int, int, int, int] | list[int],
    ) -> tuple[int, int, int, int]:
        """Normalize arbitrary RGBW values to a 100 % peak."""

        native = tuple(max(0, min(100, int(v))) for v in values)
        peak = max(native)
        if peak <= 0:
            return (0, 0, 0, 100)

        return tuple(round(v * 100 / peak) for v in native)

    @classmethod
    def _set_rgbw_scaled(
        cls,
        item: Any,
        base_color: tuple[int, int, int, int] | list[int],
        brightness_percent: int,
    ) -> None:
        """Apply colour and HA brightness directly to physical RGBW channels."""

        brightness_percent = max(0, min(100, int(brightness_percent)))

        if brightness_percent <= 0:
            item.r = 0
            item.g = 0
            item.b = 0
            item.w = 0
            item.brightness = 0
            return

        normalized = cls._normalize_rgbw_values(base_color)
        scaled = tuple(
            max(0, min(100, round(v * brightness_percent / 100)))
            for v in normalized
        )

        item.r, item.g, item.b, item.w = scaled
        item.brightness = 100

    @property
    def available(self) -> bool:
        """Return availability."""

        return self._device.values is not None and self._state_item() is not None

    @property
    def is_on(self) -> bool | None:
        """Return whether this light is physically producing output."""

        item = self._state_item()
        if item is None:
            return None

        if self.key == "rgbw":
            level = self._rgbw_level(item)
            if level > 0:
                self._last_nonzero_percent = level
            return level > 0

        percent = self._read_percent()
        if percent is None:
            return None

        if self.key == "rgb":
            self._remember_color_from_item(item)
            visible = not self._rgb_is_black(item)
            if percent > 0 and visible:
                self._last_nonzero_percent = percent
            return percent > 0 and visible

        if percent > 0:
            self._last_nonzero_percent = percent

        return percent > 0

    @property
    def icon(self) -> str | None:
        """Return icon."""

        return self._entity_description.icon

    @property
    def brightness(self) -> int | None:
        """Return Home Assistant brightness 0-255."""

        item = self._state_item()
        if item is None:
            return None

        if self.key == "rgbw":
            percent = self._rgbw_level(item)
            if percent > 0:
                self._last_nonzero_percent = percent
            return round(percent * 255 / 100)

        percent = self._read_percent()
        if percent is None:
            return None

        if self.key == "rgb":
            self._remember_color_from_item(item)

        if percent > 0:
            self._last_nonzero_percent = percent

        return round(percent * 255 / 100)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return RGB color."""

        state = self._state_item()

        if state is not None and hasattr(state, "r"):
            self._remember_color_from_item(state)
            return tuple(
                int(self._clamp_percent(i) * 255 / 100)
                for i in (state.r, state.g, state.b)
            )

        return None

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        """Return RGBW colour independently of the HA brightness level."""

        state = self._state_item()

        if state is not None and hasattr(state, "w"):
            normalized = self._rgbw_normalized_channels(state)
            return tuple(round(v * 255 / 100) for v in normalized)

        return None

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return color temperature."""

        state = self._state_item()

        if state is not None and hasattr(state, "relative_ct"):
            relative = max(0, min(100, int(state.relative_ct)))

            return int(
                (relative / 100)
                * (self.max_color_temp_kelvin - self.min_color_temp_kelvin)
                + self.min_color_temp_kelvin
            )

        return None

    @property
    def color_mode(self) -> ColorMode | str | None:
        """Return active color mode."""

        state = self._state_item()

        if state is not None:
            if hasattr(state, "w"):
                return ColorMode.RGBW
            if hasattr(state, "r"):
                return ColorMode.RGB
            if hasattr(state, "relative_ct"):
                return ColorMode.COLOR_TEMP
            if hasattr(state, "brightness"):
                return ColorMode.BRIGHTNESS

        return self._attr_color_mode

    async def _async_publish_value(self, ha_val: Any) -> None:
        """Publish one already prepared iNELS value."""

        await self.hass.async_add_executor_job(
            self._device.set_ha_value,
            ha_val,
        )
        self.async_write_ha_state()

    async def _async_rgbw_turn_off(self) -> None:
        """Turn DA3-03M/RGBW fully off."""

        revision = self._next_rgb_revision()

        lock = self._rgb_command_lock
        if lock is None:
            return

        async with lock:
            if not self._rgb_revision_is_current(revision):
                return

            ha_val = self._device.get_value().ha_value
            if ha_val is None or not hasattr(ha_val, self.key):
                return

            item = ha_val.__dict__[self.key][self.index]
            current_level = self._rgbw_level(item)
            if current_level > 0:
                self._last_nonzero_percent = current_level

            self._set_rgbw_scaled(item, (0, 0, 0, 0), 0)
            await self._async_publish_value(ha_val)

    async def _async_rgbw_turn_on(self, kwargs: dict[str, Any]) -> None:
        """Handle DA3-03M/RGBW colour and brightness independently."""

        revision = self._next_rgb_revision()

        if any(
            key in kwargs
            for key in (
                ATTR_BRIGHTNESS,
                ATTR_RGB_COLOR,
                ATTR_RGBW_COLOR,
            )
        ):
            await asyncio.sleep(RGB_UI_DEBOUNCE_SECONDS)
            if not self._rgb_revision_is_current(revision):
                return

        lock = self._rgb_command_lock
        if lock is None:
            return

        async with lock:
            if not self._rgb_revision_is_current(revision):
                return

            ha_val = self._device.get_value().ha_value
            if ha_val is None or not hasattr(ha_val, self.key):
                return

            item = ha_val.__dict__[self.key][self.index]
            current_level = self._rgbw_level(item)
            explicit_brightness = ATTR_BRIGHTNESS in kwargs

            if explicit_brightness:
                ha_brightness = max(0, min(255, int(kwargs[ATTR_BRIGHTNESS])))
                target_level = round(ha_brightness * 100 / 255)
            else:
                target_level = current_level if current_level > 0 else 100

            if target_level <= 0:
                self._set_rgbw_scaled(item, (0, 0, 0, 0), 0)
                await self._async_publish_value(ha_val)
                return

            if ATTR_RGBW_COLOR in kwargs:
                rgbw = kwargs[ATTR_RGBW_COLOR]
                requested = tuple(
                    round(max(0, min(255, int(v))) * 100 / 255)
                    for v in rgbw
                )
                base_color = self._normalize_rgbw_values(requested)

            elif ATTR_RGB_COLOR in kwargs:
                rgb = kwargs[ATTR_RGB_COLOR]
                requested = (
                    round(max(0, min(255, int(rgb[0]))) * 100 / 255),
                    round(max(0, min(255, int(rgb[1]))) * 100 / 255),
                    round(max(0, min(255, int(rgb[2]))) * 100 / 255),
                    0,
                )
                base_color = self._normalize_rgbw_values(requested)

            elif current_level > 0:
                base_color = self._rgbw_normalized_channels(item)

            else:
                base_color = (0, 0, 0, 100)

            if not kwargs and current_level > 0:
                return

            self._set_rgbw_scaled(item, base_color, target_level)
            self._last_nonzero_percent = target_level
            await self._async_publish_value(ha_val)

    async def _async_rgb_turn_off(self) -> None:
        """Turn RGB/RGBW fully off and remember its previous state."""

        revision = self._next_rgb_revision()

        lock = self._rgb_command_lock
        if lock is None:
            return

        async with lock:
            if not self._rgb_revision_is_current(revision):
                return

            ha_val = self._device.get_value().ha_value

            if ha_val is None or not hasattr(ha_val, self.key):
                return

            item = ha_val.__dict__[self.key][self.index]

            # Save the state BEFORE clearing the physical channels.
            try:
                current_percent = int(item.brightness)
            except (AttributeError, TypeError, ValueError):
                current_percent = 0

            if current_percent > 0:
                self._last_nonzero_percent = max(1, min(100, current_percent))

            self._remember_color_from_item(item)

            # Important for DA3-03M/RGBW:
            # brightness/Y=0 alone is not a reliable physical OFF. Clear the
            # actual R/G/B/W outputs as well.
            self._clear_visible_channels(item)
            item.brightness = 0

            await self._async_publish_value(ha_val)

    async def _async_rgb_turn_on(self, kwargs: dict[str, Any]) -> None:
        """Apply the newest RGB/RGBW UI command without stale replays."""

        revision = self._next_rgb_revision()

        is_adjustment = any(
            key in kwargs
            for key in (
                ATTR_BRIGHTNESS,
                ATTR_RGB_COLOR,
                ATTR_RGBW_COLOR,
            )
        )

        if is_adjustment:
            await asyncio.sleep(RGB_UI_DEBOUNCE_SECONDS)
            if not self._rgb_revision_is_current(revision):
                return

        lock = self._rgb_command_lock
        if lock is None:
            return

        async with lock:
            if not self._rgb_revision_is_current(revision):
                return

            ha_val = self._device.get_value().ha_value

            if ha_val is None or not hasattr(ha_val, self.key):
                return

            item = ha_val.__dict__[self.key][self.index]

            color_changed = False
            explicit_brightness = ATTR_BRIGHTNESS in kwargs
            any_change = False

            if ATTR_RGB_COLOR in kwargs:
                rgb = kwargs[ATTR_RGB_COLOR]
                item.r = round(rgb[0] * 100 / 255)
                item.g = round(rgb[1] * 100 / 255)
                item.b = round(rgb[2] * 100 / 255)
                color_changed = True
                any_change = True
                self._remember_color_from_item(item)

            if ATTR_RGBW_COLOR in kwargs:
                rgbw = kwargs[ATTR_RGBW_COLOR]
                item.r = round(rgbw[0] * 100 / 255)
                item.g = round(rgbw[1] * 100 / 255)
                item.b = round(rgbw[2] * 100 / 255)
                item.w = round(rgbw[3] * 100 / 255)
                color_changed = True
                any_change = True
                self._remember_color_from_item(item)

            if explicit_brightness:
                ha_brightness = max(
                    0,
                    min(255, int(kwargs[ATTR_BRIGHTNESS])),
                )
                percent = round(ha_brightness * 100 / 255)
                any_change = True

                if percent <= 0:
                    # HA may express OFF as light.turn_on(brightness=0).
                    # Treat that exactly like turn_off.
                    self._remember_color_from_item(item)
                    self._clear_visible_channels(item)
                    item.brightness = 0
                    await self._async_publish_value(ha_val)
                    return

                self._restore_or_default_color(item)
                item.brightness = percent
                self._last_nonzero_percent = percent
                self._remember_color_from_item(item)

            if color_changed and not explicit_brightness:
                try:
                    current_percent = int(item.brightness)
                except (AttributeError, TypeError, ValueError):
                    current_percent = 0

                if current_percent <= 0:
                    restored = self._restore_brightness()
                    item.brightness = restored
                    self._last_nonzero_percent = restored

            if not any_change:
                try:
                    current_percent = int(item.brightness)
                except (AttributeError, TypeError, ValueError):
                    current_percent = 0

                visible = (
                    not self._rgbw_is_black(item)
                    if hasattr(item, "w")
                    else not self._rgb_is_black(item)
                )

                if current_percent > 0 and visible:
                    return

                self._restore_or_default_color(item)
                restored = self._restore_brightness()
                item.brightness = restored
                self._last_nonzero_percent = restored
                self._remember_color_from_item(item)

            await self._async_publish_value(ha_val)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn light off."""

        if self.key == "rgbw":
            await self._async_rgbw_turn_off()
            return

        if self.key == "rgb":
            await self._async_rgb_turn_off()
            return

        self._remember_current_brightness()

        ha_val = self._device.get_value().ha_value

        if ha_val is None or not hasattr(ha_val, self.key):
            return

        item = ha_val.__dict__[self.key][self.index]
        item.brightness = 0

        await self._async_publish_value(ha_val)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn light on or change brightness/color."""

        if self.key == "rgbw":
            await self._async_rgbw_turn_on(dict(kwargs))
            return

        if self.key == "rgb":
            await self._async_rgb_turn_on(dict(kwargs))
            return

        ha_val = self._device.get_value().ha_value

        if ha_val is None or not hasattr(ha_val, self.key):
            return

        item = ha_val.__dict__[self.key][self.index]

        color_changed = False
        explicit_brightness = ATTR_BRIGHTNESS in kwargs
        any_change = False

        if ATTR_RGB_COLOR in kwargs:
            rgb = kwargs[ATTR_RGB_COLOR]
            item.r = round(rgb[0] * 100 / 255)
            item.g = round(rgb[1] * 100 / 255)
            item.b = round(rgb[2] * 100 / 255)
            color_changed = True
            any_change = True

        if ATTR_RGBW_COLOR in kwargs:
            rgbw = kwargs[ATTR_RGBW_COLOR]
            item.r = round(rgbw[0] * 100 / 255)
            item.g = round(rgbw[1] * 100 / 255)
            item.b = round(rgbw[2] * 100 / 255)
            item.w = round(rgbw[3] * 100 / 255)
            color_changed = True
            any_change = True

        if explicit_brightness:
            ha_brightness = max(
                0,
                min(255, int(kwargs[ATTR_BRIGHTNESS])),
            )
            percent = round(ha_brightness * 100 / 255)
            item.brightness = percent
            any_change = True

            if percent > 0:
                self._last_nonzero_percent = percent

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            color_temp = max(
                self.min_color_temp_kelvin,
                min(
                    self.max_color_temp_kelvin,
                    int(kwargs[ATTR_COLOR_TEMP_KELVIN]),
                ),
            )

            item.relative_ct = round(
                100
                * (color_temp - self.min_color_temp_kelvin)
                / (
                    self.max_color_temp_kelvin
                    - self.min_color_temp_kelvin
                )
            )
            any_change = True

        if color_changed and not explicit_brightness:
            try:
                current_percent = int(item.brightness)
            except (AttributeError, TypeError, ValueError):
                current_percent = 0

            if current_percent <= 0:
                restored = self._restore_brightness()
                item.brightness = restored
                self._last_nonzero_percent = restored

        if not any_change:
            item.brightness = 100
            self._last_nonzero_percent = 100

        await self._async_publish_value(ha_val)
