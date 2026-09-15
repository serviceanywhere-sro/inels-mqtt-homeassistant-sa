iNELS MQTT Home Assistant SA – 0.2.15

Nahraďte po verzi 0.2.14 pouze:
custom_components/inels_mqtt_homeassistant_sa/__init__.py
custom_components/inels_mqtt_homeassistant_sa/manifest.json

Změna:
- všechny odchozí příkazy inels/set/... jsou natvrdo odesílány s retain=False
- v logu zůstává záznam každého SET příkazu
- log obsahuje retain=False a requested_retain=... pro kontrolu, co požadovala knihovna
- aktivní COMM TEST jednotlivých komponent zůstává vypnutý

POZOR:
Staré retained zprávy uložené v MQTT brokeru je nutné jednorázově smazat.
Tato verze je už znovu nevytvoří.
