# homebrain.py: Zigbee-Erweiterung in die laufende Version portieren

Für Hermes (oder wer sonst am Laptop arbeitet). Die laufende `~/lichtagent/homebrain.py` (Stand heute: Lernen, Skills-Statistik, neue Szenen, Halluzinations-Schutz) bleibt die Basis. Aus der Download-Version werden nur die Zigbee-Stücke übernommen. `docs/homebrain_zigbee.diff` ist der vollständige Unterschied zur Original-Datei vom 09.09. (`diff -u`); dieses Dokument listet die Stücke einzeln, damit sie sich auch in eine veränderte Datei einfügen lassen.

**Prinzip:** Alles Zigbee-Spezifische steckt in eigenen Methoden (`_z2m_*`), die vom bestehenden Code nur an wenigen Stellen aufgerufen werden. Ohne Zigbee2MQTT-Client verhält sich alles wie vorher (Transport `agent.z2m_set()`).

## 0. Vorbereitung

```bash
cd ~/lichtagent && cp homebrain.py homebrain.py.vor-zigbee   # Sicherung
python3 -c "import z2m, z2m_api"                              # muss ohne Fehler laufen
```

## 1. Imports und automatischer Zigbee2MQTT-Client (Modulebene, vor `class HomeBrain`)

`import os, sys` ergänzen (falls nicht vorhanden). Dann:

```python
_Z2M_AUTO = None   # ein gemeinsamer Zigbee2MQTT-Client pro Prozess, wenn lichtapp.py keinen übergibt


def _auto_z2m(cfg):
    """Eigenen Zigbee2MQTT-Client aus config.json starten (z2m_autoconnect, Standard: an).

    Damit kennt HomeBrain alle Zigbee-Leuchten, auch wenn lichtapp.py unverändert bleibt.
    Verbindet im Hintergrund; ohne Broker läuft alles wie bisher über agent.z2m_set().
    """
    global _Z2M_AUTO
    if _Z2M_AUTO is not None:
        return _Z2M_AUTO
    if not cfg or not cfg.get("mqtt_host") or cfg.get("z2m_autoconnect") is False:
        return None
    try:
        from z2m import Zigbee2MQTT
        z = Zigbee2MQTT(cfg, client_id="thomyshomeagent-brain-%d" % os.getpid())
        z.start(block_until_connected=False)
    except Exception as e:  # z2m.py fehlt oder Konfiguration unbrauchbar → wie bisher ohne Zigbee-Client
        print("homebrain: kein Zigbee2MQTT-Client (%s)" % e, file=sys.stderr)
        return None
    _Z2M_AUTO = z
    return z
```

Im Konstruktor `HomeBrain.__init__` den Parameter `z2m=None` aufnehmen und setzen:

```python
        self.z2m = z2m if z2m is not None else getattr(agent, "z2m", None)
        if self.z2m is None:
            self.z2m = _auto_z2m(getattr(agent, "cfg", None))
```

## 2. Synonyme für Zigbee-Geräte (Modulebene, neben `RAEUME`)

```python
# Umgangssprachliche Namen -> Zigbee2MQTT-Gerät (friendly_name oder IEEE-Adresse).
# "@bar" / "@panel" werden durch config.json ("bar", "panel") ersetzt.
# Zusätzlich ist jeder friendly_name aus Zigbee2MQTT direkt als Ziel nutzbar.
Z2M_GERAETE = {
    "bar": "@bar", "lichtleiste": "@bar", "leiste": "@bar", "lightbar": "@bar",
    "hue panel": "@panel", "hue-panel": "@panel", "zigbee panel": "@panel", "wandpanel": "@panel",
}
```

`panel`/`hexagon` bleiben in `RAEUME` die Govee-Panels über Home Assistant; die Hue-Panels heissen `wandpanel`, `hue panel` oder direkt mit ihrem Zigbee2MQTT-Namen (`küchen panel`, `spense panel`).

## 3. Neue Hilfsmethoden in `class HomeBrain`

```python
    @staticmethod
    def _ha_excluded(entity_id, exclude=()):
        """True, wenn die HA-Entität zu einem Raum aus der 'except'-Liste gehört."""
        ex_prefix = [RAEUME.get(e.lower(), "") for e in exclude or ()]
        return any(entity_id.startswith(p) for p in ex_prefix if p)

    def _ha_targets(self, target, exclude=()):
        """HA-Entitäten nur ermitteln, wenn das Ziel ein HA-Ziel sein kann (spart den
        HA-Aufruf bei reinen Zigbee-Zielen wie 'bar' — und Zigbee geht auch, wenn HA offline ist)."""
        t = (target or "").lower()
        if t in ("all", "alles") or t in RAEUME or t.startswith("light."):
            return self._entities(target, exclude)
        return []

    # ---------- Zigbee2MQTT-Ziele ----------
    def _z2m_synonyms(self):
        """Alle Synonyme aus Z2M_GERAETE, deren Gerät in config.json bekannt ist."""
        return [k for k, v in Z2M_GERAETE.items() if self._z2m_resolve_alias(v)]

    def _z2m_resolve_alias(self, v):
        if isinstance(v, str) and v.startswith("@"):
            return (self.a.cfg or {}).get(v[1:])
        return v

    def _z2m_light_names(self):
        """friendly_names aller Zigbee-Leuchten aus Zigbee2MQTT (klein geschrieben)."""
        if self.z2m is None:
            return []
        try:
            return [n.lower() for n in self.z2m.light_names()]
        except Exception:
            return []

    def _z2m_names(self, target, exclude=()):
        """Ziel -> Liste von Zigbee2MQTT-friendly_names (leer, wenn kein Zigbee-Ziel)."""
        cfg = self.a.cfg or {}
        t = (target or "").lower()
        if t in ("all", "alles"):
            names = []
            if self.z2m is not None:
                try: names = list(self.z2m.light_names())
                except Exception: names = []
            if cfg.get("bar") and cfg["bar"] not in names:
                names.append(cfg["bar"])
            ex = set()
            for e in exclude:
                ex.update(n.lower() for n in self._z2m_names(e))
            return [n for n in names if n.lower() not in ex]
        if t in Z2M_GERAETE:
            n = self._z2m_resolve_alias(Z2M_GERAETE[t])
            return [n] if n else []
        for n in ([cfg.get("bar"), cfg.get("panel")] + (self.z2m.light_names() if self.z2m is not None else [])):
            if n and n.lower() == t:
                return [n]
        if t.startswith("0x") and self.z2m is not None and self.z2m.device(target):
            return [self.z2m.device(target).get("friendly_name") or target]
        return []

    def _z2m_has_color(self, name):
        """True/False je nach Zigbee2MQTT-Fähigkeiten der Leuchte, None wenn unbekannt (kein z2m)."""
        if self.z2m is None:
            return None
        try:
            l = self.z2m.light(name)
        except Exception:
            return None
        return bool(l["color"]) if l else None

    def _z2m_send(self, name, payload):
        """Schickt ein /set an Zigbee2MQTT — über z2m.py wenn verbunden, sonst wie bisher über den Agenten."""
        if self.z2m is not None and self.z2m.connected:
            self.z2m.set(name, payload)
        else:
            self.a.z2m_set(name, payload)
```

`_entities()` benutzt im `all`-Zweig statt der eigenen `ex_prefix`-Logik jetzt `self._ha_excluded(e, exclude)` (gleiche Wirkung, eine Stelle).

## 4. `_system()`: Zigbee-Leuchten in den Ollama-Prompt

Im `SYSTEM`-Text die Zielzeile ergänzen: `<ziel> ist "all" (alles), "bar", ein Raumname aus: %ROOMS%, oder eine Zigbee-Leuchte aus: %ZIGBEE%.` — und `_system()` so:

```python
    def _system(self):
        rooms = ", ".join(sorted(set(RAEUME.keys())))
        scenes = ", ".join(self._scene_names())
        zig = ", ".join(sorted(set(self._z2m_synonyms()) | set(self._z2m_light_names()))) or "bar"
        return SYSTEM.replace("%ROOMS%", rooms).replace("%SCENES%", scenes).replace("%ZIGBEE%", zig)
```

## 5. `_rules()` (Regel-Fallback), drei Stellen

a) Nach der `for r in RAEUME:`-Schleife (innerhalb des `else:`-Zweigs, vor der `bar`-Prüfung):

```python
            # Zigbee-Leuchten aus Zigbee2MQTT (Synonyme + friendly_names), längste zuerst
            for z in sorted(set(self._z2m_synonyms()) | set(self._z2m_light_names()), key=len, reverse=True):
                if z != "bar" and re.search(r'(?<!\w)' + re.escape(z) + r'(?!\w)', t_main):   # nur ganze Wörter
                    target = z; break
```

b) Direkt nach der bestehenden „alles aus“-Zeile (`if any(w in t for w in ["aus", …`):

```python
        # "alles aus ausser küche": eigenständiges Wort "aus" VOR dem "ausser" → Ausschalten mit Ausnahmen
        if ("ausser" in t or "außer" in t) and re.search(r'\b(aus|ausschalten|ausmachen)\b', re.split(r'\bausser\b|\baußer\b', t)[0]):
            exc = re.findall(r'(?:ausser|außer)\s+([a-zäöüß]+)', t)
            return [{"action": "power", "target": "all", "on": False, "except": exc}]
```

c) Bei `heller`/`dunkler` die Ausnahmen mitgeben: `{"action": "brightness", "target": target, "pct": 85, "except": exc}` bzw. `pct: 25`.

## 6. `execute()`: der Block für `color`, `brightness`, `power`

Die drei bisherigen `elif typ == "color"/"brightness"/"power":`-Zweige durch diesen einen Block ersetzen (HA und Zigbee laufen unabhängig; ein HA-Ausfall stoppt Zigbee nicht; Leuchten ohne Farbfähigkeit werden bei Farbbefehlen nur eingeschaltet):

```python
                elif typ in ("color", "brightness", "power"):
                    exc = act.get("except", []) or []
                    znames = self._z2m_names(tgt, exc)
                    hexc = self._hex(act.get("color")) if typ == "color" else None
                    pct = int(act.get("pct", 60)) if typ == "brightness" else None
                    on = act.get("on", True) if typ == "power" else None
                    # --- Home Assistant (Fehler hier stoppen Zigbee nicht) ---
                    ents, ha_err = [], None
                    try:
                        if typ == "color":
                            ents = self._ha_targets(tgt, exc)
                            if ents:
                                self.a.ha.light(ents, rgb_color=list(_rgb(hexc)), brightness=180, transition=2)
                        elif typ == "brightness":
                            ents = self._ha_targets(tgt, exc) or ([RAEUME[tgt.lower()]] if tgt.lower() in RAEUME else [])
                            if not ents and tgt in ("all", "alles"):     # wie im Original: erst RGB-Lichter, sonst alle eingeschalteten
                                ents = [s["entity_id"] for s in self.a.ha.states()
                                        if s["entity_id"].startswith("light.") and s["state"] == "on"
                                        and not self._ha_excluded(s["entity_id"], exc)]
                            if ents: self.a.ha.light(ents, brightness_pct=pct, transition=2)
                        else:
                            if tgt in ("all", "alles"):
                                ents = [s["entity_id"] for s in self.a.ha.states()
                                        if s["entity_id"].startswith("light.") and s["state"] != "unavailable"
                                        and not self._ha_excluded(s["entity_id"], exc)]
                            else:
                                ents = self._ha_targets(tgt)
                            if ents:
                                if on: self.a.ha.light(ents, transition=2)
                                else: self.a.ha.light_off(ents, transition=2)
                    except Exception as e:
                        ha_err = e
                    # --- Zigbee2MQTT ---
                    no_color = []
                    for zn in znames:
                        if typ == "color":
                            if zn == cfg.get("bar"):
                                self.a.bar(hexc)                       # bewährter Weg für die Bar
                            elif self._z2m_has_color(zn) is False:     # z. B. Hue-Panel „White Ambiance“: nur an + hell
                                self._z2m_send(zn, {"state": "ON", "brightness": 180, "transition": 2})
                                no_color.append(zn)
                            else:
                                self._z2m_send(zn, {"state": "ON", "color": {"hex": hexc.upper()}, "brightness": 180, "transition": 2})
                        elif typ == "brightness":
                            self._z2m_send(zn, {"state": "ON", "brightness": int(pct*2.54), "transition": 2})
                        else:
                            self._z2m_send(zn, {"state": "ON" if on else "OFF", "transition": 2})
                    if not ents and not znames and ha_err is None:
                        done.append(f"'{tgt}' unbekannt")
                    elif typ == "color":
                        label = "Bar" if tgt == "bar" else tgt
                        done.append(f"{label} {act.get('color')}" + (f" (ausser {','.join(exc)})" if exc else "")
                                    + (f" ({', '.join(no_color)}: kein Farblicht, nur an)" if no_color else ""))
                    elif typ == "brightness":
                        done.append(f"{tgt} {pct}%" + (f" (ausser {','.join(exc)})" if exc else ""))
                    else:
                        done.append(f"{tgt} {'an' if on else 'aus'}" + (f" (ausser {','.join(exc)})" if exc else ""))
                    if ha_err is not None:
                        done.append(f"[HA-Fehler: {ha_err}]")
```

Dazu im Szenen-Zweig (`sc.get("bar")`): `self._z2m_send(cfg["bar"], sc["bar"])` statt `self.a.z2m_set(...)`, und optional der neue `zigbee`-Block:

```python
                            for zname, zpayload in (sc.get("zigbee") or {}).items():
                                self._z2m_send(zname, zpayload)
```

Achtung: Im Block wird `cfg = self.a.cfg or {}` am Anfang von `execute()` erwartet (`cfg` statt `self.a.cfg`).

## 7. Prüfen

```bash
cd ~/lichtagent && python3 -m py_compile homebrain.py
python3 - <<'PY'
import homebrain, json
class HA:                                   # Attrappe: kein echtes HA nötig
    def states(self): return []
    def light(self, *a, **k): print("HA light", a, k)
    def light_off(self, *a, **k): print("HA off", a, k)
class Agent:
    cfg = json.load(open("config.json")); ha = HA()
    def bar(self, h): print("bar", h)
    def z2m_set(self, n, p): print("z2m_set", n, p)
hb = homebrain.HomeBrain(Agent())
import time; time.sleep(2)                  # Zigbee2MQTT-Client verbindet im Hintergrund
print("Zigbee-Leuchten:", hb.z2m.light_names() if hb.z2m else "kein Client")
print(hb._rules("küchen panel dunkler"))
print(hb._rules("alles aus ausser küche"))
PY
systemctl --user restart thomyshomeagent.service
curl -s -X POST 'http://127.0.0.1:8099/api/ask?text=k%C3%BCchen%20panel%20dunkler'
```

Erwartet: die Leuchtennamen aus Zigbee2MQTT, `[{'action': 'brightness', 'target': 'küchen panel', 'pct': 25, 'except': []}]`, und nach dem Neustart dimmt das Küchen-Panel wirklich.

## Und lichtapp.py?

Muss für Zigbee-Seite, `/api/z2m/*` und Handy-App **nicht** geändert werden: `thomyshome_proxy.py` (mit `thomyshome-proxy.service`) läuft auf Port 8098 vor dem Dashboard, siehe README „Ohne Änderung an lichtapp.py“. Falls die Dateien noch fehlen: aktuellen Stand erneut herunterladen (Befehl in der README, Abschnitt Installation).
