# Lagerbuchhaltungssystem – Konzept

> Status: Entwurf (Konzeptphase, noch kein Code)
> Fokus dieser Phase: **Artikelstammdaten + Bestandsverwaltung**
> Erstellt: 2026-07-22

---

## 1. Ziel & Abgrenzung

Ziel ist ein einfaches, erweiterbares System zur **Verwaltung von Artikeln und deren Lagerbeständen**.

**In dieser ersten Ausbaustufe enthalten:**

- Artikelstammdaten pflegen (anlegen, ändern, deaktivieren)
- Aktuellen Bestand je Artikel führen und anzeigen
- Grundlage schaffen, auf der spätere Buchungen (Ein-/Ausgänge) sauber aufsetzen

**Bewusst (noch) NICHT enthalten** – aber im Datenmodell schon mitgedacht:

- Buchen von Wareneingängen/-ausgängen als Bewegungen
- Bestandsbewertung (FIFO, Durchschnittspreis, Lagerwert)
- Auswertungen/Berichte (Mindestbestand-Warnungen, Inventurlisten, Export)
- Mehrere Lagerorte, Benutzerverwaltung, Rechte

Diese Punkte sind in Abschnitt 6 als Ausbaustufen skizziert.

---

## 2. Grundbegriffe

| Begriff | Bedeutung |
|---|---|
| **Artikel** | Ein eindeutig identifizierbares Produkt (z.B. „Schraube M4x20“). Hat Stammdaten. |
| **Bestand** | Die aktuell vorhandene Menge eines Artikels im Lager. |
| **Buchung / Bewegung** | Eine Veränderung des Bestands (Zugang +, Abgang −). *(Ausbaustufe 2)* |
| **Einheit** | Mengeneinheit des Artikels (Stück, kg, Liter, Meter …). |
| **SKU / Artikelnummer** | Interne, eindeutige Kennung des Artikels. |

**Wichtiges Prinzip (Buchhaltung):** Der Bestand ist idealerweise **kein frei editierbares Feld**, sondern das Ergebnis aller Buchungen. In Stufe 1 führen wir den Bestand zunächst direkt; in Stufe 2 wird er aus den Bewegungen berechnet. Das ist bei der Datenmodellierung schon berücksichtigt.

---

## 3. Datenmodell

### Stufe 1 (jetzt)

**Tabelle `artikel`**

| Feld | Typ | Beschreibung |
|---|---|---|
| `id` | Integer (PK) | Technischer Schlüssel |
| `artikelnummer` | Text (unique) | Eindeutige SKU / Artikelnummer |
| `bezeichnung` | Text | Anzeigename |
| `beschreibung` | Text (optional) | Freitext |
| `einheit` | Text | z.B. „Stück“, „kg“ |
| `bestand` | Dezimal | Aktueller Lagerbestand |
| `mindestbestand` | Dezimal (optional) | Für spätere Warnungen |
| `aktiv` | Boolean | Deaktivieren statt Löschen |
| `erstellt_am` | Zeitstempel | |
| `geaendert_am` | Zeitstempel | |

### Stufe 2 (vorbereitet)

**Tabelle `buchung`** – der Bestand wird dann daraus abgeleitet:

| Feld | Typ | Beschreibung |
|---|---|---|
| `id` | Integer (PK) | |
| `artikel_id` | FK → artikel.id | |
| `typ` | Text | `zugang` / `abgang` / `korrektur` |
| `menge` | Dezimal | Immer positiv; Vorzeichen ergibt sich aus `typ` |
| `preis` | Dezimal (optional) | Einzelpreis für spätere Bewertung |
| `beleg` | Text (optional) | Referenz (Lieferschein, Auftrag …) |
| `gebucht_am` | Zeitstempel | |

> `bestand(artikel) = Σ(zugänge) − Σ(abgänge) ± korrekturen`

Dieses Muster (Bewegungen als „Journal“, Bestand als Saldo) ist der eigentliche Kern einer *Buchhaltung* – deshalb wird es früh mitgedacht.

---

## 4. Kernfunktionen (Stufe 1)

1. **Artikel anlegen** – mit Pflichtfeldern Artikelnummer, Bezeichnung, Einheit.
2. **Artikel auflisten/suchen** – Übersicht mit Bestand, Filter nach Bezeichnung/Nummer.
3. **Artikel bearbeiten** – Stammdaten ändern.
4. **Artikel deaktivieren** – statt Löschen (`aktiv = false`), um Historie zu erhalten.
5. **Bestand anzeigen** – aktueller Wert je Artikel.

---

## 5. Empfehlung zum Tech-Stack

Da der Stack offen ist, hier eine pragmatische Empfehlung – abgestuft nach Anspruch:

### Empfehlung: **Python + SQLite, später optional Weboberfläche**

- **Datenhaltung:** SQLite (eine Datei, kein Server nötig, ideal für Start & Einzelplatz).
- **Logik:** Python – gut lesbar, große Auswahl an Bibliotheken, leicht zu warten.
- **Oberfläche – 2 Wege:**
  - **Start klein:** einfache **CLI** (Kommandozeile) oder ein Python-Skript. Schnell umsetzbar, um das Datenmodell zu validieren.
  - **Ausbau:** kleines **Web-UI mit FastAPI + einfachem Frontend** (oder Streamlit für sehr schnellen Prototyp).

**Warum diese Wahl?**

| Kriterium | Bewertung |
|---|---|
| Einstiegshürde | Niedrig – keine Serverinfrastruktur nötig |
| Erweiterbar | Ja – SQLite → PostgreSQL später problemlos migrierbar |
| Wartbarkeit | Hoch |
| Multi-User später | Über Wechsel auf PostgreSQL + Web-Backend abbildbar |

### Alternativen (falls andere Präferenzen)

- **Web-App im Vue/TypeScript-Stack** (passend, wenn du bei Web/JS bleiben willst) – Frontend + kleines Backend (Node/Express) + SQLite/Postgres.
- **Excel/Tabelle** – nur sinnvoll für sehr kleine, einmalige Fälle; skaliert und prüft Daten schlecht, deshalb für ein „System“ nicht empfohlen.

---

## 6. Ausbaustufen (Roadmap)

| Stufe | Inhalt | Nutzen |
|---|---|---|
| **1 (jetzt)** | Artikelstammdaten + Bestände | Fundament: Was gibt es, wie viel ist da? |
| **2** | Buchungen (Ein-/Ausgänge), Bestand als Saldo | Echte, nachvollziehbare Bestandsführung |
| **3** | Bewertung (FIFO/Durchschnitt), Lagerwert | Kaufmännische Aussagen, Lagerwert |
| **4** | Auswertungen: Mindestbestand-Warnung, Inventurliste, CSV/PDF-Export | Steuerung & Reporting |
| **5** | Mehrere Lager, Benutzer/Rechte, Barcode/QR | Skalierung zum Mehrplatzsystem |

---

## 7. Offene Fragen (vor Umsetzung zu klären)

1. **Nutzungsumfang:** Einzelplatz oder mehrere Nutzer gleichzeitig?
2. **Mengenlogik:** Sind Nachkommastellen nötig (kg/Liter) oder nur ganze Stück?
3. **Bestandsführung:** Direkt editierbar (einfach) oder streng über Buchungen (Buchhaltung)? – Empfehlung: mittelfristig über Buchungen.
4. **Oberfläche:** CLI zum Start akzeptabel, oder direkt Weboberfläche gewünscht?
5. **Datenübernahme:** Gibt es bestehende Artikeldaten (z.B. Excel) zum Import?

---

## 8. Vorgeschlagene nächste Schritte

1. Tech-Stack final bestätigen (Empfehlung: Python + SQLite).
2. Datenmodell Stufe 1 festlegen (Abschnitt 3 als Basis).
3. Minimalen Prototyp bauen: Artikel anlegen/auflisten/bearbeiten + Bestand anzeigen.
4. Am Prototyp testen, dann Stufe 2 (Buchungen) planen.

> Sag Bescheid, welche der offenen Fragen (Abschnitt 7) du beantworten möchtest –
> dann konkretisiere ich das Konzept oder starte mit dem Prototyp.
