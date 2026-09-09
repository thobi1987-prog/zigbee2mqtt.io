#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
z2m.py — Zigbee2MQTT-Anbindung für ThomysHomeAgent
===================================================
Verbindet ThomysHomeAgent (lichtapp.py / homebrain.py) mit einer laufenden
Zigbee2MQTT-Instanz (getestet gegen die MQTT-API von Zigbee2MQTT 2.13.0,
Frontend WindFront 2.14.0) über den MQTT-Broker.

Nur Python-Standardbibliothek — kein paho, kein pip (wie der Rest der App).

Verwendung:

    from z2m import Zigbee2MQTT
    z2m = Zigbee2MQTT(cfg)          # cfg = Inhalt von config.json
    z2m.start()                     # verbindet im Hintergrund, hält Cache aktuell
    z2m.summary()                   # Version, Koordinator, Health … fürs Info-Panel
    z2m.lights()                    # alle Zigbee-Leuchten (aus bridge/devices)
    z2m.light_set("ThomysHomeBar", hex_color="#0033FF", brightness_pct=70)
    z2m.permit_join(120)            # Anlernen für 2 Minuten erlauben
    z2m.stop()

Benutzte Zigbee2MQTT-Topics (Basis-Topic aus config.json → "z2m_base"):
    <base>/bridge/state            online/offline (retained)
    <base>/bridge/info             Version, Commit, Koordinator, Netzwerk, Config
    <base>/bridge/health           Health-Check (alle 10 min, s. Doku "Health")
    <base>/bridge/devices          Geräteliste inkl. "exposes" (retained)
    <base>/bridge/groups           Gruppen (retained)
    <base>/bridge/event            device_joined / device_leave / …
    <base>/bridge/logging          Log (nur warning/error werden gemerkt)
    <base>/bridge/request/<x>      Anfragen (permit_join, health_check, …)
    <base>/bridge/response/<x>     Antworten dazu (mit "transaction" gematcht)
    <base>/<FRIENDLY_NAME>         Zustand eines Geräts
    <base>/<FRIENDLY_NAME>/availability  online/offline je Gerät
    <base>/<FRIENDLY_NAME>/set     Gerät steuern
    <base>/<FRIENDLY_NAME>/get     Zustand anfordern
"""
import json
import socket
import struct
import threading
import time
from collections import deque

# ---------------------------------------------------------------------------
# Minimaler MQTT-3.1.1-Client (QoS 0, mit Subscribe, Keepalive, Reconnect)
# ---------------------------------------------------------------------------

_CONNECT, _CONNACK, _PUBLISH, _PUBACK = 1, 2, 3, 4
_SUBSCRIBE, _SUBACK, _UNSUBSCRIBE, _UNSUBACK = 8, 9, 10, 11
_PINGREQ, _PINGRESP, _DISCONNECT = 12, 13, 14

_CONNACK_ERRORS = {
    1: "Protokollversion wird vom Broker nicht unterstützt",
    2: "Client-ID abgelehnt",
    3: "Broker nicht verfügbar",
    4: "Benutzername oder Passwort falsch (rc=4)",
    5: "Nicht autorisiert (rc=5) — mqtt_user / mqtt_pass in config.json prüfen",
}


class MqttError(Exception):
    pass


def _encode_str(s):
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def _encode_len(n):
    out = bytearray()
    while True:
        d = n % 128
        n //= 128
        if n > 0:
            d |= 0x80
        out.append(d)
        if n == 0:
            return bytes(out)


def topic_matches(pattern, topic):
    """MQTT-Wildcard-Abgleich ('+' eine Ebene, '#' Rest)."""
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t):
            return False
        if part != "+" and part != t[i]:
            return False
    return len(p) == len(t)


class MiniMqtt:
    """Kleiner MQTT-3.1.1-Client auf Basis von socket + Threads.

    - QoS 0 für Publish/Subscribe (reicht für Zigbee2MQTT)
    - Benutzername/Passwort, Keepalive (PINGREQ), automatischer Reconnect
    - Abonnements werden nach einem Reconnect automatisch erneuert
    - on_message(topic, payload_bytes) wird im Leser-Thread aufgerufen
    """

    def __init__(self, host, port=1883, user=None, password=None, client_id=None,
                 keepalive=60, on_message=None, on_connect=None, on_disconnect=None,
                 reconnect=True, timeout=10.0):
        self.host, self.port = host, int(port)
        self.user, self.password = user, password
        self.client_id = client_id or "thomyshomeagent-%d" % (int(time.time()) % 100000)
        self.keepalive = int(keepalive)
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self._reconnect_wanted = bool(reconnect)   # konfigurierter Wunsch (stop() schaltet nur temporär ab)
        self.auto_reconnect = bool(reconnect)
        self.timeout = timeout

        self._sock = None
        self._send_lock = threading.Lock()
        self._subs = {}          # topic -> qos
        self._pending_subs = {}  # packet id -> topic (bis SUBACK)
        self.rejected_subscriptions = []
        self._pid = 0
        self._running = False
        self._threads = []
        self._last_rx = 0.0
        self._last_tx = 0.0
        self.connected = False
        self.last_error = None
        self.connect_count = 0

    # ---------- Verbindung ----------
    def connect(self):
        """Verbindet synchron (wirft MqttError bei Fehler)."""
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise MqttError("Verbindung zu %s:%s fehlgeschlagen: %s" % (self.host, self.port, e))
        sock.settimeout(self.timeout)
        flags = 0x02  # clean session
        payload = _encode_str(self.client_id)
        if self.user is not None:
            flags |= 0x80
            payload += _encode_str(self.user)
            if self.password is not None:
                flags |= 0x40
                payload += _encode_str(self.password)
        var = _encode_str("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive)
        pkt = bytes([_CONNECT << 4]) + _encode_len(len(var) + len(payload)) + var + payload
        try:
            sock.sendall(pkt)
            ptype, _, body = self._read_packet(sock)
        except (OSError, MqttError) as e:
            sock.close()
            raise MqttError("Verbindung zu %s:%s fehlgeschlagen: %s" % (self.host, self.port, e))
        if ptype != _CONNACK or len(body) < 2:
            sock.close()
            raise MqttError("Ungültige CONNACK-Antwort vom Broker")
        rc = body[1]
        if rc != 0:
            sock.close()
            raise MqttError(_CONNACK_ERRORS.get(rc, "CONNACK-Fehler rc=%d" % rc))
        self._sock = sock
        self.connected = True
        self.connect_count += 1
        self.last_error = None
        self._last_rx = self._last_tx = time.time()
        # Abos erneuern (bei Reconnect)
        for topic, qos in list(self._subs.items()):
            self._send_subscribe(topic, qos)
        if self.on_connect:
            try:
                self.on_connect(self)
            except Exception as e:  # pragma: no cover - Callback-Fehler nicht eskalieren
                self.last_error = "on_connect: %s" % e

    def start(self, block_until_connected=True):
        """Startet Leser- und Keepalive-Thread; verbindet (mit Reconnect-Schleife).

        Schlägt der synchrone erste Verbindungsversuch fehl, wird nichts gestartet und
        start() kann später erneut aufgerufen werden. Nach stop() ist start() ebenfalls
        wieder möglich (inkl. automatischem Reconnect).
        """
        if self._running:
            return
        self.auto_reconnect = self._reconnect_wanted
        if block_until_connected:
            self.connect()  # erster Verbindungsversuch synchron → Fehler sofort sichtbar
        self._running = True
        t1 = threading.Thread(target=self._reader_loop, name="mqtt-reader", daemon=True)
        t2 = threading.Thread(target=self._keepalive_loop, name="mqtt-keepalive", daemon=True)
        self._threads = [t1, t2]
        t1.start()
        t2.start()

    def stop(self):
        self._running = False
        self.auto_reconnect = False       # während des Herunterfahrens nicht neu verbinden
        sock = self._sock
        if sock is not None:
            try:
                with self._send_lock:
                    sock.sendall(bytes([_DISCONNECT << 4, 0]))
            except OSError:
                pass
            self._close()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=2)
        self._threads = []

    def _close(self):
        self.connected = False
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self.on_disconnect:
            try:
                self.on_disconnect(self)
            except Exception:  # pragma: no cover
                pass

    # ---------- Senden ----------
    def _send(self, data):
        sock = self._sock
        if sock is None or not self.connected:
            raise MqttError("MQTT nicht verbunden")
        with self._send_lock:
            try:
                sock.sendall(data)
                self._last_tx = time.time()
            except OSError as e:
                self.last_error = str(e)
                self._close()
                raise MqttError("Senden fehlgeschlagen: %s" % e)

    def _next_pid(self):
        self._pid = (self._pid % 65535) + 1
        return self._pid

    def publish(self, topic, payload, retain=False, qos=0):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False)
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        elif payload is None:
            payload = b""
        hdr = (_PUBLISH << 4) | (qos << 1) | (1 if retain else 0)
        var = _encode_str(topic)
        if qos > 0:
            var += struct.pack("!H", self._next_pid())
        self._send(bytes([hdr]) + _encode_len(len(var) + len(payload)) + var + payload)

    def subscribe(self, topic, qos=0):
        self._subs[topic] = qos
        if self.connected:
            self._send_subscribe(topic, qos)

    def _send_subscribe(self, topic, qos):
        pid = self._next_pid()
        self._pending_subs[pid] = topic
        var = struct.pack("!H", pid)
        payload = _encode_str(topic) + bytes([qos])
        self._send(bytes([(_SUBSCRIBE << 4) | 0x02]) + _encode_len(len(var) + len(payload)) + var + payload)

    def unsubscribe(self, topic):
        self._subs.pop(topic, None)
        if self.connected:
            var = struct.pack("!H", self._next_pid())
            payload = _encode_str(topic)
            self._send(bytes([(_UNSUBSCRIBE << 4) | 0x02]) + _encode_len(len(var) + len(payload)) + var + payload)

    def ping(self):
        self._send(bytes([_PINGREQ << 4, 0]))

    # ---------- Empfangen ----------
    @staticmethod
    def _read_exact(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise MqttError("Verbindung vom Broker geschlossen")
            buf += chunk
        return buf

    def _read_packet(self, sock):
        first = self._read_exact(sock, 1)[0]  # socket.timeout hier = einfach nichts los
        try:
            mult, length = 1, 0
            for _ in range(4):
                d = self._read_exact(sock, 1)[0]
                length += (d & 0x7F) * mult
                mult *= 128
                if not d & 0x80:
                    break
            body = self._read_exact(sock, length) if length else b""
        except socket.timeout:
            raise MqttError("Timeout mitten im MQTT-Paket")
        return first >> 4, first & 0x0F, body

    def _reader_loop(self):
        backoff = 1
        while self._running:
            sock = self._sock
            if sock is None or not self.connected:
                if not self.auto_reconnect:
                    return
                try:
                    self.connect()
                    backoff = 1
                except MqttError as e:
                    self.last_error = str(e)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                continue
            try:
                ptype, flags, body = self._read_packet(sock)
            except socket.timeout:
                continue
            except (OSError, MqttError) as e:
                if self._running:
                    self.last_error = str(e)
                    self._close()
                continue
            self._last_rx = time.time()
            if ptype == _PUBLISH:
                self._handle_publish(flags, body)
            elif ptype == _SUBACK and len(body) >= 3:
                pid = struct.unpack("!H", body[:2])[0]
                topic = self._pending_subs.pop(pid, "?")
                if any(rc == 0x80 for rc in body[2:]):
                    # Broker lehnt das Abo ab (meist ACL) — sonst bliebe der Cache stumm und leer
                    if topic not in self.rejected_subscriptions:
                        self.rejected_subscriptions.append(topic)
                    self.last_error = "Abo '%s' vom Broker abgelehnt (rc=0x80) — ACL/Rechte des MQTT-Benutzers prüfen" % topic
            # PINGRESP / UNSUBACK / PUBACK: nur als "Lebenszeichen" relevant

    def _handle_publish(self, flags, body):
        qos = (flags >> 1) & 0x03
        tlen = struct.unpack("!H", body[:2])[0]
        topic = body[2:2 + tlen].decode("utf-8", "replace")
        pos = 2 + tlen
        if qos > 0:
            pid = struct.unpack("!H", body[pos:pos + 2])[0]
            pos += 2
            try:
                self._send(bytes([_PUBACK << 4, 2]) + struct.pack("!H", pid))
            except MqttError:
                pass
        payload = body[pos:]
        if self.on_message:
            try:
                self.on_message(topic, payload)
            except Exception as e:  # Callback-Fehler dürfen den Leser nicht beenden
                self.last_error = "on_message(%s): %s" % (topic, e)

    def _keepalive_loop(self):
        interval = max(5, self.keepalive // 2)
        while self._running:
            time.sleep(1)
            if not self.connected:
                continue
            now = time.time()
            if now - self._last_rx > self.keepalive * 1.5:
                self.last_error = "Keepalive-Timeout, verbinde neu"
                self._close()
                continue
            if now - self._last_tx >= interval:
                try:
                    self.ping()
                except MqttError:
                    pass


# ---------------------------------------------------------------------------
# Zigbee2MQTT-Client
# ---------------------------------------------------------------------------

class Z2MError(Exception):
    pass


def _parse_version(v):
    """'2.13.0' oder '2.13.0-dev' → (2, 13, 0); unbekannt → None."""
    if not v:
        return None
    try:
        return tuple(int(x) for x in str(v).split("-")[0].split("+")[0].split(".")[:3])
    except ValueError:
        return None


def _light_features(exposes):
    """Liest aus einer expose-Liste die Fähigkeiten aller 'light'-Einträge."""
    feats = []
    for e in exposes or []:
        if not isinstance(e, dict):
            continue
        if e.get("type") == "light":
            for f in e.get("features") or []:
                n = f.get("name") if isinstance(f, dict) else None
                if n and n not in feats:
                    feats.append(n)
    return feats


class Zigbee2MQTT:
    """Hält die Verbindung zu Zigbee2MQTT und einen Cache aller Bridge-/Gerätedaten."""

    DEFAULT_EXPECTED_VERSION = "2.13.0"

    def __init__(self, cfg, on_event=None, client_id=None):
        self.cfg = cfg or {}
        self.base = (self.cfg.get("z2m_base") or "zigbee2mqtt").strip("/")
        self.expected_version = self.cfg.get("z2m_expected_version") or self.DEFAULT_EXPECTED_VERSION
        self.on_event = on_event
        self.started_at = None

        self.bridge_state = None      # "online" | "offline" | None (noch nichts empfangen)
        self.info = {}
        self.health = {}
        self.devices = []
        self.groups = []
        self.states = {}              # friendly_name -> letzter Zustand (dict)
        self.state_updated_at = {}    # friendly_name -> Zeitpunkt des letzten Zustands
        self.availability = {}        # friendly_name -> "online"/"offline"
        self.events = deque(maxlen=50)
        self.logs = deque(maxlen=50)  # nur warning/error
        self.last_message_at = None
        self.last_error = None

        self._pending = {}            # transaction -> {"event": Event, "response": dict}
        self._tx = int(time.time()) % 10000
        self._lock = threading.Lock()

        self.mqtt = MiniMqtt(
            host=self.cfg.get("mqtt_host", "localhost"),
            port=self.cfg.get("mqtt_port", 1883),
            user=self.cfg.get("mqtt_user"),
            password=self.cfg.get("mqtt_pass"),
            client_id=client_id,
            keepalive=int(self.cfg.get("mqtt_keepalive", 60)),
            on_message=self._on_message,
            on_connect=self._on_connect,
        )
        # '#' liefert auch bridge/*, Gerätezustände, availability — alles in einem Abo.
        self.mqtt.subscribe(self.base + "/#")

    # ---------- Lebenszyklus ----------
    def start(self, block_until_connected=True):
        self.started_at = time.time()
        self.mqtt.start(block_until_connected=block_until_connected)

    def stop(self):
        self.mqtt.stop()

    @property
    def connected(self):
        return self.mqtt.connected

    # ---------- Eingehende Nachrichten ----------
    def _on_connect(self, _client):
        self._emit({"type": "mqtt_connected", "data": {"host": self.mqtt.host}})

    def _emit(self, event):
        event = dict(event)
        event.setdefault("time", time.time())
        self.events.append(event)
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:  # pragma: no cover
                pass

    def _on_message(self, topic, raw):
        prefix = self.base + "/"
        if not topic.startswith(prefix):
            return
        rest = topic[len(prefix):]
        parts = rest.split("/")
        # Eigene Steuer-Topics ignorieren (kommen wegen '#' auch bei uns an) — zählen nicht als Nachricht von Z2M
        if not rest.startswith("bridge/") and ("set" in parts or "get" in parts):
            return
        self.last_message_at = time.time()
        text = raw.decode("utf-8", "replace")
        data = None
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                data = text

        if rest.startswith("bridge/"):
            self._on_bridge(rest[len("bridge/"):], data)
            return
        if parts[-1] == "availability" and len(parts) > 1:
            name = "/".join(parts[:-1])
            state = data.get("state") if isinstance(data, dict) else data
            with self._lock:
                self.availability[name] = state
            return
        if isinstance(data, dict):
            with self._lock:
                self.states[rest] = data
                self.state_updated_at[rest] = time.time()

    def _on_bridge(self, sub, data):
        if sub == "state":
            state = data.get("state") if isinstance(data, dict) else data
            changed = state != self.bridge_state
            self.bridge_state = state
            if changed:
                self._emit({"type": "bridge_state", "data": {"state": state}})
        elif sub == "info" and isinstance(data, dict):
            self.info = data
        elif sub == "health" and isinstance(data, dict):
            self.health = data
        elif sub == "devices" and isinstance(data, list):
            with self._lock:
                self.devices = data
        elif sub == "groups" and isinstance(data, list):
            with self._lock:
                self.groups = data
        elif sub == "event" and isinstance(data, dict):
            self._emit(data)
        elif sub == "logging" and isinstance(data, dict):
            if data.get("level") in ("warning", "error"):
                self.logs.append(dict(data, time=time.time()))
        elif sub.startswith("response/") and isinstance(data, dict):
            tx = data.get("transaction")
            with self._lock:
                p = self._pending.get(tx) if tx is not None else None
            if p is not None:
                p["response"] = data
                p["event"].set()

    # ---------- Anfragen an die Bridge ----------
    def request(self, path, payload=None, timeout=10.0):
        """Sendet <base>/bridge/request/<path> und wartet auf die Antwort.

        Gibt das 'data'-Objekt der Antwort zurück; wirft Z2MError bei
        status=error, Timeout oder fehlender Verbindung.
        """
        payload = dict(payload or {})
        with self._lock:
            self._tx = (self._tx % 1000000) + 1
            tx = self._tx
            entry = {"event": threading.Event(), "response": None}
            self._pending[tx] = entry
        payload["transaction"] = tx
        try:
            self.mqtt.publish("%s/bridge/request/%s" % (self.base, path), payload)
            if not entry["event"].wait(timeout):
                raise Z2MError("Keine Antwort von Zigbee2MQTT auf '%s' (Timeout %ss)" % (path, timeout))
        except MqttError as e:
            raise Z2MError(str(e))
        finally:
            with self._lock:
                self._pending.pop(tx, None)
        resp = entry["response"] or {}
        if resp.get("status") != "ok":
            raise Z2MError(resp.get("error") or "Zigbee2MQTT meldet Fehler bei '%s'" % path)
        return resp.get("data", {})

    def permit_join(self, seconds=254, device=None):
        """Anlernen erlauben (seconds=0 → sperren). Optional nur über ein Gerät/Router."""
        payload = {"time": int(seconds)}
        if device:
            payload["device"] = device
        return self.request("permit_join", payload)

    def health_check(self):
        return self.request("health_check")

    def coordinator_check(self):
        return self.request("coordinator_check", timeout=30)

    def restart(self):
        return self.request("restart")

    def rename(self, old, new, homeassistant_rename=False):
        return self.request("device/rename", {"from": old, "to": new, "homeassistant_rename": homeassistant_rename})

    def device_options(self, name, options):
        return self.request("device/options", {"id": name, "options": options})

    # ---------- Geräte steuern ----------
    @staticmethod
    def _topic_name(name):
        """friendly_name/IEEE für ein Topic prüfen: keine MQTT-Wildcards, keine leeren Segmente."""
        name = str(name)
        if not name or any(c in name for c in "+#\x00") or name.startswith("/") or name.endswith("/") or "//" in name:
            raise Z2MError("Ungültiger Gerätename %r" % name)
        return name

    def set(self, name, payload):
        name = self._topic_name(name)
        try:
            self.mqtt.publish("%s/%s/set" % (self.base, name), payload)
        except MqttError as e:
            raise Z2MError(str(e))

    def get(self, name, attributes=("state",)):
        name = self._topic_name(name)
        try:
            self.mqtt.publish("%s/%s/get" % (self.base, name), {a: "" for a in attributes})
        except MqttError as e:
            raise Z2MError(str(e))

    def light_set(self, name, state=None, brightness_pct=None, brightness=None,
                  hex_color=None, color_temp=None, transition=None):
        """Bequeme Licht-Steuerung; baut das /set-JSON gemäss Zigbee2MQTT-Doku."""
        payload = {}
        if state is not None:
            if isinstance(state, bool):
                state = "ON" if state else "OFF"
            payload["state"] = str(state).upper()
        if brightness_pct is not None:
            brightness = max(0, min(254, int(round(float(brightness_pct) * 2.54))))
        if brightness is not None:
            payload["brightness"] = max(0, min(254, int(brightness)))
            payload.setdefault("state", "ON" if payload["brightness"] > 0 else "OFF")
        if hex_color:
            payload["color"] = {"hex": normalize_hex(hex_color)}
            payload.setdefault("state", "ON")
        if color_temp is not None:
            payload["color_temp"] = int(color_temp)
            payload.setdefault("state", "ON")
        if transition is not None:
            payload["transition"] = transition
        if not payload:
            raise Z2MError("light_set: nichts zu setzen")
        self.set(name, payload)
        return payload

    def toggle(self, name, transition=None):
        payload = {"state": "TOGGLE"}
        if transition is not None:
            payload["transition"] = transition
        self.set(name, payload)
        return payload

    # ---------- Cache-Zugriff ----------
    def device(self, name_or_ieee):
        with self._lock:
            for d in self.devices:
                if d.get("friendly_name") == name_or_ieee or d.get("ieee_address") == name_or_ieee:
                    return d
        return None

    def friendly_name(self, name_or_ieee):
        """friendly_name zu einem Gerät (IEEE-Adresse → friendly_name; sonst unverändert)."""
        d = self.device(name_or_ieee)
        return (d or {}).get("friendly_name") or name_or_ieee

    def state(self, name):
        """Letzter bekannter Zustand eines Geräts (friendly_name oder IEEE-Adresse)."""
        key = self.friendly_name(name)
        with self._lock:
            return self.states.get(key) or self.states.get(name)

    def available(self, name):
        """Erreichbarkeit ('online'/'offline'/None) — auch per IEEE-Adresse."""
        key = self.friendly_name(name)
        with self._lock:
            return self.availability.get(key, self.availability.get(name))

    def refresh_state(self, name, attributes=("state",), timeout=2.0):
        """Sendet /get und wartet, bis für DIESES Gerät ein neuer Zustand eintrifft.

        Gibt den (ggf. aktualisierten) Zustand zurück, None wenn keiner bekannt ist.
        Andere MQTT-Nachrichten (Log, andere Geräte, eigenes /get-Echo) beenden das Warten nicht.
        """
        key = self.friendly_name(name)
        with self._lock:
            before = self.state_updated_at.get(key)
        self.get(name, attributes)
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            with self._lock:
                if self.state_updated_at.get(key) != before:
                    break
            time.sleep(0.05)
        return self.state(name)

    def lights(self):
        """Alle Geräte mit einem 'light'-Expose (nur unterstützte, nicht deaktivierte)."""
        out = []
        with self._lock:
            devices = list(self.devices)
            states = dict(self.states)
            avail = dict(self.availability)
        for d in devices:
            if d.get("type") == "Coordinator" or d.get("disabled") or not d.get("supported", True):
                continue
            definition = d.get("definition") or {}
            feats = _light_features(definition.get("exposes"))
            if not feats:
                continue
            name = d.get("friendly_name") or d.get("ieee_address")
            st = states.get(name) or {}
            out.append({
                "friendly_name": name,
                "ieee_address": d.get("ieee_address"),
                "vendor": definition.get("vendor"),
                "model": definition.get("model"),
                "description": definition.get("description") or d.get("description"),
                "features": feats,
                "color": any(f in feats for f in ("color_xy", "color_hs")),
                "state": st.get("state"),
                "brightness": st.get("brightness"),
                "available": avail.get(name),
            })
        return out

    def light_names(self):
        return [l["friendly_name"] for l in self.lights()]

    # ---------- Zusammenfassung (Info-Panel) ----------
    def version_status(self):
        have, want = _parse_version(self.info.get("version")), _parse_version(self.expected_version)
        if have is None or want is None:
            return "unknown"
        if have == want:
            return "ok"
        return "newer" if have > want else "older"

    def frontend_url(self):
        url = self.cfg.get("z2m_frontend_url")
        if url:
            return url
        fe = ((self.info.get("config") or {}).get("frontend")) or {}
        if not fe or fe.get("enabled") is False:
            return None
        if fe.get("url"):
            return fe["url"]
        port = fe.get("port") or 8080
        base_url = (fe.get("base_url") or "/").rstrip("/")
        scheme = "https" if fe.get("ssl_cert") and fe.get("ssl_key") else "http"
        # Zigbee2MQTT läuft üblicherweise auf demselben Host wie der Broker (mqtt://localhost)
        return "%s://%s:%s%s" % (scheme, self.mqtt.host, port, base_url)

    def permit_join_remaining_sec(self):
        """Restzeit des offenen Anlern-Fensters in Sekunden (None wenn geschlossen).

        Zigbee2MQTT liefert `permit_join_end` als Unix-Zeit — je nach Version in Sekunden
        oder Millisekunden; beides wird erkannt.
        """
        end = self.info.get("permit_join_end")
        if not end or not self.info.get("permit_join"):
            return None
        try:
            end = float(end)
        except (TypeError, ValueError):
            return None
        end_s = end / 1000.0 if end > 1e11 else end
        return max(0, int(round(end_s - time.time())))

    def summary(self):
        info, health = self.info, self.health
        coord = info.get("coordinator") or {}
        meta = coord.get("meta") or {}
        net = info.get("network") or {}
        osinfo = info.get("os") or {}
        mq = info.get("mqtt") or {}
        lights = self.lights()
        with self._lock:
            n_devices = len([d for d in self.devices if d.get("type") != "Coordinator"])
            offline = sorted(n for n, s in self.availability.items() if s == "offline")
        return {
            "mqtt_connected": self.mqtt.connected,
            "mqtt_host": "%s:%s" % (self.mqtt.host, self.mqtt.port),
            "mqtt_error": self.mqtt.last_error,
            "bridge_state": self.bridge_state,
            "online": self.mqtt.connected and self.bridge_state == "online",
            "version": info.get("version"),
            "expected_version": self.expected_version,
            "version_status": self.version_status(),
            "commit": info.get("commit"),
            "coordinator": {
                "type": coord.get("type"),
                "ieee_address": coord.get("ieee_address"),
                "revision": meta.get("revision"),
            },
            "zigbee_herdsman": (info.get("zigbee_herdsman") or {}).get("version"),
            "zigbee_herdsman_converters": (info.get("zigbee_herdsman_converters") or {}).get("version"),
            "network": {"channel": net.get("channel"), "pan_id": net.get("pan_id")},
            "os": osinfo.get("version"),
            "node_version": osinfo.get("node_version"),
            "cpus": osinfo.get("cpus"),
            "memory_mb": osinfo.get("memory_mb"),
            "z2m_mqtt_server": mq.get("server"),
            "z2m_mqtt_version": mq.get("version"),
            "log_level": info.get("log_level"),
            "permit_join": info.get("permit_join"),
            "permit_join_end": info.get("permit_join_end"),
            "permit_join_remaining_sec": self.permit_join_remaining_sec(),
            "restart_required": info.get("restart_required"),
            "frontend_url": self.frontend_url(),
            "health": {
                "checked_at": health.get("response_time"),
                "uptime_sec": (health.get("process") or {}).get("uptime_sec"),
                "process_memory_mb": (health.get("process") or {}).get("memory_used_mb"),
                "os_memory_percent": (health.get("os") or {}).get("memory_percent"),
                "os_load_average": (health.get("os") or {}).get("load_average"),
                "mqtt_connected": (health.get("mqtt") or {}).get("connected"),
            },
            "devices_total": n_devices,
            "lights_total": len(lights),
            "lights": [l["friendly_name"] for l in lights],
            "devices_offline": offline,
            "last_message_at": self.last_message_at,
            "last_events": list(self.events)[-5:],
            "last_warnings": list(self.logs)[-5:],
        }

    def status_text(self):
        """Einzeiler fürs Info-Panel, z.B. 'Zigbee2MQTT 2.13.0 ✓ · online · 12 Geräte'."""
        s = self.summary()
        if not s["mqtt_connected"]:
            return "Zigbee2MQTT: MQTT nicht verbunden (%s)" % (s["mqtt_error"] or "?")
        if s["bridge_state"] != "online":
            return "Zigbee2MQTT: Bridge %s" % (s["bridge_state"] or "unbekannt")
        mark = {"ok": "✓", "newer": "↑ neuer als %s" % s["expected_version"],
                "older": "⚠ älter als %s" % s["expected_version"]}.get(s["version_status"], "?")
        return "Zigbee2MQTT %s %s · online · %d Geräte (%d Leuchten)" % (
            s["version"] or "?", mark, s["devices_total"], s["lights_total"])


def normalize_hex(color):
    """'#ff00aa', 'ff00aa', '#F0A' → '#FF00AA'."""
    c = str(color).strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in c):
        raise Z2MError("Ungültige Farbe: %r" % color)
    return "#" + c.upper()


def load_config(path="config.json"):
    with open(path) as f:
        return json.load(f)


if __name__ == "__main__":  # kleiner Selbsttest: python3 z2m.py [config.json]
    import sys
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.json")
    z = Zigbee2MQTT(cfg)
    try:
        z.start()
    except MqttError as e:
        print("FEHLER:", e)
        sys.exit(1)
    print("Verbunden mit %s, warte auf bridge/info …" % z.mqtt.host)
    for _ in range(50):
        if z.info and z.devices:
            break
        time.sleep(0.2)
    print(z.status_text())
    print(json.dumps(z.summary(), indent=2, ensure_ascii=False, default=str))
    for l in z.lights():
        print(" - %-28s %-12s %s (%s)" % (l["friendly_name"], l["state"] or "?", l["description"], ", ".join(l["features"])))
    z.stop()
