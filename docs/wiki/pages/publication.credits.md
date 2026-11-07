# Verwendete Patches und Credits

Hinter der Fanedition stehen viele Vorarbeiten aus der Adventure- und Werkzeug-Community.

Die Ultimate Talkie Edition, kurz UTE, stammt von Marek Roth (LogicDeLuxe) und Mitwirkenden. Ihre Tabelle monster.tbl ist LogicDeLuxe zugeschrieben und wurde über SCUMMKit mit Erlaubnis bereitgestellt. Für die deutsche Spielbasis kommt der Patch v1.01 von la-patches / gandimann und Mitwirkenden hinzu.

Bei der Zuordnung der Dialoge helfen ScummTR und ScummRP von Thomas Combeleran und Donovan Watteau. Die lokale Anpassung ergänzt den Actor-ID-Export. Die Werkzeuge stehen unter MIT.

Die Spracherzeugung baut auf llama.cpp-omni von tc-mb und den ggml-Autoren unter MIT sowie VoxCPM2 von OpenBMB und Mitwirkenden unter Apache-2.0 auf. Danke auch an 0seba / seba für VoxCPMANE und die ANE-Konvertierungen sowie an DennisHuang648 für VoxCPM2-GGUF.

Weitere Aufgaben übernehmen FFmpeg, SoX, whisper.cpp von ggml-org, Whisper von OpenAI, Wine mit den Bestandteilen seiner macOS-Distribution einschließlich Wine Mono und MoltenVK, CPython, python-build-standalone, CMake, Search von driceroland, innoextract, Boost, Zstandard, XZ Utils, Core ML Tools und weitere Python-Bibliotheken.

Danke an das ScummVM-Team, das die fertige Fassung auf heutigen Rechnern spielbar macht, und an alle, die mit Hörtests, Fehlersuche und Korrekturen beitragen.

Die vollständigen Quellenangaben stehen in THIRD_PARTY_NOTICES.md und docs/THIRD_PARTY_COMPONENTS.md im Patch-Paket und Quellprojekt. Die [Drittanbieter-Lizenztexte](publication.licenses.md) sind auch hier in der lokalen Hilfe lesbar. Sie bleiben neben der Projektlizenz erhalten; Quellenlinks ersetzen die Lizenztexte nicht.

## Weiterführende Hilfe

- [Drittanbieter-Lizenztexte](publication.licenses.md)
- [Lizenz und Rechteumfang](publication.license.md)
- [Welche Daten entstehen bei der Arbeit?](publication.architecture.md)
- [Schritt 4: die Spielbasis vorbereiten](launcher.preparation-workflow.md)
- [Vom ersten Start zum spielbaren Monkey Island](wiki.start.md)

## Originalquellen und Bedingungen

- [Original-UTE-Builder und Projektseite](https://www.gratissaugen.de/ultimatetalkies/monkey1.html)
- [Deutscher UTE-Patch von la-patches](https://www.la-patches.de/p_mi1_ute_us-de.php)
- [SCUMMKit: Herkunft der LogicDeLuxe-Tabelle](https://github.com/jmnunezizu/scummkit/blob/a6ec88b02c97e66ce1c57069814c971248e62566/third_party/ultimate-talkie/README.md)
- [Originale UTE-Bedingungen bei SCUMMKit](https://github.com/jmnunezizu/scummkit/blob/a6ec88b02c97e66ce1c57069814c971248e62566/licenses/original_ute_builder.txt)
- [ScummTR: verwendeter Upstream-Stand](https://github.com/dwatteau/scummtr/tree/a50c6f4bf03566721b3f2c00df257a41a82e22ed)
- [llama.cpp-omni: verwendeter Upstream-Stand](https://github.com/tc-mb/llama.cpp-omni/tree/64d092c60db4b4ee45768476bd752f03fdcc98ea)
- [OpenBMB/VoxCPM](https://github.com/OpenBMB/VoxCPM)
- [VoxCPMANE / 0seba](https://github.com/0seba/VoxCPMANE)
- [ANE-Konvertierungen / seba](https://huggingface.co/seba)
- [VoxCPM2-GGUF / DennisHuang648](https://huggingface.co/DennisHuang648/VoxCPM2-GGUF)
- [ScummVM](https://www.scummvm.org/)
