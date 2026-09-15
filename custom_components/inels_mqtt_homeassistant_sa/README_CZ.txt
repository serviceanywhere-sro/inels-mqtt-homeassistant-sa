iNELS MQTT Home Assistant SA – 0.2.14 diagnostic safety update

Nahraďte v repozitáři tyto soubory:
custom_components/inels_mqtt_homeassistant_sa/__init__.py
custom_components/inels_mqtt_homeassistant_sa/binary_sensor.py
custom_components/inels_mqtt_homeassistant_sa/manifest.json

Změny:
1. Vypnut aktivní upstream COMM TEST na jednotlivé komponenty.
   Integrace kvůli kontrole komunikace neposílá žádný inels/set na BUS/RF prvky.

2. Diagnostika komunikace je zobrazována pouze na úrovni CU/gateway:
   - MQTT broker connection
   - CU MQTT communication
   - BUS 1
   - BUS 2
   Individuální "Communication" entity komponent se již nevytvářejí.
   Staré entity Home Assistant při reloadu odstraní jako již nepoužívané.

3. Každý odchozí inels/set je zapsán do HA logu na úrovni WARNING:
   iNELS SET TX topic=... qos=... retain=... payload=...

Důležité:
Tato verze zatím NEMĚNÍ retain příznak odchozích příkazů, pouze ho loguje.
Pokud v logu uvidíme retain=True, můžeme následně bezpečně rozhodnout,
zda v další úpravě vynutit retain=False a případně vyčistit staré retained SET zprávy.
