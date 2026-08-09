"""The iNELS integration."""
from __future__ import annotations

from typing import Any

import inelsmqtt
import paho.mqtt.client as paho_mqtt
from inelsmqtt import InelsMqtt
from inelsmqtt.const import MQTT_TIMEOUT
from inelsmqtt.discovery import InelsDiscovery

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


# ---------------------------------------------------------------------------
# Compatibility layer for current paho-mqtt used by Home Assistant.
#
# elkoep-mqtt 0.2.32 still uses:
#
#   mqtt.base62(...)
#   mqtt.Client(client_id, ...)
#
# Current paho-mqtt does not provide base62() and its Client constructor
# changed. This wrapper preserves compatibility.
# ---------------------------------------------------------------------------


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


# Replace the paho module reference used internally by inelsmqtt.
inelsmqtt.mqtt = _PahoCompat()


# ---------------------------------------------------------------------------
# Home Assistant platforms
# ---------------------------------------------------------------------------

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

    # Do not modify ConfigEntry.data directly.
    broker_config = dict(entry.data)

    # CU3 messages are not retained.
    # Give discovery enough time to collect BUS devices,
    # virtual bits and integers.
    broker_config[MQTT_TIMEOUT] = 60

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

    # Test MQTT broker connection.
    test_result = await hass.async_add_executor_job(
        mqtt.test_connection
    )

    if isinstance(test_result, int):
        LOGGER.error(
            "Unable to connect to MQTT broker, error %s",
            test_result,
        )
        return False

    # Make integration data available.
    hass.data.setdefault(
        DOMAIN,
        {},
    )[entry.entry_id] = inels_data

    # -----------------------------------------------------------------------
    # iNELS MQTT discovery
    # -----------------------------------------------------------------------

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

    # -----------------------------------------------------------------------
    # Remember old HA entities.
    #
    # Individual platforms remove recreated entities from this list.
    # Anything remaining afterwards is obsolete.
    # -----------------------------------------------------------------------

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

    hass.data[DOMAIN][entry.entry_id] = (
        inels_data
    )

    # Setup all entity platforms.
    await hass.config_entries.async_forward_entry_setups(
        entry,
        PLATFORMS,
    )

    LOGGER.info(
        "iNELS platform setup complete"
    )

    # -----------------------------------------------------------------------
    # Remove entities which disappeared.
    # -----------------------------------------------------------------------

    remaining_entries = (
        hass.data[DOMAIN][entry.entry_id][
            OLD_ENTITIES
        ]
    )

    for entity_ids in remaining_entries.values():
        for entity_id in entity_ids:
            entity_registry.async_remove(
                entity_id
            )

    # -----------------------------------------------------------------------
    # Remove devices which no longer have any entities.
    # -----------------------------------------------------------------------

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