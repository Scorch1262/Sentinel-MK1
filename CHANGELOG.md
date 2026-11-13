# Änderungen

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
