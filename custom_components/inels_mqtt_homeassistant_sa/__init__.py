"""The iNELS integration."""
from __future__ import annotations

from typing import Any

import inelsmqtt
import paho.mqtt.client as paho_mqtt
from inelsmqtt import InelsMqtt
from inelsmqtt.const import LIGHT, MQTT_TIMEOUT
from inelsmqtt.devices import Device
from inelsmqtt.discovery import InelsDiscovery
from inelsmqtt.protocols.cu3 import DT_114, DT_153
from inelsmqtt.utils.common import Formatter

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .communication import CommunicationTracker
from .const import (
    BROKER,
    BROKER_CONFIG,
    DEVICES,
    DOMAIN,
    LOGGER,
    OLD_ENTITIES,
)

COMM_TRACKER = "communication_tracker"


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
# RC3-610M/DALI (type 114) protocol mapping.
#
# Based on ELKO EP "Integrace iNELS do MQTT – BUS", Rev. 2:
# STATUS
#   TEMP1      Data2-3
#   AOUT1-2    Data4-5
#   RE1-8      Data8-15
#   TEMP2      Data18-19
#   DALI1-4    Data20-23
#   DIN1-6     Data24 bits 0-5
#   RE overflow Data25 bits 0-7
#   Alerts     Data26 (bit 6 DALI power, bit 7 DALI communication)
#   DALI5-8    Data28-31
#   DALI9-12   Data36-39
#   DALI13-16  Data44-47
#
# SET
#   Data0-1    AOUT ramps
#   Data2-3    reserved
#   Data4-5    AOUT1-2
#   Data6-7    reserved
#   Data8-15   RE1-8 commands (0x07 ON, 0x06 OFF)
#   Data16-19  DALI1-4 ramps
#   Data20-23  DALI1-4 values
#   Data24-27  DALI5-8 ramps
#   Data28-31  DALI5-8 values
#   Data32-35  DALI9-12 ramps
#   Data36-39  DALI9-12 values
#   Data40-43  DALI13-16 ramps
#   Data44-47  DALI13-16 values
#
# Keep the old elkoep-mqtt dependency, but force its type-114 mapping and
# command construction to the documented byte layout.
# ---------------------------------------------------------------------------

DT_114.DATA.update(
    {
        "temp_in": [2, 3, 18, 19],
        "aout": [4, 5],
        "relay": list(range(8, 16)),
        "dali": [
            20, 21, 22, 23,
            28, 29, 30, 31,
            36, 37, 38, 39,
            44, 45, 46, 47,
        ],
        "din": [24],
        "relay_overflow": [25],
        "alert": [26],
    }
)

# RC3 is a mixed BUS actuator: its relays remain switch entities, while its
# 16 DALI channels are dimmable light entities. The upstream protocol class
# labels the complete type 114 as SWITCH; force LIGHT here so Home Assistant
# does not classify the DALI part as a simple on/off device.
#
# switch.py still creates the "relay" sub-entities as switches from the parsed
# RC3 state, so changing this protocol-level classification does not remove or
# change the eight relay outputs.
DT_114.HA_TYPE = LIGHT


def _rc3_percent(value: Any) -> int:
    """Clamp an RC3 analog/DALI level to the documented 0-100 % range."""

    return max(0, min(100, int(value)))


def _dt114_create_inels_set_value(cls, device_value: Any) -> str:
    """Create RC3-610M/DALI SET payload exactly according to the MQTT spec."""

    value = device_value.ha_value

    command: list[int] = [0, 0, 0, 0]

    # Data4-5: analog outputs; Data6-7: reserved.
    command.extend(_rc3_percent(a.brightness) for a in value.aout)
    command.extend([0, 0])

    # Data8-15: relay commands.
    # 0x07 = state ON + instruction 011 (immediate execution)
    # 0x06 = state OFF + instruction 011 (immediate execution)
    command.extend(0x07 if relay.is_on else 0x06 for relay in value.relay)

    # Data16-47: four groups of 4 ramp bytes followed by 4 DALI levels.
    for first in range(0, 16, 4):
        command.extend([0, 0, 0, 0])
        command.extend(
            _rc3_percent(value.dali[index].brightness)
            for index in range(first, first + 4)
        )

    return Formatter.format_data(command)


DT_114.create_inels_set_value = classmethod(_dt114_create_inels_set_value)


# ---------------------------------------------------------------------------
# GLOBAL LIVE MQTT STATUS REFRESH
#
# The legacy elkoep-mqtt Device.callback() performs a differential comparison
# and only calls callbacks that it believes changed. On current CU3/HA setups
# this can leave Home Assistant stale even though a fresh inels/status/...
# packet was received correctly by the broker (for example SA3-06M changing
# 06 -> 07 after a physical wall-control action).
#
# MQTT STATUS is the source of truth. Therefore every received status packet
# is parsed again and ALL HA entities belonging to that physical iNELS device
# are refreshed. This applies uniformly to relays, dimmers, DALI, covers,
# sensors, climates, binary inputs, selectors and other supported entities.
#
# This does NOT poll CU3 and does NOT generate any additional BUS/MQTT traffic;
# it only refreshes HA entities when a packet is already received.
# ---------------------------------------------------------------------------


def _device_callback_with_live_status_refresh(
    self: Device,
    availability_update: bool,
) -> None:
    """Refresh every entity of a device from every received MQTT state."""

    try:
        self.get_value()
        self.complete_callback()
    except Exception:  # noqa: BLE001 - one malformed packet must not kill MQTT
        LOGGER.exception(
            "Unable to process live MQTT state for %s (%s)",
            self.unique_id,
            self.inels_type,
        )


Device.callback = _device_callback_with_live_status_refresh


# ---------------------------------------------------------------------------
# DA3-03M/RGBW (type 153) protocol mapping.
#
# Based on ELKO EP "Integrace iNELS do MQTT – BUS", Rev. 2:
# STATUS / SET for LED 3:
#   Data22 = LED3-R
#   Data23 = LED3-G
#   Data28 = LED3-B
#   Data29 = LED3-W
#   Data30 = LED3-Y
#
# Keep status parsing and SET payload generation aligned with the official
# MQTT BUS documentation.
# ---------------------------------------------------------------------------

DT_153.DATA["LED_3"] = [22, 23, 28, 29, 30]


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
        led_3.r,          # Data22 = LED3-R
        led_3.g,          # Data23 = LED3-G
        0,
        0,
        0,
        0,
        led_3.b,          # Data28 = LED3-B
        led_3.w,          # Data29 = LED3-W
        led_3.brightness, # Data30 = LED3-Y
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

    tracker: CommunicationTracker | None = None

    try:
        discovery = InelsDiscovery(mqtt)

        await hass.async_add_executor_job(
            discovery.discovery
        )

        inels_data[DEVICES] = discovery.devices

        # Passive communication diagnostics. This only observes MQTT packets
        # that are already flowing and adds no polling traffic to CU3/BUS.
        tracker = CommunicationTracker(
            hass,
            mqtt,
            discovery.devices,
        )
        tracker.start()
        inels_data[COMM_TRACKER] = tracker

    except Exception as exc:
        if tracker is not None:
            tracker.close()
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

    tracker = hass_data.get(COMM_TRACKER)
    if tracker is not None:
        tracker.close()

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
