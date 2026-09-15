iNELS MQTT Home Assistant SA 0.2.19 - RGBW wall switch white fix

Nahraj pouze:
- custom_components/inels_mqtt_homeassistant_sa/light.py
- custom_components/inels_mqtt_homeassistant_sa/manifest.json

Změna proti 0.2.18:
- zachovává správnou regulaci jasu RGBW podle fyzických R/G/B/W kanálů,
- HA ON po OFF zůstává čistá bílá,
- nově detekuje externí/nástěnné OFF->ON u RGBW typu 153,
- pokud DA3-03M interně obnoví starou barvu, po 150 ms odešle jeden opravný SET na čistou bílou,
- zachová přitom skutečnou intenzitu z fyzických RGBW kanálů,
- HA-originované zapnutí/změna barvy se od externího ON rozlišuje a neopravuje se omylem.

Poznámka:
Při zapnutí RGBW typu 153 nástěnným ovladačem se nyní záměrně objeví jeden inels/set/... retain=false.
Je to nutné, protože jinak DA3-03M fyzicky obnoví svou interně uloženou předchozí barvu.

Po nahrání proveď plný restart Home Assistantu.
