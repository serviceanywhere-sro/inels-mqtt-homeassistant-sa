"""iNELS switch entity."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from inelsmqtt.devices import Device

from homeassistant.components.switch import (
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
)
from homeassistant.util import slugify

from .const import (
    DEVICES,
    DOMAIN,
    ICON_SWITCH,
    LOGGER,
    OLD_ENTITIES,
)
from .entity import InelsBaseEntity


# ---------------------------------------------------------------------------
# Switch definitions
# ---------------------------------------------------------------------------


@dataclass
class InelsSwitchAlert:
    """iNELS switch alert."""

    key: str
    message: str


relay_overflow = InelsSwitchAlert(
    key="overflow",
    message="Relay overflow in %s of %d",
)


@dataclass
class InelsSwitchType:
    """iNELS switch type."""

    name: str = "Relay"

    icon: str = ICON_SWITCH

    overflow: str | None = None

    alerts: list[InelsSwitchAlert] | None = None


INELS_SWITCH_TYPES: dict[
    str,
    InelsSwitchType,
] = {

    # Virtual System Bits:
    #
    # inels/status/.../bits/D00001
    #
    # Number of bits is NOT hard-coded.
    "bit": InelsSwitchType(
        name="Bit"
    ),

    # Simple relay outputs.
    "simple_relay": InelsSwitchType(),

    # BUS relay outputs including SA3-014M.
    "relay": InelsSwitchType(
        alerts=[relay_overflow]
    ),
}


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Load iNELS switches."""

    device_list: list[Device] = (
        hass.data[DOMAIN][
            config_entry.entry_id
        ][DEVICES]
    )

    old_entities: list[str] = (
        hass.data[DOMAIN][
            config_entry.entry_id
        ][OLD_ENTITIES].get(
            Platform.SWITCH
        )
        or []
    )

    entities: list[InelsBaseEntity] = []

    for device in device_list:

        for (
            key,
            type_dict,
        ) in INELS_SWITCH_TYPES.items():

            if not hasattr(
                device.state,
                key,
            ):
                continue

            values = (
                device.state.__dict__[key]
            )

            for index, value in enumerate(
                values
            ):

                # -----------------------------------------------------------
                # Virtual System Bits
                # -----------------------------------------------------------

                if device.inels_type == "BITS":

                    # Address comes directly from MQTT:
                    #
                    # 000
                    # 001
                    # 002
                    # ...
                    #
                    # Therefore there is no fixed amount.
                    address = value.addr

                    entity_name = (
                        f"Bit {address}"
                    )

                    description_key = (
                        f"{key}_{address}"
                    )

                # -----------------------------------------------------------
                # Normal relay
                # -----------------------------------------------------------

                else:

                    if len(values) == 1:

                        entity_name = (
                            type_dict.name
                        )

                        description_key = key

                    else:

                        entity_name = (
                            f"{type_dict.name} "
                            f"{index + 1}"
                        )

                        description_key = (
                            f"{key}{index}"
                        )

                entities.append(
                    InelsBusSwitch(
                        device=device,
                        key=key,
                        index=index,
                        description=(
                            InelsSwitchEntityDescription(
                                key=description_key,
                                name=entity_name,
                                icon=type_dict.icon,
                                overload_key=(
                                    type_dict.overflow
                                ),
                                alerts=(
                                    type_dict.alerts
                                ),
                            )
                        ),
                    )
                )

    async_add_entities(
        entities,
        False,
    )

    # Remove recreated entities from old list.
    for entity in entities:

        if entity.entity_id in old_entities:

            old_entities.remove(
                entity.entity_id
            )

    hass.data[DOMAIN][
        config_entry.entry_id
    ][Platform.SWITCH] = old_entities


# ---------------------------------------------------------------------------
# Entity description
# ---------------------------------------------------------------------------


@dataclass
class InelsSwitchEntityDescription(
    SwitchEntityDescription
):
    """Description for iNELS switch."""

    overload_key: str | None = None

    alerts: (
        list[InelsSwitchAlert] | None
    ) = None


# ---------------------------------------------------------------------------
# Switch entity
# ---------------------------------------------------------------------------


class InelsBusSwitch(
    InelsBaseEntity,
    SwitchEntity,
):
    """iNELS BUS/RF switch."""

    entity_description: (
        InelsSwitchEntityDescription
    )

    def __init__(
        self,
        device: Device,
        key: str,
        index: int,
        description: (
            InelsSwitchEntityDescription
        ),
    ) -> None:
        """Initialize switch."""

        super().__init__(
            device=device,
            key=key,
            index=index,
        )

        self.entity_description = (
            description
        )

        self._attr_unique_id = slugify(
            f"{self._attr_unique_id}_"
            f"{description.key}"
        )

        self.entity_id = (
            f"{Platform.SWITCH}."
            f"{self._attr_unique_id}"
        )

        self._attr_name = (
            f"{self._attr_name} "
            f"{description.name}"
        )

    # -----------------------------------------------------------------------
    # Availability
    # -----------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return entity availability."""

        if self.entity_description.alerts:

            try:

                last_state = (
                    self._device
                    .last_values
                    .ha_value
                    .__dict__[self.key][
                        self.index
                    ]
                )

                for alert in (
                    self.entity_description.alerts
                ):

                    if hasattr(
                        self._device.state,
                        alert.key,
                    ):

                        alert_values = (
                            self._device
                            .state
                            .__dict__[
                                alert.key
                            ]
                        )

                        if alert_values:

                            try:
                                previous_alert = (
                                    last_state
                                    .__dict__
                                    .get(
                                        alert.key,
                                        False,
                                    )
                                )
                            except AttributeError:
                                previous_alert = False

                            if not previous_alert:

                                LOGGER.warning(
                                    alert.message,
                                    self.name,
                                    self._device_id,
                                )

                            return False

            except (
                AttributeError,
                IndexError,
                KeyError,
                TypeError,
            ):
                pass

        return super().available

    # -----------------------------------------------------------------------
    # State
    # -----------------------------------------------------------------------

    @property
    def is_on(self) -> bool | None:
        """Return whether switch is ON."""

        state = self._device.state

        return (
            state.__dict__[
                self.key
            ][self.index].is_on
        )

    @property
    def icon(self) -> str | None:
        """Return icon."""

        return (
            self.entity_description.icon
        )

    # -----------------------------------------------------------------------
    # Control
    # -----------------------------------------------------------------------

    async def async_turn_off(
        self,
        **kwargs: Any,
    ) -> None:
        """Turn output/bit OFF."""

        # IMPORTANT:
        #
        # Do not use:
        #
        # if not self._device.is_available:
        #     return
        #
        # This was the reason switching did not work
        # correctly with the CU3 in our previous version.

        ha_val = self._device.state

        ha_val.__dict__[
            self.key
        ][self.index].is_on = False

        await self.hass.async_add_executor_job(
            self._device.set_ha_value,
            ha_val,
        )

    async def async_turn_on(
        self,
        **kwargs: Any,
    ) -> None:
        """Turn output/bit ON."""

        ha_val = self._device.state

        ha_val.__dict__[
            self.key
        ][self.index].is_on = True

        await self.hass.async_add_executor_job(
            self._device.set_ha_value,
            ha_val,
        )