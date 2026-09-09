#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
z2m_api.py — HTTP-Endpunkte /api/z2m/* für ThomysHomeAgent (lichtapp.py)
=========================================================================
Einbindung in lichtapp.py (3 Zeilen, siehe README):

    from z2m import Zigbee2MQTT
    from z2m_api import Z2MApi
    z2m = Zigbee2MQTT(cfg); z2m.start(block_until_connected=False)
    z2m_api = Z2MApi(z2m)
    ...
    # im HTTP-Handler, bevor die eigenen Routen geprüft werden:
    hit = z2m_api.handle(method, path, params)   # params = dict aus der Query
    if hit is not None:
        status, body = hit
        -> body als JSON mit HTTP-Status 'status' zurückgeben

Alle Endpunkte (GET = lesen, POST = ausführen; Parameter als Query-String):

    GET  /api/z2m/info                   Version, Koordinator, Health, Frontend-Link …
    GET  /api/z2m/status                 Einzeiler fürs Info-Panel
    GET  /api/z2m/health                 letzter bridge/health-Datensatz
    GET  /api/z2m/devices                alle Geräte (kompakt)
    GET  /api/z2m/lights                 alle Leuchten mit Zustand
    GET  /api/z2m/groups                 Gruppen
    GET  /api/z2m/state?name=            Zustand eines Geräts (refresh=1 → vorher /get senden)
    GET  /api/z2m/events                 letzte Ereignisse + Warnungen
    POST /api/z2m/set?name=&hex=&brightness=&state=&color_temp=&transition=
    POST /api/z2m/toggle?name=
    POST /api/z2m/get?name=&attr=state
    POST /api/z2m/permit_join?time=254&device=
    POST /api/z2m/health_check
    POST /api/z2m/coordinator_check
    POST /api/z2m/rename?from=&to=
    POST /api/z2m/restart?confirm=1      (nur mit confirm=1)

Standalone zum Testen (ohne lichtapp.py):  python3 z2m_api.py [config.json] [port]
"""
import json
import time
import urllib.parse

from z2m import Zigbee2MQTT, Z2MError, MqttError

PREFIX = "/api/z2m/"


def _first(params, key, default=None):
    v = params.get(key, default)
    if isinstance(v, (list, tuple)):
        v = v[0] if v else default
    return v


def _compact_device(d, z2m):
    definition = d.get("definition") or {}
    name = d.get("friendly_name") or d.get("ieee_address")
    return {
        "friendly_name": name,
        "ieee_address": d.get("ieee_address"),
        "type": d.get("type"),
        "supported": d.get("supported"),
        "disabled": d.get("disabled", False),
        "vendor": definition.get("vendor"),
        "model": definition.get("model"),
        "description": definition.get("description") or d.get("description"),
        "power_source": d.get("power_source"),
        "interview_state": d.get("interview_state"),
        "available": z2m.availability.get(name),
        "state": z2m.states.get(name),
    }


class Z2MApi:
    def __init__(self, z2m):
        self.z2m = z2m

    # ---------- Router ----------
    def handle(self, method, path, params=None):
        """Gibt (status, body) zurück oder None, wenn der Pfad nicht zu /api/z2m gehört."""
        if not path.startswith(PREFIX):
            return None
        action = path[len(PREFIX):].strip("/")
        params = params or {}
        method = (method or "GET").upper()
        fn = getattr(self, "%s_%s" % (method.lower(), action), None)
        if fn is None:
            if getattr(self, "get_%s" % action, None) or getattr(self, "post_%s" % action, None):
                return 405, {"ok": False, "error": "Methode %s nicht erlaubt für %s" % (method, action)}
            return 404, {"ok": False, "error": "Unbekannter Endpunkt %s" % path}
        try:
            result = fn(params)
            if isinstance(result, tuple):
                return result
            return 200, {"ok": True, "result": result}
        except (Z2MError, MqttError) as e:
            return 502, {"ok": False, "error": str(e)}
        except ValueError as e:
            return 400, {"ok": False, "error": "Ungültiger Parameter: %s" % e}

    # ---------- Lesen ----------
    def get_info(self, params):
        return self.z2m.summary()

    def get_status(self, params):
        return {"text": self.z2m.status_text(), "online": self.z2m.summary()["online"]}

    def get_health(self, params):
        return {"bridge_state": self.z2m.bridge_state, "health": self.z2m.health}

    def get_devices(self, params):
        return [_compact_device(d, self.z2m) for d in list(self.z2m.devices)]

    def get_lights(self, params):
        return self.z2m.lights()

    def get_groups(self, params):
        return list(self.z2m.groups)

    def get_state(self, params):
        name = self._name(params)
        if _first(params, "refresh") in ("1", "true", "yes"):
            self.z2m.get(name)
            deadline = time.time() + float(_first(params, "wait", 2))
            before = self.z2m.last_message_at
            while time.time() < deadline and self.z2m.last_message_at == before:
                time.sleep(0.05)
        st = self.z2m.state(name)
        if st is None:
            return 404, {"ok": False, "error": "Kein Zustand bekannt für '%s'" % name}
        return {"name": name, "state": st, "available": self.z2m.availability.get(name)}

    def get_events(self, params):
        return {"events": list(self.z2m.events), "warnings": list(self.z2m.logs)}

    # ---------- Steuern ----------
    def post_set(self, params):
        name = self._name(params)
        kwargs = {}
        if _first(params, "hex"):
            kwargs["hex_color"] = _first(params, "hex")
        if _first(params, "brightness") not in (None, ""):
            kwargs["brightness_pct"] = float(_first(params, "brightness"))
        if _first(params, "state"):
            kwargs["state"] = _first(params, "state")
        if _first(params, "color_temp") not in (None, ""):
            kwargs["color_temp"] = int(_first(params, "color_temp"))
        if _first(params, "transition") not in (None, ""):
            kwargs["transition"] = float(_first(params, "transition"))
        payload = self.z2m.light_set(name, **kwargs)
        return {"name": name, "sent": payload}

    def post_toggle(self, params):
        name = self._name(params)
        return {"name": name, "sent": self.z2m.toggle(name)}

    def post_get(self, params):
        name = self._name(params)
        attrs = [a for a in str(_first(params, "attr", "state")).split(",") if a]
        self.z2m.get(name, attrs)
        return {"name": name, "requested": attrs}

    def post_permit_join(self, params):
        seconds = int(_first(params, "time", 254))
        device = _first(params, "device") or None
        return self.z2m.permit_join(seconds, device)

    def post_health_check(self, params):
        return self.z2m.health_check()

    def post_coordinator_check(self, params):
        return self.z2m.coordinator_check()

    def post_rename(self, params):
        old, new = _first(params, "from"), _first(params, "to")
        if not old or not new:
            raise ValueError("from und to erforderlich")
        return self.z2m.rename(old, new, _first(params, "homeassistant_rename") in ("1", "true"))

    def post_restart(self, params):
        if _first(params, "confirm") not in ("1", "true", "yes"):
            return 400, {"ok": False, "error": "Neustart nur mit confirm=1"}
        return self.z2m.restart()

    # ---------- Hilfen ----------
    @staticmethod
    def _name(params):
        name = _first(params, "name")
        if not name:
            raise ValueError("Parameter 'name' fehlt")
        return name


# ---------------------------------------------------------------------------
# Standalone-Server (nur zum Testen / Vorschau des Info-Panels)
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html><meta charset="utf-8"><title>Zigbee2MQTT · ThomysHomeAgent</title>
<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 1em;background:#111;color:#eee}
h1{font-size:1.3em}table{border-collapse:collapse;width:100%}td{padding:.3em .5em;border-bottom:1px solid #333;vertical-align:top}
td:first-child{color:#9ab;width:34%}.ok{color:#5d5}.warn{color:#fb4}.bad{color:#f55}button{margin:.2em;padding:.4em .8em}
pre{background:#000;padding:.6em;overflow:auto;font-size:.85em}</style>
<h1>Zigbee2MQTT in ThomysHomeAgent</h1>
<p id="status">lade …</p>
<table id="info"></table>
<h2>Leuchten</h2><div id="lights"></div>
<h2>Roh-Daten</h2><pre id="raw"></pre>
<script>
const $=s=>document.querySelector(s);
async function api(p,m){const r=await fetch(p,{method:m||'GET'});return r.json();}
function row(k,v,cls){return `<tr><td>${k}</td><td class="${cls||''}">${v??'–'}</td></tr>`}
async function refresh(){
  const {result:s}=await api('/api/z2m/info');
  const st=await api('/api/z2m/status');
  $('#status').innerHTML=`<b class="${s.online?'ok':'bad'}">${st.result.text}</b>`;
  const vcls={ok:'ok',newer:'warn',older:'bad'}[s.version_status]||'';
  $('#info').innerHTML=[
    row('Zigbee2MQTT-Version',`${s.version??'?'} (erwartet ${s.expected_version}, Commit ${s.commit??'?'})`,vcls),
    row('Frontend',s.frontend_url?`<a href="${s.frontend_url}" target="_blank" style="color:#8cf">${s.frontend_url}</a>`:'–'),
    row('zigbee-herdsman-converters',s.zigbee_herdsman_converters),row('zigbee-herdsman',s.zigbee_herdsman),
    row('Koordinator',`${s.coordinator.type??'?'} · ${s.coordinator.ieee_address??'?'} · Revision ${s.coordinator.revision??'?'}`),
    row('Netzwerk',`Kanal ${s.network.channel??'?'}, PAN-ID ${s.network.pan_id??'?'}`),
    row('Maschine',`${s.os??'?'} · CPU: ${s.cpus??'?'} · RAM: ${s.memory_mb??'?'} MB · Node ${s.node_version??'?'}`),
    row('MQTT (Z2M-Seite)',`${s.z2m_mqtt_server??'?'} (Protokoll ${s.z2m_mqtt_version??'?'})`),
    row('MQTT (unsere Seite)',`${s.mqtt_host} · ${s.mqtt_connected?'verbunden':'getrennt'} ${s.mqtt_error?'· '+s.mqtt_error:''}`,s.mqtt_connected?'ok':'bad'),
    row('Bridge',s.bridge_state,s.bridge_state==='online'?'ok':'bad'),
    row('Anlernen (permit_join)',s.permit_join?`offen bis ${new Date(s.permit_join_end*1000).toLocaleTimeString()}`:'gesperrt',s.permit_join?'warn':''),
    row('Neustart nötig',s.restart_required?'ja':'nein',s.restart_required?'warn':''),
    row('Health',s.health.uptime_sec!=null?`Laufzeit ${Math.round(s.health.uptime_sec/60)} min · Z2M ${s.health.process_memory_mb} MB · System-RAM ${s.health.os_memory_percent}% · Load ${JSON.stringify(s.health.os_load_average)}`:'noch kein Health-Check empfangen'),
    row('Geräte',`${s.devices_total} (davon ${s.lights_total} Leuchten)${s.devices_offline.length?' · offline: '+s.devices_offline.join(', '):''}`,s.devices_offline.length?'warn':''),
    row('Letzte Warnungen',s.last_warnings.map(w=>`[${w.level}] ${w.message}`).join('<br>')||'keine'),
  ].join('');
  const {result:lights}=await api('/api/z2m/lights');
  $('#lights').innerHTML=lights.map(l=>`<div>${l.friendly_name} — ${l.description??''} · <b>${l.state??'?'}</b> ${l.brightness!=null?Math.round(l.brightness/2.54)+'%':''}
    ${l.available==='offline'?'<span class="bad">offline</span>':''}
    <button onclick="api('/api/z2m/toggle?name=${encodeURIComponent(l.friendly_name)}','POST').then(refresh)">an/aus</button>
    ${l.color?['#FF1010','#00C000','#0033FF','#FF6A00','#FFFFFF'].map(h=>`<button style="background:${h}" onclick="api('/api/z2m/set?name=${encodeURIComponent(l.friendly_name)}&hex=${encodeURIComponent(h)}&transition=1','POST').then(refresh)">&nbsp;</button>`).join(''):''}
  </div>`).join('')||'keine Leuchten in bridge/devices gefunden';
  $('#raw').textContent=JSON.stringify(s,null,1);
}
refresh();setInterval(refresh,5000);
</script>"""


def serve(cfg, port=8098):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    z2m = Zigbee2MQTT(cfg)
    z2m.start(block_until_connected=False)
    api = Z2MApi(z2m)

    class Handler(BaseHTTPRequestHandler):
        def _route(self):
            u = urllib.parse.urlsplit(self.path)
            params = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            if u.path in ("/", "/index.html"):
                body = _PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            hit = api.handle(self.command, u.path, params)
            if hit is None:
                hit = (404, {"ok": False, "error": "nicht gefunden"})
            status, payload = hit
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _route

        def log_message(self, fmt, *args):  # ruhig bleiben
            pass

    srv = HTTPServer(("0.0.0.0", port), Handler)
    print("Zigbee2MQTT-Testserver auf http://0.0.0.0:%d  (Strg+C beendet)" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        z2m.stop()


if __name__ == "__main__":
    import sys
    from z2m import load_config
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8098
    serve(load_config(cfg_path), port)
