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
    GET  /api/z2m/activity[?limit=50]   Zustandsänderungen je Gerät, neueste zuerst („Aktuelle Aktivität“)
    GET  /zigbee                         Zigbee-Oberfläche (HTML) → Z2MApi.page(path), siehe z2m_page.py
    POST /api/z2m/set?name=&hex=&brightness=&state=&color_temp=&transition=
    POST /api/z2m/toggle?name=
    POST /api/z2m/get?name=&attr=state
    POST /api/z2m/permit_join?time=254&device=
    POST /api/z2m/health_check
    POST /api/z2m/coordinator_check
    POST /api/z2m/rename?from=&to=
    POST /api/z2m/restart?confirm=1      (nur mit confirm=1)

Standalone zum Testen (ohne lichtapp.py):  python3 z2m_api.py [config.json] [port] [server.crt server.key]
Die Vorschau-Seite ist dank pwa.py auf dem Handy als App installierbar (siehe README).
"""
import json
import urllib.parse

from z2m import Zigbee2MQTT, Z2MError, MqttError, normalize_hex

try:
    import pwa                      # Startbildschirm-App (optional)
except ImportError:                 # pragma: no cover
    pwa = None
try:
    import z2m_page                 # Zigbee-Oberfläche /zigbee (optional)
except ImportError:                 # pragma: no cover
    z2m_page = None

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
        "available": z2m.available(name),
        "state": z2m.state(name),
    }


def _check_name(name):
    """Gerätename für MQTT-Topics prüfen: keine Wildcards, keine leeren Segmente."""
    name = str(name)
    if not name or any(c in name for c in "+#\x00") or name.startswith("/") or name.endswith("/") or "//" in name:
        raise ValueError("Ungültiger Gerätename %r" % name)
    return name


def _num(params, key, lo, hi, cast=float):
    """Zahl aus den Query-Parametern lesen und auf [lo, hi] prüfen (None wenn nicht angegeben)."""
    v = _first(params, key)
    if v in (None, ""):
        return None
    try:
        x = cast(str(v).strip())
    except (ValueError, TypeError, OverflowError):
        raise ValueError("%s muss eine Zahl sein" % key)
    if isinstance(x, float) and (x != x or x in (float("inf"), float("-inf"))):
        raise ValueError("%s muss eine endliche Zahl sein" % key)
    if not (lo <= x <= hi):
        raise ValueError("%s muss zwischen %s und %s liegen" % (key, lo, hi))
    return x


class Z2MApi:
    GET_ROUTES = {
        "info": "get_info", "status": "get_status", "health": "get_health", "devices": "get_devices",
        "lights": "get_lights", "groups": "get_groups", "state": "get_state", "events": "get_events",
        "activity": "get_activity",
    }
    POST_ROUTES = {
        "set": "post_set", "toggle": "post_toggle", "get": "post_get", "permit_join": "post_permit_join",
        "health_check": "post_health_check", "coordinator_check": "post_coordinator_check",
        "rename": "post_rename", "restart": "post_restart",
    }
    MAX_WAIT = 10.0   # Sekunden; blockiert den (einfädigen) HTTP-Server von lichtapp.py

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
        if method not in ("GET", "POST"):
            return 405, {"ok": False, "error": "Methode %s nicht erlaubt" % method}
        table, other = (self.GET_ROUTES, self.POST_ROUTES) if method == "GET" else (self.POST_ROUTES, self.GET_ROUTES)
        if action not in table:
            if action in other:
                return 405, {"ok": False, "error": "Methode %s nicht erlaubt für %s" % (method, action)}
            return 404, {"ok": False, "error": "Unbekannter Endpunkt %s" % path}
        try:
            result = getattr(self, table[action])(params)
            if isinstance(result, tuple):
                return result
            return 200, {"ok": True, "result": result}
        except (ValueError, TypeError, OverflowError) as e:
            return 400, {"ok": False, "error": "Ungültiger Parameter: %s" % e}
        except (Z2MError, MqttError) as e:
            return 502, {"ok": False, "error": str(e)}

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
            st = self.z2m.refresh_state(name, timeout=self._wait(params))
        else:
            st = self.z2m.state(name)
        if st is None:
            return 404, {"ok": False, "error": "Kein Zustand bekannt für '%s'" % name}
        return {"name": name, "friendly_name": self.z2m.friendly_name(name), "state": st,
                "available": self.z2m.available(name)}

    def get_events(self, params):
        return {"events": list(self.z2m.events), "warnings": list(self.z2m.logs)}

    def get_activity(self, params):
        limit = _num(params, "limit", 1, 200, int) or 50
        return list(reversed(list(self.z2m.activity)))[:limit]

    # ---------- Zigbee-Oberfläche (HTML) ----------
    def page(self, path, head_extra=None):
        """(status, content_type, body) für die Zigbee-Seite unter /zigbee, sonst None.

        head_extra: zusätzliche <head>-Zeilen, z. B. pwa.head_tags() für die Handy-App.
        """
        if z2m_page is None or path.rstrip("/") != z2m_page.PAGE_PATH:
            return None
        if head_extra is None and pwa is not None:
            head_extra = pwa.head_tags()
        return 200, "text/html; charset=utf-8", z2m_page.html(head_extra or "").encode("utf-8")

    # ---------- Steuern ----------
    def post_set(self, params):
        name = self._name(params)
        kwargs = {}
        hexc = _first(params, "hex")
        if hexc:
            try:
                kwargs["hex_color"] = normalize_hex(hexc)
            except Z2MError as e:
                raise ValueError(str(e))
        pct = _num(params, "brightness", 0, 100)
        if pct is not None:
            kwargs["brightness_pct"] = pct
        state = _first(params, "state")
        if state:
            if str(state).upper() not in ("ON", "OFF", "TOGGLE"):
                raise ValueError("state muss ON, OFF oder TOGGLE sein")
            kwargs["state"] = str(state).upper()
        ct = _num(params, "color_temp", 1, 65279, int)
        if ct is not None:
            kwargs["color_temp"] = ct
        tr = _num(params, "transition", 0, 3600)
        if tr is not None:
            kwargs["transition"] = tr
        if not kwargs:
            raise ValueError("nichts zu setzen — hex, brightness, state oder color_temp angeben")
        payload = self.z2m.light_set(name, **kwargs)
        return {"name": name, "sent": payload}

    def post_toggle(self, params):
        name = self._name(params)
        return {"name": name, "sent": self.z2m.toggle(name, transition=_num(params, "transition", 0, 3600))}

    def post_get(self, params):
        name = self._name(params)
        attrs = [a.strip() for a in str(_first(params, "attr", "state")).split(",") if a.strip()]
        if not attrs or any(not a.replace("_", "").isalnum() for a in attrs):
            raise ValueError("attr: kommagetrennte Attributnamen erwartet")
        self.z2m.get(name, attrs)
        return {"name": name, "requested": attrs}

    def post_permit_join(self, params):
        seconds = _num(params, "time", 0, 254, int)
        if seconds is None:
            seconds = 254
        device = _first(params, "device") or None
        if device:
            _check_name(device)
        return self.z2m.permit_join(seconds, device)

    def post_health_check(self, params):
        return self.z2m.health_check()

    def post_coordinator_check(self, params):
        return self.z2m.coordinator_check()

    def post_rename(self, params):
        old, new = _first(params, "from"), _first(params, "to")
        if not old or not new:
            raise ValueError("from und to erforderlich")
        return self.z2m.rename(_check_name(old), _check_name(new), _first(params, "homeassistant_rename") in ("1", "true"))

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
        return _check_name(name)

    def _wait(self, params):
        """'wait' in Sekunden, standardmässig 2, höchstens MAX_WAIT (der HTTP-Server blockiert solange)."""
        w = _num(params, "wait", 0, 1e9)
        return min(self.MAX_WAIT, 2.0 if w is None else w)


# ---------------------------------------------------------------------------
# Standalone-Server (nur zum Testen / Vorschau des Info-Panels)
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Zigbee2MQTT · ThomysHomeAgent</title>
%%PWA_HEAD%%
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
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(p,m){const r=await fetch(p,{method:m||'GET'});return r.json();}
function row(k,v,cls){return `<tr><td>${k}</td><td class="${cls||''}">${v??'–'}</td></tr>`}
async function refresh(){
  const {result:s}=await api('/api/z2m/info');
  const st=await api('/api/z2m/status');
  $('#status').innerHTML=`<b class="${s.online?'ok':'bad'}">${esc(st.result.text)}</b>`;
  const vcls={ok:'ok',newer:'warn',older:'bad'}[s.version_status]||'';
  const pjEnd=s.permit_join_end?new Date(s.permit_join_end>1e11?s.permit_join_end:s.permit_join_end*1000):null; // Z2M: Sekunden oder Millisekunden
  $('#info').innerHTML=[
    row('Zigbee2MQTT-Version',esc(`${s.version??'?'} (erwartet ${s.expected_version}, Commit ${s.commit??'?'})`),vcls),
    row('Frontend',s.frontend_url?`<a href="${esc(s.frontend_url)}" target="_blank" rel="noopener" style="color:#8cf">${esc(s.frontend_url)}</a>`:'–'),
    row('zigbee-herdsman-converters',esc(s.zigbee_herdsman_converters)),row('zigbee-herdsman',esc(s.zigbee_herdsman)),
    row('Koordinator',esc(`${s.coordinator.type??'?'} · ${s.coordinator.ieee_address??'?'} · Revision ${s.coordinator.revision??'?'}`)),
    row('Netzwerk',esc(`Kanal ${s.network.channel??'?'}, PAN-ID ${s.network.pan_id??'?'}`)),
    row('Maschine',esc(`${s.os??'?'} · CPU: ${s.cpus??'?'} · RAM: ${s.memory_mb??'?'} MB · Node ${s.node_version??'?'}`)),
    row('MQTT (Z2M-Seite)',esc(`${s.z2m_mqtt_server??'?'} (Protokoll ${s.z2m_mqtt_version??'?'})`)),
    row('MQTT (unsere Seite)',esc(`${s.mqtt_host} · ${s.mqtt_connected?'verbunden':'getrennt'} ${s.mqtt_error?'· '+s.mqtt_error:''}`),s.mqtt_connected?'ok':'bad'),
    row('Bridge',esc(s.bridge_state),s.bridge_state==='online'?'ok':'bad'),
    row('Anlernen (permit_join)',s.permit_join?`offen${pjEnd?' bis '+pjEnd.toLocaleTimeString():''}${s.permit_join_remaining_sec!=null?' ('+s.permit_join_remaining_sec+' s)':''}`:'gesperrt',s.permit_join?'warn':''),
    row('Neustart nötig',s.restart_required?'ja':'nein',s.restart_required?'warn':''),
    row('Health',s.health.uptime_sec!=null?esc(`Laufzeit ${Math.round(s.health.uptime_sec/60)} min · Z2M ${s.health.process_memory_mb} MB · System-RAM ${s.health.os_memory_percent}% · Load ${JSON.stringify(s.health.os_load_average)}`):'noch kein Health-Check empfangen'),
    row('Geräte',esc(`${s.devices_total} (davon ${s.lights_total} Leuchten)${s.devices_offline.length?' · offline: '+s.devices_offline.join(', '):''}`),s.devices_offline.length?'warn':''),
    row('Letzte Warnungen',s.last_warnings.map(w=>esc(`[${w.level}] ${w.message}`)).join('<br>')||'keine'),
  ].join('');
  const {result:lights}=await api('/api/z2m/lights');
  $('#lights').innerHTML=lights.map(l=>`<div>${esc(l.friendly_name)} — ${esc(l.description)} · <b>${esc(l.state??'?')}</b> ${l.brightness!=null?Math.round(l.brightness/2.54)+'%':''}
    ${l.available==='offline'?'<span class="bad">offline</span>':''}
    <button data-act="toggle" data-name="${esc(l.friendly_name)}">an/aus</button>
    ${l.color?['#FF1010','#00C000','#0033FF','#FF6A00','#FFFFFF'].map(h=>`<button data-act="set" data-hex="${h}" data-name="${esc(l.friendly_name)}" style="background:${h}">&nbsp;</button>`).join(''):''}
  </div>`).join('')||'keine Leuchten in bridge/devices gefunden';
  $('#raw').textContent=JSON.stringify(s,null,1);
}
$('#lights').addEventListener('click',ev=>{
  const b=ev.target.closest('button[data-act]'); if(!b) return;
  const n=encodeURIComponent(b.dataset.name);
  const url=b.dataset.act==='toggle'?`/api/z2m/toggle?name=${n}`:`/api/z2m/set?name=${n}&hex=${encodeURIComponent(b.dataset.hex)}&transition=1`;
  api(url,'POST').then(refresh);
});
refresh();setInterval(refresh,5000);
</script>"""


def serve(cfg, port=8098, certfile=None, keyfile=None):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    z2m = Zigbee2MQTT(cfg)
    z2m.start(block_until_connected=False)
    api = Z2MApi(z2m)
    page = _PAGE.replace("%%PWA_HEAD%%", pwa.head_tags() if pwa else "").encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def _raw(self, status, ctype, body):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _route(self):
            u = urllib.parse.urlsplit(self.path)
            params = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            if u.path in ("/", "/index.html") or u.path.rstrip("/") == "/zigbee":
                hit = api.page("/zigbee")
                if hit is not None:
                    return self._raw(*hit)
            if u.path == "/preview":
                return self._raw(200, "text/html; charset=utf-8", page)
            if pwa and self.command == "GET":
                hit = pwa.handle(u.path)
                if hit is not None:
                    return self._raw(*hit)
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
    scheme = "http"
    if certfile and keyfile and pwa:
        pwa.wrap_https(srv, certfile, keyfile)
        scheme = "https"
    print("Zigbee2MQTT-Testserver auf %s://0.0.0.0:%d  (Strg+C beendet)" % (scheme, port))
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
    cert = sys.argv[3] if len(sys.argv) > 3 else None      # optional: python3 z2m_api.py config.json 8098 server.crt server.key
    key = sys.argv[4] if len(sys.argv) > 4 else None
    serve(load_config(cfg_path), port, cert, key)
