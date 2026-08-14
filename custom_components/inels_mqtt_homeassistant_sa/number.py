"""iNELS number entities."""
from __future__ import annotations

from dataclasses import dataclass

from inelsmqtt.const import JA3_014M
from inelsmqtt.devices import Device

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, Platform, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import (
    DEVICES,
    DOMAIN,
    ICON_NUMBER,
    OLD_ENTITIES,
)
from .entity import InelsBaseEntity
from .shutter_runtime import (
    ShutterRuntimeManager,
    async_get_shutter_manager,
)


@dataclass
class InelsNumberType:
    """iNELS number type."""

    name: str = "Integer"
    icon: str = ICON_NUMBER


INELS_NUMBER_TYPES: dict[str, InelsNumberType] = {
    "number": InelsNumberType(),
}


@dataclass(frozen=True)
class ShutterTimingDescription:
    """One configurable JA3 timing."""

    key: str
    name: str
    max_value: float


SHUTTER_TIMINGS: tuple[ShutterTimingDescription, ...] = (
    ShutterTimingDescription("travel_up", "Travel time up", 300.0),
    ShutterTimingDescription("travel_down", "Travel time down", 300.0),
    ShutterTimingDescription("tilt_up", "Tilt time up", 30.0),
    ShutterTimingDescription("tilt_down", "Tilt time down", 30.0),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Load iNELS integers and JA3 timing settings."""

    device_list: list[Device] = hass.data[DOMAIN][config_entry.entry_id][DEVICES]
    old_entities: list[str] = (
        hass.data[DOMAIN][config_entry.entry_id][OLD_ENTITIES].get(Platform.NUMBER)
        or []
    )

    manager = await async_get_shutter_manager(hass)
    entities: list[NumberEntity] = []

    # Native iNELS virtual integers.
    for device in device_list:
        for key, type_dict in INELS_NUMBER_TYPES.items():
            if not hasattr(device.state, key):
                continue

            values = device.state.__dict__[key]

            for index, value in enumerate(values):
                entities.append(
                    InelsBusNumber(
                        device=device,
                        key=key,
                        index=index,
                        description=NumberEntityDescription(
                            key=f"{key}_{value.addr}",
                            name=f"{type_dict.name} {value.addr}",
                            icon=type_dict.icon,
                        ),
                    )
                )

    # Four timing settings for every JA3-014M shutter pair.
    for device in device_list:
        if device.inels_type != JA3_014M:
            continue

        if not hasattr(device.state, "simple_shutters"):
            continue

        count = len(device.state.simple_shutters)

        for index in range(count):
            for timing in SHUTTER_TIMINGS:
                entities.append(
                    InelsShutterTimingNumber(
                        device=device,
                        shutter_index=index,
                        timing=timing,
                        manager=manager,
                    )
                )

    async_add_entities(entities, False)

    for entity in entities:
        if entity.entity_id in old_entities:
            old_entities.remove(entity.entity_id)

    hass.data[DOMAIN][config_entry.entry_id][Platform.NUMBER] = old_entities


class InelsBusNumber(InelsBaseEntity, NumberEntity):
    """Native iNELS virtual system integer."""

    entity_description: NumberEntityDescription

    def __init__(
        self,
        device: Device,
        key: str,
        index: int,
        description: NumberEntityDescription,
    ) -> None:
        super().__init__(device=device, key=key, index=index)

        self.entity_description = description
        self._attr_native_max_value = 2147483647
        self._attr_native_min_value = -2147483648
        self._attr_native_step = 1
        self._attr_unique_id = slugify(f"{self._attr_unique_id}_{description.key}")
        self.entity_id = f"{Platform.NUMBER}.{self._attr_unique_id}"
        self._attr_name = f"{self._attr_name} {description.name}"

    @property
    def native_value(self) -> int | None:
        """Return integer value."""
        return self._device.state.__dict__[self.key][self.index].value

    @property
    def icon(self) -> str | None:
        """Return icon."""
        return self.entity_description.icon

    async def async_set_native_value(self, value: float) -> None:
        """Set integer value."""
        ha_val = self._device.state
        ha_val.__dict__[self.key][self.index].value = int(value)
        await self.hass.async_add_executor_job(self._device.set_ha_value, ha_val)


class InelsShutterTimingNumber(NumberEntity):
    """Persistent timing configuration for one JA3-014M shutter."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0.0
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_icon = "mdi:timer-cog-outline"

    # The entity name is relative to the per-shutter Home Assistant device.
    # Renaming the device therefore keeps all four timing settings visually
    # tied to the correct shutter.
    _attr_has_entity_name = True

    def __init__(
        self,
        device: Device,
        shutter_index: int,
        timing: ShutterTimingDescription,
        manager: ShutterRuntimeManager,
    ) -> None:
        self._device = device
        self._shutter_index = shutter_index
        self._timing = timing
        self._manager = manager

        self._attr_native_max_value = timing.max_value
        self._attr_unique_id = slugify(
            f"{device.unique_id}_shutter_{shutter_index + 1}_{timing.key}"
        )
        self.entity_id = f"{Platform.NUMBER}.{self._attr_unique_id}"
        self._attr_name = timing.name

    @property
    def native_value(self) -> float:
        """Return configured time in seconds."""
        value = self._manager.get_value(
            self._device.unique_id,
            self._shutter_index,
            self._timing.key,
        )
        return float(value or 0.0)

    @property
    def device_info(self) -> DeviceInfo:
        """Attach timing setting to the exact JA3 shutter channel."""
        info = self._device.info()
        shutter_number = self._shutter_index + 1
        shutter_device_id = (
            f"{self._device.unique_id}_shutter_{shutter_number}"
        )

        return DeviceInfo(
            identifiers={(DOMAIN, shutter_device_id)},
            manufacturer=info.manufacturer,
            model=info.model_number,
            name=f"{self._device.title} Shutter {shutter_number}",
            sw_version=info.sw_version,
        )

    async def async_set_native_value(self, value: float) -> None:
        """Persist a timing value."""
        self._manager.set_value(
            self._device.unique_id,
            self._shutter_index,
            self._timing.key,
            round(float(value), 1),
        )
        self.async_write_ha_state()