"""iNELS sensor entities."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from inelsmqtt.const import BUS_SENSOR_ERRORS, TEMP_IN, TEMP_OUT
from inelsmqtt.devices import Device

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    LIGHT_LUX,
    PERCENTAGE,
    Platform,
    UnitOfElectricPotential,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .entity import InelsBaseEntity
from .const import (
    DEVICES,
    DOMAIN,
    ICON_CARD_ID,
    ICON_DEW_POINT,
    ICON_FLASH,
    ICON_HUMIDITY,
    ICON_LIGHT_IN,
    ICON_TEMPERATURE,
    LOGGER,
    OLD_ENTITIES,
)

COMM_TRACKER = "communication_tracker"


@dataclass
class InelsSensorType:
    """Sensor type property description."""

    name: str
    icon: str
    unit: str | None
    indexed: bool = False
    raw_sensor_value: bool = False
    device_class: SensorDeviceClass | None = None


INELS_SENSOR_TYPES: dict[str, InelsSensorType] = {
    "temp": InelsSensorType(
        name="Temperature sensor",
        icon=ICON_TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    TEMP_IN: InelsSensorType(
        name="Internal temperature sensor",
        icon=ICON_TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    TEMP_OUT: InelsSensorType(
        name="External temperature sensor",
        icon=ICON_TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    "light_in": InelsSensorType(
        name="Light intensity",
        icon=ICON_LIGHT_IN,
        unit=LIGHT_LUX,
        device_class=SensorDeviceClass.ILLUMINANCE,
    ),
    "ain": InelsSensorType(
        name="Analog temperature sensor",
        icon=ICON_TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    "humidity": InelsSensorType(
        name="Humidity",
        icon=ICON_HUMIDITY,
        unit=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
    ),
    "dewpoint": InelsSensorType(
        name="Dew point",
        icon=ICON_DEW_POINT,
        unit=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    "temps": InelsSensorType(
        name="Temperature sensor",
        icon=ICON_TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        indexed=True,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    "ains": InelsSensorType(
        name="Analog input",
        icon=ICON_FLASH,
        unit=UnitOfElectricPotential.VOLT,
        indexed=True,
        device_class=SensorDeviceClass.VOLTAGE,
    ),
    "card_id": InelsSensorType(
        name="Last card ID",
        icon=ICON_CARD_ID,
        unit=None,
        raw_sensor_value=False,
    ),
}


def _process_value(val: str) -> tuple[float, bool]:
    middle_fs = True
    for char in val[1:-1]:
        if char.capitalize() != "F":
            middle_fs = False

    if (
        middle_fs
        and val[0] == "7"
        and (("A" <= val[-1] <= "F") or val[-1] == "9")
    ):
        last = int(val[-1], 16)
        error = BUS_SENSOR_ERRORS.get(last)
        if error is not None:
            LOGGER.warning(error)
            return (float(int(val, 16)) / 100, True)

    return (float(int(val, 16)) / 100, False)


@dataclass
class InelsSensorDescriptionMixin:
    """Mixin keys."""


@dataclass
class InelsSensorDescription(
    SensorEntityDescription,
    InelsSensorDescriptionMixin,
):
    """Class for describing iNELS sensor entities."""

    raw_sensor_value: bool = False


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Load iNELS sensors and passive last-seen diagnostics."""
    device_list: list[Device] = hass.data[DOMAIN][config_entry.entry_id][DEVICES]
    old_entities: list[str] = (
        hass.data[DOMAIN][config_entry.entry_id][OLD_ENTITIES].get(Platform.SENSOR)
        or []
    )

    entities: list[SensorEntity] = []

    for device in device_list:
        for key, type_dict in INELS_SENSOR_TYPES.items():
            if not hasattr(device.state, key):
                continue

            if type_dict.indexed:
                for index in range(len(device.state.__dict__[key])):
                    entities.append(
                        InelsSensor(
                            device=device,
                            key=key,
                            index=index,
                            description=InelsSensorDescription(
                                key=f"{key}{index}",
                                name=f"{type_dict.name} {index + 1}",
                                icon=type_dict.icon,
                                native_unit_of_measurement=type_dict.unit,
                                raw_sensor_value=type_dict.raw_sensor_value,
                                device_class=type_dict.device_class,
                            ),
                        )
                    )
            else:
                entities.append(
                    InelsSensor(
                        device=device,
                        key=key,
                        index=-1,
                        description=InelsSensorDescription(
                            key=key,
                            name=type_dict.name,
                            icon=type_dict.icon,
                            native_unit_of_measurement=type_dict.unit,
                            raw_sensor_value=type_dict.raw_sensor_value,
                            device_class=type_dict.device_class,
                        ),
                    )
                )

    tracker = hass.data[DOMAIN][config_entry.entry_id].get(COMM_TRACKER)
    if tracker is not None:
        for device in device_list:
            if tracker.is_physical_device(device):
                entities.append(InelsLastCommunicationSensor(device, tracker))

        for mac in tracker.gateway_macs:
            entities.append(InelsGatewayLastCommunicationSensor(mac, tracker))

    async_add_entities(entities, True)

    for entity in entities:
        if entity.entity_id in old_entities:
            old_entities.remove(entity.entity_id)

    hass.data[DOMAIN][config_entry.entry_id][Platform.SENSOR] = old_entities


class InelsSensor(InelsBaseEntity, SensorEntity):
    """Platform class for Home Assistant BUS sensors."""

    entity_description: InelsSensorDescription
    sensor_error: bool = False

    def __init__(
        self,
        device: Device,
        key: str,
        index: int,
        description: InelsSensorDescription,
    ) -> None:
        super().__init__(device=device, key=key, index=index)
        self.entity_description = description
        self._attr_unique_id = slugify(f"{self._attr_unique_id}_{description.key}")
        self.entity_id = f"{Platform.SENSOR}.{self._attr_unique_id}"
        self._attr_name = f"{self._attr_name} {description.name}"

        if self.index != -1:
            val = self._device.state.__dict__[self.key][self.index]
        else:
            val = self._device.state.__dict__[self.key]

        if not self.entity_description.raw_sensor_value and isinstance(val, str):
            val, self.sensor_error = _process_value(val)

        self._attr_device_class = self.entity_description.device_class
        self._attr_icon = self.entity_description.icon
        self._attr_native_value = val

    def _callback(self) -> None:
        super()._callback()

        if self.index != -1:
            val = self._device.state.__dict__[self.key][self.index]
        else:
            val = self._device.state.__dict__[self.key]

        if not self.entity_description.raw_sensor_value and isinstance(val, str):
            val, self.sensor_error = _process_value(val)

        self._attr_native_value = val

    @property
    def available(self) -> bool:
        return (not self.sensor_error) and super().available


class _TrackerTimestampSensor(SensorEntity):
    """Base class for passive last-communication sensors."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, tracker: Any, tracker_key: str) -> None:
        self._tracker = tracker
        self._tracker_key = tracker_key

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._tracker.add_listener(self._tracker_key, self._tracker_updated)
        )

    @callback
    def _tracker_updated(self) -> None:
        self.async_write_ha_state()


class InelsLastCommunicationSensor(_TrackerTimestampSensor):
    """Last fresh MQTT packet received for one physical device."""

    _attr_name = "Last communication"

    def __init__(self, device: Device, tracker: Any) -> None:
        super().__init__(tracker, f"device:{device.unique_id}")
        self._device = device
        self._attr_unique_id = slugify(f"{device.unique_id}_last_communication")
        self.entity_id = f"{Platform.SENSOR}.{self._attr_unique_id}"

    @property
    def native_value(self) -> datetime | None:
        return self._tracker.device_last_seen(self._device.unique_id)

    @property
    def device_info(self) -> DeviceInfo:
        info = self._device.info()
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.unique_id)},
            manufacturer=info.manufacturer,
            model=info.model_number,
            name=self._device.title,
            sw_version=info.sw_version,
        )


class InelsGatewayLastCommunicationSensor(_TrackerTimestampSensor):
    """Last live MQTT traffic received from one iNELS gateway installation."""

    _attr_name = "Last MQTT communication"

    def __init__(self, mac: str, tracker: Any) -> None:
        super().__init__(tracker, f"gateway:{mac}")
        self._mac = mac
        self._attr_unique_id = slugify(f"gateway_{mac}_last_mqtt_communication")
        self.entity_id = f"{Platform.SENSOR}.{self._attr_unique_id}"

    @property
    def native_value(self) -> datetime | None:
        return self._tracker.gateway_last_seen(self._mac)

    @property
    def device_info(self) -> DeviceInfo:
        metadata = self._tracker.gateway_metadata(self._mac)
        return DeviceInfo(
            identifiers={(DOMAIN, f"gateway_{self._mac}")},
            manufacturer="ELKO EP",
            model=metadata.get("model", "iNELS gateway"),
            name=f"iNELS Gateway {self._mac}",
            sw_version=metadata.get("firmware"),
        )
