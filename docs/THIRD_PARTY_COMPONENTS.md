# Komponentenverzeichnis

Dieses Verzeichnis ergänzt die [Drittanbieterhinweise](../THIRD_PARTY_NOTICES.md). Es beschreibt die im Einrichtungsablauf direkt verwendeten Komponenten. Maßgeblich für konkrete Downloads und Prüfsummen sind die verlinkten Rezepte. „Separat“ bedeutet: Download beziehungsweise lokaler Build im Arbeitsordner, kein Drittprogramm und keine Modellgewichte im veröffentlichten Patch-Paket. Mitgeliefert werden die eigenen Integrationsdateien, Transformationsrezepte und die ausdrücklich genannten Lizenztexte.

Lizenzangaben gelten jeweils für die bezeichnete Komponente, nicht pauschal für alle Bestandteile eines Originalpakets. Originale Copyright-, Lizenz- und NOTICE-Dateien bleiben bei den Downloads erhalten. Projektlinks ergänzen diese Texte; sie ersetzen sie nicht.

## Laufzeit und Werkzeuge

Quelle: [runtime.json](../generator_launcher/recipes/runtime.json), [preparation.json](../generator_launcher/recipes/preparation.json), [native-audio.json](../generator_launcher/recipes/native-audio.json).

| Komponente / Quelle | Festgelegter Stand | Lizenz / Originalhinweise | Lokale Änderungen | Lieferung |
| --- | --- | --- | --- | --- |
| CPython / [python-build-standalone](https://github.com/astral-sh/python-build-standalone) | CPython 3.12.14, Build 20260901 | [PSF und historische CPython-Bedingungen](https://github.com/python/cpython/blob/v3.12.14/LICENSE); MPL-2.0 der Buildskripte ist keine globale Interpreterlizenz | Keine Quelltextänderungen; isolierte Umgebung | Separates Originalarchiv |
| [Search / driceroland](https://github.com/driceroland/Search) | 1.0.4 | [MIT](https://github.com/driceroland/Search/blob/cddb9cdd19e64321c95d5e733217799908ad3b52/LICENSE) | Original-App unverändert, Name und Symbol bleiben erhalten | Separater Originaldownload |
| [CMake / Kitware](https://github.com/Kitware/CMake) | 4.1.2 | [BSD-3-Clause und mitgelieferte Hinweise](https://github.com/Kitware/CMake/blob/v4.1.2/LICENSE.rst) | Keine Quelltextänderungen | Separates Originalarchiv |
| [llama.cpp-omni / tc-mb und ggml-Autoren](https://github.com/tc-mb/llama.cpp-omni/tree/64d092c60db4b4ee45768476bd752f03fdcc98ea) | 64d092c60db4b4ee45768476bd752f03fdcc98ea | MIT; [Text im Patch-Paket](../licenses/third-party/llama-cpp-omni-MIT.txt) | VoxCPM2-Integration, kombinierter Ablauf und Feature-Cache gemäß vox-combined-speed.json | Quellen separat; Rezept und Lizenztext mitgeliefert |
| [ScummTR / ScummRP, Thomas Combeleran und Donovan Watteau](https://github.com/dwatteau/scummtr/tree/a50c6f4bf03566721b3f2c00df257a41a82e22ed) | a50c6f4bf03566721b3f2c00df257a41a82e22ed | MIT; [Text im Patch-Paket](../licenses/third-party/ScummTR-MIT.txt) | Actor-ID-Export gemäß scummtr-actor.json | Quellen separat; Rezept und Lizenztext mitgeliefert |
| [FFmpeg-Team](https://github.com/FFmpeg/FFmpeg/tree/b08d7969c550a804a59511c7b83f2dd8cc0499b8) | 7.1; b08d7969c550a804a59511c7b83f2dd8cc0499b8 | LGPL-2.1-or-later für den gewählten Minimalbuild; Originaltexte bleiben erhalten | Build ohne externe Autodetektion und ohne GPL-, nonfree- oder version3-Option | Separater Quellabruf und lokaler Build |
| [whisper.cpp / ggml-org](https://github.com/ggml-org/whisper.cpp/tree/d09f61a708f3487afa956ff578e60eae5e7a233c) | d09f61a708f3487afa956ff578e60eae5e7a233c | MIT; Original-LICENSE | Lokaler Build | Separater Quellabruf |
| [Whisper / OpenAI](https://github.com/openai/whisper), [GGML-Konvertierung / ggerganov](https://huggingface.co/ggerganov/whisper.cpp/tree/5359861c739e955e79d9a303bcbc70fb988958b1) | base / medium; GGML-Stand 5359861c739e955e79d9a303bcbc70fb988958b1 | [MIT für Whisper-Code und Gewichte](https://github.com/openai/whisper/blob/v20250625/LICENSE) | Keine Änderungen an den bezogenen Gewichten | Separate Modelldownloads |
| [SoX-Projekt](https://sox.sourceforge.net/) | 14.3.1; mi1-native-sox-14.3.1-r1 | Programm GPL-2.0-or-later; Bibliothek LGPL-2.1-or-later; COPYING / LICENSE.GPL / LICENSE.LGPL | config.sub um aarch64 ergänzt; Build ohne externe Codecs und Gerätetreiber | Separater Quellabruf und lokaler Build |
| [WineHQ / Gcenx macOS Wine](https://github.com/Gcenx/macOS_Wine_builds/releases/tag/11.18) | Wine devel 11.18 | [Wine LGPL-2.1-or-later](https://github.com/wine-mirror/wine/blob/wine-11.18/LICENSE); Bibliotheken nach ihren eigenen Hinweisen | Original-App-Paket erhalten; eigener lokaler WINEPREFIX | Separates vollständiges Originalarchiv |
| [Wine Mono](https://github.com/wine-mono/wine-mono/tree/wine-mono-11.3.0) | 11.3.0 im Wine-Paket | [Gemischte Komponentenbedingungen](https://github.com/wine-mono/wine-mono/blob/wine-mono-11.3.0/COPYING), u. a. LGPL, MIT, GPL mit Ausnahme, MS-PL und zlib | Keine Projektänderungen | Bestandteil des separat bezogenen Wine-Pakets |
| [Wine Gecko](https://gitlab.winehq.org/wine/wine-gecko) | 2.47.4 im Wine-Paket | MPL-Familie und weitere Originalhinweise pro Bestandteil | Keine Projektänderungen | Bestandteil des separat bezogenen Wine-Pakets |
| [MoltenVK / Khronos](https://github.com/KhronosGroup/MoltenVK) | Im Wine-Paket; Rezept-Lizenzbeleg v1.4.0, konkrete Binär-Buildversion nicht vollständig zugeordnet | [Apache-2.0](https://github.com/KhronosGroup/MoltenVK/blob/v1.4.0/LICENSE) und Originalhinweise | Keine Projektänderungen | Soweit im Original-Wine-Paket enthalten; kein zusätzlicher Download |

Die Rezepte dokumentieren Grenzen der genauen Zuordnung einzelner interner Wine- und historischer UTE-Hilfsbibliotheken. Die Übersicht erfindet dafür keine exakten Versionen oder einheitlichen Lizenzen. Die jeweiligen vollständigen Originalpakete und ihre Hinweise bleiben erhalten.

## VoxCPM2 und ANE-Laufzeit

| Komponente / Quelle | Festgelegter Stand | Lizenz / Originalhinweise | Lokale Änderungen | Lieferung |
| --- | --- | --- | --- | --- |
| [VoxCPM / OpenBMB und Mitwirkende](https://github.com/OpenBMB/VoxCPM) | VoxCPM2; Tokenizerquelle [32279effe8c19989596f05d353d1447f51d9e915](https://huggingface.co/openbmb/VoxCPM2/tree/32279effe8c19989596f05d353d1447f51d9e915) | Apache-2.0; [Text im Patch-Paket](../licenses/third-party/VoxCPM-Apache-2.0.txt); Originalmodellkarte | Teile des Generierungsdesigns in lokale Integration übertragen | Originalquelle / Tokenizer separat; abgeleitete Rezeptanteile und Lizenztext mitgeliefert |
| [VoxCPM2-GGUF / DennisHuang648](https://huggingface.co/DennisHuang648/VoxCPM2-GGUF/tree/f8934b10e3635b1fd35b03654ec0d2be45077e58) | f8934b10e3635b1fd35b03654ec0d2be45077e58; BaseLM-F16 und Acoustic-F16 | Apache-2.0 laut Konverterkarte, einschließlich Verweisen auf die ursprünglichen Modellbedingungen | Keine Änderungen an den bezogenen Gewichten; exakte ursprüngliche Konvertierungseingabe wird nicht behauptet | Separate Modelldownloads |
| [VoxCPMANE2 / 0seba](https://github.com/0seba/VoxCPMANE), [PyPI](https://pypi.org/project/voxcpmane2/0.1.3b1/) | 0.1.3b1 | MIT für Laufzeitcode | Eigene Anbindung und Startkonfiguration; Original-Wheel separat | Separates Original-Wheel und isolierte pip-Umgebung |
| [Core ML Tools / Apple](https://github.com/apple/coremltools/tree/9.0) | 9.0 | BSD-3-Clause; Original-LICENSE | Keine Projektänderungen | Separat über pip |

Die Modellquellen von **seba** sind [VoxCPM2ANE-Preview, def350ecae1aa3e4028970a5eae8faa7b3800d40](https://huggingface.co/seba/VoxCPM2ANE-Preview/tree/def350ecae1aa3e4028970a5eae8faa7b3800d40) und [VoxCPMANE2-Debug-Models, 3a33ff35d350da2ac3d562dafc8e930faeeae59f](https://huggingface.co/seba/VoxCPMANE2-Debug-Models/tree/3a33ff35d350da2ac3d562dafc8e930faeeae59f). Die zugehörigen Herkunftsstände stehen zusätzlich unter [generator_launcher/provenance/models](../generator_launcher/provenance/models).

Die Installation erfolgt durch [setup_ane_runtime.py](../tools/setup_ane_runtime.py) aus dem Original-Wheel. Die [ane-constraints.txt](../generator_launcher/recipes/ane-constraints.txt) hält die beobachteten Paketversionen fest. Die folgende Übersicht nennt die direkten Laufzeitabhängigkeiten aus der [Upstream-Paketdefinition](https://github.com/0seba/VoxCPMANE/blob/voxcpmane2/pyproject.toml); sie ist keine vollständige transitive Stückliste. Für jedes Paket gelten die eigenen, im Download enthaltenen Lizenztexte und Metadaten.

| Direkte Python-Abhängigkeit / Paketmetadaten | Version laut Constraints | Lizenz / Originalhinweise | Lokale Änderungen | Lieferung |
| --- | --- | --- | --- | --- |
| [numpy](https://pypi.org/project/numpy/2.5.3/) | 2.5.3 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |
| [ml_dtypes](https://pypi.org/project/ml_dtypes/0.6.0/) | 0.6.0 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |
| [soundfile](https://pypi.org/project/soundfile/0.14.0/) | 0.14.0 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |
| [soxr](https://pypi.org/project/soxr/1.1.0/) | 1.1.0 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |
| [tokenizers](https://pypi.org/project/tokenizers/0.23.2/) | 0.23.2 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |
| [fastapi](https://pypi.org/project/fastapi/0.142.2/) | 0.142.2 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |
| [uvicorn](https://pypi.org/project/uvicorn/0.54.0/) | 0.54.0 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |
| [aiofiles](https://pypi.org/project/aiofiles/25.1.0/) | 25.1.0 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |
| [huggingface_hub](https://pypi.org/project/huggingface_hub/1.33.0/) | 1.33.0 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |
| [sounddevice](https://pypi.org/project/sounddevice/0.5.6/) | 0.5.6 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |
| [tqdm](https://pypi.org/project/tqdm/4.70.1/) | 4.70.1 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |
| [ftfy](https://pypi.org/project/ftfy/6.3.1/) | 6.3.1 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |
| [inflect](https://pypi.org/project/inflect/7.5.0/) | 7.5.0 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |
| [wetext](https://pypi.org/project/wetext/0.1.8/) | 0.1.8 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |
| [regex](https://pypi.org/project/regex/2026.9.29/) | 2026.9.29 | Eigene Paketlizenz und enthaltene Lizenztexte gemäß verlinkten Originalmetadaten | Keine Projektänderungen | Separat über pip |

Weitere durch pip aufgelöste Abhängigkeiten sind in den Constraints aufgeführt und behalten ihre Originalmetadaten, einschließlich der Lizenzen gegebenenfalls eingebetteter nativer Bibliotheken. Das optionale Entwicklungs-Extra wird vom Einrichtungsbefehl nicht angefordert.

## GOG-Extraktion

Quelle und genaue Homebrew-Bottle-URLs: [gog-extractor.json](../generator_launcher/recipes/gog-extractor.json). Die Bibliotheken werden separat bezogen und für den isolierten lokalen Betrieb eingerichtet; sie sind nicht Teil des Patch-Pakets.

| Komponente / Quelle | Festgelegter Stand | Lizenz / Originalhinweise | Lokale Änderungen | Lieferung |
| --- | --- | --- | --- | --- |
| [innoextract / Daniel Scharrer](https://github.com/dscharrer/innoextract) | 1.9_14, arm64_sonoma Bottle | zlib; enthaltene LICENSE | Lokale Pfadanpassung für isolierten Betrieb; keine Quelltextänderungen | Separate Bottle |
| [Boost und Mitwirkende](https://www.boost.org/) | 1.92.0 | [Boost Software License 1.0](https://github.com/boostorg/boost/blob/boost-1.92.0/LICENSE_1_0.txt); Text separat im Rezept gebunden | Lokale Bibliothekspfade | Separate Bottle und Lizenztext |
| [Zstandard / Meta und Mitwirkende](https://github.com/facebook/zstd) | 1.5.7_1 | BSD-3-Clause oder GPL-2.0; enthaltene LICENSE und COPYING | Lokale Bibliothekspfade | Separate Bottle |
| [XZ Utils / Tukaani und Mitwirkende](https://tukaani.org/xz/) | 5.8.1 | Bedingungen pro Bestandteil, u. a. 0BSD, GPL und LGPL; enthaltene COPYING-Dateien | Lokale Bibliothekspfade | Separate Bottle |

## Spielbasis und separat installiertes Spielprogramm

| Komponente / Quelle | Festgelegter Stand | Lizenz / Originalhinweise | Lokale Änderungen | Lieferung |
| --- | --- | --- | --- | --- |
| [Ultimate Talkie Edition / Marek Roth (LogicDeLuxe) und Mitwirkende](https://www.gratissaugen.de/ultimatetalkies/monkey1.html) | Builder-Paket 1.02 | Originale Paketbedingungen und Hinweise der enthaltenen Helfer | Native Umsetzung der vorgesehenen Audio-Rezeptur; Originalpaket bleibt erhalten | Separates vollständiges Originalpaket |
| [Deutscher UTE-Patch / la-patches, gandimann](https://www.la-patches.de/p_mi1_ute_us-de.php) | 1.01 für UTE 1.02 | Originale Liesmich und Autorenbedingungen | Originalpatch lokal angewendet | Separater Originaldownload |
| [SCUMMKit / jmnunezizu und Mitwirkende](https://github.com/jmnunezizu/scummkit/tree/a6ec88b02c97e66ce1c57069814c971248e62566) | Herkunftsbeleg a6ec88b02c97e66ce1c57069814c971248e62566 | [UTE-Herkunft und Erlaubnis für monster.tbl](https://github.com/jmnunezizu/scummkit/blob/a6ec88b02c97e66ce1c57069814c971248e62566/third_party/ultimate-talkie/README.md) | Keine pauschale Übernahme der Projektlizenz für fremde UTE-Daten | Herkunftsreferenz, kein fremder Patchinhalt im Release |
| [ScummVM-Team und Mitwirkende](https://www.scummvm.org/) | Vom Nutzer separat gewählte Installation, nicht gepinnt | GPL und Originalhinweise der gewählten Ausgabe | Keine Projektänderungen | Separat vom Nutzer installiert |

## Zielpfade der mitgelieferten Transformationsrezepte

Die folgenden Pfade beziehen sich auf den jeweils separat heruntergeladenen Quellbaum. Abgeleitete Quelltextanteile in diesen Rezepten sind von der ausschließlich eigenen PolyForm-Lizenzierung ausgenommen. Die ursprünglichen MIT-/Apache-2.0-Hinweise gelten weiter; das gilt auch dann, wenn das Rezept nur eingefügte Zeilen und Prüfsummen enthält.

[scummtr-actor.json](../generator_launcher/recipes/scummtr-actor.json):

- `src/ScummTr/script.cpp`
- `src/ScummTr/script.hpp`
- `src/ScummTr/text.cpp`
- `src/ScummTr/text.hpp`

[vox-combined-speed.json](../generator_launcher/recipes/vox-combined-speed.json):

- `tools/omni/voxcpm2/voxcpm2_cli.cpp`
- `tools/omni/voxcpm2/voxcpm2_runtime.cpp`
- `tools/omni/voxcpm2/voxcpm2_runtime.h`
- `tools/omni/voxcpm2/mi1_feature_cache.h` (neu angelegte Datei)

Die minimale aarch64-Ergänzung an SoX `config.sub` steht separat in [native-audio.json](../generator_launcher/recipes/native-audio.json). Der vollständige Originalquellbaum und seine Lizenzdateien werden separat bezogen.

Die lokalen Änderungen sind zusätzlich in [UPSTREAM_CHANGES.md](UPSTREAM_CHANGES.md) dokumentiert.
