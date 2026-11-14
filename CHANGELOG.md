# Änderungen

## 2.3.1
- GPU-Engine-Bau auf dem Jetson Nano 2GB: Streams und KI-Modelle werden für den einmaligen Bau
  angehalten, trtexec baut speichersparend (ohne cuBLAS/cuDNN-Taktiken) und versucht es bei
  Speichermangel ein zweites Mal mit weniger Arbeitsspeicher – vorher brach der Bau mit
  „Code -9“ ab (vom System wegen Speichermangel beendet)
- Gescheiterter Engine-Bau wird gemerkt und nicht bei jedem Neustart wiederholt; neuer Knopf
  „GPU-Engine erneut bauen“ unter System
- Behoben: Dauerte die NVDEC-Prüfung (gst-inspect) beim Start zu lange, liefen alle Streams bis
  zum nächsten Neustart über ffmpeg auf der CPU statt über den Hardware-Dekoder
- Protokoll: Grund, wenn kein Hardware-Dekoder genutzt wird; Startfehler von Streams (z. B. zu
  wenig Arbeitsspeicher) werden angezeigt; „Codec/Größe nicht ermittelbar“ nur noch einmal
- Beim Start: Speicher und Auslagerung im Protokoll, Warnung bei weniger als 2 GB Auslagerung

## 2.3.0
- Kachel-Analyse (Standard mit GPU): große Kamerabilder werden zusätzlich in 2–4 überlappenden
  Ausschnitten geprüft – deutlich mehr kleine, weit entfernte Personen werden gefunden;
  abgeschnittene Teile am Kachelrand werden zusammengesetzt
- Neues Modell YOLOX-M (46,9 statt 40,5 COCO-mAP) als Option für die Jetson-GPU; solange die
  Engine gebaut wird, läuft übergangsweise YOLOX-S
- Bewegungsfilter misst die bewegte Bildfläche statt des mittleren Bildunterschieds – kleine
  Personen in ruhigen Szenen lösen die Analyse jetzt sofort aus (früher bis zu 30 s später);
  feineres Bewegungsraster
- Neue Streams starten mit 1280 px Analyse-Breite (mit GPU); Hinweise zur Auflösung
- Systemanzeige: Durchläufe je Bild (inkl. Kacheln)

## 2.2.0
- Einstellungen überarbeitet: nichts überlappt mehr oder ragt über den Rand – Felder mit
  Einheit/Erklärung brechen sauber um, zwei Spalten erst ab ausreichender Breite,
  Broker-, Repo- und Lüfterzeilen passen auch aufs Handy
- RTSP-Transport „automatisch“ (neuer Standard): erst TCP, liefert die Kamera darüber keine
  Bilder, wird auf UDP gewechselt (wie VLC); bestehende Streams werden umgestellt
- Neuer Knopf „Testen“ je Stream: prüft vom Jetson aus Netzwerk, RTSP OPTIONS/DESCRIBE
  (mit Klartext-Hinweis bei 401/404/453/461/503), Videoempfang über TCP und UDP sowie
  ffprobe – inklusive Empfehlung zum Übernehmen und SDP der Kamera
- Unter jedem Stream steht der aktive Dekoder und Transport; Fehler mit Tipps (Passwort,
  Pfad, Kamera nur per UDP)
- Behoben: ffmpeg-Empfang konnte hängen bleiben, wenn die Bildgröße vorab unbekannt war
  (neuere ffmpeg-Versionen) – Größe wird jetzt vorab ermittelt, Rohdaten werden gepuffert

## 2.1.0
- Bewegungspflicht: neue Personen, Tiere und Fahrräder werden nur gemeldet, wenn sich an der
  Stelle etwas bewegt – stehende Schatten, Plakate und Muster fallen heraus (je Gruppe einstellbar)
- Person/Objekt von Hand anlegen („+ Person anlegen“) und danach Erkennungen zuweisen
- Galerie: Auswahlmodus – mehrere Bilder anklicken und auf einmal zuweisen, als Fehlerkennung
  lernen oder löschen; Filter „nur noch nicht zugewiesene“
- Bild-Dialog: „Wer ist das? Zuweisen“ direkt sichtbar, inkl. „Neue Person anlegen“
- Objekt-Dialog: „+ Erkennungen zuweisen“ mit Bildauswahl (nur passende Arten)
- Zuweisen in eine falsche Art (z. B. Auto in eine Person) wird verhindert
- „Alles löschen“ (Galerie und Einstellungen → Speicher): alle Erkennungen und Bilder, wahlweise
  auch Personen/Objekte, gelernte Fehlerkennungen und Alarmverlauf
- Behoben: Mehrere Objekte im selben Bild überschrieben gegenseitig ihre Bilddateien

## 2.0.0
- Sentinel MK1 ist jetzt ausschließlich für den NVIDIA Jetson Nano: Unterstützung für den
  GL.iNet Brume 2 (install.sh, opkg/procd, Netzwerk-Core) entfernt
- install.sh entfällt (wird beim Update automatisch gelöscht); Installation nur noch mit
  install-jetson.sh
- Standardwerte für den Jetson: Analyse-Abstand 0,3 s, Speicher bis 8 GB, ohne GPU YOLOX-Tiny
- Texte und Hinweise in Oberfläche und README auf den Jetson zugeschnitten

## 1.4.0
- Deutlich bessere Objekterkennung: neues Modell YOLOX-S (640 px, Standard auf der
  Jetson-GPU) und YOLOX-Tiny; NanoDet bleibt für den Brume 2
- Zweitprüfung: neue Objekte werden im vergrößerten Ausschnitt nochmals geprüft –
  Schatten, Spiegelungen und Muster fallen dabei heraus
- Mindest-Sicherheit je Gruppe (Personen standardmäßig 55 %), Formprüfung für
  Personen (breite, flache Treffer wie Schatten werden verworfen)
- „Fehlerkennung“ lernen: falsche Treffer in der Galerie oder ganze Objekte markieren –
  an derselben Stelle wird so etwas künftig nicht mehr gemeldet (Liste unter Einstellungen)
- Sichtung erst nach 3 Treffern in Folge; Live-Bild zeigt nur bestätigte Objekte
- Bestehende Einstellungen werden automatisch auf die strengeren Werte angehoben
- Kästchen in den Einstellungen einheitlich groß

## 1.3.0
- Lüftersteuerung für den Jetson Nano (4-Pin-PWM-Anschluss): Sentinel regelt den
  Lüfter nach der höchsten CPU/GPU-Temperatur (einstellbare Kurve mit Vorschau)
- Betriebsarten: Sentinel-Kurve, Jetson-Standard, feste Drehzahl, volle Drehzahl
- Notbetrieb ab 75 °C, Anlaufhilfe für träge 5-V-Lüfter, sanftes Herunterregeln
- Lüfter-Drehzahl in der Kopfzeile; beim Beenden übernimmt wieder der Jetson

## 1.2.2
- Kamerabilder auf dem Jetson: MJPEG-Kameras laufen automatisch über ffmpeg
  (Hardware-Dekoder nur für H.264/H.265), statt mit „not-linked“ abzubrechen
- GStreamer verbindet nur noch den Video-Teil des RTSP-Streams (Kameras mit Ton)
- Startwächter: kommt nach 25 s kein Bild, wird neu verbunden; nach zwei
  Fehlversuchen wechselt der Stream automatisch auf ffmpeg
- ffmpeg-Weg mit älteren ffmpeg-Versionen (Jetson 3.4): JPEG aus H.264/H.265
  funktioniert jetzt („Non full-range YUV“)
- Aussagekräftigere Fehlermeldungen im Protokoll

## 1.2.1
- Jetson-Installer: Paketlisten werden zuerst geladen (frischer Jetson: „kein
  Installationskandidat für curl“), Downloads klappen auch ohne curl (wget bzw. Python)
- Brume-Installer: gleiche Download-Rückfälle

## 1.2.0
- Updates direkt aus dem GitHub-Repo: Einstellungen → System → „Update installieren“
- Automatische Suche nach neuen Versionen (alle 6 Stunden), Hinweis in der Kopfzeile
- Sicherung der laufenden Version vor jedem Update (data/backup/), automatische
  Wiederherstellung, falls die neue Version nicht startet
- Repo und Zweig für Updates einstellbar

## 1.1.0
- Unterstützung für NVIDIA Jetson Nano (2GB/4GB) mit install-jetson.sh
- Objekterkennung und Gesichtsmerkmale auf der GPU (TensorRT FP16)
- Hardware-Videodekoder (GStreamer nvv4l2decoder/nvvidconv), H.264 und H.265
- Kopfzeile mit GPU-Last, Temperatur und RAM

## 1.0.0
- Erste Version für GL.iNet Brume 2: Erkennung, Galerie, Objekte mit Namen und
  Kategorien, Personen-Wiedererkennung, Korrekturen, Alarme, MQTT
