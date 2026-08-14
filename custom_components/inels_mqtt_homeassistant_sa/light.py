"""iNELS light platform.

Includes robust brightness handling for BUS dimmers such as DA3-66M:
- brightness 0 means OFF, never "unavailable"
- plain ON restores the last non-zero brightness seen by this entity
- slider writes 0-100 % to iNELS through elkoep-mqtt
"""
from __future__ import annotations

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

        # Seed the remembered value from the current MQTT state, if non-zero.
        current = self._read_percent()
        if current is not None and current > 0:
            self._last_nonzero_percent = current

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

    def _read_percent(self) -> int | None:
        """Read brightness on the native iNELS 0-100 scale."""

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

    @property
    def available(self) -> bool:
        """Return availability.

        A dimmer level of 0 is a perfectly valid OFF state. It must not make
        the entity unavailable. We therefore consider the light available
        whenever its MQTT payload has been parsed and this channel exists.

        This deliberately does not use legacy device.is_available, because
        newer CU3 MQTT installations may not publish the old per-device
        connected topic reliably.
        """

        return self._device.values is not None and self._state_item() is not None

    @property
    def is_on(self) -> bool | None:
        """Return True when brightness is above zero."""

        percent = self._read_percent()

        if percent is None:
            return None

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

        percent = self._read_percent()

        if percent is None:
            return None

        if percent > 0:
            self._last_nonzero_percent = percent

        return round(percent * 255 / 100)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return RGB color."""

        state = self._state_item()

        if state is not None and hasattr(state, "r"):
            return tuple(
                int(max(0, min(100, i)) * 255 / 100)
                for i in (state.r, state.g, state.b)
            )

        return None

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        """Return RGBW color."""

        state = self._state_item()

        if state is not None and hasattr(state, "w"):
            return tuple(
                int(max(0, min(100, i)) * 255 / 100)
                for i in (state.r, state.g, state.b, state.w)
            )

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

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn light off without losing the previous brightness."""

        self._remember_current_brightness()

        ha_val = self._device.get_value().ha_value

        if ha_val is None or not hasattr(ha_val, self.key):
            return

        ha_val.__dict__[self.key][self.index].brightness = 0

        await self.hass.async_add_executor_job(
            self._device.set_ha_value,
            ha_val,
        )

        # Reflect OFF immediately. The next status packet from CU3 remains
        # authoritative and will update the entity again.
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn light on or set its level."""

        ha_val = self._device.get_value().ha_value

        if ha_val is None or not hasattr(ha_val, self.key):
            return

        item = ha_val.__dict__[self.key][self.index]
        changed = False

        if ATTR_RGB_COLOR in kwargs:
            rgb = kwargs[ATTR_RGB_COLOR]
            item.r = round(rgb[0] * 100 / 255)
            item.g = round(rgb[1] * 100 / 255)
            item.b = round(rgb[2] * 100 / 255)
            changed = True

        if ATTR_RGBW_COLOR in kwargs:
            rgbw = kwargs[ATTR_RGBW_COLOR]
            item.r = round(rgbw[0] * 100 / 255)
            item.g = round(rgbw[1] * 100 / 255)
            item.b = round(rgbw[2] * 100 / 255)
            item.w = round(rgbw[3] * 100 / 255)
            changed = True

        if ATTR_BRIGHTNESS in kwargs:
            percent = round(
                max(0, min(255, int(kwargs[ATTR_BRIGHTNESS])))
                * 100
                / 255
            )

            item.brightness = percent
            changed = True

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
            changed = True

        if not changed:
            # Plain ON switches the dimmer to 100 %.
            item.brightness = 100

        await self.hass.async_add_executor_job(
            self._device.set_ha_value,
            ha_val,
        )

        self.async_write_ha_state()
