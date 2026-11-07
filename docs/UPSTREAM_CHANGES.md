# Änderungen an fremden Quellen

Die folgenden Hinweise gehören zu den im Projekt ausgelieferten Transformationsrezepten. Diese enthalten Einfügungen, Zeilenpositionen und Prüfsummen; die vollständigen Ausgangsquellen werden beim Einrichten von den angegebenen Originalprojekten geladen. Die Rezepte sind lokale Anpassungen der Fanedition und kein unveränderter Upstream-Stand.

Die Copyright- und Lizenzhinweise der Ausgangsprojekte bleiben erhalten. Für übernommene oder abgeleitete Teile gelten deren MIT- beziehungsweise Apache-2.0-Bedingungen. Die PolyForm-Projektlizenz ersetzt diese Bedingungen nicht. Die vollständigen Hinweise stehen in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md); die mitgelieferten Lizenztexte liegen unter [licenses/third-party](../licenses/third-party/).

## llama.cpp-omni und VoxCPM2

Rezept: [vox-combined-speed.json](../generator_launcher/recipes/vox-combined-speed.json).

Ausgangsprojekt: [llama.cpp-omni von tc-mb und den ggml-Autoren, Revision 64d092c60db4b4ee45768476bd752f03fdcc98ea](https://github.com/tc-mb/llama.cpp-omni/tree/64d092c60db4b4ee45768476bd752f03fdcc98ea), MIT, Copyright 2023–2026 The ggml authors. Teile des kombinierten Erzeugungsablaufs beruhen auf der [VoxCPM2-Implementierung von OpenBMB](https://github.com/OpenBMB/VoxCPM), Copyright OpenBMB, Apache-2.0. Für die OpenBMB-Vorlage ist hier kein exakter Ausgangscommit belegt; die llama.cpp-omni-Eingaben sind über Commit und Dateiprüfsummen gebunden.

| Betroffene Datei im heruntergeladenen Projekt | Lokale Anpassung |
| --- | --- |
| `tools/omni/voxcpm2/voxcpm2_cli.cpp` | Kombinierte Referenz- und Prompt-Eingaben, mehrere Anfragen im geladenen Prozess, Seed- und Cache-Optionen sowie Zeitmessungen. |
| `tools/omni/voxcpm2/voxcpm2_runtime.cpp` | Kombinierter Erzeugungsablauf nach Teilen des OpenBMB-Designs, Rücksetzen je Anfrage, Feature-Cache und Zeitmessungen. |
| `tools/omni/voxcpm2/voxcpm2_runtime.h` | Ergänzte Schnittstellen für kombinierten Ablauf, Anfragezustand und Cache. |
| `tools/omni/voxcpm2/mi1_feature_cache.h` | Neue Hilfsdatei für den lokalen Feature-Cache. |

## ScummTR

Rezept: [scummtr-actor.json](../generator_launcher/recipes/scummtr-actor.json).

Ausgangsprojekt: [ScummTR, Revision a50c6f4bf03566721b3f2c00df257a41a82e22ed](https://github.com/dwatteau/scummtr/tree/a50c6f4bf03566721b3f2c00df257a41a82e22ed), MIT. Copyright 2003–2006 Thomas Combeleran und 2020–2026 Donovan Watteau.

| Betroffene Datei im heruntergeladenen Projekt | Lokale Anpassung |
| --- | --- |
| `src/ScummTr/script.cpp` | Sprecherzuordnung bei der Verarbeitung von Sprachzeilen mitführen. |
| `src/ScummTr/script.hpp` | Datentypen und Schnittstellen für diese Sprecherzuordnung ergänzen. |
| `src/ScummTr/text.cpp` | Sprecherkennungen zusammen mit den Sprachzeilen exportieren. |
| `src/ScummTr/text.hpp` | Zugehörige Export-Schnittstelle ergänzen. |

## Native Audioaufbereitung mit SoX

Rezept: [native-audio.json](../generator_launcher/recipes/native-audio.json).

Die unveränderten SoX-14.3.1-Quellen werden separat vom [SoX-Projekt](https://sourceforge.net/projects/sox/files/sox/14.3.1/) geladen. Das Rezept ergänzt in `config.sub` die Erkennung von `aarch64-*` für den lokalen Mac-Bau. Das Programm steht unter GPL-2.0-or-later, die Bibliothek unter LGPL-2.1-or-later; die Originaldateien `COPYING`, `LICENSE.GPL` und `LICENSE.LGPL` bleiben im heruntergeladenen Quellbaum erhalten. Das App-Paket enthält weder diese Quellen noch ein SoX-Programm.

## Umfang dieser Ergänzung

Die am 07.11.2026 hinzugefügten Herkunfts- und Änderungshinweise stehen auch in den Rezeptdateien. Die Einfügungen für den Programmablauf, ihre Reihenfolge sowie sämtliche Eingabe- und Ausgabeprüfsummen bleiben unverändert. Die Rezepte ändern beim Einrichten keine vorhandenen Upstream-Lizenzdateien. Wer selbst die daraus gebauten Fremdprogramme weitergibt, muss die Bedingungen des jeweiligen Projekts auch bei dieser Weitergabe erfüllen.
