# Sentinel MK1

Objekterkennung für mehrere **RTSP-Kamerastreams** auf dem **GL.iNet Brume 2** oder einem
**NVIDIA Jetson Nano (2GB/4GB)** mit GPU-Beschleunigung – im dunklen,
technischen Design der übrigen Dashboards. Personen, Tiere, Fahrzeuge usw. werden in jedem Stream
automatisch erkannt, farbig umrahmt, als Bild in einer Galerie gespeichert und **über alle Streams
hinweg wiedererkannt**. Personen werden einzeln identifiziert (Gesicht + Aussehen), Korrekturen
verbessern die nächste Zuordnung, und Alarmregeln melden, wenn z. B. dieselbe Person mehrfach
auftaucht.

**Version: 1.2.0** · Web-Port **8096** · läuft komplett lokal, keine Cloud

![Live-Ansicht mit drei Streams und farbigen Rahmen](docs/vorschau.png)

## Funktionen

- **Mehrere RTSP-Streams** gleichzeitig (auch HTTP/MJPEG oder Videodateien – alles, was ffmpeg öffnet).
  Pro Stream einstellbar: Bilder/s, Bildbreite, TCP/UDP, „nur Keyframes“ (spart Rechenleistung).
- **Automatische Erkennung** (80 Arten, NanoDet-Plus) mit Rahmenfarben je Gruppe:

  | Gruppe | Farbe | Arten |
  |---|---|---|
  | Personen | 🔴 Rot | Person |
  | Tiere | 🔵 Blau | Vogel, Katze, Hund, Pferd, Schaf, Kuh, … |
  | Fahrzeuge | 🟡 Gelb | Auto, LKW, Bus, Motorrad |
  | Fahrräder | 🟢 Grün | Fahrrad |
  | Weitere Verkehrsmittel | 🟠 Orange | Flugzeug, Zug, Boot (aus) |
  | Taschen & Gepäck | 🟣 Violett | Rucksack, Handtasche, Koffer, Schirm (aus) |
  | Sonstige Gegenstände | ⚪ Grau | einzeln zuschaltbar |

- **Galerie:** von jedem erkannten Objekt wird ein Bild gespeichert (Vollbild + Ausschnitt) mit
  **Quell-Stream und Zeitstempel des Systems**; Filter nach Stream, Art, Kategorie, Tag.
  Bei längerer Anwesenheit optional weitere Bilder (Standard alle 120 s).
- **Objekt-Datenbank über alle Streams:** Wird ein Objekt mehrmals erkannt, erscheint es in der
  Liste **„Objekte“** – dort Namen vergeben und in **selbst angelegte Kategorien** einsortieren
  (z. B. Familie, Nachbarn, Lieferdienst, Eigene Fahrzeuge).
- **Personen einzeln identifizieren:** Gesicht finden (YuNet) und vergleichen (SFace, 128-dim.
  Merkmal). Ohne sichtbares Gesicht wird über Kleidung/Farben zugeordnet (zeitlich begrenzt,
  Standard 12 h). Taucht das Gesicht später auf, wird die Sichtung automatisch umgebucht.
- **Fehlzuordnungen korrigieren – das System lernt:** Bild (oder ganze Sichtung) einem anderen
  oder einem neuen Objekt zuordnen, mehrere Bilder auf einmal verschieben, Objekte zusammenführen.
  Die Merkmale werden beim richtigen Objekt als feste Beispiele und beim falschen als
  **Gegenbeispiele** gespeichert – die nächste Erkennung ordnet entsprechend besser zu.

  ![Zuordnung korrigieren](docs/korrektur.png)

- **Alarme:** Regeln für eine Art/Gruppe, eine Kategorie, ein bestimmtes Objekt oder alle Objekte,
  z. B. *„dieselbe Person ≥ 3× in 30 min in mindestens 2 Streams“* oder *„unbekannte Person …“*.
  Zählweise je Objekt einzeln oder gesamt, Pause nach Alarm. Meldung in der Oberfläche (mit Ton),
  per **MQTT** (`sentinel/alarm`) und/oder **Webhook** (JSON per POST).
- Live-Ansicht: flüssiges Live-Bild oder exakt das analysierte Bild, Vollbild per Doppelklick,
  Klick auf einen Rahmen öffnet das Objekt. iPhone/iPad-tauglich.
- **Bewegungsfilter:** ruhige Szenen werden nicht analysiert (spart Rechenzeit auf dem Brume).
- **Speicherverwaltung:** Bilder nach x Tagen bzw. ab x MB automatisch löschen – gelernte
  Personen/Objekte bleiben erhalten.
- Alle Einstellungen in `config.json` (gut lesbar), Daten in `data/` – beides bleibt bei Updates erhalten.

![Objektliste mit Namen und Kategorien](docs/objekte.png)

## Installation auf dem NVIDIA Jetson Nano 2GB

Ein Skript richtet alles ein und nutzt die Hardware des Jetson:

| Aufgabe | läuft auf |
|---|---|
| Video dekodieren (H.264/H.265) + verkleinern | **NVDEC + VIC** (Hardware, `nvv4l2decoder` / `nvvidconv`) |
| Objekterkennung NanoDet (416 px) | **GPU, TensorRT FP16** |
| Gesichtsmerkmale SFace | **GPU, TensorRT FP16** |
| Gesichtssuche, Verfolgung, Datenbank, Weboberfläche | 4 CPU-Kerne |
| Live-Bild / Galerie-JPEGs | OpenCV, nur bei Bedarf |

Voraussetzung: JetPack 4.6 (L4T R32.7, Ubuntu 18.04) – das Standard-Image des Jetson Nano 2GB.

```sh
# ZIP auf den Jetson kopieren und dort:
unzip -o Sentinel-MK1.zip && sudo bash Sentinel-MK1/install-jetson.sh

# oder direkt von GitHub:
wget -O /tmp/install-jetson.sh https://raw.githubusercontent.com/Scorch1262/Sentinel-MK1/main/install-jetson.sh
sudo bash /tmp/install-jetson.sh
```

`install-jetson.sh` erledigt:

1. Pakete per `apt`: numpy, ffmpeg, GStreamer (rtsp, h264/h265-Parser), OpenBLAS; prüft OpenCV und TensorRT aus JetPack
2. 4 GB Auslagerungsdatei, falls weniger als 3 GB Swap vorhanden (nötig für den Engine-Bau beim 2-GB-Modell)
3. Programm nach **`/opt/Sentinel-MK1`** (`config.json` und `data/` bleiben erhalten)
4. KI-Modelle laden und daraus einmalig die **TensorRT-Engines** bauen (FP16, insgesamt ca. 5–20 min)
5. Leistungsmodus MAXN (`nvpmodel -m 0`)
6. Fragt, ob die grafische Oberfläche abgeschaltet werden soll (spart 0,5–0,8 GB RAM – beim 2-GB-Modell empfohlen)
7. systemd-Dienst **`sentinel`** mit Autostart, ggf. ufw-Regel

| Befehl | Wirkung |
|---|---|
| `sudo bash install-jetson.sh` | installieren / aktualisieren |
| `… --ohne-desktop` / `--mit-desktop` | grafische Oberfläche aus / wieder an (ab Neustart) |
| `… --maxtakt` | bei jedem Start `jetson_clocks` (höchste Taktraten, Lüfter empfohlen) |
| `… --port 8096` / `--ohne-swap` | Port setzen / keine Auslagerungsdatei |
| `sudo bash install-jetson.sh uninstall` | Dienst entfernen (Daten bleiben) |
| `journalctl -u sentinel -f` | Protokoll |
| `sudo systemctl restart sentinel` | Neustart |

`sh install.sh` erkennt einen Jetson ebenfalls und startet automatisch `install-jetson.sh`.

Die Kopfzeile zeigt auf dem Jetson zusätzlich GPU-Auslastung, Temperatur und RAM. Unter
**Einstellungen → System** stehen Gerät, TensorRT-Version, Video-Dekoder und der Zustand der
GPU-Engines. Fehlen die Engines (z. B. nach einem JetPack-Update), baut Sentinel sie im
Hintergrund neu und rechnet bis dahin auf der CPU. Liefert der Hardware-Dekoder für einen Stream
keine Bilder, wechselt dieser Stream automatisch zu ffmpeg; je Stream ist der Dekoder auch fest
wählbar (Einstellungen → Streams).

Richtwerte Jetson Nano: NanoDet mit TensorRT FP16 ca. 20–40 ms je Bild (statt etwa 1 s auf der
CPU), dazu bis zu 8 × 1080p30-H.264 im Hardware-Dekoder. Damit sind auch Hauptstreams und 4–6
Kameras mit je 2–5 Bildern/s möglich. *Diese Werte sind Erfahrungswerte, auf dem Gerät noch nicht
gemessen – der echte Wert steht in der Kopfzeile unter „KI“.*

## Installation auf dem Brume 2

### Variante A – ZIP kopieren (wie bei den anderen Programmen)

```sh
# am PC: ZIP auf den Brume kopieren (auf dem Brume funktioniert scp nur mit -O)
scp -O Sentinel-MK1.zip root@192.168.8.1:/root/

# auf dem Brume (ssh root@192.168.8.1)
cd /root && unzip -o Sentinel-MK1.zip && sh Sentinel-MK1/install.sh
```

Die ZIP entpackt direkt nach `/root/Sentinel-MK1`; für ein Update einfach dieselben Befehle mit der
neuen ZIP ausführen (`config.json` und `data/` sind nicht in der ZIP und bleiben unverändert).

### Variante B – direkt von GitHub

```sh
wget -O /tmp/install.sh https://raw.githubusercontent.com/Scorch1262/Sentinel-MK1/main/install.sh
sh /tmp/install.sh
```

`install.sh` erledigt alles in einem Durchgang:

1. Pakete per `opkg`: `python3`, `python3-pip`, `python3-sqlite3`, `ffmpeg`, `curl`, `ca-bundle`
2. Programm nach **`/root/Sentinel-MK1`** (vorhandene `config.json` und `data/` bleiben erhalten)
3. `numpy` als fertiges Paket in `/root/Sentinel-MK1/.pylibs` (keine Kompilierung)
4. KI-Modelle (ca. 43 MB) aus dem OpenCV Model Zoo nach `data/models` (mit Prüfsumme)
5. Autostart als procd-Dienst **`/etc/init.d/sentinel`** (Neustart bei Absturz)
6. Firewall-Regel für den WAN-Port (abschaltbar mit `--ohne-firewall`)

Danach im Browser `http://<IP-des-Brume>:8096/` öffnen und unter **Einstellungen → Streams**
die Kameras eintragen, z. B. `rtsp://benutzer:passwort@192.168.200.50:554/stream2`.

| Befehl | Wirkung |
|---|---|
| `sh install.sh` | installieren / aktualisieren |
| `sh install.sh --port 8096` | anderen Port setzen |
| `sh install.sh uninstall` | Dienst entfernen (Daten bleiben in `/root/Sentinel-MK1`) |
| `/etc/init.d/sentinel restart` | neu starten |
| `logread -e sentinel` | Protokoll ansehen |

## Updates über GitHub

Unter **Einstellungen → System → Updates (GitHub)** zeigt Sentinel die installierte und die neueste
Version im Repo [Scorch1262/Sentinel-MK1](https://github.com/Scorch1262/Sentinel-MK1) samt
Änderungen aus `CHANGELOG.md`. **„Update installieren“** lädt die neue Version, sichert die laufende
nach `data/backup/v<version>/`, kopiert die Programmdateien und startet neu – `config.json`,
Datenbank, Bilder, Modelle und GPU-Engines bleiben unverändert. Ist eine neue Version da, erscheint
oben in der Kopfzeile ein Hinweis (automatische Prüfung alle 6 Stunden, abschaltbar).

- **Sicher:** Vor der Installation werden alle Python-Dateien der neuen Version auf Syntaxfehler
  geprüft. Startet die neue Version zweimal hintereinander nicht, spielt Sentinel beim dritten Start
  automatisch die Sicherung zurück (Hinweis unter Updates).
- **Download:** zuerst das Archiv des Zweigs (`codeload.github.com`), sonst Einzeldateien über
  `raw.githubusercontent.com` mit der Dateiliste der GitHub-API bzw. der Datei `MANIFEST` im Repo.
- **Neue Version veröffentlichen:** im Repo `VERSION` **und** `VERSION = "…"` in `app.py` erhöhen,
  einen Abschnitt `## x.y.z` in `CHANGELOG.md` ergänzen und bei neuen Dateien `MANIFEST` erweitern.
  Optional führt eine `update_hook.py` im Repo nach dem Kopieren zusätzliche Schritte aus
  (Aufruf: `python3 update_hook.py <alte> <neue Version>`).
- Repo und Zweig sind einstellbar (z. B. ein Test-Zweig). Für ein privates Repo einen GitHub-Token in
  `data/github_token` ablegen. Ältere Versionen werden nicht installiert (kein Downgrade).
- Bei Installation über den Netzwerk-Core funktioniert zusätzlich dessen Update-Button wie gewohnt.

## Netzwerk-Core MK1

Sentinel MK1 ist mit dem Netzwerk-Core kompatibel. Empfohlen ist der Modus **„eigener Installer“**
(wie beim Klingelbildschirm), weil `install.sh` auch ffmpeg und die Modelle einrichtet.
Zeile für die App-Liste (`apps.conf`):

```
sentinel|Sentinel MK1|Sentinel-MK1|8096|@install.sh|||Objekterkennung in RTSP-Streams (Personen, Tiere, Fahrzeuge)|
```

- Der Dienst heißt `/etc/init.d/sentinel` – der Netzwerk-Core findet ihn über die ID `sentinel`
  und startet ihn nach Updates neu. Die Version steht in `VERSION` und in `app.py` (`VERSION = "…"`).
- Einstellungen liegen außerhalb des Core-Ordners; für Sicherung/Upload:
  `EXTERN["sentinel"] = ("/root/Sentinel-MK1", ["config.json"])`.
  Datenbank, Bilder und Modelle liegen in `/root/Sentinel-MK1/data/`.
- Auch der normale Modus funktioniert (`…|8096|app.py|||…|--port 8096`): `requirements.txt`
  enthält nur `numpy`, Daten liegen dann unter `apps/sentinel/` (`config.json`, `data/`
  werden vom Core nicht überschrieben), fehlendes ffmpeg installiert das Programm selbst per opkg
  und die Modelle lädt es beim ersten Start.
- MQTT: Auf dem Netzwerk-Core läuft Mosquitto auf demselben Gerät → Broker `127.0.0.1:1883`.

## Leistung auf dem Brume 2

Der Brume 2 hat zwei ARM-Kerne (Cortex-A53) und kein OpenCV/onnxruntime. Sentinel bringt deshalb
eine eigene, schlanke KI-Laufzeit mit, die nur numpy braucht (`sentinel/onnxlite.py`, Ergebnisse
identisch zu onnxruntime). Richtwerte:

- Objekterkennung 320 px: auf einem PC ca. 50 ms je Bild; auf dem Brume 2 sind etwa **1–2 s je
  Bild** zu erwarten (noch nicht auf dem Gerät gemessen – der aktuelle Wert steht oben in der
  Kopfzeile unter „KI“). Mehrere Streams werden abwechselnd analysiert.
- Empfehlungen: **Substream** der Kamera (640×360 o. ä.), **1–2 Bilder/s**, Bewegungsfilter an.
  Bei vielen Kameras „nur Keyframes“ aktivieren – das spart beim Dekodieren am meisten.
- Modellgröße 416 px ist genauer, braucht aber ca. 1,7× so lange.

## Wie die Wiedererkennung funktioniert

1. Ein Objekt zählt als **Sichtung**, sobald es mehrmals hintereinander erkannt wurde
   (Standard 2×). Eine durchgehende Anwesenheit zählt einmal; erst nach 60 s Abwesenheit im selben
   Stream zählt eine neue Sichtung.
2. Für jede Sichtung werden Merkmale gebildet: bei Personen Gesicht (falls ≥ 28 px sichtbar) und
   Aussehen (Farbverteilung in drei Bereichen), bei allen anderen das Aussehen.
3. Vergleich mit allen bekannten Objekten derselben Art – **egal aus welchem Stream**. Passt keins,
   wird ein neues Objekt angelegt („Person 17“, „Auto 3“ …).
4. Korrekturen und Zusammenführungen werden als feste Beispiele bzw. Gegenbeispiele gespeichert.

Schwellen (Gesicht 0,40 · Aussehen 0,90 · Zeitfenster) sind unter **Einstellungen → Wiedererkennung**
anpassbar. Fahrzeuge und Tiere werden nur über ihr Aussehen (Farbe) unterschieden – gleichfarbige
Autos lassen sich dadurch nicht sicher auseinanderhalten; Kennzeichenerkennung gibt es nicht.

## MQTT

| Topic | Inhalt |
|---|---|
| `sentinel/alarm` | `{"alarm": "...", "object": "Max", "object_id": 7, "cls": "person", "count": 3, "streams": ["Einfahrt","Hof"], "text": "...", "time": "2026-11-13 17:20:00"}` |
| `sentinel/detection` | jede Sichtung (optional, Einstellungen → MQTT) |
| `sentinel/test` | Testnachricht |

Präfix `sentinel` ist änderbar.

## Dateien

```
app.py                 Start (python3 app.py [--port 8096] [--host 0.0.0.0] [--data ORDNER])
install.sh             Installer für den Brume 2 (auch für den Netzwerk-Core; startet auf dem Jetson install-jetson.sh)
install-jetson.sh      Installer für NVIDIA Jetson Nano (apt, TensorRT-Engines, systemd)
requirements.txt       numpy
VERSION
sentinel/
  engine.py            Streams, Analyse, Verfolgung, Wiedererkennung, Alarme, Aufräumen
  detector.py          NanoDet (Objekte), YuNet (Gesichter), SFace (Gesichtsmerkmale)
  trt.py               TensorRT-Ausführung auf der Jetson-GPU (CUDA per ctypes, ohne pycuda)
  sysinfo.py           Geräteerkennung, GPU-Last, Temperatur, RAM
  onnxlite.py          KI-Laufzeit nur mit numpy (ONNX-Leser + Operatoren)
  streams.py           Empfang per ffmpeg oder GStreamer mit Hardware-Dekoder (Jetson)
  store.py             SQLite-Datenbank
  web.py               Webserver + JSON-API
  updater.py           Updates aus dem GitHub-Repo (Sicherung, Installation, Neustart)
  imaging.py, models.py, mqtt.py, config.py
static/                Weboberfläche (HTML/CSS/JS)
docs/                  Bilder für diese Anleitung
CHANGELOG.md           Änderungen je Version (wird beim Update angezeigt)
MANIFEST               Dateiliste für Updates ohne Archiv-Download
```

Laufzeitdaten (nicht im Repo): `config.json`, `data/sentinel.db`, `data/snaps/JJJJ-MM-TT/`,
`data/avatars/`, `data/models/`, `data/backup/` (Sicherungen vor Updates).

Am PC testen: `pip install numpy` und ffmpeg installieren, dann `python3 app.py`
(ist OpenCV installiert, wird es für die Objekterkennung automatisch genutzt).

## Hinweis Datenschutz

Gesichtsmerkmale sind biometrische Daten. Kameras nur auf eigenem Grundstück betreiben, keine
öffentlichen Flächen oder Nachbargrundstücke erfassen, Betroffene informieren und die
Aufbewahrungsdauer (Einstellungen → Speicher) knapp halten.

## Lizenz

MIT (siehe `LICENSE`). KI-Modelle aus dem [OpenCV Model Zoo](https://github.com/opencv/opencv_zoo):
NanoDet-Plus (Apache-2.0), YuNet (MIT), SFace (Apache-2.0) – werden bei der Installation geladen,
nicht mitgeliefert.
