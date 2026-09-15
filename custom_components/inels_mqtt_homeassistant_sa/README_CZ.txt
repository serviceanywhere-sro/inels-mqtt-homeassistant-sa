iNELS MQTT Home Assistant SA 0.2.18 - DA3-03M/RGBW jas + bile ON

Nahraj do:
custom_components/inels_mqtt_homeassistant_sa/light.py
custom_components/inels_mqtt_homeassistant_sa/manifest.json

Zmeny pouze pro RGBW:
- HA jas uz nereguluje pouze Y, ale skaluje skutecne R/G/B/W kanaly.
- Y je pri ON drzen na 100 %, pri OFF na 0 %.
- OFF posila R=G=B=W=Y=0.
- Proste ON z vypnuteho stavu nastavi cistou bilou.
- Pokud je svetlo zapnute barevne a meni se jen jas, odstin se zachova.
- Pri prime volbe barvy se pouzije vybrana barva.
- Ostatni typy svetel zustavaji beze zmeny.

Po nahrani proved plny restart Home Assistantu.
