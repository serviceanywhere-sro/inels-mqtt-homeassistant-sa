"""Passive MQTT communication diagnostics for iNELS."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import LOGGER

COMM_WATCHDOG_SECONDS = 5
COMM_LAST_SEEN_REPORT_SECONDS = 30
COMM_LIVE_CONFIRMATION_SECONDS = 300


_TRUE_TEXT = {
    "1", "true", "on", "online", "ok", "connected", "running",
    "run", "active", "ready",
}
_FALSE_TEXT = {
    "0", "false", "off", "offline", "error", "fault", "disconnected",
    "failed", "fail",
}
_UNKNOWN_TEXT = {
    "", "none", "null", "unknown", "n/a", "na", "notused",
    "not used", "unused",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _decode_payload(payload: Any) -> str:
    if isinstance(payload, (bytes, bytearray)):
        return payload.decode(errors="replace").strip()
    return str(payload).strip()


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, dict):
        for key in ("status", "state", "value", "connected"):
            if key in value:
                return _to_bool(value[key])
        return None

    text = str(value).strip().lower()
    if text in _TRUE_TEXT:
        return True
    if text in _FALSE_TEXT:
        return False
    if text in _UNKNOWN_TEXT:
        return None
    return None


def _parse_connected(payload: Any) -> bool | None:
    text = _decode_payload(payload)
    if not text:
        return None

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _to_bool(text)

    if isinstance(parsed, dict) and "status" in parsed:
        return _to_bool(parsed["status"])
    return _to_bool(parsed)


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _flatten_mapping(value: Any, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not isinstance(value, dict):
        return result

    for key, item in value.items():
        current = f"{prefix}{key}"
        if isinstance(item, dict):
            result.update(_flatten_mapping(item, current))
        else:
            result[_normalize_key(current)] = item
    return result


def _parse_gateway_payload(payload: Any) -> tuple[dict[str, Any], dict[int, bool | None]]:
    text = _decode_payload(payload)
    if not text:
        return {}, {1: None, 2: None}

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}, {1: None, 2: None}

    if not isinstance(parsed, dict):
        return {}, {1: None, 2: None}

    flat = _flatten_mapping(parsed)
    buses: dict[int, bool | None] = {1: None, 2: None}

    for bus_number in (1, 2):
        wanted = f"bus{bus_number}"
        candidates = [
            value
            for key, value in flat.items()
            if key == wanted or key.startswith(wanted) or key.endswith(wanted)
        ]
        for candidate in candidates:
            parsed_bool = _to_bool(candidate)
            if parsed_bool is not None or str(candidate).strip().lower() in _UNKNOWN_TEXT:
                buses[bus_number] = parsed_bool
                break

    return parsed, buses


def _first_metadata_value(payload: dict[str, Any], names: tuple[str, ...]) -> str | None:
    flat = _flatten_mapping(payload)
    normalized_names = {_normalize_key(name) for name in names}
    for key, value in flat.items():
        if key in normalized_names and value not in (None, ""):
            return str(value)
    return None


class CommunicationTracker:
    """Track iNELS MQTT availability and live traffic without BUS polling."""

    def __init__(self, hass: HomeAssistant, mqtt: Any, devices: list[Any]) -> None:
        self.hass = hass
        self.mqtt = mqtt
        self.devices = devices

        self._device_by_uid: dict[str, Any] = {
            device.unique_id: device for device in devices
        }
        self._device_mac: dict[str, str] = {}
        self._device_connected: dict[str, bool | None] = {}
        self._device_connected_cached: dict[str, bool | None] = {}
        self._device_last_seen: dict[str, datetime] = {}
        self._device_reported_last_seen: dict[str, datetime] = {}

        self._gateway_connected: dict[str, bool | None] = {}
        self._gateway_connected_cached: dict[str, bool | None] = {}
        self._gateway_last_seen: dict[str, datetime] = {}
        self._gateway_reported_last_seen: dict[str, datetime] = {}
        self._gateway_status: dict[str, dict[str, Any]] = {}
        self._gateway_bus: dict[str, dict[int, bool | None]] = {}
        self._gateway_health_reported: dict[str, str] = {}

        self._listeners: dict[str, set[Callable[[], None]]] = defaultdict(set)
        self._dirty: set[str] = set()
        self._unsub_interval: Callable[[], None] | None = None
        self._original_on_message: Callable[..., Any] | None = None
        self._message_wrapper: Callable[..., Any] | None = None
        self._last_broker_online: bool | None = None
        self._started = False

        for device in devices:
            parts = device.state_topic.split("/")
            if len(parts) >= 5:
                self._device_mac[device.unique_id] = parts[2]

        self._gateway_macs: set[str] = set(self._device_mac.values())

    @property
    def gateway_macs(self) -> tuple[str, ...]:
        return tuple(sorted(self._gateway_macs))

    def is_physical_device(self, device: Any) -> bool:
        parts = device.state_topic.split("/")
        if len(parts) < 5:
            return False
        return parts[3].lower() not in {"bits", "integers", "gw"}

    def device_mac(self, device_or_uid: Any) -> str | None:
        uid = device_or_uid if isinstance(device_or_uid, str) else device_or_uid.unique_id
        return self._device_mac.get(uid)

    @property
    def broker_online(self) -> bool:
        try:
            return bool(self.mqtt.client.is_connected())
        except Exception:
            return False

    def gateway_live_age_seconds(
        self, mac: str, now: datetime | None = None
    ) -> int | None:
        last_seen = self._gateway_last_seen.get(mac)
        if last_seen is None:
            return None
        current = now or _utcnow()
        return max(0, int((current - last_seen).total_seconds()))

    def gateway_health_state(
        self, mac: str, now: datetime | None = None
    ) -> str:
        """Return broker_offline/offline/ok/stale/unknown for one CU.

        Retained/cached MQTT values never prove current communication. A CU is
        green only after a fresh, non-retained packet has really arrived since
        this HA session started. If that proof becomes old, the state changes
        to ``stale`` (HA binary sensor = unknown), not falsely to offline.
        """
        if not self.broker_online:
            return "broker_offline"

        # Only a LIVE connected=false packet is authoritative for offline.
        if self._gateway_connected.get(mac) is False:
            return "offline"

        age = self.gateway_live_age_seconds(mac, now)
        if age is None:
            return "unknown"
        if age >= COMM_LIVE_CONFIRMATION_SECONDS:
            return "stale"
        return "ok"

    def gateway_online(self, mac: str) -> bool | None:
        state = self.gateway_health_state(mac)
        if state in {"broker_offline", "offline"}:
            return False
        if state == "ok":
            return True
        return None

    def gateway_diagnostic_attributes(self, mac: str) -> dict[str, Any]:
        now = _utcnow()
        result = self.gateway_metadata(mac)
        last_communication = self.gateway_last_seen(mac)
        result.update(
            {
                "communication_state": self.gateway_health_state(mac, now),
                "communication_mode": "live_mqtt_confirmation",
                "live_confirmation_valid_seconds": COMM_LIVE_CONFIRMATION_SECONDS,
                "live_message_age_seconds": self.gateway_live_age_seconds(mac, now),
                "live_connected_state": self._gateway_connected.get(mac),
                "cached_connected_state": self._gateway_connected_cached.get(mac),
                "last_mqtt_communication": (
                    last_communication.isoformat() if last_communication else None
                ),
            }
        )
        return result

    def device_online(self, uid: str) -> bool | None:
        mac = self._device_mac.get(uid)
        if mac is None:
            return None

        gateway_online = self.gateway_online(mac)
        if gateway_online is False:
            return False
        if gateway_online is not True:
            # CU communication is not currently live-confirmed. Do not keep an
            # individual BUS device falsely green from an older connected=true.
            return None

        connected = self._device_connected.get(uid)
        if connected is False:
            return False
        if connected is True:
            return True

        if uid in self._device_last_seen:
            return True
        return None

    def bus_state(self, mac: str, bus_number: int) -> bool | None:
        # BUS state from the last gw/status packet must not stay green when
        # communication with the CU is stale or has not yet been confirmed.
        if self.gateway_online(mac) is not True:
            return None
        return self._gateway_bus.get(mac, {}).get(bus_number)

    def device_last_seen(self, uid: str) -> datetime | None:
        return self._device_reported_last_seen.get(uid)

    def gateway_last_seen(self, mac: str) -> datetime | None:
        return self._gateway_reported_last_seen.get(mac)

    def gateway_metadata(self, mac: str) -> dict[str, Any]:
        payload = self._gateway_status.get(mac, {})
        if not payload:
            return {}

        result: dict[str, Any] = {}
        model = _first_metadata_value(
            payload, ("model", "device", "device_type", "type", "gw_type")
        )
        firmware = _first_metadata_value(
            payload, ("fw", "firmware", "firmware_version", "version")
        )
        cloud = _first_metadata_value(
            payload, ("cloud", "cloud_status", "cloudstatus")
        )
        if model:
            result["model"] = model
        if firmware:
            result["firmware"] = firmware
        if cloud:
            result["cloud_status"] = cloud
        return result

    def add_listener(
        self, key: str, listener: Callable[[], None]
    ) -> Callable[[], None]:
        self._listeners[key].add(listener)

        @callback
        def _remove() -> None:
            listeners = self._listeners.get(key)
            if listeners is None:
                return
            listeners.discard(listener)
            if not listeners:
                self._listeners.pop(key, None)

        return _remove

    def start(self) -> None:
        if self._started:
            return
        self._started = True

        self._seed_from_existing_messages()
        self._install_message_hook()

        for mac in self.gateway_macs:
            try:
                self.mqtt.client.subscribe(f"inels/status/{mac}/gw", qos=0)
                self.mqtt.client.subscribe(f"inels/connected/{mac}/gw", qos=0)
            except Exception as exc:
                LOGGER.debug(
                    "Unable to subscribe gateway diagnostics for %s: %s", mac, exc
                )

        self._last_broker_online = self.broker_online

        self._unsub_interval = async_track_time_interval(
            self.hass,
            self._watchdog_tick,
            timedelta(seconds=COMM_WATCHDOG_SECONDS),
        )

    def close(self) -> None:
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

        client = getattr(self.mqtt, "client", None)
        if (
            client is not None
            and self._message_wrapper is not None
            and client.on_message is self._message_wrapper
        ):
            client.on_message = self._original_on_message

        self._listeners.clear()
        self._dirty.clear()
        self._started = False

    def _seed_from_existing_messages(self) -> None:
        try:
            messages = dict(self.mqtt.messages())
        except Exception:
            return

        for topic, payload in messages.items():
            self._process_message(topic, payload, retained=True, fresh=False)

    def _install_message_hook(self) -> None:
        client = self.mqtt.client
        current = client.on_message

        if current is self._message_wrapper:
            return

        self._original_on_message = current

        def _wrapper(client_obj: Any, userdata: Any, msg: Any) -> None:
            topic = str(msg.topic)
            payload = bytes(msg.payload) if isinstance(msg.payload, bytearray) else msg.payload
            retained = bool(getattr(msg, "retain", False))

            self.hass.loop.call_soon_threadsafe(
                self._process_message, topic, payload, retained, True
            )

            if self._original_on_message is not None:
                self._original_on_message(client_obj, userdata, msg)

        self._message_wrapper = _wrapper
        client.on_message = _wrapper

    def _ensure_message_hook(self) -> None:
        client = self.mqtt.client
        if self._message_wrapper is not None and client.on_message is self._message_wrapper:
            return

        self._original_on_message = client.on_message
        self._install_message_hook()

    def _process_message(
        self, topic: str, payload: Any, retained: bool, fresh: bool
    ) -> None:
        parts = topic.split("/")
        if len(parts) < 4 or parts[0] != "inels":
            return

        message_type = parts[1]
        if message_type not in {"status", "connected"}:
            return

        mac = parts[2]
        element_type = parts[3]
        now = _utcnow()

        if mac not in self._gateway_macs:
            self._gateway_macs.add(mac)

        if fresh and not retained:
            self._gateway_last_seen[mac] = now
            self._maybe_report_gateway_seen(mac, now)

            # Any fresh packet other than an explicit gw connected=false proves
            # that the MQTT path from this CU is alive again. Clear an older
            # offline flag so a real status packet can restore communication.
            if not (element_type == "gw" and message_type == "connected"):
                if self._gateway_connected.get(mac) is False:
                    self._gateway_connected[mac] = None
                    self._mark_gateway_dirty(mac, include_devices=True)

        if element_type == "gw":
            if message_type == "connected":
                new_state = _parse_connected(payload)

                if fresh and not retained:
                    if self._gateway_connected.get(mac) != new_state:
                        self._gateway_connected[mac] = new_state
                        self._mark_gateway_dirty(mac, include_devices=True)
                    self._dirty.add(f"gateway:{mac}")
                else:
                    # Retained/cached state is useful only as diagnostics. It
                    # must never turn the live communication sensor green.
                    self._gateway_connected_cached[mac] = new_state

            elif message_type == "status":
                parsed, buses = _parse_gateway_payload(payload)
                if parsed and parsed != self._gateway_status.get(mac):
                    self._gateway_status[mac] = parsed
                    self._dirty.add(f"gateway:{mac}")
                if buses != self._gateway_bus.get(mac):
                    self._gateway_bus[mac] = buses
                    self._dirty.add(f"gateway:{mac}")
            return

        if len(parts) < 5:
            return

        address = parts[4]
        uid = f"{mac}_{address}"
        if uid not in self._device_by_uid:
            return

        if message_type == "connected":
            new_state = _parse_connected(payload)
            if fresh and not retained:
                if self._device_connected.get(uid) != new_state:
                    self._device_connected[uid] = new_state
                    self._dirty.add(f"device:{uid}")
            else:
                self._device_connected_cached[uid] = new_state

        if fresh and not retained:
            # A fresh device STATUS packet proves that the device is talking
            # again, even if an older live connected=false was seen before.
            if message_type == "status" and self._device_connected.get(uid) is False:
                self._device_connected[uid] = None
                self._dirty.add(f"device:{uid}")

            self._device_last_seen[uid] = now
            self._maybe_report_device_seen(uid, now)

    def _maybe_report_device_seen(self, uid: str, now: datetime) -> None:
        reported = self._device_reported_last_seen.get(uid)
        if (
            reported is None
            or (now - reported).total_seconds() >= COMM_LAST_SEEN_REPORT_SECONDS
        ):
            self._device_reported_last_seen[uid] = now
            self._dirty.add(f"device:{uid}")

    def _maybe_report_gateway_seen(self, mac: str, now: datetime) -> None:
        reported = self._gateway_reported_last_seen.get(mac)
        if (
            reported is None
            or (now - reported).total_seconds() >= COMM_LAST_SEEN_REPORT_SECONDS
        ):
            self._gateway_reported_last_seen[mac] = now
            self._dirty.add(f"gateway:{mac}")

    def _flush_pending_last_seen(self, now: datetime) -> None:
        for uid, exact in self._device_last_seen.items():
            reported = self._device_reported_last_seen.get(uid)
            if reported is None:
                self._device_reported_last_seen[uid] = exact
                self._dirty.add(f"device:{uid}")
            elif exact > reported and (
                now - reported
            ).total_seconds() >= COMM_LAST_SEEN_REPORT_SECONDS:
                self._device_reported_last_seen[uid] = exact
                self._dirty.add(f"device:{uid}")

        for mac, exact in self._gateway_last_seen.items():
            reported = self._gateway_reported_last_seen.get(mac)
            if reported is None:
                self._gateway_reported_last_seen[mac] = exact
                self._dirty.add(f"gateway:{mac}")
            elif exact > reported and (
                now - reported
            ).total_seconds() >= COMM_LAST_SEEN_REPORT_SECONDS:
                self._gateway_reported_last_seen[mac] = exact
                self._dirty.add(f"gateway:{mac}")

    def _mark_gateway_dirty(self, mac: str, include_devices: bool) -> None:
        self._dirty.add(f"gateway:{mac}")
        if not include_devices:
            return
        for uid, device_mac in self._device_mac.items():
            if device_mac == mac:
                self._dirty.add(f"device:{uid}")

    def _check_gateway_health_transitions(self, now: datetime) -> None:
        """Refresh entities when live proof expires or communication returns."""
        for mac in self.gateway_macs:
            state = self.gateway_health_state(mac, now)
            previous = self._gateway_health_reported.get(mac)
            if previous == state:
                continue

            self._gateway_health_reported[mac] = state
            self._mark_gateway_dirty(mac, include_devices=True)

            if state == "stale":
                LOGGER.warning(
                    "iNELS CU %s MQTT communication is no longer live-confirmed "
                    "(no fresh packet for %s s)",
                    mac,
                    self.gateway_live_age_seconds(mac, now),
                )
            elif state == "offline":
                LOGGER.warning("iNELS CU %s reports MQTT offline", mac)
            elif state == "ok" and previous in {"stale", "offline", "unknown"}:
                LOGGER.info("iNELS CU %s MQTT live communication confirmed", mac)

    @callback
    def _watchdog_tick(self, now: datetime) -> None:
        self._ensure_message_hook()

        current = _utcnow()
        broker_online = self.broker_online
        if broker_online != self._last_broker_online:
            self._last_broker_online = broker_online
            for mac in self.gateway_macs:
                self._mark_gateway_dirty(mac, include_devices=True)

        self._check_gateway_health_transitions(current)
        self._flush_pending_last_seen(current)
        self._notify_dirty()

    def _notify_dirty(self) -> None:
        if not self._dirty:
            return

        dirty = tuple(self._dirty)
        self._dirty.clear()
        for key in dirty:
            for listener in tuple(self._listeners.get(key, ())):
                try:
                    listener()
                except Exception as exc:
                    LOGGER.debug(
                        "Communication diagnostic listener failed: %s", exc
                    )
