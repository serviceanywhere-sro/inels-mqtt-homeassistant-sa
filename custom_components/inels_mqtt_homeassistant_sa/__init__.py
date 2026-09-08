"""The iNELS integration."""
from __future__ import annotations

from typing import Any

import inelsmqtt
import paho.mqtt.client as paho_mqtt
from inelsmqtt import InelsMqtt
from inelsmqtt.const import MQTT_TIMEOUT
from inelsmqtt.discovery import InelsDiscovery
from inelsmqtt.protocols.cu3 import DT_153
from inelsmqtt.utils.common import Formatter

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    BROKER,
    BROKER_CONFIG,
    DEVICES,
    DOMAIN,
    LOGGER,
    OLD_ENTITIES,
)


class _PahoCompat:
    """Compatibility proxy for old inelsmqtt code."""

    @staticmethod
    def base62(number: int, padding: int = 0) -> str:
        """Convert integer to base62."""
        alphabet = (
            "0123456789"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz"
        )

        if number == 0:
            result = "0"
        else:
            chars: list[str] = []
            while number:
                number, remainder = divmod(number, 62)
                chars.append(alphabet[remainder])
            result = "".join(reversed(chars))

        if padding:
            result = result.rjust(padding, "0")

        return result

    @staticmethod
    def Client(
        client_id: str = "",
        *args: Any,
        **kwargs: Any,
    ):
        """Create paho MQTT client using callback API v1."""

        kwargs.setdefault(
            "callback_api_version",
            paho_mqtt.CallbackAPIVersion.VERSION1,
        )
        kwargs["client_id"] = client_id

        return paho_mqtt.Client(
            *args,
            **kwargs,
        )

    def __getattr__(self, name: str) -> Any:
        """Forward all other attributes to paho."""
        return getattr(paho_mqtt, name)


inelsmqtt.mqtt = _PahoCompat()


# ---------------------------------------------------------------------------
# DA3-03M/RGBW (type 153) protocol correction.
#
# Measured CU3 MQTT mapping for LED 3:
#   W = byte 23, B = byte 24, G = byte 29, R = byte 30, Y = byte 31
#
# elkoep-mqtt 0.2.33b3 interprets/sends those four colour positions as
# R, G, B, W. That makes requested white appear as red on LED 3.
# Correct both status parsing and SET payload generation here.
# ---------------------------------------------------------------------------

DT_153.DATA["LED_3"] = [29, 28, 23, 22, 30]


def _dt153_create_inels_set_value(cls, device_value: Any) -> str:
    """Create corrected DA3-03M/RGBW SET payload."""

    led_1, led_2, led_3 = device_value.ha_value.rgbw

    command = [
        0,
        0,
        0,
        0,
        led_1.r,
        led_1.g,
        led_1.b,
        led_1.w,
        0,
        0,
        0,
        0,
        led_1.brightness,
        led_2.r,
        led_2.g,
        led_2.b,
        0,
        0,
        0,
        0,
        led_2.w,
        led_2.brightness,
        led_3.w,          # byte 23 = W3
        led_3.b,          # byte 24 = B3
        0,
        0,
        0,
        0,
        led_3.g,          # byte 29 = G3
        led_3.r,          # byte 30 = R3
        led_3.brightness, # byte 31 = Y3
        0,
    ]

    return Formatter.format_data(command)


DT_153.create_inels_set_value = classmethod(_dt153_create_inels_set_value)


PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.LIGHT,
    Platform.COVER,
    Platform.SENSOR,
    Platform.CLIMATE,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
]


async def _async_config_entry_updated(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reload integration when configuration changes."""

    await hass.config_entries.async_reload(
        entry.entry_id
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Set up iNELS from a config entry."""

    if CONF_HOST not in entry.data:
        LOGGER.error(
            "MQTT broker is not configured"
        )
        return False

    broker_config = dict(entry.data)

    # CU3 messages are not retained.
    # 15 seconds is enough to collect BUS devices,
    # virtual bits and integers on a normally running installation.
    broker_config[MQTT_TIMEOUT] = 15

    inels_data: dict[str, Any] = {
        BROKER_CONFIG: broker_config,
    }

    mqtt: InelsMqtt = await hass.async_add_executor_job(
        InelsMqtt,
        broker_config,
    )

    inels_data[BROKER] = mqtt

    entry.async_on_unload(
        entry.add_update_listener(
            _async_config_entry_updated
        )
    )

    test_result = await hass.async_add_executor_job(
        mqtt.test_connection
    )

    if isinstance(test_result, int):
        LOGGER.error(
            "Unable to connect to MQTT broker, error %s",
            test_result,
        )
        return False

    hass.data.setdefault(
        DOMAIN,
        {},
    )[entry.entry_id] = inels_data

    try:
        discovery = InelsDiscovery(mqtt)

        await hass.async_add_executor_job(
            discovery.discovery
        )

        inels_data[DEVICES] = discovery.devices

    except Exception as exc:
        await hass.async_add_executor_job(
            mqtt.close
        )
        raise ConfigEntryNotReady from exc

    LOGGER.info(
        "Finished iNELS discovery: %d devices found",
        len(inels_data[DEVICES]),
    )

    old_entries: dict[str, list[str]] = {}

    entity_registry = er.async_get(hass)

    registry_entries = (
        er.async_entries_for_config_entry(
            entity_registry,
            entry.entry_id,
        )
    )

    for entity in registry_entries:
        old_entries.setdefault(
            entity.domain,
            [],
        ).append(
            entity.entity_id
        )

    inels_data[OLD_ENTITIES] = old_entries
    hass.data[DOMAIN][entry.entry_id] = inels_data

    await hass.config_entries.async_forward_entry_setups(
        entry,
        PLATFORMS,
    )

    LOGGER.info(
        "iNELS platform setup complete"
    )

    remaining_entries = (
        hass.data[DOMAIN][entry.entry_id][OLD_ENTITIES]
    )

    for entity_ids in remaining_entries.values():
        for entity_id in entity_ids:
            entity_registry.async_remove(
                entity_id
            )

    device_registry = dr.async_get(hass)

    registered_devices = [
        device_entry.id
        for device_entry
        in dr.async_entries_for_config_entry(
            registry=device_registry,
            config_entry_id=entry.entry_id,
        )
    ]

    for device_id in registered_devices:
        if not er.async_entries_for_device(
            entity_registry,
            device_id,
            include_disabled_entities=True,
        ):
            LOGGER.info(
                "Removing device %s because it has no entities",
                device_id,
            )

            device_registry.async_remove_device(
                device_id=device_id
            )

    return True


async def async_reload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reload iNELS integration."""

    await hass.config_entries.async_reload(
        entry.entry_id
    )


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Unload iNELS integration."""

    hass_data = hass.data[DOMAIN][
        entry.entry_id
    ]

    broker: InelsMqtt = hass_data[BROKER]

    broker.unsubscribe_listeners()

    await hass.async_add_executor_job(
        broker.disconnect
    )

    unload_ok = (
        await hass.config_entries.async_unload_platforms(
            entry,
            PLATFORMS,
        )
    )

    if unload_ok:
        hass.data[DOMAIN].pop(
            entry.entry_id,
            None,
        )

        if not hass.data[DOMAIN]:
            hass.data.pop(
                DOMAIN,
                None,
            )

    return unload_ok
