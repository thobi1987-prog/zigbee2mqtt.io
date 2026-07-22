# Walo Böden – Lager auf dem Raspberry Pi einrichten

So läuft die Lagerverwaltung dauerhaft auf einem Raspberry Pi. Alle Geräte im
selben Netzwerk (PC, Tablet, Handy) greifen dann per Browser auf **dieselben**
Bestände zu. Es werden **keine Zusatzpakete** benötigt – nur Python 3, das auf
Raspberry Pi OS bereits installiert ist.

> **Voraussetzung:** ein Raspberry Pi mit Raspberry Pi OS, im Netzwerk (LAN/WLAN).

---

## 1. Dateien auf den Pi kopieren

Benötigt werden diese Dateien/Ordner (aus dem Ordner „Walo Böden"):

```
server.py
inventar.py
static/index.html
walo-inventar.service   (optional, für Autostart)
```

Kopiere sie z.B. nach `/home/pi/walo-boeden/`. Per USB-Stick, oder vom PC aus mit
`scp`:

```bash
scp -r "Walo Böden" pi@raspberrypi.local:/home/pi/walo-boeden
```

## 2. Python prüfen

Auf dem Pi (per SSH oder Terminal):

```bash
python3 --version      # sollte 3.9 oder neuer zeigen
```

## 3. Testlauf

```bash
cd /home/pi/walo-boeden
python3 server.py
```

Ausgabe etwa:

```
Walo Böden – Lager läuft auf http://0.0.0.0:8000
```

Jetzt an einem beliebigen Gerät im Netzwerk im Browser öffnen:

```
http://raspberrypi.local:8000
```

Falls `raspberrypi.local` nicht funktioniert, die IP-Adresse des Pi verwenden
(auf dem Pi mit `hostname -I` anzeigen), z.B. `http://192.168.1.50:8000`.

Beenden mit **STRG+C**.

## 4. Automatisch starten (empfohlen)

Damit die Anwendung nach jedem Neustart des Pi von selbst läuft, richten wir
einen systemd-Dienst ein.

1. Datei `walo-inventar.service` bei Bedarf anpassen (Benutzer/Pfad – Standard
   ist Benutzer `pi` und `/home/pi/walo-boeden`).
2. Dienst installieren und starten:

```bash
sudo cp /home/pi/walo-boeden/walo-inventar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now walo-inventar
```

3. Status prüfen:

```bash
systemctl status walo-inventar
```

Ab jetzt startet die Lagerverwaltung automatisch mit dem Pi und läuft im
Hintergrund weiter.

Nützliche Befehle:

```bash
sudo systemctl restart walo-inventar   # neu starten
sudo systemctl stop walo-inventar      # anhalten
journalctl -u walo-inventar -f         # Log live ansehen
```

## 5. Datensicherung

Alle Daten liegen in **einer** Datei: `inventar.db` (im Ordner der Anwendung).
Für ein Backup einfach diese Datei kopieren – am besten regelmäßig, z.B. per
`cron` auf einen USB-Stick oder eine Netzwerkfreigabe:

```bash
cp /home/pi/walo-boeden/inventar.db /media/usb/inventar-backup.db
```

---

## Hinweise

- **Zugriff nur im lokalen Netz:** Der Server ist absichtlich nur im eigenen
  Netzwerk erreichbar. Für Zugriff von außen (Internet) bitte zusätzlich
  Absicherung (VPN, Reverse-Proxy mit Passwort/HTTPS) vorsehen – das ist in
  dieser Version bewusst noch nicht enthalten.
- **Port ändern:** `python3 server.py --port 8080` oder im Service-File.
- **Mehrere Nutzer gleichzeitig:** kein Problem – SQLite und der Server kommen
  mit gleichzeitigen Zugriffen im kleinen Betrieb gut zurecht.
- **CLI weiter nutzbar:** `python3 inventar.py list` etc. arbeiten auf derselben
  Datenbank wie die Weboberfläche.
