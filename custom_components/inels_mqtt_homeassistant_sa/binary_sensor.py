"""iNELS binary sensor entities."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from inelsmqtt.devices import Device

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .entity import InelsBaseEntity
from .const import (
    DEVICES,
    DOMAIN,
    ICON_BINARY_INPUT,
    ICON_CARD_PRESENT,
    ICON_HEAT_WAVE,
    ICON_PROXIMITY,
    ICON_SNOWFLAKE,
    LOGGER,
    OLD_ENTITIES,
)

COMM_TRACKER = "communication_tracker"


@dataclass
class InelsBinarySensorType:
    """Binary sensor type property description."""

    name: str
    icon: str | None = None
    is_binary_input: bool = False
    indexed: bool = False
    device_class: BinarySensorDeviceClass | None = None


INELS_BINARY_SENSOR_TYPES: dict[str, InelsBinarySensorType] = {
    "low_battery": InelsBinarySensorType(
        name="Battery",
        device_class=BinarySensorDeviceClass.BATTERY,
    ),
    "prox": InelsBinarySensorType(
        name="Proximity Sensor",
        icon=ICON_PROXIMITY,
        device_class=BinarySensorDeviceClass.MOVING,
    ),
    "input": InelsBinarySensorType(
        name="Binary input sensor",
        icon=ICON_BINARY_INPUT,
        is_binary_input=True,
        indexed=True,
    ),
    "heating_out": InelsBinarySensorType(
        name="Heating",
        icon=ICON_HEAT_WAVE,
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    "cooling_out": InelsBinarySensorType(
        name="Cooling",
        icon=ICON_SNOWFLAKE,
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    "detected": InelsBinarySensorType(name="Detector"),
    "tamper": InelsBinarySensorType(
        name="Tamper",
        device_class=BinarySensorDeviceClass.TAMPER,
    ),
    "motion": InelsBinarySensorType(
        name="Motion detector",
        device_class=BinarySensorDeviceClass.MOTION,
    ),
    "flooded": InelsBinarySensorType(
        name="Flooded",
        device_class=BinarySensorDeviceClass.MOISTURE,
    ),
    "card_present": InelsBinarySensorType(
        name="Card present",
        icon=ICON_CARD_PRESENT,
        device_class=BinarySensorDeviceClass.OCCUPANCY,
    ),
}


@dataclass
class InelsBinarySensorEntityDescriptionMixin:
    """Mixin keys."""


@dataclass
class InelsBinarySensorEntityDescription(
    BinarySensorEntityDescription,
    InelsBinarySensorEntityDescriptionMixin,
):
    """Class for describing binary sensor iNELS entities."""


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Load iNELS binary sensors and communication diagnostics."""
    device_list: list[Device] = hass.data[DOMAIN][config_entry.entry_id][DEVICES]
    old_entities: list[str] = (
        hass.data[DOMAIN][config_entry.entry_id][OLD_ENTITIES].get(Platform.BINARY_SENSOR)
        or []
    )

    entities: list[BinarySensorEntity] = []

    for device in device_list:
        for key, type_dict in INELS_BINARY_SENSOR_TYPES.items():
            if not hasattr(device.state, key):
                continue

            binary_sensor_type = (
                InelsBinaryInputSensor
                if type_dict.is_binary_input
                else InelsBinarySensor
            )

            if not type_dict.indexed:
                entities.append(
                    binary_sensor_type(
                        device=device,
                        key=key,
                        index=-1,
                        description=InelsBinarySensorEntityDescription(
                            key=key,
                            name=type_dict.name,
                            icon=type_dict.icon,
                            device_class=type_dict.device_class,
                        ),
                    )
                )
            else:
                for index in range(len(device.state.__dict__[key])):
                    entities.append(
                        binary_sensor_type(
                            device=device,
                            key=key,
                            index=index,
                            description=InelsBinarySensorEntityDescription(
                                key=f"{key}{index}",
                                name=f"{type_dict.name} {index + 1}",
                                icon=type_dict.icon,
                                device_class=type_dict.device_class,
                            ),
                        )
                    )

    tracker = hass.data[DOMAIN][config_entry.entry_id].get(COMM_TRACKER)
    if tracker is not None:
        for device in device_list:
            if tracker.is_physical_device(device):
                entities.append(InelsCommunicationBinarySensor(device, tracker))

        for mac in tracker.gateway_macs:
            entities.extend(
                [
                    InelsGatewayMqttBinarySensor(mac, tracker),
                    InelsGatewayOnlineBinarySensor(mac, tracker),
                    InelsGatewayBusBinarySensor(mac, tracker, 1),
                    InelsGatewayBusBinarySensor(mac, tracker, 2),
                ]
            )

    async_add_entities(entities, True)

    for entity in entities:
        if entity.entity_id in old_entities:
            old_entities.remove(entity.entity_id)

    hass.data[DOMAIN][config_entry.entry_id][Platform.BINARY_SENSOR] = old_entities


class InelsBinarySensor(InelsBaseEntity, BinarySensorEntity):
    """The platform class for binary sensors for Home Assistant."""

    entity_description: InelsBinarySensorEntityDescription

    def __init__(
        self,
        device: Device,
        key: str,
        index: int,
        description: InelsBinarySensorEntityDescription,
    ) -> None:
        super().__init__(device=device, key=key, index=index)
        self.entity_description = description
        self._attr_unique_id = slugify(f"{self._attr_unique_id}_{description.key}")
        self.entity_id = f"{Platform.BINARY_SENSOR}.{self._attr_unique_id}"
        self._attr_name = f"{self._attr_name} {description.name}"

    @property
    def is_on(self) -> bool | None:
        if self.index != -1:
            return self._device.values.ha_value.__dict__[self.key][self.index]
        return self._device.values.ha_value.__dict__[self.key]


class InelsBinaryInputSensor(InelsBaseEntity, BinarySensorEntity):
    """Binary sensor for iNELS binary input values."""

    entity_description: InelsBinarySensorEntityDescription

    def __init__(
        self,
        device: Device,
        key: str,
        index: int,
        description: InelsBinarySensorEntityDescription,
    ) -> None:
        super().__init__(device=device, key=key, index=index)
        self.entity_description = description
        self._attr_unique_id = slugify(f"{self._attr_unique_id}_{description.key}")
        self.entity_id = f"{Platform.BINARY_SENSOR}.{self._attr_unique_id}"
        self._attr_name = f"{self._attr_name} {description.name}"

    @property
    def available(self) -> bool:
        val = self._device.values.ha_value.__dict__[self.key]
        last_val = self._device.last_values.ha_value.__dict__[self.key]
        if self.index != -1:
            val = val[self.index]
            last_val = last_val[self.index]

        if val in [0, 1]:
            return True
        if last_val != val:
            if val == 2:
                LOGGER.warning("%s ALERT", self._attr_unique_id)
            elif val == 3:
                LOGGER.warning("%s TAMPER", self._attr_unique_id)
        return False

    @property
    def is_on(self) -> bool | None:
        if self.index != -1:
            return self._device.values.ha_value.__dict__[self.key][self.index] == 1
        return self._device.values.ha_value.__dict__[self.key] == 1


class _TrackerBinarySensor(BinarySensorEntity):
    """Base class for passive communication diagnostics."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
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


class InelsCommunicationBinarySensor(_TrackerBinarySensor):
    """Communication state of one physical iNELS device."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_has_entity_name = True
    _attr_name = "Communication"

    def __init__(self, device: Device, tracker: Any) -> None:
        super().__init__(tracker, f"device:{device.unique_id}")
        self._device = device
        self._attr_unique_id = slugify(f"{device.unique_id}_communication")
        self.entity_id = f"{Platform.BINARY_SENSOR}.{self._attr_unique_id}"

    @property
    def is_on(self) -> bool | None:
        return self._tracker.device_online(self._device.unique_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        mac = self._tracker.device_mac(self._device)
        return {
            "gateway_mac": mac,
            "communication_mode": "passive_mqtt",
        }

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


class _GatewayBinarySensor(_TrackerBinarySensor):
    _attr_has_entity_name = True

    def __init__(self, mac: str, tracker: Any, suffix: str) -> None:
        super().__init__(tracker, f"gateway:{mac}")
        self._mac = mac
        self._attr_unique_id = slugify(f"gateway_{mac}_{suffix}")
        self.entity_id = f"{Platform.BINARY_SENSOR}.{self._attr_unique_id}"

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


class InelsGatewayMqttBinarySensor(_GatewayBinarySensor):
    """Whether the integration is connected to the MQTT broker."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "MQTT broker connection"

    def __init__(self, mac: str, tracker: Any) -> None:
        super().__init__(mac, tracker, "mqtt")

    @property
    def is_on(self) -> bool:
        return self._tracker.broker_online


class InelsGatewayOnlineBinarySensor(_GatewayBinarySensor):
    """Whether the iNELS gateway reports itself online."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "iNELS gateway"

    def __init__(self, mac: str, tracker: Any) -> None:
        super().__init__(mac, tracker, "online")

    @property
    def is_on(self) -> bool | None:
        return self._tracker.gateway_online(self._mac)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._tracker.gateway_metadata(self._mac)


class InelsGatewayBusBinarySensor(_GatewayBinarySensor):
    """BUS communication state reported by the gateway status payload."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, mac: str, tracker: Any, bus_number: int) -> None:
        super().__init__(mac, tracker, f"bus_{bus_number}")
        self._bus_number = bus_number
        self._attr_name = f"BUS {bus_number}"

    @property
    def is_on(self) -> bool | None:
        return self._tracker.bus_state(self._mac, self._bus_number)
