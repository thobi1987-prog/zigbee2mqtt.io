#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
homebrain.py — Das "Gehirn" des Home-Agenten
=============================================
Wandelt natürliche Sprache ("mach Abendlicht", "Bar auf blau",
"alles grün ausser Küche") in Aktionen um und führt sie über den
Lichtagenten (HA + Zigbee-Bar) aus. Nutzt ein LOKALES Modell via Ollama.

  from homebrain import HomeBrain
  hb = HomeBrain(agent)          # agent = lichtagent.Lichtagent
  print(hb.handle("mach die bar blau und den rest gemütlich"))

Fällt Ollama aus, greift ein regelbasierter Parser als Fallback.

Zigbee2MQTT-Integration (z2m.py):
  hb = HomeBrain(agent, z2m=z2m)   # z2m = z2m.Zigbee2MQTT(cfg), gestartet
  Damit sind ALLE Zigbee-Leuchten aus Zigbee2MQTT (bridge/devices) Ziele —
  per friendly_name ("panel", "ThomysHomeBar") oder Synonym (Z2M_GERAETE),
  und "alles" umfasst HA-Lichter + Zigbee-Leuchten. Ohne z2m verhält sich
  alles wie bisher (nur die Bar über agent.z2m_set / agent.bar).
"""
import json, os, re, sys, urllib.request

OLLAMA_URL = "http://192.168.1.54:11434"
DEFAULT_MODEL = "llama3.2:3b"

FARBEN = {
    "rot": "#FF1010", "grün": "#00C000", "gruen": "#00C000", "blau": "#0033FF",
    "gelb": "#FFDF00", "orange": "#FF6A00", "pink": "#FF1493", "magenta": "#FF00AA",
    "türkis": "#00E0C0", "tuerkis": "#00E0C0", "lila": "#8A2BE2", "violett": "#8A2BE2",
    "weiss": "#FFFFFF", "weiß": "#FFFFFF", "warmweiss": "#FF8A3D", "bernstein": "#FF6A1E",
}
# Umgangssprachliche Raum-Synonyme -> HA-Entitätspräfix (Gruppe)
RAEUME = {
    "wohnzimmer": "light.stube", "wohnen": "light.stube", "stube": "light.stube",
    "star": "light.star", "polster": "light.polstergruppe_polstergruppe",
    "polstergruppe": "light.polstergruppe_polstergruppe", "lunox": "light.lunox_lunox",
    "küche": "light.kochinsel_kochinsel", "kueche": "light.kochinsel_kochinsel",
    "kochinsel": "light.kochinsel_kochinsel", "essen": "light.essen_essen",
    "esszimmer": "light.esszimmer", "flur": "light.flur_flur", "gang": "light.flur_flur",
    "wc": "light.wc_wc", "bad": "light.wc_wc", "dusche": "light.wc_dusche",
    "balkon": "light.balkon_balkon", "terrasse": "light.balkon_balkon",
    "treppe": "light.treppe_eingang_treppe_eingang", "eingang": "light.treppe_eingang_treppe_eingang",
    "obergeschoss": "light.obergeschoss", "og": "light.treppe_og_dg_treppe_og_dg",
    # Govee Glide Hexa (H606A), lokal via govee_light_local
    "hexagon": "light.h606a", "hexagons": "light.h606a", "hexa": "light.h606a",
    "panels": "light.h606a", "panel": "light.h606a", "govee": "light.h606a",
    "sechsecke": "light.h606a", "waben": "light.h606a",
}
# Umgangssprachliche Namen -> Zigbee2MQTT-Gerät (friendly_name oder IEEE-Adresse).
# "@bar" / "@panel" werden durch config.json ("bar", "panel") ersetzt.
# Zusätzlich ist jeder friendly_name aus Zigbee2MQTT direkt als Ziel nutzbar.
Z2M_GERAETE = {
    "bar": "@bar", "lichtleiste": "@bar", "leiste": "@bar", "lightbar": "@bar",
    "hue panel": "@panel", "hue-panel": "@panel", "zigbee panel": "@panel", "wandpanel": "@panel",
}

SYSTEM = """Du bist die Steuer-KI für ein Smart Home (Home Assistant + Zigbee-Leuchten über Zigbee2MQTT, u.a. eine Lichtleiste "Bar").
Antworte NUR mit einem JSON-Objekt der Form {"actions":[ ... ]} — keine Erklärung, kein Text drumherum.
Zerlege zusammengesetzte Befehle in MEHRERE Aktionen (jeder Teil = eine Aktion).
Nutze "scene" NUR bei einem echten Szenennamen (siehe Liste); wenn einzelne Räume/Farben/Helligkeiten genannt sind, nutze color/brightness/power je Ziel.

Aktionstypen:
- {"action":"scene","name":"<szene>"}          mögliche Szenen: %SCENES%
- {"action":"color","target":"<ziel>","color":"<farbe-oder-#hex>","except":["<raum>",...]}
- {"action":"brightness","target":"<ziel>","pct":<0-100>}
- {"action":"power","target":"<ziel>","on":true|false}
- {"action":"lock","do":"lock|unlock|open"}   (Nuki-Tür: abschliessen / aufschliessen / Tür öffnen)

<ziel> ist "all" (alles), "bar", ein Raumname aus: %ROOMS%, oder eine Zigbee-Leuchte aus: %ZIGBEE%.
Farben: rot, grün, blau, gelb, orange, pink, türkis, lila, weiss — oder #RRGGBB.
"except" nur bei target "all" nutzen, um Räume auszunehmen.

Beispiele:
"mach abendlicht" -> {"actions":[{"action":"scene","name":"abend"}]}
"bar auf blau" -> {"actions":[{"action":"color","target":"bar","color":"blau"}]}
"alles grün ausser küche" -> {"actions":[{"action":"color","target":"all","color":"grün","except":["küche"]}]}
"wohnzimmer heller" -> {"actions":[{"action":"brightness","target":"wohnzimmer","pct":85}]}
"alles aus" -> {"actions":[{"action":"power","target":"all","on":false}]}
"mach das wohnzimmer orange und schliess die tür ab" -> {"actions":[{"action":"color","target":"wohnzimmer","color":"orange"},{"action":"lock","do":"lock"}]}
"bar auf lila, essen gedimmt, küche warm" -> {"actions":[{"action":"color","target":"bar","color":"lila"},{"action":"brightness","target":"essen","pct":25},{"action":"brightness","target":"küche","pct":60}]}
"""

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


class HomeBrain:
    def __init__(self, agent, model=DEFAULT_MODEL, z2m=None):
        self.a = agent
        self.model = model
        # Zigbee2MQTT-Client (z2m.Zigbee2MQTT): übergeben, am Agenten, oder automatisch aus config.json
        self.z2m = z2m if z2m is not None else getattr(agent, "z2m", None)
        if self.z2m is None:
            self.z2m = _auto_z2m(getattr(agent, "cfg", None))

    def _load_scenes(self):
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        for p in ("/data/scenes.json", os.path.join(here, "scenes.json")):
            if os.path.exists(p):
                try:
                    with open(p) as f: return json.load(f)
                except Exception: pass
        return []

    def _scene_names(self):
        return ["abend", "hell", "aus", "brasilien"] + \
               [s.get("name") for s in self._load_scenes() if s.get("name")]

    def _system(self):
        rooms = ", ".join(sorted(set(RAEUME.keys())))
        scenes = ", ".join(self._scene_names())
        zig = ", ".join(sorted(set(self._z2m_synonyms()) | set(self._z2m_light_names()))) or "bar"
        return SYSTEM.replace("%ROOMS%", rooms).replace("%SCENES%", scenes).replace("%ZIGBEE%", zig)

    # ---------- LLM ----------
    def _ollama(self, text):
        body = {"model": self.model, "stream": False, "format": "json",
                "messages": [{"role": "system", "content": self._system()},
                             {"role": "user", "content": text}],
                "options": {"temperature": 0.0}}
        req = urllib.request.Request(OLLAMA_URL + "/api/chat",
              data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read())["message"]["content"]
        data = json.loads(out)
        if isinstance(data, dict):
            data = data.get("actions", [data])          # {"actions":[...]} auspacken
        if not isinstance(data, list):
            data = [data]
        actions = [a for a in data if isinstance(a, dict) and a.get("action")]
        if not actions:
            raise ValueError("keine gültigen Aktionen")   # -> Regel-Fallback
        return actions

    def interpret(self, text):
        try:
            return self._ollama(text)
        except Exception:
            return self._rules(text)   # Fallback

    # ---------- Regelbasierter Fallback ----------
    def _rules(self, text):
        t = text.lower()
        if "tür" in t or "tuer" in t or "schloss" in t or "nuki" in t:
            if "öffn" in t or "offn" in t: do = "open"
            elif "auf" in t or "aufschl" in t or "aufsperr" in t or "entrieg" in t: do = "unlock"
            else: do = "lock"
            return [{"action": "lock", "do": do}]
        for s in self._load_scenes():                     # eigene Szenen (Sommerblau, Gemütlich bunt …)
            nm = (s.get("name") or "").lower()
            if nm and nm in t:
                return [{"action": "scene", "name": s["name"]}]
        if any(w in t for w in ["aus", "ausschalten", "aus machen", "licht aus"]) and "ausser" not in t and "außer" not in t:
            return [{"action": "power", "target": "all", "on": False}]
        # "alles aus ausser küche": eigenständiges Wort "aus" VOR dem "ausser" → Ausschalten mit Ausnahmen
        if ("ausser" in t or "außer" in t) and re.search(r'\b(aus|ausschalten|ausmachen)\b', re.split(r'\bausser\b|\baußer\b', t)[0]):
            exc = re.findall(r'(?:ausser|außer)\s+([a-zäöüß]+)', t)
            return [{"action": "power", "target": "all", "on": False, "except": exc}]
        for kw, name in [("abend", "abend"), ("gemütlich", "abend"), ("hell", "hell"),
                         ("arbeitslicht", "hell"), ("brasil", "brasilien"), ("party", "brasilien")]:
            if kw in t:
                return [{"action": "scene", "name": name}]
        col = next((c for c in FARBEN if c in t), None)
        exc = re.findall(r'(?:ausser|außer)\s+([a-zäöüß]+)', t)
        t_main = re.split(r'\bausser\b|\baußer\b', t)[0]      # nur der Teil VOR "ausser"
        if any(w in t_main for w in ("alles", "ganze", "überall", "gesamt")):
            target = "all"
        else:
            target = "all"
            for r in RAEUME:
                if r in t_main:
                    target = r; break
            # Zigbee-Leuchten aus Zigbee2MQTT (Synonyme + friendly_names), längste zuerst
            for z in sorted(set(self._z2m_synonyms()) | set(self._z2m_light_names()), key=len, reverse=True):
                if z != "bar" and re.search(r'(?<!\w)' + re.escape(z) + r'(?!\w)', t_main):   # nur ganze Wörter
                    target = z; break
        if "bar" in t_main.split():
            target = "bar"
        if col:
            return [{"action": "color", "target": target, "color": col, "except": exc}]
        if "heller" in t: return [{"action": "brightness", "target": target, "pct": 85, "except": exc}]
        if "dunkler" in t or "dimm" in t: return [{"action": "brightness", "target": target, "pct": 25, "except": exc}]
        return []

    # ---------- Ausführung ----------
    @staticmethod
    def _ha_excluded(entity_id, exclude=()):
        """True, wenn die HA-Entität zu einem Raum aus der 'except'-Liste gehört."""
        ex_prefix = [RAEUME.get(e.lower(), "") for e in exclude or ()]
        return any(entity_id.startswith(p) for p in ex_prefix if p)

    def _entities(self, target, exclude=()):
        """Ziel -> Liste von HA-Entitäten (nur RGB-fähige, für Farbe)."""
        states = self.a.ha.states()
        def rgb_on(s):
            cm = s["attributes"].get("supported_color_modes") or []
            return any(m in cm for m in ("xy", "hs", "rgb"))
        if target in ("all", "alles"):
            out = []
            for s in states:
                e = s["entity_id"]
                if not e.startswith("light.") or s["state"] == "unavailable": continue
                if "ambilight" in e: continue
                if self._ha_excluded(e, exclude): continue
                if rgb_on(s): out.append(e)
            return out
        ent = RAEUME.get(target.lower())
        if ent: return [ent]
        if target.startswith("light."): return [target]
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

    def _ha_targets(self, target, exclude=()):
        """HA-Entitäten nur ermitteln, wenn das Ziel ein HA-Ziel sein kann (spart den
        HA-Aufruf bei reinen Zigbee-Zielen wie 'bar' — und Zigbee geht auch, wenn HA offline ist)."""
        t = (target or "").lower()
        if t in ("all", "alles") or t in RAEUME or t.startswith("light."):
            return self._entities(target, exclude)
        return []

    def _hex(self, color):
        c = (color or "").strip().lower()
        if c.startswith("#"): return c
        return FARBEN.get(c, "#FF6A00")

    def execute(self, actions):
        done = []
        cfg = self.a.cfg or {}
        for act in actions:
            if not isinstance(act, dict):
                continue
            typ = act.get("action")
            tgt = act.get("target", "all")
            try:
                if typ == "scene":
                    n = str(act.get("name", "abend"))
                    fn = {"abend": self.a.szene_abend, "hell": self.a.szene_hell,
                          "aus": self.a.szene_aus, "brasilien": self.a.szene_brasilien}.get(n.lower())
                    if fn:
                        fn(); done.append(f"Szene {n}")
                    else:
                        sc = next((s for s in self._load_scenes()
                                   if s.get("name", "").lower() == n.lower()), None)
                        if sc:
                            if sc.get("ha_scene"):
                                self.a.ha.call_service("scene", "turn_on", {"entity_id": sc["ha_scene"]})
                            if sc.get("bar"):
                                self._z2m_send(cfg["bar"], sc["bar"])
                            for zname, zpayload in (sc.get("zigbee") or {}).items():   # optional: weitere Zigbee-Leuchten je Szene
                                self._z2m_send(zname, zpayload)
                            done.append("Szene " + sc["name"])
                        else:
                            done.append(f"Szene '{n}' unbekannt")
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
                elif typ == "lock":
                    base = cfg.get("nuki_base")
                    do = act.get("do", "lock")
                    code = {"lock": "2", "unlock": "1", "open": "3"}.get(do)
                    if base and code:
                        m = self.a._mqtt()
                        try: m.publish(f"{base}/lockAction", code)
                        finally: m.close()
                        done.append("Tür " + {"lock": "abgeschlossen", "unlock": "aufgeschlossen", "open": "geöffnet"}[do])
            except Exception as e:
                done.append(f"[Fehler bei {typ}: {e}]")
        return done

    def handle(self, text):
        actions = self.interpret(text)
        if not actions:
            return "Das habe ich nicht verstanden. Versuch z.B. 'mach Abendlicht', 'Bar auf blau', 'alles grün ausser Küche'."
        done = self.execute(actions)
        return "✓ " + "; ".join(done) if done else "Nichts ausgeführt."

def _rgb(h):
    h = h.lstrip("#")
    return int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
