#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_z2m.py — Tests für z2m.py, z2m_api.py und homebrain.py
===========================================================
Startet einen kleinen MQTT-Broker im Prozess (FakeBroker) und einen
simulierten Zigbee2MQTT 2.13.0 (FakeZ2M), der sich wie die echte Bridge
verhält: retained bridge/state, bridge/info, bridge/devices, bridge/groups,
bridge/health, Antworten auf bridge/request/* mit "transaction",
Gerätezustände nach /set, availability, Events.

    python3 test_z2m.py          # alle Tests
    python3 test_z2m.py -v       # ausführlich

Nur Standardbibliothek, keine Netzwerkverbindung nach aussen.
"""
import json
import socket
import struct
import threading
import time
import unittest

import z2m as z2mmod
from z2m import MiniMqtt, MqttError, Zigbee2MQTT, Z2MError, normalize_hex, topic_matches, _encode_str, _encode_len
from z2m_api import Z2MApi
import homebrain


# ---------------------------------------------------------------------------
# Mini-Broker (MQTT 3.1.1, QoS 0, retained, Wildcards, Benutzer/Passwort)
# ---------------------------------------------------------------------------

class FakeBroker:
    def __init__(self, user=None, password=None):
        self.user, self.password = user, password
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.clients = {}          # sock -> {"subs": [...], "id": str, "lock": Lock}
        self.retained = {}
        self.reject = set()        # Topic-Filter, die der Broker mit rc=0x80 ablehnt (ACL-Simulation)
        self.received = []         # (topic, payload_bytes, retain)
        self.lock = threading.Lock()
        self.running = False

    def start(self):
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()
        return self

    def stop(self):
        self.running = False
        try:
            self.srv.close()
        except OSError:
            pass
        self.kick_all()

    def kick(self, client_id):
        with self.lock:
            socks = [s for s, c in self.clients.items() if c["id"] == client_id]
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def kick_all(self):
        with self.lock:
            socks = list(self.clients)
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def _accept(self):
        while self.running:
            try:
                sock, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(sock,), daemon=True).start()

    @staticmethod
    def _read_exact(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    def _read_packet(self, sock):
        first = self._read_exact(sock, 1)[0]
        mult, length = 1, 0
        while True:
            d = self._read_exact(sock, 1)[0]
            length += (d & 0x7F) * mult
            mult *= 128
            if not d & 0x80:
                break
        return first >> 4, first & 0x0F, self._read_exact(sock, length) if length else b""

    def _send(self, sock, data):
        c = self.clients.get(sock)
        lock = c["lock"] if c else threading.Lock()
        with lock:
            sock.sendall(data)

    def _client(self, sock):
        entry = {"subs": [], "id": "?", "lock": threading.Lock()}
        try:
            ptype, _, body = self._read_packet(sock)
            if ptype != 1:
                sock.close()
                return
            # CONNECT parsen
            pos = 0
            plen = struct.unpack("!H", body[pos:pos + 2])[0]; pos += 2 + plen   # "MQTT"
            pos += 1                                                             # level
            flags = body[pos]; pos += 1
            pos += 2                                                             # keepalive
            clen = struct.unpack("!H", body[pos:pos + 2])[0]; pos += 2
            entry["id"] = body[pos:pos + clen].decode(); pos += clen
            user = password = None
            if flags & 0x80:
                ulen = struct.unpack("!H", body[pos:pos + 2])[0]; pos += 2
                user = body[pos:pos + ulen].decode(); pos += ulen
            if flags & 0x40:
                plen = struct.unpack("!H", body[pos:pos + 2])[0]; pos += 2
                password = body[pos:pos + plen].decode(); pos += plen
            rc = 0
            if self.user is not None and (user != self.user or password != self.password):
                rc = 5
            sock.sendall(bytes([0x20, 2, 0, rc]))
            if rc != 0:
                sock.close()
                return
            with self.lock:
                self.clients[sock] = entry
            while self.running:
                ptype, flags, body = self._read_packet(sock)
                if ptype == 3:      # PUBLISH
                    retain = flags & 0x01
                    qos = (flags >> 1) & 0x03
                    tlen = struct.unpack("!H", body[:2])[0]
                    topic = body[2:2 + tlen].decode()
                    pos = 2 + tlen
                    if qos:
                        pid = struct.unpack("!H", body[pos:pos + 2])[0]; pos += 2
                        self._send(sock, bytes([0x40, 2]) + struct.pack("!H", pid))
                    payload = body[pos:]
                    self._publish(topic, payload, retain)
                elif ptype == 8:    # SUBSCRIBE
                    pid = body[:2]
                    pos, filters = 2, []
                    while pos < len(body):
                        flen = struct.unpack("!H", body[pos:pos + 2])[0]; pos += 2
                        filters.append(body[pos:pos + flen].decode()); pos += flen + 1
                    entry["subs"].extend(f for f in filters if f not in self.reject)
                    self._send(sock, bytes([0x90, 2 + len(filters)]) + pid + bytes([0x80 if f in self.reject else 0 for f in filters]))
                    accepted = [f for f in filters if f not in self.reject]
                    with self.lock:
                        retained = list(self.retained.items())
                    for topic, payload in retained:
                        if any(topic_matches(f, topic) for f in accepted):
                            self._send(sock, self._pub_packet(topic, payload, True))
                elif ptype == 10:   # UNSUBSCRIBE
                    pid = body[:2]
                    pos = 2
                    while pos < len(body):
                        flen = struct.unpack("!H", body[pos:pos + 2])[0]; pos += 2
                        f = body[pos:pos + flen].decode(); pos += flen
                        if f in entry["subs"]:
                            entry["subs"].remove(f)
                    self._send(sock, bytes([0xB0, 2]) + pid)
                elif ptype == 12:   # PINGREQ
                    self._send(sock, bytes([0xD0, 0]))
                elif ptype == 14:   # DISCONNECT
                    break
        except (OSError, ConnectionError, struct.error):
            pass
        finally:
            with self.lock:
                self.clients.pop(sock, None)
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _pub_packet(topic, payload, retain=False):
        var = _encode_str(topic)
        return bytes([0x30 | (1 if retain else 0)]) + _encode_len(len(var) + len(payload)) + var + payload

    def _publish(self, topic, payload, retain=False):
        with self.lock:
            self.received.append((topic, payload, bool(retain)))
            if retain:
                if payload:
                    self.retained[topic] = payload
                else:
                    self.retained.pop(topic, None)
            targets = [(s, c) for s, c in self.clients.items() if any(topic_matches(f, topic) for f in c["subs"])]
        pkt = self._pub_packet(topic, payload, False)
        for s, c in targets:
            try:
                with c["lock"]:
                    s.sendall(pkt)
            except OSError:
                pass

    def messages(self, topic):
        with self.lock:
            return [json.loads(p) if p else None for t, p, _ in self.received if t == topic]


# ---------------------------------------------------------------------------
# Simulierter Zigbee2MQTT 2.13.0 (wie die Instanz auf dem SMHUB)
# ---------------------------------------------------------------------------

LIGHT_EXPOSE = {"type": "light", "features": [
    {"type": "binary", "name": "state", "property": "state", "value_on": "ON", "value_off": "OFF", "value_toggle": "TOGGLE", "access": 7},
    {"type": "numeric", "name": "brightness", "property": "brightness", "value_min": 0, "value_max": 254, "access": 7},
    {"type": "composite", "name": "color_xy", "property": "color", "features": [
        {"type": "numeric", "name": "x", "property": "x", "access": 7}, {"type": "numeric", "name": "y", "property": "y", "access": 7}]},
]}

BRIDGE_INFO = {
    "version": "2.13.0", "commit": "fcbb7ff4",
    "coordinator": {"ieee_address": "0x00124b0033cb0f78", "type": "zStack3x0",
                    "meta": {"revision": 20250325, "transportrev": 2, "product": 2, "majorrel": 2, "minorrel": 7, "maintrel": 2}},
    "zigbee_herdsman_converters": {"version": "26.90.0"},
    "zigbee_herdsman": {"version": "10.8.0"},
    "network": {"channel": 11, "pan_id": 6754, "extended_pan_id": "0xdddddddddddddddd"},
    "log_level": "info", "permit_join": False, "restart_required": False,
    "os": {"version": "Linux - 6.18.17-patch21 - riscv64", "node_version": "v22.22.0", "cpus": "unknown (x1)", "memory_mb": 489},
    "mqtt": {"server": "mqtt://localhost:1883", "version": 4},
    "config": {"frontend": {"enabled": True, "package": "zigbee2mqtt-windfront", "port": 8080}, "mqtt": {"base_topic": "zigbee2mqtt"}},
}

DEVICES = [
    {"ieee_address": "0x00124b0033cb0f78", "type": "Coordinator", "network_address": 0, "supported": False, "disabled": False,
     "friendly_name": "Coordinator", "definition": None, "interview_state": "SUCCESSFUL"},
    {"ieee_address": "0xa4c138aaaaaaaaaa", "type": "Router", "network_address": 1234, "supported": True, "disabled": False,
     "friendly_name": "ThomysHomeBar", "definition": {"source": "native", "model": "TS0505B", "vendor": "Tuya",
     "description": "Zigbee RGB+CCT light", "options": [], "exposes": [LIGHT_EXPOSE]}, "power_source": "Mains (single phase)",
     "interview_state": "SUCCESSFUL"},
    {"ieee_address": "0x001788010efccbdb", "type": "Router", "network_address": 2345, "supported": True, "disabled": False,
     "friendly_name": "0x001788010efccbdb", "definition": {"source": "native", "model": "915005987201", "vendor": "Philips",
     "description": "Hue Signe floor light", "options": [], "exposes": [LIGHT_EXPOSE]}, "power_source": "Mains (single phase)",
     "interview_state": "SUCCESSFUL"},
    {"ieee_address": "0x00158d0001112233", "type": "EndDevice", "network_address": 3456, "supported": True, "disabled": False,
     "friendly_name": "Sensor Flur", "definition": {"source": "native", "model": "RTCGQ11LM", "vendor": "Aqara",
     "description": "Motion sensor", "options": [], "exposes": [{"type": "binary", "name": "occupancy", "property": "occupancy", "access": 1}]},
     "power_source": "Battery", "interview_state": "SUCCESSFUL"},
]

HEALTH = {"response_time": 1757440000000, "os": {"load_average": [0.5, 0.4, 0.3], "memory_used_mb": 310.2, "memory_percent": 63.4},
          "process": {"uptime_sec": 3600, "memory_used_mb": 95.1, "memory_percent": 19.4},
          "mqtt": {"connected": True, "queued": 0, "published": 100, "received": 20}, "devices": {}}


class FakeZ2M:
    """Verhält sich auf dem Broker wie Zigbee2MQTT 2.13.0."""

    def __init__(self, broker, base="zigbee2mqtt", user=None, password=None):
        self.base = base
        self.info = json.loads(json.dumps(BRIDGE_INFO))
        self.states = {"ThomysHomeBar": {"state": "ON", "brightness": 200, "color": {"x": 0.3, "y": 0.3}, "linkquality": 120},
                       "0x001788010efccbdb": {"state": "OFF", "brightness": 254, "linkquality": 90}}
        self.requests = []
        self.mqtt = MiniMqtt("127.0.0.1", broker.port, user, password, client_id="fake-z2m",
                             on_message=self._on_message, on_connect=self._on_connect)
        self.mqtt.subscribe(base + "/bridge/request/#")
        self.mqtt.subscribe(base + "/+/set")
        self.mqtt.subscribe(base + "/+/get")

    def start(self):
        self.mqtt.start()
        return self

    def stop(self):
        self.mqtt.stop()

    def _on_connect(self, _c):
        self.mqtt.publish(self.base + "/bridge/state", {"state": "online"}, retain=True)
        self.mqtt.publish(self.base + "/bridge/info", self.info, retain=True)
        self.mqtt.publish(self.base + "/bridge/devices", DEVICES, retain=True)
        self.mqtt.publish(self.base + "/bridge/groups", [{"id": 1, "friendly_name": "Alle Leuchten", "scenes": [], "members": []}], retain=True)
        self.mqtt.publish(self.base + "/bridge/health", HEALTH, retain=True)
        for name, st in self.states.items():
            # Wie echtes Zigbee2MQTT (device retain: false): Zustände NICHT retained → Client muss /get schicken
            self.mqtt.publish("%s/%s" % (self.base, name), st, retain=False)
            self.mqtt.publish("%s/%s/availability" % (self.base, name), {"state": "online"}, retain=True)

    def _on_message(self, topic, raw):
        rest = topic[len(self.base) + 1:]
        data = json.loads(raw) if raw else {}
        if rest.startswith("bridge/request/"):
            path = rest[len("bridge/request/"):]
            self.requests.append((path, data))
            tx = data.get("transaction")
            resp = {"status": "ok", "data": {}}
            if path == "permit_join":
                self.info["permit_join"] = data.get("time", 0) > 0
                self.info["permit_join_end"] = int(time.time()) + data.get("time", 0)
                resp["data"] = {"time": data.get("time")}
                self.mqtt.publish(self.base + "/bridge/info", self.info, retain=True)
            elif path == "health_check":
                resp["data"] = {"healthy": True}
            elif path == "device/rename":
                if data.get("from") not in self.states:
                    resp = {"status": "error", "error": "Device '%s' does not exist" % data.get("from"), "data": {}}
                else:
                    resp["data"] = {"from": data["from"], "to": data["to"], "homeassistant_rename": False}
            elif path == "restart":
                resp["data"] = {}
            elif path == "never_answer":
                return
            if tx is not None:
                resp["transaction"] = tx
            self.mqtt.publish(self.base + "/bridge/response/" + path, resp)
            return
        parts = rest.split("/")
        name, cmd = "/".join(parts[:-1]), parts[-1]
        if name not in self.states:
            self.mqtt.publish(self.base + "/bridge/logging", {"level": "error", "message": "Entity '%s' is unknown" % name, "namespace": "z2m"})
            return
        if cmd == "set":
            st = self.states[name]
            for k, v in data.items():
                if k == "transition":
                    continue
                if k == "color" and isinstance(v, dict) and "hex" in v:
                    st["color"] = {"x": 0.1, "y": 0.2}   # Z2M rechnet hex → xy um
                    st["color_mode"] = "xy"
                elif k == "state" and v == "TOGGLE":
                    st["state"] = "OFF" if st.get("state") == "ON" else "ON"
                else:
                    st[k] = v
        self.mqtt.publish("%s/%s" % (self.base, name), self.states[name], retain=False)


def wait_for(cond, timeout=5.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


CFG = {"mqtt_host": "127.0.0.1", "mqtt_user": "smhub", "mqtt_pass": "geheim", "z2m_base": "zigbee2mqtt",
       "bar": "ThomysHomeBar", "panel": "0x001788010efccbdb", "nuki_base": "nuki/4BCE74DF"}


class Z2MTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.broker = FakeBroker(user="smhub", password="geheim").start()
        cls.fake = FakeZ2M(cls.broker, user="smhub", password="geheim").start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()
        cls.broker.stop()

    def make_client(self, **over):
        cfg = dict(CFG, mqtt_port=self.broker.port, **over)
        z = Zigbee2MQTT(cfg, client_id="test-%d" % (time.time_ns() % 100000))
        z.start()
        self.addCleanup(z.stop)
        self.assertTrue(wait_for(lambda: z.info and z.devices and z.bridge_state == "online"), "Cache nicht befüllt")
        self.assertTrue(wait_for(lambda: z.state("ThomysHomeBar") and z.state("0x001788010efccbdb")), "Zustände nicht per /get geholt")
        return z


class TestMqtt(Z2MTestCase):
    def test_bad_credentials(self):
        z = Zigbee2MQTT(dict(CFG, mqtt_port=self.broker.port, mqtt_pass="falsch"))
        with self.assertRaises(MqttError) as cm:
            z.start()
        self.assertIn("rc=5", str(cm.exception))
        self.assertIn("mqtt_user", str(cm.exception))

    def test_connection_refused(self):
        z = Zigbee2MQTT(dict(CFG, mqtt_port=1))
        with self.assertRaises(MqttError):
            z.start()

    def test_topic_matches(self):
        self.assertTrue(topic_matches("zigbee2mqtt/#", "zigbee2mqtt/bridge/info"))
        self.assertTrue(topic_matches("zigbee2mqtt/+/set", "zigbee2mqtt/Bar/set"))
        self.assertFalse(topic_matches("zigbee2mqtt/+/set", "zigbee2mqtt/a/b/set"))
        self.assertFalse(topic_matches("zigbee2mqtt/+", "zigbee2mqtt/a/b"))

    def test_normalize_hex(self):
        self.assertEqual(normalize_hex("#ff00aa"), "#FF00AA")
        self.assertEqual(normalize_hex("f0a"), "#FF00AA")
        with self.assertRaises(Z2MError):
            normalize_hex("blau")

    def test_start_after_failed_connect_and_after_stop(self):
        z = Zigbee2MQTT(dict(CFG, mqtt_port=1), client_id="lifecycle-%d" % (time.time_ns() % 100000))
        with self.assertRaises(MqttError):
            z.start()                                  # Broker nicht erreichbar → nichts gestartet
        self.assertFalse(z.mqtt._running)
        z.mqtt.port = self.broker.port
        z.start()                                      # zweiter Versuch muss funktionieren
        self.addCleanup(z.stop)
        self.assertTrue(wait_for(lambda: z.info and z.bridge_state == "online"))
        z.stop()
        self.assertFalse(z.connected)
        z.start()                                      # nach stop() wieder startbar …
        self.assertTrue(z.connected)
        self.broker.kick(z.mqtt.client_id)             # … inklusive automatischem Reconnect
        self.assertTrue(wait_for(lambda: z.connected and z.mqtt.connect_count == 3, 8), "kein Reconnect nach stop()/start()")

    def test_rejected_subscription_is_reported(self):
        self.broker.reject.add("zigbee2mqtt/#")
        try:
            z = Zigbee2MQTT(dict(CFG, mqtt_port=self.broker.port), client_id="acl-%d" % (time.time_ns() % 100000))
            z.start()
            self.addCleanup(z.stop)
            self.assertTrue(wait_for(lambda: z.mqtt.rejected_subscriptions == ["zigbee2mqtt/#"]))
            self.assertIn("abgelehnt", z.mqtt.last_error)
            self.assertIn("abgelehnt", z.summary()["mqtt_error"])
            time.sleep(0.3)
            self.assertEqual(z.devices, [])                    # kein Abo → kein Cache, aber sichtbarer Fehler
        finally:
            self.broker.reject.discard("zigbee2mqtt/#")
        z2 = self.make_client()                                # normale Abos sind unbeeinträchtigt
        self.assertEqual(z2.mqtt.rejected_subscriptions, [])

    def test_states_fetched_after_restart(self):
        """Nach einem App-Neustart sind die Zustände nicht retained — der Client holt sie per /get."""
        n_get = len(self.broker.messages("zigbee2mqtt/ThomysHomeBar/get"))
        z = self.make_client()
        self.assertEqual(z.state("ThomysHomeBar")["state"], self.fake.states["ThomysHomeBar"]["state"])
        self.assertGreaterEqual(len(self.broker.messages("zigbee2mqtt/ThomysHomeBar/get")), n_get + 1)
        self.assertIn("ThomysHomeBar", z._state_requested)
        # erneutes bridge/devices löst kein zweites /get aus, solange der Zustand bekannt ist
        n_get = len(self.broker.messages("zigbee2mqtt/ThomysHomeBar/get"))
        self.fake.mqtt.publish("zigbee2mqtt/bridge/devices", DEVICES, retain=True)
        time.sleep(0.3)
        self.assertEqual(len(self.broker.messages("zigbee2mqtt/ThomysHomeBar/get")), n_get)

    def test_stop_during_backoff_then_start_has_single_thread_pair(self):
        z = self.make_client()
        cid = z.mqtt.client_id
        mine = lambda: [t for t in threading.enumerate() if t.name.endswith(cid) and t.is_alive()]
        self.assertEqual(len(mine()), 2)
        z.mqtt.port = 1                                   # Reconnect schlägt fehl → Backoff-Schleife
        self.broker.kick(cid)
        self.assertTrue(wait_for(lambda: z.mqtt.last_error and "fehlgeschlagen" in z.mqtt.last_error, 5))
        t0 = time.time()
        z.stop()                                          # muss die Backoff-Pause sofort abbrechen
        self.assertLess(time.time() - t0, 3)
        z.mqtt.port = self.broker.port
        z.start()
        self.assertTrue(z.connected)
        self.assertTrue(wait_for(lambda: len(mine()) == 2, 5), "alte Threads leben weiter: %r" % [t.name for t in mine()])
        self.assertTrue(wait_for(lambda: z.info and z.state("ThomysHomeBar")))

    def test_reconnect_after_kick(self):
        z = self.make_client()
        cid = z.mqtt.client_id
        self.broker.kick(cid)
        # Der Reconnect erfolgt sofort (Backoff greift erst bei fehlgeschlagenen Versuchen)
        self.assertTrue(wait_for(lambda: z.connected and z.mqtt.connect_count == 2, 8), "kein Reconnect")
        # Nach dem Reconnect kommen neue Nachrichten wieder an (Abo erneuert)
        before = z.states["ThomysHomeBar"].get("brightness")
        self.fake.mqtt.publish("zigbee2mqtt/ThomysHomeBar", {"state": "ON", "brightness": 42})
        self.assertTrue(wait_for(lambda: z.states["ThomysHomeBar"].get("brightness") == 42))
        self.fake.mqtt.publish("zigbee2mqtt/ThomysHomeBar", {"state": "ON", "brightness": before})


class TestZigbee2MQTT(Z2MTestCase):
    def test_cache_and_summary(self):
        z = self.make_client()
        s = z.summary()
        self.assertEqual(s["version"], "2.13.0")
        self.assertEqual(s["version_status"], "ok")
        self.assertEqual(s["commit"], "fcbb7ff4")
        self.assertEqual(s["coordinator"], {"type": "zStack3x0", "ieee_address": "0x00124b0033cb0f78", "revision": 20250325})
        self.assertEqual(s["zigbee_herdsman"], "10.8.0")
        self.assertEqual(s["zigbee_herdsman_converters"], "26.90.0")
        self.assertEqual(s["node_version"], "v22.22.0")
        self.assertEqual(s["memory_mb"], 489)
        self.assertEqual(s["frontend_url"], "http://127.0.0.1:8080")
        self.assertEqual(s["devices_total"], 3)
        self.assertEqual(s["lights_total"], 2)
        self.assertTrue(s["online"])
        self.assertTrue(wait_for(lambda: z.health))
        self.assertEqual(z.summary()["health"]["uptime_sec"], 3600)
        self.assertIn("Zigbee2MQTT 2.13.0 ✓", z.status_text())
        self.assertIn("3 Geräte (2 Leuchten)", z.status_text())

    def test_version_status(self):
        z = self.make_client(z2m_expected_version="2.12.0")
        self.assertEqual(z.version_status(), "newer")
        self.assertIn("↑ neuer als 2.12.0", z.status_text())
        z2 = self.make_client(z2m_expected_version="2.14.0")
        self.assertEqual(z2.version_status(), "older")
        self.assertIn("⚠ älter als 2.14.0", z2.status_text())

    def test_frontend_url_override(self):
        z = self.make_client(z2m_frontend_url="http://192.168.1.45:8080")
        self.assertEqual(z.frontend_url(), "http://192.168.1.45:8080")

    def test_lights(self):
        z = self.make_client()
        lights = {l["friendly_name"]: l for l in z.lights()}
        self.assertEqual(set(lights), {"ThomysHomeBar", "0x001788010efccbdb"})
        bar = lights["ThomysHomeBar"]
        self.assertEqual(bar["features"], ["state", "brightness", "color_xy"])
        self.assertTrue(bar["color"])
        self.assertEqual(bar["state"], "ON")
        self.assertEqual(bar["vendor"], "Tuya")
        self.assertTrue(wait_for(lambda: z.availability.get("ThomysHomeBar") == "online"))
        self.assertEqual(z.device("0x001788010efccbdb")["definition"]["vendor"], "Philips")
        self.assertEqual(z.state("0xa4c138aaaaaaaaaa")["state"], "ON")   # per IEEE → friendly_name

    def test_light_set_and_state_update(self):
        z = self.make_client()
        sent = z.light_set("ThomysHomeBar", hex_color="#0033ff", brightness_pct=70, transition=2)
        self.assertEqual(sent, {"state": "ON", "brightness": 178, "color": {"hex": "#0033FF"}, "transition": 2})
        self.assertTrue(wait_for(lambda: self.broker.messages("zigbee2mqtt/ThomysHomeBar/set")[-1:] == [sent]))
        self.assertTrue(wait_for(lambda: z.state("ThomysHomeBar").get("brightness") == 178))
        self.assertEqual(z.light_set("ThomysHomeBar", state=False), {"state": "OFF"})
        self.assertTrue(wait_for(lambda: z.state("ThomysHomeBar").get("state") == "OFF"))
        z.toggle("ThomysHomeBar")
        self.assertTrue(wait_for(lambda: z.state("ThomysHomeBar").get("state") == "ON"))
        self.assertEqual(z.light_set("ThomysHomeBar", brightness=0), {"brightness": 0, "state": "OFF"})
        with self.assertRaises(Z2MError):
            z.light_set("ThomysHomeBar")
        # Zustand wiederherstellen
        z.light_set("ThomysHomeBar", state="ON", brightness=200)
        self.assertTrue(wait_for(lambda: z.state("ThomysHomeBar").get("brightness") == 200))

    def test_refresh_state_waits_for_this_device(self):
        z = self.make_client()
        st = z.refresh_state("ThomysHomeBar", timeout=2)
        self.assertEqual(st["state"], self.fake.states["ThomysHomeBar"]["state"])
        self.assertIn("ThomysHomeBar", z.state_updated_at)
        self.assertIsNotNone(z.refresh_state("0xa4c138aaaaaaaaaa", timeout=2))   # per IEEE-Adresse
        # Unbekanntes Gerät: Z2M antwortet nur mit einem Log-Fehler (fremder Verkehr) → volles Timeout, None
        t0 = time.time()
        self.assertIsNone(z.refresh_state("gibtsnicht", timeout=0.5))
        self.assertGreaterEqual(time.time() - t0, 0.5)
        self.assertTrue(wait_for(lambda: any("gibtsnicht" in l.get("message", "") for l in z.logs)))
        self.assertEqual(z.available("0xa4c138aaaaaaaaaa"), "online")
        self.assertEqual(z.friendly_name("0xa4c138aaaaaaaaaa"), "ThomysHomeBar")
        self.assertEqual(z.friendly_name("unbekannt"), "unbekannt")

    def test_get(self):
        z = self.make_client()
        before = z.last_message_at
        z.get("ThomysHomeBar", ("state", "brightness"))
        self.assertTrue(wait_for(lambda: self.broker.messages("zigbee2mqtt/ThomysHomeBar/get")[-1:] == [{"state": "", "brightness": ""}]))
        self.assertTrue(wait_for(lambda: z.last_message_at != before))

    def test_request_permit_join(self):
        z = self.make_client()
        self.assertIsNone(z.permit_join_remaining_sec())
        self.assertEqual(z.permit_join(120), {"time": 120})
        self.assertTrue(wait_for(lambda: z.info.get("permit_join") is True))
        self.assertTrue(100 <= z.permit_join_remaining_sec() <= 120)          # FakeZ2M: Sekunden
        info_ms = dict(z.info, permit_join_end=int((time.time() + 90) * 1000))  # neuere Z2M: Millisekunden
        self.fake.mqtt.publish("zigbee2mqtt/bridge/info", info_ms, retain=True)
        self.assertTrue(wait_for(lambda: z.info.get("permit_join_end") == info_ms["permit_join_end"]))
        self.assertTrue(80 <= z.permit_join_remaining_sec() <= 90)
        self.assertEqual(z.summary()["permit_join_remaining_sec"], z.permit_join_remaining_sec())
        self.assertEqual(z.permit_join(0), {"time": 0})
        self.assertTrue(wait_for(lambda: z.info.get("permit_join") is False))
        self.assertEqual(self.fake.requests[-1][1]["time"], 0)
        self.assertIn("transaction", self.fake.requests[-1][1])

    def test_request_health_check(self):
        z = self.make_client()
        self.assertEqual(z.health_check(), {"healthy": True})

    def test_request_error(self):
        z = self.make_client()
        with self.assertRaises(Z2MError) as cm:
            z.rename("gibtsnicht", "neu")
        self.assertIn("does not exist", str(cm.exception))
        self.assertEqual(z.rename("ThomysHomeBar", "ThomysHomeBar")["to"], "ThomysHomeBar")

    def test_request_timeout(self):
        z = self.make_client()
        t0 = time.time()
        with self.assertRaises(Z2MError) as cm:
            z.request("never_answer", timeout=0.3)
        self.assertIn("Timeout", str(cm.exception))
        self.assertLess(time.time() - t0, 2)
        self.assertEqual(z._pending, {})

    def test_events_availability_logging(self):
        z = self.make_client()
        self.fake.mqtt.publish("zigbee2mqtt/bridge/event", {"type": "device_joined", "data": {"friendly_name": "0xneu", "ieee_address": "0xneu"}})
        self.fake.mqtt.publish("zigbee2mqtt/Sensor Flur/availability", {"state": "offline"}, retain=True)
        self.fake.mqtt.publish("zigbee2mqtt/bridge/logging", {"level": "warning", "message": "Failed to ping 'Sensor Flur'", "namespace": "z2m"})
        self.fake.mqtt.publish("zigbee2mqtt/bridge/logging", {"level": "info", "message": "uninteressant", "namespace": "z2m"})
        self.assertTrue(wait_for(lambda: any(e.get("type") == "device_joined" for e in z.events)))
        self.assertTrue(wait_for(lambda: z.availability.get("Sensor Flur") == "offline"))
        self.assertTrue(wait_for(lambda: len(z.logs) == 1))
        self.assertEqual(z.logs[0]["level"], "warning")
        self.assertEqual(z.summary()["devices_offline"], ["Sensor Flur"])
        self.fake.mqtt.publish("zigbee2mqtt/Sensor Flur/availability", {"state": "online"}, retain=True)
        self.assertTrue(wait_for(lambda: z.availability.get("Sensor Flur") == "online"))

    def test_bridge_offline(self):
        z = self.make_client()
        self.fake.mqtt.publish("zigbee2mqtt/bridge/state", {"state": "offline"}, retain=True)
        self.assertTrue(wait_for(lambda: z.bridge_state == "offline"))
        self.assertFalse(z.summary()["online"])
        self.assertIn("Bridge offline", z.status_text())
        t0 = time.time()
        with self.assertRaises(Z2MError) as cm:            # sofort, nicht erst nach dem Timeout
            z.permit_join(10)
        self.assertIn("offline", str(cm.exception))
        self.assertLess(time.time() - t0, 1)
        self.fake.mqtt.publish("zigbee2mqtt/bridge/state", {"state": "online"}, retain=True)
        self.assertTrue(wait_for(lambda: z.bridge_state == "online"))

    def test_own_set_messages_not_treated_as_state(self):
        z = self.make_client()
        z.set("Neu/Gerät", {"state": "ON"})
        time.sleep(0.2)
        self.assertNotIn("Neu/Gerät/set", z.states)
        self.assertNotIn("Neu/Gerät", z.states)


class TestApi(Z2MTestCase):
    def test_routes(self):
        z = self.make_client()
        api = Z2MApi(z)
        self.assertIsNone(api.handle("GET", "/api/state", {}))
        status, body = api.handle("GET", "/api/z2m/info", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["version"], "2.13.0")
        status, body = api.handle("GET", "/api/z2m/status", {})
        self.assertTrue(body["ok"] and body["result"]["online"])
        status, body = api.handle("GET", "/api/z2m/lights", {})
        self.assertEqual(len(body["result"]), 2)
        status, body = api.handle("GET", "/api/z2m/devices", {})
        self.assertEqual(len(body["result"]), 4)
        status, body = api.handle("GET", "/api/z2m/groups", {})
        self.assertEqual(body["result"][0]["friendly_name"], "Alle Leuchten")
        status, body = api.handle("GET", "/api/z2m/state", {"name": "ThomysHomeBar", "refresh": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["state"]["state"], "ON")
        status, body = api.handle("GET", "/api/z2m/state", {"name": "0xa4c138aaaaaaaaaa", "refresh": "1", "wait": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["friendly_name"], "ThomysHomeBar")
        self.assertEqual(body["result"]["available"], "online")
        status, body = api.handle("GET", "/api/z2m/devices", {})
        self.assertEqual([d["available"] for d in body["result"] if d["friendly_name"] == "ThomysHomeBar"], ["online"])
        status, body = api.handle("GET", "/api/z2m/state", {"name": "unbekannt"})
        self.assertEqual(status, 404)
        status, body = api.handle("GET", "/api/z2m/state", {})
        self.assertEqual(status, 400)
        status, body = api.handle("POST", "/api/z2m/set", {"name": "ThomysHomeBar", "hex": "#00c000", "brightness": "50", "transition": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["sent"], {"state": "ON", "brightness": 127, "color": {"hex": "#00C000"}, "transition": 1.0})
        status, body = api.handle("POST", "/api/z2m/set", {"name": "ThomysHomeBar", "hex": "keinefarbe"})
        self.assertEqual(status, 400)
        for bad in ({"name": "ThomysHomeBar", "brightness": "inf"}, {"name": "ThomysHomeBar", "brightness": "150"},
                    {"name": "ThomysHomeBar", "brightness": "1e999"}, {"name": "ThomysHomeBar", "transition": "abc"},
                    {"name": "ThomysHomeBar", "state": "vielleicht"}, {"name": "ThomysHomeBar"},
                    {"name": "a/+", "hex": "#ff0000"}, {"name": "#", "hex": "#ff0000"}, {"name": "/x", "hex": "#ff0000"}):
            status, body = api.handle("POST", "/api/z2m/set", bad)
            self.assertEqual(status, 400, bad)
            self.assertFalse(body["ok"])
        status, body = api.handle("POST", "/api/z2m/permit_join", {"time": "300"})
        self.assertEqual(status, 400)
        status, body = api.handle("POST", "/api/z2m/rename", {"from": "a/#", "to": "b"})
        self.assertEqual(status, 400)
        status, body = api.handle("PUT", "/api/z2m/info", {})
        self.assertEqual(status, 405)
        status, body = api.handle("__INIT", "/api/z2m/_", {"x": 1})
        self.assertEqual(status, 405)
        self.assertIs(api.z2m, z)                                  # Instanz unangetastet
        self.assertEqual(api._wait({"wait": "99999"}), 10.0)      # wait ist begrenzt (HTTP-Server blockiert)
        self.assertEqual(api._wait({}), 2.0)
        with self.assertRaises(Z2MError):
            z.set("a/+", {"state": "ON"})
        status, body = api.handle("POST", "/api/z2m/toggle", {"name": "ThomysHomeBar"})
        self.assertEqual(body["result"]["sent"], {"state": "TOGGLE"})
        status, body = api.handle("POST", "/api/z2m/permit_join", {"time": "60"})
        self.assertEqual(body["result"], {"time": 60})
        status, body = api.handle("POST", "/api/z2m/permit_join", {"time": "0"})
        status, body = api.handle("POST", "/api/z2m/health_check", {})
        self.assertEqual(body["result"], {"healthy": True})
        status, body = api.handle("POST", "/api/z2m/restart", {})
        self.assertEqual(status, 400)
        status, body = api.handle("POST", "/api/z2m/rename", {"from": "nix", "to": "neu"})
        self.assertEqual(status, 502)
        status, body = api.handle("GET", "/api/z2m/events", {})
        self.assertIn("events", body["result"])
        status, body = api.handle("GET", "/api/z2m/permit_join", {})
        self.assertEqual(status, 405)
        status, body = api.handle("GET", "/api/z2m/nix", {})
        self.assertEqual(status, 404)
        self.assertNotIn("restart", [r[0] for r in self.fake.requests])   # ohne confirm=1 nie gesendet

    def test_standalone_server(self):
        import urllib.request
        from http.server import HTTPServer
        import z2m_api
        # serve() blockiert — deshalb Handler-Logik über einen Thread mit Timeout testen
        cfg = dict(CFG, mqtt_port=self.broker.port)
        t = threading.Thread(target=z2m_api.serve, args=(cfg, 18098), daemon=True)
        t.start()
        self.assertTrue(wait_for(lambda: _http_ok("http://127.0.0.1:18098/api/z2m/status"), 5))
        def info():
            return json.loads(urllib.request.urlopen("http://127.0.0.1:18098/api/z2m/info", timeout=3).read())["result"]
        self.assertTrue(wait_for(lambda: info()["version"] == "2.13.0", 5))   # Server verbindet im Hintergrund
        r = info()
        self.assertEqual(r["coordinator"]["type"], "zStack3x0")
        page = urllib.request.urlopen("http://127.0.0.1:18098/", timeout=3).read().decode()
        self.assertIn("Zigbee2MQTT in ThomysHomeAgent", page)
        self.assertNotIn("%%", page)
        self.assertNotIn("onclick", page)                       # keine Inline-Handler mit Gerätenamen
        self.assertIn('data-name', page)


def _http_ok(url):
    import urllib.request
    try:
        return urllib.request.urlopen(url, timeout=1).status == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HomeBrain mit Zigbee2MQTT
# ---------------------------------------------------------------------------

class FakeHA:
    def __init__(self, down=False):
        self.calls = []
        self.down = down
        self._states = [
            {"entity_id": "light.stube", "state": "on", "attributes": {"supported_color_modes": ["xy"]}},
            {"entity_id": "light.kochinsel_kochinsel", "state": "on", "attributes": {"supported_color_modes": ["hs"]}},
            {"entity_id": "light.flur_flur", "state": "off", "attributes": {"supported_color_modes": ["brightness"]}},
            {"entity_id": "light.65pus8000_12_ambilight", "state": "on", "attributes": {"supported_color_modes": ["rgb"]}},
            {"entity_id": "switch.x", "state": "on", "attributes": {}},
        ]

    def states(self):
        if self.down:
            raise ConnectionError("HA nicht erreichbar")
        return self._states

    def light(self, ents, **kw):
        self.calls.append(("light", list(ents), kw))

    def light_off(self, ents, **kw):
        self.calls.append(("light_off", list(ents), kw))

    def call_service(self, d, s, data):
        self.calls.append(("service", d, s, data))


class FakeMqttHandle:
    def __init__(self, log):
        self.log = log

    def publish(self, topic, payload):
        self.log.append((topic, payload))

    def close(self):
        pass


class FakeAgent:
    def __init__(self, cfg, ha_down=False):
        self.cfg = cfg
        self.ha = FakeHA(ha_down)
        self.bar_calls = []
        self.z2m_set_calls = []
        self.nuki = []

    def bar(self, hexc):
        self.bar_calls.append(hexc)

    def z2m_set(self, name, payload):
        self.z2m_set_calls.append((name, payload))

    def _mqtt(self):
        return FakeMqttHandle(self.nuki)

    def szene_abend(self): self.ha.calls.append(("szene", "abend"))
    def szene_hell(self): self.ha.calls.append(("szene", "hell"))
    def szene_aus(self): self.ha.calls.append(("szene", "aus"))
    def szene_brasilien(self): self.ha.calls.append(("szene", "brasilien"))


class TestHomeBrain(Z2MTestCase):
    def brain(self, with_z2m=True, ha_down=False):
        agent = FakeAgent(dict(CFG), ha_down=ha_down)
        z = self.make_client() if with_z2m else None
        hb = homebrain.HomeBrain(agent, z2m=z)
        hb._ollama = lambda text: (_ for _ in ()).throw(ConnectionError("kein Ollama"))   # → Regel-Fallback
        hb._load_scenes = lambda: [{"name": "Gemütlich bunt", "bar": {"state": "ON", "color": {"hex": "#FF00AA"}},
                                    "zigbee": {"0x001788010efccbdb": {"state": "ON", "brightness": 80}}}]
        return hb, agent, z

    def sets(self, topic):
        return self.broker.messages("zigbee2mqtt/%s/set" % topic)

    def test_system_prompt_lists_zigbee_lights(self):
        hb, agent, z = self.brain()
        sysprompt = hb._system()
        self.assertNotIn("%ZIGBEE%", sysprompt)
        self.assertIn("thomyshomebar", sysprompt)
        self.assertIn("wandpanel", sysprompt)
        self.assertIn("bar", sysprompt)

    def test_bar_color_without_ha(self):
        hb, agent, z = self.brain(ha_down=True)
        self.assertEqual(hb.handle("bar auf blau"), "✓ Bar blau")
        self.assertEqual(agent.bar_calls, ["#0033FF"])
        self.assertEqual(agent.ha.calls, [])

    def test_zigbee_panel_by_synonym(self):
        hb, agent, z = self.brain()
        n_before = len(self.sets("0x001788010efccbdb"))
        self.assertEqual(hb.handle("wandpanel grün"), "✓ wandpanel grün")
        self.assertTrue(wait_for(lambda: len(self.sets("0x001788010efccbdb")) == n_before + 1))
        self.assertEqual(self.sets("0x001788010efccbdb")[-1], {"state": "ON", "color": {"hex": "#00C000"}, "brightness": 180, "transition": 2})
        self.assertEqual(agent.ha.calls, [])
        self.assertTrue(wait_for(lambda: z.state("0x001788010efccbdb").get("color_mode") == "xy"))

    def test_zigbee_light_by_friendly_name(self):
        hb, agent, z = self.brain()
        n_before = len(self.sets("ThomysHomeBar"))
        done = hb.execute([{"action": "brightness", "target": "ThomysHomeBar", "pct": 50}])
        self.assertEqual(done, ["ThomysHomeBar 50%"])
        self.assertTrue(wait_for(lambda: len(self.sets("ThomysHomeBar")) == n_before + 1))
        self.assertEqual(self.sets("ThomysHomeBar")[-1], {"state": "ON", "brightness": 127, "transition": 2})

    def test_govee_panel_stays_home_assistant(self):
        hb, agent, z = self.brain()
        self.assertEqual(hb.handle("panel blau"), "✓ panel blau")
        self.assertEqual(agent.ha.calls, [("light", ["light.h606a"], {"rgb_color": [0, 51, 255], "brightness": 180, "transition": 2})])

    def test_all_color_except_kitchen(self):
        hb, agent, z = self.brain()
        n_panel = len(self.sets("0x001788010efccbdb"))
        self.assertEqual(hb.handle("alles rot ausser küche"), "✓ all rot (ausser küche)")
        self.assertEqual(agent.ha.calls, [("light", ["light.stube"], {"rgb_color": [255, 16, 16], "brightness": 180, "transition": 2})])
        self.assertEqual(agent.bar_calls, ["#FF1010"])
        self.assertTrue(wait_for(lambda: len(self.sets("0x001788010efccbdb")) == n_panel + 1))

    def test_all_off(self):
        hb, agent, z = self.brain()
        n_bar, n_panel = len(self.sets("ThomysHomeBar")), len(self.sets("0x001788010efccbdb"))
        self.assertEqual(hb.handle("alles aus"), "✓ all aus")
        self.assertEqual(agent.ha.calls, [("light_off", ["light.stube", "light.kochinsel_kochinsel", "light.flur_flur", "light.65pus8000_12_ambilight"], {"transition": 2})])
        self.assertTrue(wait_for(lambda: len(self.sets("ThomysHomeBar")) == n_bar + 1 and len(self.sets("0x001788010efccbdb")) == n_panel + 1))
        self.assertEqual(self.sets("ThomysHomeBar")[-1], {"state": "OFF", "transition": 2})
        self.assertTrue(wait_for(lambda: z.state("ThomysHomeBar").get("state") == "OFF"))
        hb.execute([{"action": "power", "target": "all", "on": True}])
        self.assertTrue(wait_for(lambda: z.state("ThomysHomeBar").get("state") == "ON"))

    def test_all_off_except_kitchen(self):
        hb, agent, z = self.brain()
        self.assertEqual(hb._rules("alles aus ausser küche"), [{"action": "power", "target": "all", "on": False, "except": ["küche"]}])
        self.assertEqual(hb._rules("alles grün ausser küche"), [{"action": "color", "target": "all", "color": "grün", "except": ["küche"]}])
        self.assertEqual(hb._rules("alles aus"), [{"action": "power", "target": "all", "on": False}])
        self.assertEqual(hb._rules("alles dunkler ausser küche"), [{"action": "brightness", "target": "all", "pct": 25, "except": ["küche"]}])
        self.assertEqual(hb.handle("alles dunkler ausser küche"), "✓ all 25% (ausser küche)")
        self.assertEqual(agent.ha.calls, [("light", ["light.stube"], {"brightness_pct": 25, "transition": 2})])
        dim = {"state": "ON", "brightness": 63, "transition": 2}
        self.assertTrue(wait_for(lambda: self.sets("ThomysHomeBar")[-1:] == [dim] and self.sets("0x001788010efccbdb")[-1:] == [dim]))
        agent.ha.calls.clear()
        n_bar, n_panel = len(self.sets("ThomysHomeBar")), len(self.sets("0x001788010efccbdb"))
        self.assertEqual(hb.handle("alles aus ausser küche"), "✓ all aus (ausser küche)")
        self.assertEqual(agent.ha.calls, [("light_off", ["light.stube", "light.flur_flur", "light.65pus8000_12_ambilight"], {"transition": 2})])
        self.assertTrue(wait_for(lambda: len(self.sets("ThomysHomeBar")) == n_bar + 1 and len(self.sets("0x001788010efccbdb")) == n_panel + 1))
        hb.execute([{"action": "power", "target": "all", "on": True}])

    def test_all_brightness_except_kitchen(self):
        hb, agent, z = self.brain()
        done = hb.execute([{"action": "brightness", "target": "all", "pct": 30, "except": ["küche"]}])
        self.assertEqual(done, ["all 30% (ausser küche)"])
        # wie im Original: RGB-fähige Lichter (ohne Ambilight), Küche ausgenommen
        self.assertEqual(agent.ha.calls, [("light", ["light.stube"], {"brightness_pct": 30, "transition": 2})])
        self.assertTrue(wait_for(lambda: self.sets("ThomysHomeBar")[-1:] == [{"state": "ON", "brightness": 76, "transition": 2}]))
        hb.execute([{"action": "brightness", "target": "ThomysHomeBar", "pct": 79}])

    def test_all_brightness_matches_original_semantics(self):
        hb, agent, z = self.brain()
        hb.execute([{"action": "brightness", "target": "all", "pct": 40}])
        self.assertEqual(agent.ha.calls, [("light", ["light.stube", "light.kochinsel_kochinsel"], {"brightness_pct": 40, "transition": 2})])
        # keine RGB-Lichter mehr → Original-Fallback: alle eingeschalteten Lichter
        agent.ha.calls.clear()
        for st in agent.ha._states:
            st["attributes"]["supported_color_modes"] = ["brightness"]
        hb.execute([{"action": "brightness", "target": "all", "pct": 40}])
        self.assertEqual(agent.ha.calls, [("light", ["light.stube", "light.kochinsel_kochinsel", "light.65pus8000_12_ambilight"], {"brightness_pct": 40, "transition": 2})])
        hb.execute([{"action": "brightness", "target": "ThomysHomeBar", "pct": 79}])

    def test_zigbee_names_match_whole_words_only(self):
        hb, agent, z = self.brain()
        homebrain.Z2M_GERAETE["bad"] = "@panel"          # Synonym, das in "badezimmer"/"bad" steckt
        try:
            self.assertEqual(hb._rules("bad rot")[0]["target"], "bad")
            self.assertEqual(hb._rules("dusche rot")[0]["target"], "dusche")   # kein Zigbee-Treffer in "dusche"
            self.assertEqual(hb._rules("wandpanel rot")[0]["target"], "wandpanel")
        finally:
            del homebrain.Z2M_GERAETE["bad"]

    def test_all_off_with_ha_down_still_switches_zigbee(self):
        hb, agent, z = self.brain(ha_down=True)
        n_bar = len(self.sets("ThomysHomeBar"))
        out = hb.handle("alles aus")
        self.assertTrue(out.startswith("✓ all aus; [HA-Fehler: HA nicht erreichbar"), out)
        self.assertTrue(wait_for(lambda: len(self.sets("ThomysHomeBar")) == n_bar + 1))
        hb.execute([{"action": "power", "target": "all", "on": True}])

    def test_room_brightness_only_ha(self):
        hb, agent, z = self.brain()
        n_bar = len(self.sets("ThomysHomeBar"))
        # ("heller" trifft im Regel-Fallback die Szene "hell" — unverändertes Verhalten, daher "dunkler")
        self.assertEqual(hb.handle("wohnzimmer dunkler"), "✓ wohnzimmer 25%")
        self.assertEqual(agent.ha.calls, [("light", ["light.stube"], {"brightness_pct": 25, "transition": 2})])
        time.sleep(0.1)
        self.assertEqual(len(self.sets("ThomysHomeBar")), n_bar)

    def test_unknown_target(self):
        hb, agent, z = self.brain()
        self.assertEqual(hb.execute([{"action": "color", "target": "garage", "color": "rot"}]), ["'garage' unbekannt"])

    def test_scene_with_zigbee(self):
        hb, agent, z = self.brain()
        n_bar, n_panel = len(self.sets("ThomysHomeBar")), len(self.sets("0x001788010efccbdb"))
        self.assertEqual(hb.handle("mach gemütlich bunt"), "✓ Szene Gemütlich bunt")
        self.assertTrue(wait_for(lambda: len(self.sets("ThomysHomeBar")) == n_bar + 1 and len(self.sets("0x001788010efccbdb")) == n_panel + 1))
        self.assertEqual(self.sets("0x001788010efccbdb")[-1], {"state": "ON", "brightness": 80})
        self.assertEqual(hb.handle("mach abendlicht"), "✓ Szene abend")

    def test_lock_unchanged(self):
        hb, agent, z = self.brain()
        self.assertEqual(hb.handle("schliess die tür ab"), "✓ Tür abgeschlossen")
        self.assertEqual(agent.nuki, [("nuki/4BCE74DF/lockAction", "2")])

    def test_without_z2m_uses_agent_transport(self):
        hb, agent, z = self.brain(with_z2m=False)
        self.assertIsNone(hb.z2m)
        self.assertEqual(hb.handle("bar auf blau"), "✓ Bar blau")
        self.assertEqual(agent.bar_calls, ["#0033FF"])
        self.assertEqual(hb.handle("alles aus"), "✓ all aus")
        self.assertEqual(agent.z2m_set_calls, [("ThomysHomeBar", {"state": "OFF", "transition": 2})])
        self.assertEqual(hb.handle("wandpanel rot"), "✓ wandpanel rot")
        self.assertEqual(agent.z2m_set_calls[-1], ("0x001788010efccbdb", {"state": "ON", "color": {"hex": "#FF1010"}, "brightness": 180, "transition": 2}))


if __name__ == "__main__":
    unittest.main()
