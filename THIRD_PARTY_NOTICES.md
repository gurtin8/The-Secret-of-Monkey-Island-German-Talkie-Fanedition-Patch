# Drittanbieterhinweise und Credits

Die Fanedition baut auf den unten genannten Projekten auf. Danke an ihre Autorinnen, Autoren und Mitwirkenden. Diese Hinweise ergänzen die Original-Lizenztexte; sie ersetzen sie nicht. Ein Projektlink ist eine Quellenangabe und ersetzt keine erforderliche Kopie des Lizenztexts.

## Mitgelieferte Quelltextanteile und Lizenztexte

Die [Projektlizenz](LICENSE) (PolyForm Noncommercial 1.0.0) gilt für die eigenen rechteverfügbaren Bestandteile. **Drittcode und daraus abgeleitete Quelltextanteile in den Transformationsrezepten sind davon ausgenommen.** Ihre MIT- beziehungsweise Apache-2.0-Bedingungen bleiben erhalten:

- **ScummTR und ScummRP**, Thomas Combeleran und Donovan Watteau: [Upstream](https://github.com/dwatteau/scummtr/tree/a50c6f4bf03566721b3f2c00df257a41a82e22ed), [mitgelieferter MIT-Text](licenses/third-party/ScummTR-MIT.txt). Das Rezept [scummtr-actor.json](generator_launcher/recipes/scummtr-actor.json) ergänzt den Actor-ID-Export in `src/ScummTr/script.cpp`, `script.hpp`, `text.cpp` und `text.hpp`.
- **llama.cpp-omni**, tc-mb und die ggml-Autoren: [Upstream](https://github.com/tc-mb/llama.cpp-omni/tree/64d092c60db4b4ee45768476bd752f03fdcc98ea), [mitgelieferter MIT-Text](licenses/third-party/llama-cpp-omni-MIT.txt). Das Rezept [vox-combined-speed.json](generator_launcher/recipes/vox-combined-speed.json) ändert `tools/omni/voxcpm2/voxcpm2_cli.cpp`, `voxcpm2_runtime.cpp` und `voxcpm2_runtime.h` und ergänzt dort `mi1_feature_cache.h`.
- **VoxCPM / VoxCPM2**, OpenBMB und Mitwirkende: [Originalprojekt](https://github.com/OpenBMB/VoxCPM), [mitgelieferter Apache-2.0-Text](licenses/third-party/VoxCPM-Apache-2.0.txt). Teile der lokalen VoxCPM2-Integration und des genannten Vox-Rezepts basieren auf dem Python-Generierungsdesign dieses Projekts. Auch diese abgeleiteten Anteile werden nicht als ausschließlich PolyForm-lizenziert ausgewiesen.

Die Rezepte enthalten Änderungen und Prüfsummen, keine vollständigen Upstream-Quellbäume. Die heruntergeladenen Originalquellen behalten ihre Copyright-, Lizenz- und gegebenenfalls NOTICE-Dateien. Die genauen Zielpfade, Stände und Änderungen stehen im [Komponentenverzeichnis](docs/THIRD_PARTY_COMPONENTS.md).

## Separat bezogene Programme und Modelle

Die Einrichtung lädt Drittprogramme, Bibliotheken und Modellgewichte separat in den lokalen Arbeitsordner. Sie sind nicht im veröffentlichten Patch-Paket enthalten und werden durch die Projektlizenz nicht gemeinsam neu lizenziert.

- **VoxCPMANE / VoxCPMANE2**, [0seba](https://github.com/0seba/VoxCPMANE) / [seba](https://huggingface.co/seba): ANE-Laufzeit und die konvertierten Modellquellen [VoxCPM2ANE-Preview](https://huggingface.co/seba/VoxCPM2ANE-Preview) und [VoxCPMANE2-Debug-Models](https://huggingface.co/seba/VoxCPMANE2-Debug-Models).
- **VoxCPM2-GGUF**, [DennisHuang648](https://huggingface.co/DennisHuang648/VoxCPM2-GGUF), auf Grundlage von [OpenBMB/VoxCPM2](https://huggingface.co/openbmb/VoxCPM2).
- **FFmpeg**, das [FFmpeg-Team](https://ffmpeg.org/); **SoX**, das [SoX-Projekt](https://sox.sourceforge.net/); **whisper.cpp**, [ggml-org und Mitwirkende](https://github.com/ggml-org/whisper.cpp); **Whisper**, [OpenAI und Mitwirkende](https://github.com/openai/whisper).
- **Wine**, [WineHQ und Mitwirkende](https://www.winehq.org/), in der macOS-Distribution von [Gcenx](https://github.com/Gcenx/macOS_Wine_builds). Zum Originalpaket gehören auch **Wine Mono**, **Wine Gecko** und weitere Bibliotheken, darunter **MoltenVK**; deren eigene Hinweise gelten jeweils für die enthaltenen Bestandteile.
- **CPython**, die [Python Software Foundation und Mitwirkende](https://www.python.org/), bereitgestellt durch [python-build-standalone](https://github.com/astral-sh/python-build-standalone); **CMake**, [Kitware und Mitwirkende](https://cmake.org/); **Search**, [driceroland](https://github.com/driceroland/Search).
- **innoextract**, [Daniel Scharrer und Mitwirkende](https://constexpr.org/innoextract/), sowie **Boost**, **Zstandard** und **XZ Utils** für die GOG-Extraktion; **Core ML Tools**, [Apple und Mitwirkende](https://github.com/apple/coremltools), und die Python-Abhängigkeiten der ANE-Laufzeit.

Versionen, Bezugsquellen, Lizenzzuordnung, lokale Änderungen und Lieferumfang stehen im [Komponentenverzeichnis](docs/THIRD_PARTY_COMPONENTS.md). Die Lizenztexte und Hinweise der Originaldownloads sind bei den jeweiligen Komponenten aufzubewahren. Weitere Abhängigkeiten innerhalb dieser Downloads behalten ihre eigenen Metadaten und Lizenzdateien; die Übersicht behauptet keine vollständige transitive Inventarisierung.

## Spielbasis und Community

- **Ultimate Talkie Edition (UTE)**: [Marek Roth / LogicDeLuxe und Mitwirkende](https://www.gratissaugen.de/ultimatetalkies/monkey1.html), Builder-Paket 1.02. Für seine Bestandteile gelten die Bedingungen des vollständigen Originalpakets.
- **Deutscher UTE-Patch v1.01**: [la-patches / gandimann und Mitwirkende](https://www.la-patches.de/p_mi1_ute_us-de.php).
- **SCUMMKit**: [Projekt von jmnunezizu und Mitwirkenden](https://github.com/jmnunezizu/scummkit). Die UTE-Tabelle `monster.tbl` ist LogicDeLuxe zugeschrieben und wurde dort mit Erlaubnis bereitgestellt: [Herkunftshinweis](https://github.com/jmnunezizu/scummkit/blob/a6ec88b02c97e66ce1c57069814c971248e62566/third_party/ultimate-talkie/README.md), [originale UTE-Bedingungen](https://github.com/jmnunezizu/scummkit/blob/a6ec88b02c97e66ce1c57069814c971248e62566/licenses/original_ute_builder.txt).
- **ScummVM-Team und Mitwirkende**: [ScummVM](https://www.scummvm.org/), separat installiertes Programm zum Spielen der fertigen Fassung.

Spiel, Texte, Übersetzungen, Aufnahmen, Marken und fremde Patchdaten gehören nicht zur Projektlizenz. Sie werden ebenso wenig wie Modellgewichte oder Drittprogramme durch diese Hinweise mitgeliefert oder freigegeben. Die Hinweise geben keine pauschale Zusicherung zur Weitergabe beliebiger daraus erzeugter Pakete. Für Originalstimmanker gelten außerdem die Hinweise in [FANEDITION.md](FANEDITION.md). Der Veröffentlichungsumfang steht in [PUBLICATION.md](docs/PUBLICATION.md).

Das App-Symbol ist die Pixelinsel des Projekts; die bearbeitbare Quelle ist der [SVG-Master](generator_launcher/native/assets/LauncherIcon.svg).

Die lokalen Änderungen sind zusätzlich in [UPSTREAM_CHANGES.md](docs/UPSTREAM_CHANGES.md) dokumentiert.
