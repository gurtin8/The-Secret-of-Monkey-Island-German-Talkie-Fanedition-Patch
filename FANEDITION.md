# MI German Talkie Patch

Der MI German Talkie Patch begleitet dich von deiner eigenen PC Special Edition bis zur deutschen Sprachfassung. Der Patch bereitet das Audio deiner Spielkopie direkt auf dem Mac auf. Für den deutschen UTE-Patch öffnet er Wine. Anschließend erzeugst du die Stimmen und hörst die Dialoge in der Review an. Dort kannst du Aussprache und Stimmklang gezielt verbessern und gute Einstellungen als Rezept speichern und weitergeben.

Ab der Review stehen die Navigationstasten oben im Fenster, auch beim Fertigstellen und nach dem Erfolg. Der Hinweis zu früheren Versuchen und zur Vorschauübernahme steht vor der Dialogliste. Einstellungen lassen sich mit **Rezept speichern** weitergeben und mit **Rezept laden** übernehmen. Deine Bearbeitungen bleiben automatisch im Arbeitsordner erhalten. Am Ende wählst du über **Patch fertigstellen** den Speicherort und startest **Spielordner erstellen**.

Wenn du von einer bisherigen Version wechselst, beende den Patch zunächst vollständig mit Command+Q und wähle in der neuen Version denselben Arbeitsordner. Der intern angelegte Datenordner „MI1 Generator“ behält seinen Namen; du brauchst ihn weder umzubenennen noch neu anzulegen. So kannst du deine Downloads, Sprachmodelle und vorhandenen Aufnahmen weiterverwenden.

Das [Wiki-Handbuch](docs/wiki/site/index.html) ist im Patch-Paket enthalten und funktioniert auch ohne Internet. Mit Shift+Klick auf eine Taste oder ein Feld öffnest du deren Erklärung; bei fokussierten Tasten funktionieren auch F1 und ?. Die verlinkten Begriffe führen innerhalb der Hilfe weiter. Escape oder „Schließen“ bringt dich zurück zur Arbeit.

**Dieser Patch ist eine Tech-Demo für KI-gestützte Neuvertonung. Wir empfehlen dir, eigene Stimmanker zu verwenden.** Originalstimmen dürfen zur technischen Demonstration nur als KI-Stimmanker genutzt werden, wenn dir die jeweiligen Synchronsprecher dafür ausdrücklich ihr Einverständnis gegeben haben. Die Zustimmung muss für jede verwendete Originalstimme vorliegen; der Patch prüft sie nicht.

Diese Anleitung beschreibt den Stand **0.26.09**. Das Patch-Paket enthält ein textfreies Rezept; vollständige Dialogtexte werden erst lokal aus der vorbereiteten Spielbasis rekonstruiert. Der Patch ist ein macOS-Testkandidat. Die vollständige Synthese-, Hör- und Spielabnahme auf dem M1 steht noch aus. Der [lokale Paketbau](README.md#launcher-aus-dem-quellcode-bauen) signiert die App ad hoc.

## Lizenz und Datenschutz

Rechteverfügbare Projektbestandteile stehen unter [PolyForm Noncommercial 1.0.0](LICENSE); das ist Source Available, keine OSI-Open-Source-Lizenz. Eine kommerzielle Nutzung außerhalb der in der Lizenz erlaubten Zwecke erfordert eine separate Genehmigung. Das gilt nicht als pauschales Verbot für alle Unternehmen: maßgeblich sind die Lizenzbedingungen und der konkrete Nutzungszweck. Spiel, Spieltexte, Marken, Aufnahmen und Drittsoftware sind nicht durch diese Projektlizenz freigegeben. Die Drittanbieterhinweise stehen in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

TTS und STT werden lokal auf deinem Mac ausgeführt. Die Einrichtung lädt Programme, Modelle und Hilfsdateien von externen Quellen herunter. Das Projekt beschreibt keinen Cloud-TTS-Dienst. Lokale Protokolle können Dateipfade und verarbeitete Texte enthalten; prüfe sie, bevor du sie weitergibst.

Der vollständige Originaltext bleibt in jeder Reviewkarte sichtbar. Leicht wechselnde Hintergründe trennen die Dialogkarten; Notizen und Prüfdetails lassen sich bei Bedarf öffnen.

## Danksagung und Credits

Ein besonderer Dank gilt den Entwicklern und Mitwirkenden der Ultimate Talkie Edition, des deutschen Patches und der verwendeten Werkzeuge. Ihre technischen Vorarbeiten ermöglichen Teile dieses Projekts. Danke auch allen, die mit Tests, Fehlersuche, technischen Hinweisen und Dokumentation beitragen.

Die [Wiki-Übersicht „Verwendete Patches und Credits“](docs/wiki/pages/publication.credits.md) nennt belegte Autoren, Einsatz und Originalquellen. Die Rechte externer Projekte bleiben bei ihren Rechteinhabern; eine Nennung bedeutet keine offizielle Zusammenarbeit oder Unterstützung.

## Was du brauchst

| Voraussetzung | Aktueller Stand |
| --- | --- |
| Rechner | Mac mit Apple Silicon, einschließlich M1; Intel-Macs und Windows werden derzeit nicht unterstützt |
| Betriebssystem | macOS 14 oder neuer; für die ANE-Modi macOS 15 oder neuer |
| Arbeitsspeicher | Mindestens 8 GB RAM; ab 16 GB empfohlen |
| Freier Speicher | Etwa 30 GB auf dem Arbeitslaufwerk für die Patch-Erstellung; zusätzliche Audiofassungen und weitere Versuche können mehr Platz benötigen |
| Spielquelle | Installierter PC-Special-Edition-Ordner oder GOG-Offline-Installer; eine bereits gebaute Ultimate Talkie Edition ist kein Ersatz für diese Eingabe |
| Einrichtung | Internetzugang für die angebotenen Downloads und Zustimmung zu den angezeigten Originalbedingungen |

Der Patch lädt die benötigten Werkzeuge und Modelle außerhalb seines Patch-Pakets in den gewählten Arbeitsordner. Dazu gehören Python, VoxCPM2 für die Sprachsynthese, Whisper für die automatische Textprüfung und FFmpeg für Audiodateien. Für die ANE kommt eine eigene CoreML-Runtime hinzu. Für die Spielbasis bezieht der Patch das UTE-Originalpaket und den deutschen Patch. Er bereitet das Audio auf dem Mac auf und führt den deutschen Patch mit Wine aus. Folge den angezeigten Schritten; nötige macOS-Abfragen für Rosetta oder die Command Line Tools bestätigst du selbst. Bei einem GOG-Installer werden die benötigten Spieldaten extrahiert, ohne die Windows-Setup-EXE auszuführen.

## So läuft die Erstellung ab

1. Öffne **MI German Talkie Patch**. Wähle einen Arbeitsordner und deine Spielquelle. Für den normalen Einstieg kannst du die mitgelieferten **Dialogvorgaben** verwenden. Sie beschreiben die Texte und Einstellungen für deine Sprachfassung und enthalten bereits die bisherigen Aussprachekorrekturen.
2. Lies die Bedingungen und starte die Downloads ausdrücklich. Die Konsole zeigt Fortschritt und Fehler. Geprüfte Downloads lassen sich wiederverwenden; unterbrochene Transfers können fortgesetzt werden. Danach werden Werkzeuge und Abhängigkeiten eingerichtet. Bereits geprüfte und eingerichtete Werkzeuge werden wiederverwendet. Erst nach Abschluss der Einrichtung geht es zur Patch-Vorbereitung.
3. Öffne **Patch-Vorbereitung**. Prüfe Rosetta, damit Wine den deutschen Patch ausführen kann. Klicke dann auf **Spieldaten vorbereiten und Patch starten**. Wine-Fenster und Originalpatch können eigene Eingaben verlangen. Warte auf deren Abschluss und kehre zum Patch zurück.
4. Wähle für jede Rolle eine Stimme und einen Rendermodus. Erst **Rendering starten** startet die Stimmvorbereitung und die Dialogerzeugung.
5. Nach einer vollständigen aktuellen Fassung kannst du die Dialoge in der **Review** anhören oder einen automatischen Reparaturversuch ausdrücklich bestätigen. Prüfe insbesondere Warnfälle mit den Ohren. Die automatische Textprüfung ersetzt keine Hörprüfung.
6. Höre deine Vorschauen an und bestätige die passenden Aufnahmen für dein Spiel. Wenn deine Auswahl fertig ist, öffne **Patch fertigstellen**, wähle den Speicherort und klicke auf **Spielordner erstellen**. Verfolge den Fortschritt bis **Patch erfolgreich erstellt**. Nur wenn die vorbereitete Spielbasis fehlt, wirst du nach dem Quellspielordner gefragt.
7. Mit **Spielordner öffnen und Patch beenden** öffnest du das Ergebnis im Finder. **Erstellungsbericht herunterladen** bewahrt den Bericht auf. Zum Spielen fügst du den fertigen Ordner in ScummVM als Spiel hinzu und wählst Deutsch. Für Musik folgst du der Datei **MUSIK.txt** im Spielordner.

Bei einzelnen Korrekturen erzeugst du eine Vorschau und hörst sie vor der Übernahme an. Falls die Vorschau-Warteliste angeboten wird, kannst du dort mehrere Dialoge sammeln und wieder entfernen. Nach dem Start entstehen erst die Aufnahmen, danach folgt ihre Prüfung. Ein fehlgeschlagener Eintrag bleibt für deinen nächsten Versuch vorgemerkt.

## Rendermodi und Ultra Fast

Auf der Renderingseite wählst du den **Rendermodus** und startest mit **Rendering starten**. Die Modusnamen sind keine garantierten Laufzeiten.

| Modus | Verarbeitung | Ultra Fast |
| --- | --- | --- |
| **Langsam** | GPU, Qualität „höchst“, Einzelbetrieb | Nicht verfügbar |
| **Mittel** | ANE, Qualität „mittel“ | Mehrere adaptive ANE-Instanzen möglich |
| **Schnell** | GPU und ANE, Qualität „mittel“; mindestens 16 GB RAM | Zusätzlich adaptive ANE-Parallelität |

**Für Guybrush empfehlen wir das Rendering auf der GPU.** Im Modus **Schnell** bleibt Guybrush automatisch auf der GPU, während andere Rollen die ANE nutzen können. Das gilt auch mit Ultra Fast. **Langsam** erzeugt alle Rollen auf der GPU mit höchster Qualität. **Mittel** verwendet die ANE auch für Guybrush; das Ultra-Fast-Häkchen ändert diesen Rendermodus nicht.

Das Häkchen **Ultra Fast · adaptive ANE-Parallelität** erlaubt bei Mittel und Schnell, mehrere Dialoge gleichzeitig über die ANE zu erzeugen. Es senkt die gewählte Qualitätsstufe nicht. Der Pool plant anhand des verfügbaren RAMs mit **15 % Reserve**: Modelle werden nacheinander geladen, das Budget nach jedem Laden erneut geprüft und die bereiten Instanzen anschließend parallel eingesetzt. Auch verschiedene Dialoge derselben ANE-Rolle können parallel laufen. Die Anzeige nennt Instanzzahl und Reglergrund.

Auf bekannten Systemen mit mehr als 8 GiB sind höchstens **fünf ANE-Instanzen** vorgesehen. Gemessener Speicherbedarf, Schutzsignale und die gültige Benchmarkempfehlung begrenzen die tatsächliche Zahl. Mehr Instanzen teilen sich dieselbe physische ANE und bringen nicht automatisch mehr Geschwindigkeit. Ultra Fast ersetzt deshalb weder den Maschinenbenchmark noch die Speicherprüfung. Erst nach einer vollständigen aktuellen Fassung wird **Weiter zur Review** freigegeben.

Der Erstlauf bearbeitet zunächst alle Dialoge mit ihrem ersten vorgesehenen Erzeugungsversuch und anschließender Textprüfung. Ein Aussprachebefund löst dabei keine automatische Kette weiterer Versuche aus. Solche hörbaren Ausgaben bleiben Warnfälle für die Review. Weitere automatische Reparaturen beginnen erst nach einem vollständigen Erstlauf und deiner Bestätigung.

Während des Renderns zeigt ein Statusring neben **Läuft** und ein größerer hinter **Fortsetzen** den Zustand an: grün im normalen Betrieb, orange bei Warnungen, kaminrot bei Fehlern. Bei Fehlerzuständen steht und pulsiert der Ring; beim bewussten Pausieren steht er ohne Fehlersignal.

Mit **Anhalten** kannst du das Rendering jederzeit zum Pausieren auffordern. Laufende Synthese- und Prüfschritte werden zuerst gesichert abgeschlossen. **Fortsetzen** führt denselben Auftrag weiter. Du kannst während einer Pause einen Schritt zurückgehen und anschließend zur bereits besuchten Renderingseite zurückkehren; der Seitenwechsel startet keinen neuen Auftrag. Diese Pausefunktion gilt für das Rendering; die Originalinstaller haben ihre eigene Bedienung. Behalte den Arbeitsordner, damit Downloads, Ergebnisse und Belege erhalten bleiben.

Einzelne technische Dialog- oder Whisperfehler werden für die manuelle Nacharbeit gespeichert; der Restlauf geht weiter. Die betroffenen IDs bleiben auch nach dem erneuten Öffnen sichtbar und werden bei demselben Auftrag weiterhin übersprungen. Das ist keine erfolgreiche Audioausgabe: Fehlende Audios oder gültige Prüfbelege können die vollständige Review, automatische Reparatur und den Export sperren. Eine als „manuelle Review“ gespeicherte ID bedeutet noch nicht, dass sie im Hörinterface abspielbar ist. Kritischer Speichermangel und globale Datei- oder Zuordnungsfehler können weiterhin den gesamten Lauf stoppen. Beachte den konkreten Fehlertext; wiederholtes Starten allein repariert diese Fälle nicht.

## Arbeiten mit 8 GB RAM

Ein Mac mit 8 GB ist zugelassen. Die Untergrenze verspricht aber nicht, dass unter jeder Systemlast alle Modelle erfolgreich laden oder der gesamte Lauf durchläuft. Modelle, Textprüfung, macOS und andere Programme teilen sich den verfügbaren Speicher.

Für einen vorsichtigen Einstieg:

- Starte den Mac vor dem Rendern neu und öffne nur die benötigten Programme. Das kann bisherigen Swap zurücksetzen und Speicher freigeben; während des Laufs kann macOS erneut Swap verwenden.
- Wähle zunächst **Langsam · GPU · höchst**. Dieser Modus verwendet die GPU im Einzelbetrieb. **Schnell** nutzt ANE und GPU gleichzeitig; **Mittel** nutzt die ANE. Die Namen sind keine garantierten Laufzeiten, und ANE benötigt macOS 15.
- Halte ausreichend Platz sowohl auf dem Arbeitslaufwerk für Ergebnisse als auch auf dem macOS-Systemlaufwerk für dessen Auslagerung frei. Die etwa 30 GB für die Patch-Erstellung schließen zusätzlichen Platz für weitere Audiofassungen, Versuche und macOS-Swap nicht ein.
- Bei Speicherproblemen sichere den Lauf durch **Anhalten**, beachte die Fehlermeldung und folge einer angezeigten Neustartaufforderung mit demselben Arbeitsordner. Ein gespeicherter Dialogfehler verschwindet durch Neustart nicht automatisch.

Bei höchstens **8 GiB physischem RAM** läuft die QA im Erstlauf automatisch **nach der TTS-Erzeugung**: Zuerst werden die Sprachdateien gespeichert, danach werden die TTS-Modelle entladen und anschließend die Ausgaben einzeln mit Whisper geprüft. Bis zum Ende dieser Prüfung bleiben die Audios ungeprüft; Review und Export werden dadurch nicht vorzeitig freigegeben. Bei mehr RAM laufen TTS und QA weiterhin überlappend. Im adaptiven ANE-Modus fehlt während der reinen TTS-Phase der geprüfte QA-Durchsatz als Erweiterungsnachweis; der Pool bleibt in diesem 8-GB-Erstlauf bei einer ANE-Instanz. Das senkt gleichzeitige Modelllast, ist aber keine Garantie für einen vollständigen 8-GB-Lauf.

Swap ist zulässig, kann aber langsamer sein und ersetzt keine nachgewiesene ausreichende Speicherkapazität. Ein erfolgreicher vollständiger 8-GB-Lauf ist mit dieser Dokumentation nicht belegt. Zusätzlicher freier Festplattenspeicher allein garantiert ihn ebenfalls nicht.

Plane mehrere Stunden für Vertonung und Patch ein. Als ungefähre Nutzerreferenz wurden **etwa fünf Stunden auf einem MacBook Pro M1 Max** genannt. Das ist kein vergleichender Benchmark und keine Zusage für deinen Rechner, besonders nicht für ein 8-GB-Modell.

## Stimmanker und Prewarm verstehen

Ein **Stimmanker** ist eine Sprechprobe, die dem Modell als Referenz für die Stimme einer Rolle dient. Für eigene Stimmen importierst du WAV, FLAC oder OGG mit **3–15 Sekunden** Dauer, höchstens **20 MiB**, und trägst den exakt gesprochenen Text ein. Die Probe soll mindestens drei Sekunden tatsächliche Sprache enthalten; der Patch misst die Dateidauer, prüft diese Inhaltsanforderung aber nicht automatisch. Eine zweite Probe derselben Stimme ist optional und kann bei einer ausdrücklich gestarteten Reparatur verwendet werden. Ohne zweite Probe wird diese Ankerwechselstufe ausgelassen. Nutze vorzugsweise eigene Stimmanker. Die Originalstimmen lassen sich als **Tech-Demo** wählen, sofern dir die jeweiligen Synchronsprecher ausdrücklich ihr Einverständnis zur Nutzung ihrer Stimme als KI-Stimmanker gegeben haben.

Ein **Prewarm** ist eine zusätzlich erzeugte kurze deutsche Sprachprobe, die je nach Erzeugungsmethode mit ihrem Text als weiterer Sprachkontext in die Synthese eingeht. Sie ergänzt den Stimmanker und ist nicht dasselbe wie deine hochgeladene Aufnahme oder bloß ein Aufwärmen der Hardware. Der aktuelle akzeptierte Audiobereich liegt bei **2–5 Sekunden**. Passt die erzeugte Dauer nicht, versucht die Vorbereitung begrenzt weitere Seeds mit derselben Stimme und demselben Text. Nach dem Basisversuch und höchstens drei Zusatzversuchen stoppt sie mit einer Erklärung, statt heimlich Stimme oder Text zu wechseln.

**Wann solltest du Prewarm verwenden?** Besonders empfohlen ist Prewarm, wenn eine deutsche Ausgabe einen englischen Akzent hat oder ein Dialog sehr kurz ist, etwa ein einzelnes Wort oder ein kurzer Ausruf. Der zusätzliche deutsche Sprachkontext kann dem Modell helfen, Aussprache und Sprachfluss zu stabilisieren. Aktiviere Prewarm für einen gezielten neuen Versuch und höre das Ergebnis anschließend in der Review an. Eine Verbesserung ist nicht garantiert.

Stimmanker und Prewarm bestimmen Eingaben des Modells. Sie garantieren weder dieselbe Stimme in jedem Dialog noch korrekte Aussprache oder fehlerfreie Betonung. In der Review können diese Einstellungen für gezielte neue Versuche getrennt gewählt werden.

## GPU und ANE vergleichen

Vor dem ersten produktiven Rendering führt der Patch automatisch einen Maschinenbenchmark aus, sobald genügend vorbereitete Referenzen für die kleine Dialogauswahl vorliegen. Gültige gespeicherte Ergebnisse werden bei späteren Renderstarts verwendet. Sie sind an Rechner, Betriebssystem, Modelle und Runtime gebunden; bei relevanten Änderungen wird neu gemessen.

Mit **GPU / ANE-Benchmark starten** auf der Renderingseite kannst du während der Generierung eine Vergleichsmessung vormerken. Das ist auch im pausierten Zustand möglich; ausreichend vorbereitete Referenzen reichen, die gesamte Stimmvorbereitung muss dafür nicht fertig sein. Laufende TTS- und QA-Schritte werden gesichert abgeschlossen. Der Auftrag pausiert, seine Modelle werden freigegeben und der Benchmark erzeugt dieselbe kleine Dialogauswahl frisch auf GPU, einer ANE und weiteren ANE-Stufen bis höchstens fünf. Weitere Stufen enden, wenn kein messbarer Vorteil entsteht oder das Ressourcenbudget nicht reicht. Modellladen und Aufwärmen werden getrennt von der TTS-Messzeit erfasst. Bei Überlastung erscheint ein Fehler mit Handlungshinweis.

Ein erfolgreicher Benchmark setzt einen vorhandenen Hauptauftrag automatisch mit der schnellsten zulässigen gemessenen Variante fort, auch nach einer Nutzerpause. Qualität und GPU-/ANE-Auswahl bleiben verbindlich; mehrere ANE-Instanzen nur mit Ultra Fast. Bei Abbruch oder Fehler bleibt eine vorherige Nutzerpause erhalten. **Benchmark abbrechen** beendet nur die Vergleichsmessung und gibt die Modelle wieder frei. Ergebnisse und Messbericht liegen separat; Benchmark-Audios werden nicht als geprüfte Produktivaudios oder Reviewfreigaben übernommen. Die Anzeige zeigt Zeit, tatsächliche gleichzeitige Aufträge und Vergleichsfaktoren. Fehlgeschlagene oder unvollständige Messungen liefern keinen erfundenen Beschleunigungsfaktor. Ein kleiner Benchmark ist eine Momentaufnahme und kein garantierter Geschwindigkeitsgewinn für alle Dialoge.

## Empfohlener Ablauf für Korrekturen

1. **Erstlauf abschließen und Korrekturstand sichern.** Öffne danach die Review und exportiere zunächst die aktuelle **patch-review-extension.json** als Ausgangsstand. Beginne mit Warnfällen und auffälligen Dialogen; auch automatisch unauffällige Ausgaben können hörbare Fehler enthalten.
2. **Anhören und den Fehler festhalten.** Notiere zur Dialog-ID konkret, was falsch klingt: fehlendes Wort, Aussprache, englischer Akzent, Betonung oder abweichende Stimme. Vergleiche die Aufnahme mit dem angezeigten Originaltext.
3. **Eine gezielte Änderung wählen.** Bei einem einzelnen misslungenen Versuch probiere zunächst **Vorschau mit neuem Seed** mit beibehaltenem Stimmanker. Bei englischem Akzent oder sehr kurzen Dialogen empfiehlt sich **Prewarm**. Bei einem Stimmproblem prüfe die Referenzprobe und ihre genaue Abschrift. Ändere möglichst nur eine Einstellung pro Versuch, damit du die Wirkung beurteilen kannst. Für Guybrush empfiehlt sich der Rendermodus **GPU**, sofern er für den Versuch auswählbar ist.
4. **Bei Bedarf den TTS-Text anpassen.** Nutze **TTS-Text für den nächsten Versuch** für eine gezielte Aussprachehilfe. Text und Notiz werden beim Tippen automatisch gespeichert; warte auf „Gespeichert“ und beachte einen angezeigten Speicherfehler. Bewahre Bedeutung und Inhalt des Dialogs. Das Speichern erzeugt noch kein neues Audio; starte den neuen Versuch ausdrücklich. Arbeite zunächst mit einzelnen Dialogen oder kleinen Gruppen.
5. **Versuche vergleichen und auswählen.** Höre das neue Ergebnis vollständig an und vergleiche es mit der bisherigen Ausgabe. Prüfe Wörter, Stimme, Akzent und Betonung. Bestätige die tatsächlich bessere Vorschau nach dem Anhören als gut; dieser eine Klick wählt sie gleichzeitig für die Ausgabe aus. Ein bestandener automatischer Textvergleich genügt dafür nicht. Bringt ein Versuch keine Verbesserung, behalte die bessere vorhandene Ausgabe und dokumentiere den offenen Fehler.
6. **Gute Einstellungen weitergeben.** Text, Notizen und Reviewentscheidungen werden automatisch gespeichert. Mit **Rezept speichern** erhältst du die verwendeten Einstellungen deiner ausgewählten Aufnahmen als Datei. Audios und eigene Stimmproben sind darin nicht enthalten.
7. **Fertige Fassung prüfen und ausgeben.** Höre die ausgewählten Korrekturen nochmals durch, bevor du die spielbare Version exportierst. Prüfe anschließend die korrigierten Stellen auch im Spiel. Noch ungelöste Fälle bleiben nachvollziehbar in den Notizen.

**Seeds festhalten:** **Vorschau mit gleichem Seed** verwendet den angezeigten festen Seed. **Vorschau mit neuem Seed** erzeugt einmalig einen Kandidaten mit einem anderen Seed und verändert die bisherige Auswahl noch nicht. Erst **Übernehmen** beziehungsweise die ausdrückliche Übernahme trotz Warnungen hält dessen konkrete Zahl als neue Vorgabe fest. Eine Gutbestätigung hält ebenfalls den Seed der aktuellen Ausgabe fest. Über **Diese frühere Fassung übernehmen** kannst du auf eine geprüfte alte Fassung zurückgehen. Normale spätere Läufe behalten den gespeicherten Seed; unveränderte abgenommene Proben können direkt wiederverwendet werden. Ein neu geladenes Rezept kann ausdrücklich eine andere konkrete Seedvorgabe setzen. Derselbe Seed allein garantiert über unterschiedliche Modelle oder Rendermodi hinweg kein identisches Audio.

**Ausgewählte Ausgabe und Vorschauen unterscheiden:** Der Player **Aktuelle deutsche Ausgabe · ausgewählt** spielt die gespeicherte Fassung, deren Seed in der Überschrift steht. Neue Hörversuche erscheinen separat als **Neueste Vorschau · noch nicht übernommen**. Unter **Weitere Fassungen** kannst du ältere Versuche wieder anhören und auswählen; gleiche Fassungen werden nicht doppelt angezeigt. Erst die dortige Übernahme ändert die aktuelle Ausgabe. Bei Warnungen gibt es genau eine ausdrückliche Warnungsübernahme; die konkreten Gründe stehen am Versuch und im Terminal. Einzelheiten sind unter **Text, STT und Einstellungen** aufklappbar.

**Zurück zur letzten Version:** Die Taste ersetzt „Gut aufheben“. Sie wählt die unmittelbar zuvor ausgewählte Audiofassung erneut und übernimmt ihren Text, ihre Einstellungen und ihren festen Seed. Dabei wird kein neues Audio erzeugt; später gerenderte, noch nicht übernommene Vorschauen bleiben verfügbar und werden nicht zum Rückkehrziel. Der kleine Hinweis an der Taste nennt den Seed der vorherigen Auswahl. Ohne gespeicherten Vorgänger ist sie deaktiviert. Bei alten Arbeitsständen lässt sich die vorherige Auswahl nur aus vorhandenen Übernahmen ableiten; ein zuletzt erzeugter Versuch wird nicht erraten. Unbestätigte Warnungen können weiterhin nur über die ausdrückliche Warnungsübernahme bei der früheren Fassung bestätigt werden.

Die automatische Reparatur ist eine optionale Hilfe nach dem vollständigen Erstlauf. Bei genügend gemessenem RAM-Spielraum können ein GPU- und ein ANE-Dialog gleichzeitig laufen. Der Patch hält dabei mindestens 15 % RAM-Reserve frei und prüft vor dem zweiten Modell ein zusätzliches konservatives Ladebudget. Unter 16 GiB, bei Speicherdruck oder unbekannten Messwerten arbeitet der Patch sequenziell. Neue Alternativ-Prewarms verwenden denselben warmen GPU-Modellpool wie die Review; unveränderte vorhandene Prewarms werden wiederverwendet. Der erste Modellstart bleibt erforderlich. Aktive Rendermodi stehen im Reparaturstatus. Beim Rückweg über **Zurück zu Stimmen & Rendern** werden offene Reviewfelder gespeichert. Ein ausdrücklich gestarteter Reparaturversuch arbeitet an noch nicht abgenommenen Problemfällen. Abgenommene Ausgaben bleiben erhalten. Auch gespeicherte, noch nicht übernommene Text-, Rendermodus-, Anker- und Prewarm-Änderungen werden geschützt: solche Dialoge werden mit Hinweis übersprungen, damit die Reparatur deine Vorgaben nicht ersetzt. Notizen bleiben erhalten. Höre offene Änderungen zuerst als Vorschau an und übernimm die gewünschte Fassung, oder verwirf die Änderungen, bevor die Reparatur diesen Dialog wieder bearbeiten soll. Nach Rücksetzen auf die gespeicherte Ausgangseinstellung kann eine gebundene Reparaturkette fortgesetzt werden.

Vollständig geprüfte Reparaturergebnisse ohne QA-/STT-Fehler oder Warnungen werden automatisch ausgewählt und unter **Automatisch repariert** geführt. Du musst sie nicht zusätzlich menschlich bestätigen. Der konkrete Seed und die Einstellungen dieser Fassung werden festgehalten. Warnungen, fehlende Belege und offene Fehler bleiben zur manuellen Review. Eine bewusst abgenommene Fassung wird weiterhin ausgelassen und nicht automatisch ersetzt. Neue Änderungen oder eine weitere offene Vorschau führen den Dialog zurück in **Todo**. Ein optionaler Alternativanker mit unzulässigem Prewarmformat oder unzulässiger Dauer wird mit Erklärung ausgelassen; die weiteren Seed-/Rendermodusversuche behalten die bisherige Stimme. Fehlende oder manipulierte Audio-/Hashbelege bleiben blockierend.

**Einmal abnehmen genügt:** Gutbestätigung und ausdrückliche Übernahme trotz Qualitätswarnung werden für die konkrete Aufnahme gemeinsam berücksichtigt. Du kannst dieselbe abgenommene Fassung später wieder auswählen, ohne ihre Warnung erneut zu bestätigen. Ihre historischen QA-/STT-Befunde bleiben sichtbar. Andere Versuche und neue Aufnahmen benötigen ihre eigene Abnahme; fehlende/veränderte Audios oder unpassende Belege werden dadurch nicht freigegeben.

## Rezepte austauschen

In der Review erhältst du mit **Rezept speichern** eine Datei mit den tatsächlich verwendeten Einstellungen deiner ausgewählten Aufnahmen. Darin stehen Texte, Seeds, Rendermodi und die nötigen Vorgaben für Stimmanker und Prewarm. Gib diese Datei weiter und beschreibe kurz, was du verbessert hast und mit welchem Rendermodus und welcher Stimmenwahl du gearbeitet hast.

Ein empfangenes Rezept öffnest du auf der Startseite oder in der Review über **Rezept laden**. Das Laden übernimmt die Einstellungen. Starte danach ausdrücklich die Spracherzeugung oder verarbeite die betreffenden Dialoge in der Review. Höre die neuen Ergebnisse selbst an, bevor du sie für das Spiel auswählst.

Deine persönliche Arbeit bleibt automatisch im Arbeitsordner erhalten. Wähle beim nächsten Start denselben Arbeitsordner und dieselbe Fassung. Auch ältere **patch-review-extension.json**-Arbeitsstandsdateien bleiben über **Rezept laden** verwendbar. Bewahre für eine vollständige Sicherung den gesamten Arbeitsordner einschließlich der Aufnahmen auf.

Ein Rezept enthält keine Audios, privaten Stimmaufnahmen, Modelle oder persönlichen Hörbestätigungen. Die benötigten Referenzen müssen auf dem eigenen Rechner verfügbar oder rekonstruierbar sein. Unterschiedliche Geräte und Modelle können trotz gleicher Einstellungen anders klingen.

## Technischer Stand und bekannte Grenzen

Die Vorschau hält ab 16 GiB das gebundene TTS-Modell zwischen einzelnen Versuchen bereit. Bei höchstens 8 GiB kann die Warteliste Synthese und Prüfung trennen: Zuerst werden die WAVs erzeugt, dann wird TTS freigegeben und Whisper prüft sie nacheinander. Eine vorhandene WAV wird nur mit gültigem signiertem Synthesebeleg fortgesetzt. Whisper startet weiterhin pro WAV; ein warmes Whispermodell oder ein gemessener M1-Zeitgewinn ist nicht nachgewiesen.

### Windows: was vorbereitet ist und was noch fehlt

Es gibt derzeit **keine ausgelieferte Windows-Fassung** und keinen unterstützten Windows-Renderweg. Dass die Spielquelle und einige Originalwerkzeuge aus Windows stammen, macht den macOS-Patch noch nicht zu einem Windows-Programm.

Die Codebasis enthält konkrete Grundlagen für eine Portierung: Browseroberfläche, Projektsteuerung, Dialogplan, Review und Spielausgabe sind vom nativen macOS-Starter getrennt. Erzeugungsbackends werden über eigene Profile und Adapter angebunden. Die Dateiverträge verwenden relative Pfade und berücksichtigen bereits problematische Windows-Dateinamen; für die lokale Installationskonfiguration ist ein Windows-Pfad unter `LOCALAPPDATA` vorgesehen. Die Hardwareerkennung kennt Windows als Betriebssystem.

Diese Grundlagen sind noch keine fertige Plattformunterstützung. Der aktuelle Starter verwendet Swift und WebKit, die GPU-Anbindung Metal und die ANE-Anbindung Apple CoreML. Die Runtime-Downloads und Spielvorbereitung sind derzeit für macOS gebunden; der Patch lehnt Windows-Synthese ausdrücklich ab. Für eine Windows-Ausgabe fehlen unter anderem ein Windows-Starter, passende Runtime- und GPU-Adapter, Windows-Einrichtungsrezepte und die tatsächliche Abnahme von Vorbereitung, Rendering, Wiederaufnahme, Review und Spielausgabe. Windows-Mindestanforderungen und Termine lassen sich aus dem aktuellen Stand nicht ableiten.

## Wenn der Start oder ein Lauf fehlschlägt

Die Konsole zeigt den Fortschritt und erlaubt **Protokoll exportieren** als TXT. Dieser Export enthält den vorgehaltenen Verlauf, kein unbegrenztes Archiv aller Meldungen. Seit 0.23.6 gibt es außerdem ein dauerhaftes Startprotokoll: Öffne im Patchmenü **Startprotokoll im Finder zeigen**. Nenne bei einer Fehlermeldung die Patchversion, den gewählten Modus und den letzten sichtbaren Schritt. Sichere die betroffenen Logs und den Arbeitsordner, bevor du die Vorbereitung neu beginnst. Der gemeldete reale M1-Startfehler ist noch nicht abschließend geklärt.

Beim Neustart eines abgebrochenen Auftrags kannst du einen anderen Rendermodus wählen. Vollständig geprüfte, lokal belegte fertige Audios werden wiederverwendet und behalten ihre ursprüngliche Rendermodus- und Qualitätsangabe. Der neue Modus gilt für fehlende Dialoge.

Seit 0.24.10 lädt die Review die Dialogliste ohne die vollständige Exportprüfung des Spielarchivs. Beim Öffnen von **Patch fertigstellen** erscheint **Spielbasis und Exportstatus werden geprüft …**; der Export bleibt bis zum Abschluss gesperrt. Die Spielbasis wird weiterhin vollständig vor dem Build geprüft. **Zur vollständigen Review gehen** bleibt während des Ladens gesperrt. **Rezept laden** öffnet im Patch den macOS-Dateidialog.

Seit 0.24.11 entfällt das eingerahmte Patchreview-Moduslabel neben der Überschrift. Während des Dialogladens dreht sich der Konsolenstatusring während **Lade Dialoge …** bis die Review aufgebaut ist. Ein Ladefehler beendet den laufenden Status und erscheint in der Konsole. Die laufende Konsolenabfrage prüft nur Statusmetadaten; das Spielarchiv wird weiterhin erst in der Exportansicht vollständig geprüft.

0.24.12 verringert mehrfach gehaltene JSON-Metadaten und begrenzt Beleg-/Run-Caches nach Speicherbedarf. Geteilte Metadaten bleiben unveränderlich; eigene Bearbeitungen erhalten unabhängige Kopien. Statusabfragen überlappen sich nicht mehr. Renderergebnisse, bestätigte Seeds, Reviewentscheidungen und bestehende Dateiformate bleiben erhalten.

## 0.24.13 · Review laden

Wiederverwendete Audios ohne Run-Sammelbeleg verwenden den bereits geprüften Quellhash. Die Cachegrößenberechnung zählt nur zusätzliche Belege, statt pro Dialog den gesamten Quellbestand zu durchlaufen. Die Speicherbegrenzung bleibt erhalten. Text-/Rollenprojektion und Audio-/Freigabeprüfung zeigen getrennte Fortschrittsmeldungen. Keine Änderung an Audios, Reviewentscheidungen oder Seeds.
