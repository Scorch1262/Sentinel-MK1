# Änderungen

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
