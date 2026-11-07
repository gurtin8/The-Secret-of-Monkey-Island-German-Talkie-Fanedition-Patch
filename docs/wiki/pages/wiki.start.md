# Vom ersten Start zum spielbaren Monkey Island

Vom Einrichten des MI German Talkie Patch über die ersten Hörproben bis zur fertigen Sprachfassung: Hier findest du den Weg zu deinem deutsch vertonten Monkey Island.

Mit dem MI German Talkie Patch vertonst du dein Spiel auf Deutsch. Bevor Guybrush und die anderen Figuren sprechen können, braucht der Patch deine Windows-Ausgabe von The Secret of Monkey Island: Special Edition. Er bereitet daraus eine Spielkopie vor und erzeugt die neuen Aufnahmen. Danach hörst du dir die Dialoge in Ruhe an, verbesserst bei Bedarf einzelne Stellen und stellst daraus deine spielbare Fassung zusammen.

Bei der Spracherzeugung ist die [GPU](launcher.render-mode.md), der Grafikprozessor deines Macs, die passende Wahl für die bereits abgestimmten Aussprachekorrekturen. Wenn du lieber möglichst schnell eine Fassung erzeugen und selbst weiter daran arbeiten möchtest, kannst du stattdessen [ANE und GPU gemeinsam nutzen](launcher.render-mode.md). Wie sich die Auswahl auswirkt, erfährst du in Schritt 5.

## 1 · Einen Platz für dein Projekt wählen

Wähle zunächst einen Arbeitsordner mit etwa 30 GB freiem Platz. Darin bleiben Downloads, Modelle, Stimmen und Ergebnisse erhalten. Wähle beim späteren Weiterarbeiten denselben Arbeitsordner. Als „Spielquelle“ gibst du den installierten Windows-Spielordner oder den [GOG-Offline-Installer](launcher.game-source.md) an, also die heruntergeladene Installationsdatei des Spiels.

Aus deiner Spielquelle bereitet der Patch die Ultimate Talkie Edition vor. Wenn du den Arbeitsordner und die Spielquelle gewählt hast, klicke auf „Weiter zu den Bestätigungen“.

## 2 · Die Einrichtung bestätigen

Auf der nächsten Seite findest du die Bedingungen für die Programme und KI-Dateien, die der Patch benötigt. Lies sie durch und setze die zugehörigen Häkchen. Danach führt dich „Weiter zu den Downloads“ zur Einrichtung.

## 3 · Der Patch einrichten lassen

Mit „Downloads starten“ lädt und installiert der Patch die benötigten Programme und Sprachmodelle, also die KI-Dateien zur Sprachverarbeitung. Unten im Fenster kannst du in der Konsole verfolgen, wie weit die Einrichtung ist. Dort erscheinen auch Hinweise, falls macOS eine Bestätigung von dir erwartet, etwa für die [Command Line Tools](launcher.setup-dashboard.md), Apples Werkzeugpaket für die Einrichtung.

Sind die Dateien geladen, müssen sie noch entpackt und eingerichtet werden. Warte deshalb, bis „Weiter zur Patch-Vorbereitung“ freigegeben ist. Falls die Einrichtung unterbrochen wurde, kannst du sie mit „Alle Pakete laden und Runtimes einrichten“ erneut starten; damit sind die benötigten Hilfsprogramme gemeint. Bereits geprüfte Downloads verwendet der Patch weiter.

## 4 · Deine Spielkopie vorbereiten

Bevor die Spielvorbereitung losgeht, klickst du auf „Rosetta prüfen“. Rosetta ist Apples Ausführungshilfe für die hier benötigten Intel-Programme. Falls sich ein Terminalfenster zur Installation öffnet, folge den Apple-Abfragen und prüfe anschließend erneut. Zusammen mit [Wine](launcher.wine.md), das Windows-Programme auf deinem Mac ausführt, wird Rosetta für den deutschen Patch gebraucht.

Nach erfolgreicher Prüfung startest du „Spieldaten vorbereiten und Patch starten“ und lässt die geöffneten Wine-Fenster während der Arbeit offen. Sobald der deutsche Patch nach einem Zielordner fragt, verwendest du genau den hervorgehobenen „Windows-Zielordner für den deutschen Patch“. Du kannst ihn im Patch mit Command+C kopieren und im Patch mit Control+V einfügen. Folge den weiteren Dialogen bis zur Abschlussmeldung; bei Schwierigkeiten hilft die [Anleitung zur Ordnerauswahl](launcher.patch-target.md).

Wenn die Konsole „Vorbereitung abgeschlossen“ meldet, ist deine Spielbasis bereit und du kannst zu „Sprachausgabe rendern“ weitergehen.

## 5 · Den Figuren ihre Stimmen geben

Unter „Eine Stimme für jede Rolle“ legst du nun fest, wie die Figuren klingen sollen. Öffne eine Rolle und wähle eine eigene Sprechprobe als WAV-, FLAC- oder OGG-Datei. Die Aufnahme darf 3–15 Sekunden lang und höchstens 20 MiB groß sein; mindestens drei Sekunden müssen tatsächlich gesprochen sein. Wähle die Sprache der Probe, trage ihren genauen Wortlaut ein und bestätige den gesprochenen Anteil, bevor du „Als eigene Stimme verwenden“ anklickst. So gehst du auch bei den übrigen Rollen vor.

Die Aufnahme dient als [Stimmanker](launcher.anchor.md): Sie gibt der KI ein Klangvorbild für die Figur. Eine zweite Probe derselben Person kannst du für spätere Reparaturversuche hinzufügen. Verwende vorzugsweise eigene Stimmen; für die Originalstimmen-Option brauchst du das Einverständnis der jeweiligen Synchronsprecher zur KI-Stimmnutzung.

Sind alle Stimmen gewählt, entscheidest du dich für den Rendermodus. „Rendern“ bedeutet hier, aus dem Text eine Sprachaufnahme zu erzeugen. Mit „Langsam · GPU · höchst“ nutzt du die Rechenweise, für die die aktuellen Aussprachekorrekturen abgestimmt sind. „Schnell · ANE + GPU gleichzeitig“ verbindet den Grafikprozessor mit Apples zusätzlichem KI-Rechenteil und erzeugt die Aufnahmen am schnellsten. Sie klingen allerdings etwas anders; eigene darauf abgestimmte Korrekturen fehlen bisher. Wenn du diesen Weg wählst, solltest du mehr Zeit fürs Anhören und Verbessern einplanen. Die [Modusauswahl](launcher.render-mode.md) erklärt dir die Unterschiede genauer.

Für den ersten Durchlauf wählst du „Neue Fassung“ und klickst unten neben der Navigation auf „Rendering starten“. Bevor die Dialoge entstehen, bereitet der Patch Stimmen und kurze deutsche Sprachbeispiele vor, die [Prewarms](launcher.prewarm.md). Danach erzeugt und prüft er die Aufnahmen. Das kann mehrere Stunden dauern. Mit „Anhalten“ legst du eine sichere Pause ein; zum Weiterarbeiten wählst du dieselbe Fassung und klickst erneut „Rendering starten“. Eine fertige Fassung zeigt ihre Abschlusszeit und lässt sich direkt mit „Zur Review“ öffnen. Die reine [Fassungswahl](launcher.render-list.md) startet nichts.

## 6 · Anhören, ausprobieren und die passende Fassung auswählen

Jetzt beginnt die Review, deine Hörprüfung. Die KI trifft Aussprache und Betonung nicht immer auf Anhieb, und auch die automatische Prüfung kann sich verhören. Wähle die fertige Fassung und öffne „Zur Review“. Am besten beginnst du bei den Warnungen und vergleichst Aufnahme und Text. Läuft eine andere Fassung noch, kannst du hier lesen und anhören; Änderungen, Vorschauen und Export warten bis zu ihrem Abschluss.

Welche Änderung sich lohnt, hängt davon ab, was du hörst. Hat die deutsche Aufnahme einen englischen Akzent, ist ein eingeschalteter Prewarm ein guter erster Versuch. Klingt ein kurzer Satz plötzlich nach einer anderen Person, kann ein eingeschalteter Stimmanker helfen, die Stimme der Figur beizubehalten. Für eine andere Betonung kannst du einen neuen [Seed](review.seed.md) ausprobieren, der eine weitere Sprechvariante erzeugt. In der [Aussprachehilfe](review.pronunciation.md) findest du die passenden Versuche und Kombinationen Schritt für Schritt erklärt.

Erzeuge nach einer Änderung eine Vorschau und höre sie dir vollständig an. Gefällt sie dir besser, wählst du sie mit „Vorschau als gut bestätigen“ für das Spiel aus. Solange du sie nicht übernimmst, bleibt die bisherige Aufnahme ausgewählt. Gelungene Einstellungen lassen sich als [Rezept speichern](review.recipe.md). So müssen andere dieselben Stellen nicht noch einmal von vorn verbessern und können mit dir zusammen auch eine passende Abstimmung für ANE+GPU erarbeiten.

## Zum Schluss · Deine Fassung zum Spielen vorbereiten

Wenn die ausgewählten Aufnahmen passen, öffnest du „Patch fertigstellen“. Wähle den Speicherort für den fertigen Spielordner und klicke auf „Spielordner erstellen“. Verfolge den Fortschritt bis zur Meldung „Patch erfolgreich erstellt“. Nur wenn die vorbereitete Spielbasis nicht gefunden wird, musst du den Quellspielordner wählen.

Mit „Spielordner öffnen und Patch beenden“ öffnest du das Ergebnis im Finder. Mit „Zurück zur Review“ hörst du weiter. Die [Hilfe zur Spielerstellung](review.build.md) erklärt die Ordnerwahl und die [Abschlusshilfe](review.finish.md) den Start mit ScummVM.

## Das Abenteuer starten und später weiterarbeiten

Zum Spielen brauchst du [ScummVM](review.finish.md), ein separates Programm für klassische Adventures. Füge dort den erzeugten Spielordner als Spiel hinzu und starte deine Fassung. Bewahre den Ordner als Ganzes auf, damit alle Spieldateien zusammenbleiben.

Wenn dir beim Spielen noch eine Stelle auffällt, kehrst du zum Patch zurück und wählst denselben Arbeitsordner sowie deine fertige Fassung. Im Arbeitsordner liegen Stimmen, Aufnahmen und Reviewentscheidungen. „Zur Review“ öffnet sie ohne neuen Renderlauf. Nach der Korrektur exportierst du eine weitere Spielausgabe oder ersetzt eine vorhandene bewusst.

## Wenn du unterwegs Hilfe brauchst

Die verlinkten Begriffe führen dich direkt zur passenden Erklärung. Im Patch kannst du außerdem mit gedrückter Umschalttaste auf eine Taste oder ein Feld klicken, um die zugehörige Hilfe zu öffnen. Mit der Tab-Taste erreichst du die Bedienelemente auch per Tastatur und rufst ihre Hilfe mit F1 oder ? auf. Escape schließt den Hilfedialog und bringt dich zurück zur Arbeit.

## Weiterführende Hilfe

- [Was du vor dem Start brauchst](launcher.requirements.md)
- [Schritt 5: eine Stimme für jede Rolle wählen](launcher.voices.md)
- [GPU oder ANE: den Rendermodus wählen](launcher.render-mode.md)
- [Dialoge anhören, verbessern und ins Spiel übernehmen](review.workflow.md)
- [Die spielbare Fassung erstellen](review.build.md)
- [Stimmanker, Prewarm, ID und andere Begriffe](wiki.glossary.md)
