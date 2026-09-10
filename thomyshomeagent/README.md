# Zigbee2MQTT-Integration für ThomysHomeAgent

Bindet die Zigbee2MQTT-Instanz auf dem SMHUB direkt in **ThomysHomeAgent** (die Licht-App `~/lichtagent/` auf dem Laptop, Port 8099) ein: Version/Koordinator/Health fürs Info-Panel, alle Zigbee-Leuchten als Ziele für Sprachbefehle und Dashboard, Anlernen und Health-Check per API.

Abgestimmt auf die laufende Installation:

| Komponente                 | Version / Wert                                                                                               |
| -------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Zigbee2MQTT                | [2.13.0](https://github.com/Koenkk/zigbee2mqtt/releases/tag/2.13.0), Commit `fcbb7ff4`                       |
| Frontend                   | [WindFront 2.14.0](https://github.com/Nerivec/zigbee2mqtt-windfront/releases/tag/v2.14.0)                    |
| zigbee-herdsman-converters | [26.90.0](https://github.com/Koenkk/zigbee-herdsman-converters/releases/tag/v26.90.0)                        |
| zigbee-herdsman            | [10.8.0](https://github.com/Koenkk/zigbee-herdsman/releases/tag/v10.8.0)                                     |
| Koordinator                | zStack3x0 `0x00124b0033cb0f78`, Revision 20250325                                                            |
| Maschine                   | SMHUB (riscv64, 1 CPU, 489 MB RAM), Node v22.22.0, MQTT `mqtt://localhost:1883` → im LAN `192.168.1.45:1883` |

Es wird ausschliesslich die [offizielle MQTT-API](../docs/guide/usage/mqtt_topics_and_messages.md) von Zigbee2MQTT benutzt (`bridge/info`, `bridge/devices`, `bridge/health`, `bridge/request/…`, `<Gerät>/set`). Kein paho, kein pip — nur Python-Standardbibliothek, wie der Rest der App.

```
Handy/Browser ──▶ PicoClaw (SMHUB) ──▶ ThomysHomeAgent :8099 (Laptop 192.168.1.54)
                                            │  lichtapp.py  ──▶  /api/z2m/*   (neu, z2m_api.py)
                                            │  homebrain.py ──▶  Zigbee-Ziele  (neu: alle Z2M-Leuchten)
                                            │  z2m.py       ──▶  MQTT 192.168.1.45:1883  (neu)
                                            ▼                          │
                                     Home Assistant            Zigbee2MQTT 2.13.0 (SMHUB)
```

## Dateien

| Datei                 | Zweck                                                                                                                       |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `z2m.py`              | MQTT-Client (Subscribe, Keepalive, Reconnect) + `Zigbee2MQTT`-Klasse mit Cache, Anfragen mit `transaction`, Licht-Steuerung |
| `z2m_api.py`          | HTTP-Endpunkte `/api/z2m/*` für `lichtapp.py`; läuft auch alleine als Testserver mit Vorschau-Seite                         |
| `homebrain.py`        | Ersetzt die bisherige Datei: Zigbee-Leuchten aus Zigbee2MQTT sind Sprachbefehl-Ziele, HA und Zigbee laufen unabhängig       |
| `test_z2m.py`         | Tests mit simuliertem Broker und simuliertem Zigbee2MQTT 2.13.0 (`python3 test_z2m.py`)                                     |
| `config.example.json` | Alle Schlüssel der `config.json` inkl. der neuen (ohne Zugangsdaten)                                                        |

## Installation auf dem Laptop (192.168.1.54)

1. **Dateien kopieren** (bisherige `homebrain.py` sichern):

    ```bash
    cd ~/lichtagent
    cp homebrain.py homebrain.py.bak
    cp /pfad/zu/thomyshomeagent/{z2m.py,z2m_api.py,homebrain.py,test_z2m.py} .
    ```

    Die neue `homebrain.py` enthält den Stand vom 09.09. (inkl. `OLLAMA_URL`-Fix) plus die Zigbee-Erweiterung. Falls du `RAEUME` oder `FARBEN` seither geändert hast: Diff mit `diff homebrain.py.bak homebrain.py` prüfen und übernehmen.

2. **`config.json` ergänzen** (die bestehenden Schlüssel `mqtt_host`, `mqtt_user`, `mqtt_pass`, `z2m_base`, `bar`, `panel` werden weiterverwendet):

    ```json
    "z2m_expected_version": "2.13.0",
    "z2m_frontend_url": "http://192.168.1.45:8080"
    ```

    `z2m_frontend_url` ist optional — ohne den Eintrag wird der Link aus `bridge/info` (Frontend-Port) und dem Broker-Host abgeleitet.

3. **Schnelltest ohne App** (zeigt Status, Zusammenfassung und alle Leuchten):

    ```bash
    python3 z2m.py config.json
    ```

    Erwartete erste Zeile: `Zigbee2MQTT 2.13.0 ✓ · online · N Geräte (M Leuchten)`, danach die Zusammenfassung als JSON und die Leuchtenliste. Bei `Nicht autorisiert (rc=5)` stimmen `mqtt_user`/`mqtt_pass` nicht (siehe SMHUB-Broker-Zugang).

4. **Vorschau des Info-Panels + Buttons zum Testen** (eigener Port, unabhängig von lichtapp.py):

    ```bash
    python3 z2m_api.py config.json 8098     # → http://192.168.1.54:8098
    ```

5. **In `lichtapp.py` einbinden** — beim Start der App:

    ```python
    from z2m import Zigbee2MQTT
    from z2m_api import Z2MApi

    z2m = Zigbee2MQTT(cfg)                       # cfg = geladene config.json
    z2m.start(block_until_connected=False)       # verbindet im Hintergrund, Reconnect automatisch
    z2m_api = Z2MApi(z2m)
    brain = HomeBrain(agent, z2m=z2m)            # statt HomeBrain(agent)
    ```

    Im HTTP-Handler **vor** den eigenen Routen (`params` = Query-Parameter als dict):

    ```python
    hit = z2m_api.handle(self.command, path, params)   # None, wenn nicht /api/z2m/…
    if hit is not None:
        status, body = hit
        self._json(status, body)                       # eigene JSON-Antwort-Hilfe der App
        return
    ```

    Fürs **Info/Einstellungen-Panel** (ℹ️): `z2m.status_text()` liefert die Statuszeile, `z2m.summary()` alle Felder (Version, Commit, Koordinator, zigbee-herdsman(-converters), Maschine/Node, MQTT, Kanal/PAN-ID, permit_join, Health, Frontend-Link, Geräte offline, letzte Warnungen) — dieselben Angaben wie auf der „Über“-Seite von Zigbee2MQTT. Beim Beenden `z2m.stop()`.

6. Dienst neu starten und prüfen:

    ```bash
    systemctl --user restart thomyshomeagent.service
    curl -s 'http://192.168.1.54:8099/api/z2m/status'
    ```

## Einbau-Checkliste für den KI-Helfer auf dem Laptop (Hermes)

Diese Schritte setzen voraus, dass `z2m.py`, `z2m_api.py` und die neue `homebrain.py` bereits in `~/lichtagent/` liegen (Schritt 1 oben). `lichtapp.py` und `lichtagent.py` bleiben bis auf die genannten Stellen unverändert.

1. **Vorher prüfen, dass der Broker erreichbar ist:** `cd ~/lichtagent && python3 z2m.py config.json` muss als erste Zeile `Zigbee2MQTT 2.13.0 ✓ · online …` ausgeben. Erst danach weiter.
2. **`lichtapp.py` — Imports:** neben den bestehenden Imports (`from homebrain import HomeBrain` o. ä.) ergänzen:
    ```python
    from z2m import Zigbee2MQTT
    from z2m_api import Z2MApi
    ```
3. **`lichtapp.py` — Start:** an der Stelle, an der `config.json` geladen und der Lichtagent bzw. `HomeBrain(...)` erzeugt wird (suchen nach `HomeBrain(`), direkt davor:
    ```python
    z2m = Zigbee2MQTT(cfg)
    z2m.start(block_until_connected=False)
    z2m_api = Z2MApi(z2m)
    ```
    und den Aufruf `HomeBrain(agent)` zu `HomeBrain(agent, z2m=z2m)` ändern. Variablennamen (`cfg`, `agent`) an die der App anpassen.
4. **`lichtapp.py` — HTTP-Routen:** in der Handler-Methode, die `/api/...` verteilt (suchen nach `"/api/ask"` oder `/api/state`), als **erste** Prüfung:
    ```python
    hit = z2m_api.handle(self.command, path, params)
    if hit is not None:
        status, body = hit
        # so antworten, wie die App sonst JSON zurückgibt, z. B.:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"))
        return
    ```
    `path` ist der Pfad ohne Query-String, `params` ein `dict` der Query-Parameter (`urllib.parse.parse_qs` → erste Werte). Gibt es in der App bereits eine Hilfsfunktion für JSON-Antworten, diese verwenden.
5. **`lichtapp.py` — `/api/state` und Info-Panel:** im JSON von `/api/state` (und/oder des Info-Panels) ein Feld ergänzen:
    ```python
    "zigbee2mqtt": z2m.summary(),
    "zigbee2mqtt_status": z2m.status_text(),
    ```
    Im Panel dann anzeigen: `zigbee2mqtt_status` als Statuszeile; aus `zigbee2mqtt`: `version`/`version_status`/`commit`, `coordinator.type`/`ieee_address`/`revision`, `zigbee_herdsman`, `zigbee_herdsman_converters`, `os`/`cpus`/`memory_mb`/`node_version`, `z2m_mqtt_server`, `frontend_url` (als Link), `permit_join`, `restart_required`, `health.uptime_sec`, `devices_total`/`lights_total`, `devices_offline`, `last_warnings`.
6. **`lichtapp.py` — Beenden:** falls es einen sauberen Shutdown-Pfad gibt (`finally:`/Signal-Handler), dort `z2m.stop()` aufrufen. Ohne diesen Schritt beendet systemd den Prozess trotzdem korrekt (Daemon-Threads).
7. **Dienst neu starten und verifizieren:**
    ```bash
    python3 -m py_compile ~/lichtagent/lichtapp.py ~/lichtagent/homebrain.py
    systemctl --user restart thomyshomeagent.service && sleep 3
    systemctl --user status thomyshomeagent.service --no-pager | head -5
    curl -s 'http://127.0.0.1:8099/api/z2m/status'
    curl -s 'http://127.0.0.1:8099/api/z2m/lights'
    curl -s -X POST 'http://127.0.0.1:8099/api/ask?text=bar%20auf%20blau'
    ```
    Erwartet: Status `online: true`, die Bar in der Leuchtenliste, und `✓ Bar blau` — die Bar wird tatsächlich blau. Danach `bar auf warmweiss` oder eine Szene, um den Zustand wiederherzustellen.
8. **Nicht tun:** `mqtt_user`/`mqtt_pass` oder den HA-Token in Dateien ausserhalb von `config.json` schreiben; `/api/z2m/permit_join` oder `/api/z2m/restart` in Automatisierungen ohne Rückfrage aufrufen; Timeout- oder Reconnect-Logik in `lichtagent.py` nachbauen — `z2m.py` bringt sie mit.

## HTTP-API (`/api/z2m/*`, Port 8099)

| Endpunkt                                                                                | Zweck                                                                                                    |
| --------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `GET /api/z2m/info`                                                                     | Version, Koordinator, Netzwerk, Maschine, Health, Frontend-Link, Geräte                                  |
| `GET /api/z2m/status`                                                                   | Einzeiler + `online` fürs Panel                                                                          |
| `GET /api/z2m/health`                                                                   | letzter `bridge/health`-Datensatz                                                                        |
| `GET /api/z2m/devices` · `/lights` · `/groups`                                          | Geräte (kompakt), Leuchten mit Zustand, Gruppen                                                          |
| `GET /api/z2m/state?name=<Gerät>[&refresh=1&wait=2]`                                    | Zustand aus dem Cache (`refresh=1`: `/get` senden und bis `wait` s auf die Antwort dieses Geräts warten) |
| `GET /api/z2m/events`                                                                   | letzte Ereignisse (`device_joined`, …) und Warnungen aus `bridge/logging`                                |
| `POST /api/z2m/set?name=&hex=%23rrggbb&brightness=0-100&state=&color_temp=&transition=` | Leuchte steuern (Helligkeit in %, wird auf 0–254 umgerechnet)                                            |
| `POST /api/z2m/toggle?name=`                                                            | an/aus umschalten                                                                                        |
| `POST /api/z2m/get?name=&attr=state,brightness`                                         | Zustand beim Gerät anfordern                                                                             |
| `POST /api/z2m/permit_join?time=254[&device=]`                                          | Anlernen erlauben (`time=0` sperrt)                                                                      |
| `POST /api/z2m/health_check` · `/coordinator_check`                                     | Health-Check / Router-Check des Koordinators                                                             |
| `POST /api/z2m/rename?from=&to=`                                                        | Gerät umbenennen                                                                                         |
| `POST /api/z2m/restart?confirm=1`                                                       | Zigbee2MQTT neu starten (nur mit `confirm=1`)                                                            |

`name` ist der `friendly_name` oder die IEEE-Adresse (keine MQTT-Wildcards `+`/`#`). `wait` ist auf 10 s begrenzt, weil der HTTP-Server von lichtapp.py solange blockiert; aus demselben Grund warten `permit_join`, `health_check`, `coordinator_check`, `rename` und `restart` bis zu 10 s (coordinator_check 30 s) auf die Antwort der Bridge, brechen aber sofort ab, wenn `bridge/state` offline meldet. Zustände der Leuchten werden nach dem Start automatisch per `/get` geholt (Zigbee2MQTT sendet sie nicht retained). Antworten: `{"ok":true,"result":…}` bzw. `{"ok":false,"error":"…"}` (HTTP 400 Parameter, 404 unbekannt, 502 Zigbee2MQTT/MQTT-Fehler).

Beispiele:

```bash
curl -s -X POST 'http://192.168.1.54:8099/api/z2m/set?name=ThomysHomeBar&hex=%230033FF&brightness=70&transition=2'
curl -s -X POST 'http://192.168.1.54:8099/api/z2m/permit_join?time=120'
curl -s 'http://192.168.1.54:8099/api/z2m/lights'
```

## Sprachbefehle (homebrain.py)

- **Neue Ziele:** jede Zigbee-Leuchte aus `bridge/devices` per `friendly_name` (z. B. `ThomysHomeBar`) sowie die Synonyme in `Z2M_GERAETE` (`bar`, `lichtleiste`, `leiste` → `config.json["bar"]`; `wandpanel`, `hue panel`, `zigbee panel` → `config.json["panel"]`). `panel`/`hexagon` bleiben wie bisher die Govee-Panels über Home Assistant.
- **„alles“** umfasst jetzt HA-Lichter **und** alle Zigbee-Leuchten (Farbe, Helligkeit, an/aus) — `ausser …` funktioniert für beide und für alle drei Aktionen, auch im Regel-Fallback (`alles aus ausser küche`).
- **HA und Zigbee sind entkoppelt:** ist Home Assistant nicht erreichbar, werden die Zigbee-Leuchten trotzdem geschaltet, die Antwort enthält zusätzlich `[HA-Fehler: …]`. Reine Zigbee-Ziele (`bar`, `wandpanel`) rufen HA gar nicht mehr auf.
- **Szenen** in `scenes.json` können neben `bar` einen Block `"zigbee": {"<friendly_name>": {…/set-Payload…}}` enthalten, der beim Auslösen an die jeweiligen Leuchten geht.
- Der System-Prompt für Ollama listet die Zigbee-Leuchten dynamisch (`%ZIGBEE%`), damit das Modell sie als `target` benutzen kann.
- Ollama bleibt auf dem Laptop (`OLLAMA_URL = http://192.168.1.54:11434`, unverändert); ein späterer Umzug auf einen Pi 5 wäre möglich, ist hier aber nicht eingeplant.
- Ohne `z2m=` benutzt `HomeBrain` wie vorher `agent.bar()` / `agent.z2m_set()` als Transport. Zwei Neuerungen gelten aber auch dann, weil sie aus `config.json` kommen: die Synonyme aus `Z2M_GERAETE` (`lichtleiste`, `wandpanel`, …) und dass „alles“ die Bar auch bei Helligkeit einschliesst. Wer exakt das alte Verhalten will, nimmt `homebrain.py.bak`.

## Sicherheit / Hinweise

- Port 8099 hat keine Authentifizierung (wie bisher). `permit_join` öffnet das Zigbee-Netz für neue Geräte, `restart` startet Zigbee2MQTT neu — im PicoClaw-`smarthome`-Skill diese beiden Endpunkte nur nach expliziter Bestätigung im Chat aufrufen (analog zu Nuki `unlock`/`open`).
- `z2m.py` abonniert `zigbee2mqtt/#` mit QoS 0. Eigene `/set`- und `/get`-Nachrichten werden ignoriert, alles andere landet im Cache (`states`, `availability`, `devices`, `groups`, `info`, `health`, `events`, `logs`).
- `version_status` im Info-Panel: `ok` = 2.13.0, `newer`/`older` = Zigbee2MQTT wurde aktualisiert bzw. ist älter als `z2m_expected_version` — nach einem Z2M-Update den Wert in `config.json` anpassen.
- Die MQTT-Zugangsdaten stehen nur in `config.json` (chmod 600), nie in diesen Dateien.

## Tests

```bash
python3 test_z2m.py -v
```

Startet einen kleinen MQTT-Broker im Prozess und einen simulierten Zigbee2MQTT 2.13.0 (retained `bridge/*`-Topics, `transaction`-Antworten, Zustände nach `/set`, availability, Events) und prüft Client, API-Routen und HomeBrain-Verhalten — ohne Netzwerk und ohne echte Geräte.
