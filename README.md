# iNELS MQTT Home Assistant SA

> **Home Assistant domain:** `inels_mqtt_homeassistant_sa`  
> The repository/display name is `inels-mqtt-homeassistant-sa`. The underscore domain is intentional because Home Assistant integration domains cannot contain hyphens and must be unique. This prevents overriding or colliding with the built-in `inels` integration and other HACS integrations using the `inels` domain.


Custom Home Assistant integration for iNELS BUS systems exposed through the iNELS MQTT gateway.

This repository is based on the existing iNELS Home Assistant / MQTT open-source work and contains additional compatibility fixes and device handling developed for a CU3-08M installation.

## Current additions

- compatibility fixes for recent Home Assistant / Paho MQTT versions
- extended MQTT discovery
- SA3-014M (14 relay outputs)
- JA3-014M (7 shutter channels)
- time-based shutter position/tilt handling for JA3-014M
- virtual `bits` exposed dynamically
- virtual `integers` exposed dynamically
- DA3-66M dimmer handling with six Home Assistant light entities
- dimmer OFF (`0 %`) remains available and controllable
- plain dimmer ON switches to `100 %`; the Home Assistant brightness slider sets the requested level

## Tested setup

Development testing has been performed with:

- Home Assistant 2026.8.x
- CU3-08M
- iNELS MQTT topics in the form `inels/status/...`

Other installations may behave differently. Treat this integration as experimental and test outputs carefully before using automations.

## Installation through HACS

1. Open HACS in Home Assistant.
2. Add this repository as a **Custom repository** of type **Integration**.
3. Install **iNELS MQTT Home Assistant SA**.
4. Restart Home Assistant.
5. Add/configure the iNELS integration and point it to the MQTT broker used by the iNELS gateway.

## Manual installation

Copy:

```text
custom_components/inels_mqtt_homeassistant_sa
```

to:

```text
/config/custom_components/inels_mqtt_homeassistant_sa_mqtt_homeassistant_sa
```

and restart Home Assistant.

## Important notes

### JA3-014M

The integration can estimate shutter position and tilt from configured travel times. These are **estimated**, not absolute positions, unless the installation provides separate physical position feedback.

### DA3-66M

Each of the six dimmer outputs is presented as a Home Assistant `light` entity. Brightness `0` means OFF and must not make the entity unavailable. A normal ON command sets the output to `100 %`; brightness can then be set with the Home Assistant slider.

## Upstream / attribution

This project builds on MIT-licensed iNELS Home Assistant and MQTT projects, including work from:

- `jpbaltazar/inels-hacs-new`
- `epdevlab/elkoep-hacs`
- `epdevlab/elkoep-mqtt`

The upstream MIT license is included in this repository.

## Disclaimer

This project is not an official ELKO EP product. Test changes carefully, especially relay, shutter and other actuator outputs.
