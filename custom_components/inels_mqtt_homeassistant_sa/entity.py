"""Base class for iNELS components."""
from __future__ import annotations

from inelsmqtt.const import JA3_014M
from inelsmqtt.devices import Device

from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import DOMAIN, LOGGER


class InelsBaseEntity(Entity):
    """Base Inels device."""

    def __init__(
        self,
        device: Device,
        key: str,
        index: int,
    ) -> None:
        """Init base entity."""

        self._device: Device = device
        self._device_id = self._device.unique_id
        self._attr_name = self._device.title

        self._parent_id = self._device.parent_id
        self._attr_unique_id = self._device_id

        self._key = key
        self._index = index

        self._device.add_ha_callback(
            self.key,
            self.index,
            self._callback,
        )

    async def async_added_to_hass(self) -> None:
        """Add subscription of the data listener."""

        self._device.mqtt.subscribe_listener(
            self._device.state_topic,
            self._device.unique_id,
            self._device.callback,
        )

        self.async_on_remove(
            lambda: LOGGER.info(
                "Entity %s to be removed",
                self.name,
            )
        )

    def _callback(self) -> None:
        """Get data from broker into Home Assistant."""

        self.schedule_update_ha_state()

    @property
    def should_poll(self) -> bool:
        """Need to poll."""

        return False

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info.

        JA3-014M is special: every shutter channel is exposed as a separate
        Home Assistant device. This keeps the cover and all timing settings
        bound to the same physical shutter channel independently of sorting
        or user-facing names.
        """

        info = self._device.info()

        if self._device.inels_type == JA3_014M and self.key == "simple_shutters":
            shutter_number = self.index + 1
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

        return DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    self._device.unique_id,
                )
            },
            manufacturer=info.manufacturer,
            model=info.model_number,
            name=self._device.title,
            sw_version=info.sw_version,
            via_device=(
                DOMAIN,
                self._parent_id,
            ),
        )

    @property
    def available(self) -> bool:
        """Return if entity is available.

        Compatibility fix:
        Old inelsmqtt requires an inels/connected message.
        Newer CU3 MQTT communication may provide state data
        without the connected topic expected by the old library.

        If valid device values have been received, consider
        the entity available.
        """

        return (
            self._device.values is not None
            and super().available
        )

    @property
    def key(self) -> str:
        """Return the referenced variable to read from."""

        return self._key

    @property
    def index(self) -> int:
        """Return variable list index."""

        return self._index