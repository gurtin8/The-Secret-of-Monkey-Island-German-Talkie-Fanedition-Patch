# App bauen und Quellcode exportieren

Hier erfährst du, wie du die macOS-App aus dem Quellcode baust und eine Kopie der veröffentlichbaren Quellen erzeugst.

## Voraussetzungen

Für den App-Bau brauchst du einen Mac mit Apple Silicon, macOS 14 oder neuer, Python 3.10 oder neuer sowie Apples Command Line Tools mit Swift. Die Befehle führst du im Hauptordner des Quellcodes aus. Dort liegen die README und der Ordner `generator_launcher`.

## Patch-Paket bauen und Wiki öffnen

Erzeuge zunächst die Hilfe, damit die App deine aktuelle Dokumentation enthält.

```sh
python3 tools/wiki/build.py
```

Baue anschließend die App.

```sh
python3 -m generator_launcher.package_native --output build
```

Das Ergebnis liegt unter `build/The Secret of Monkey Island German Talkie Fanedition Patch.app`. Du kannst es im Finder öffnen oder diesen Befehl verwenden.

```sh
open "build/The Secret of Monkey Island German Talkie Fanedition Patch.app"
```

Der Paketbauer kompiliert den nativen Starter und signiert das vollständige App-Paket lokal ad hoc. Für die Weitergabe als von Apple bestätigter Entwickler gehören eine Developer-ID-Signatur und die Notarisierung zu einem eigenen Veröffentlichungsschritt.

Die App enthält das Textrekonstruktionsrezept und die Offline-Hilfe. Bei der Einrichtung wählst du deine Spielquelle und den Arbeitsordner. Dort werden die separat bezogenen Werkzeuge und Modelle eingerichtet.

## Das Paket prüfen

Prüfe nach dem Bau die Signatur.

```sh
codesign --verify --strict "build/The Secret of Monkey Island German Talkie Fanedition Patch.app"
```

So prüfst du außerdem, ob die Wiki-Dateien zu den Quellen passen und die mitgelieferten Rekonstruktionstests bestehen.

```sh
python3 tools/wiki/build.py --check
python3 -m unittest discover -s tests -p 'test_*reconstruction*.py'
```

Die Offline-Hilfe öffnest du über `docs/wiki/site/index.html`.

## Quellcode für eine Weitergabe exportieren

Wähle einen neuen oder leeren Zielordner außerhalb des Quellcodeordners.

```sh
python3 tools/package_publication.py --output ../Quellcode-export
```

Der Exporter kopiert die ausdrücklich gelisteten Quellen, Dokumente, Screenshots und Lizenzhinweise. In `PUBLICATION_EXPORT.json` stehen die Dateiliste und die SHA-256-Prüfsummen. Das Standardmanifest muss ein gültiges Textrekonstruktionsrezept sein. Vollständige Dialogtexte werden erst lokal aus der Spielquelle rekonstruiert.

## Lizenz und Bestandteile

Rechteverfügbare Projektbestandteile stehen unter [PolyForm Noncommercial 1.0.0](../LICENSE). Die verwendeten Fremdprojekte behalten ihre eigenen Bedingungen. Ihre Zuordnung findest du in den [Drittanbieterhinweisen](../THIRD_PARTY_NOTICES.md), im [Komponentenverzeichnis](THIRD_PARTY_COMPONENTS.md) und in den [Änderungshinweisen](UPSTREAM_CHANGES.md). Die vollständigen mitgelieferten MIT- und Apache-Lizenztexte sind auch in der [Offlinehilfe](wiki/pages/publication.licenses.md) zugänglich. Beim eigenen Weitergeben separat gebauter Drittprogramme gelten zusätzlich deren Originalbedingungen.
