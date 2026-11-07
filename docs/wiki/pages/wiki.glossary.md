# Stimmanker, Prewarm, ID und andere Begriffe

Hier findest du die Begriffe wieder, die dir beim Einrichten, Anhören und Verbessern deiner Sprachfassung begegnen.

Wenn dich eine Bezeichnung im Patch aufhält, kannst du hier nachsehen, welche Aufgabe sie hat. Die Beispiele führen dich von der Bedeutung zur praktischen Verwendung; die verlinkten Hilfen zeigen dir anschließend die passende Einstellung.

## Rolle und Stimmanker: Wer spricht wie?

Damit eine Figur über mehrere Dialoge hinweg wiedererkennbar klingt, bekommt sie eine Aufnahme als Stimmvorbild. Diese Aufnahme heißt Stimmanker. Sprichst du zum Beispiel eine Probe für Guybrush ein und ordnest sie seiner Rolle zu, nutzt die KI den Klang deiner Stimme beim Erzeugen seiner deutschen Dialoge.

Besonders bei kurzen Ausgaben unter drei Sekunden kann der Stimmanker helfen, die Stimmfarbe beizubehalten. Die Probe muss dafür nicht denselben Satz enthalten wie der spätere Dialog. Wichtig sind eine klare Einzelstimme und ein eingetragener Wortlaut, der genau zur Aufnahme passt. Wie du einen passenden Versuch vergleichst, erklärt die [Aussprachehilfe](review.pronunciation.md).

## Prewarm: ein deutsches Sprachbeispiel für die KI

Klingt eine deutsche Aufnahme englisch eingefärbt, kann ein Prewarm helfen. Dabei erhält die KI vor dem eigentlichen Dialog ein kurzes deutsches Sprachbeispiel mit zugehörigem Text. Zusammen mit dem Stimmanker, der das Klangvorbild liefert, unterstützt es die gewünschte deutsche Aussprache.

Der Patch erzeugt benötigte Prewarms selbst; ihr Text wird im Spiel nicht an den Dialog angehängt. Du kannst in der Review ausprobieren, ob sich der Akzent mit eingeschaltetem Prewarm bessert. Welche Kombinationen mit Stimmanker und Seed sinnvoll sind, findest du in der [Aussprachehilfe](review.pronunciation.md). „Modell aufwärmen“ bezeichnet dagegen nur das Bereitmachen des Rechenmodells und ist ein anderer Vorgang.

## Dialog-ID: die Adresse einer Spielzeile

Jede Spielzeile hat eine feste Dialog-ID. In der Review steht zum Beispiel „Dialog 70905 · Guybrush“: 70905 ist die ID, Guybrush die Figur. Die Nummer ist der letzte Teil der vollständigen technischen Kennung, die du bei Bedarf in den Details findest. Suche nach der ID, um eine bestimmte Aufnahme wiederzufinden, und notiere sie bei einer Fehlermeldung.

Die ID bleibt beim Verbessern der Aufnahme gleich. Einen neuen Sprechversuch erhältst du dagegen über den [Seed](review.seed.md), die Startzahl der Erzeugung. So kannst du verschiedene Varianten derselben Spielstelle vergleichen.

## Rendern, GPU und ANE: Wie entsteht die Aufnahme?

Wenn der Patch einen Dialog „rendert“, macht er aus seinem deutschen Text eine Audiodatei. Dazu kann er den Grafikprozessor deines Macs nutzen, kurz GPU, oder die Apple Neural Engine, kurz ANE. Die ANE ist ein weiterer Rechenteil für KI-Aufgaben.

Die [Rendermodi](launcher.render-mode.md) helfen dir bei der Wahl: GPU passt zu den derzeit abgestimmten Aussprachekorrekturen, während ANE+GPU die Aufnahmen am schnellsten erzeugt. Weil die schnellere Variante etwas anders spricht und eigene abgestimmte Vorgaben noch fehlen, solltest du ihre Ergebnisse besonders sorgfältig anhören.

## Seed: eine andere Sprechvariante versuchen

Die KI kann denselben Satz auf verschiedene Weise sprechen. Welche Variante bei einem Versuch entsteht, wird unter anderem von einer Startzahl bestimmt, dem Seed. Willst du etwa die Schreibweise eines Namens verbessern, behältst du diese Zahl mit „Vorschau mit gleichem Seed“ bei und kannst dich beim Vergleich auf die Textänderung konzentrieren.

Soll dagegen bei gleichem Text eine andere Sprechweise entstehen, wählst du „Vorschau mit neuem Seed“. Das kann bei unpassender Betonung oder einem verschluckten Wort helfen. Auch die gewählte Rechenmethode beeinflusst den Klang; für einen gezielten [Hörvergleich](review.pronunciation.md) lässt du sie deshalb zunächst gleich.

## TTS, STT und QA: Erzeugen und automatisch prüfen

Aus Text wird mit TTS eine Sprachaufnahme; die Abkürzung steht für die künstliche Spracherzeugung. Anschließend versucht die Spracherkennung, STT, das Gesprochene wieder als Text aufzuschreiben. Die automatische Qualitätsprüfung, QA, vergleicht diesen erkannten Text mit der vorgesehenen Spielzeile.

Fehlt im erkannten Text ein Wort, lohnt sich das Anhören: Vielleicht hat die Stimme es verschluckt, vielleicht hat sich aber auch die Erkennung verhört. Stimme und Betonung beurteilst du ebenfalls mit den eigenen Ohren. Unter [automatische Text- und Audioprüfung](review.qa.md) erfährst du, wie die Hinweise dich dabei unterstützen.

## Vorschau und aktuelle Ausgabe: Was kommt ins Spiel?

Beim Verbessern eines Dialogs kannst du mehrere Aufnahmen ausprobieren. Ein neuer Versuch erscheint zunächst als Vorschau, damit du ihn mit der bisherigen Aufnahme vergleichen kannst. Erst wenn du eine passende Vorschau übernimmst, wird sie zur aktuellen Ausgabe und kommt beim nächsten Export ins Spiel. Die [Hilfe zur Auswahl](review.candidate.md) zeigt dir, wo du beide Fassungen findest.

## Aussprachevorgaben, Rezept und gespeicherte Arbeit

Die mitgelieferten [Dialogvorgaben](launcher.manifest.md) bestimmen, mit welchen Texten und Einstellungen die Dialoge erzeugt werden. Ihre Datei heißt technisch Manifest. Für den Einstieg kannst du die mitgelieferte, auf GPU abgestimmte Fassung verwenden.

Hast du einzelne Dialoge verbessert, kannst du die verwendeten Einstellungen als [Rezept](review.recipe.md) weitergeben. Ein Rezept lässt sich auf der Startseite oder in der Review mit „Rezept laden“ übernehmen. Deine persönliche Arbeit wird [automatisch gespeichert](review.workstate.md); für die zugehörigen Aufnahmen bewahrst du den Arbeitsordner auf.

## Beleg und Hash: Warum wird eine vorhandene Datei abgelehnt?

Zu einer fertigen Aufnahme merkt sich der Patch, mit welchem Text und welchen Einstellungen sie entstanden ist. Diese gespeicherten Nachweise heißen Belege. Ein Hash ist dabei ein Prüfwert, mit dem der Patch erkennt, ob eine Datei verändert wurde.

Meldet er eine unpassende Datei oder einen ungültigen Beleg, findest du unter [vorhandene Ausgabe wird nicht anerkannt](diagnosis.evidence.md) den nächsten Schritt. Für die normale Arbeit wählst und erzeugst du deine Aufnahmen über den Patch; die Nachweise verwaltet er dabei selbst.

## Native Vorbereitung, UTE, Wine und Rosetta

Die Ultimate Talkie Edition, kurz UTE, bildet die Spielbasis für deine neue Sprachfassung. Bei der [Spielvorbereitung](launcher.audio-mode.md) verarbeitet der Patch das Audio direkt auf dem Mac.

Für einzelne Hilfsarbeiten und den deutschen Patch bleiben Windows-Programme nötig. [Wine](launcher.wine.md) führt sie auf deinem Mac aus; Apples Ausführungshilfe Rosetta unterstützt dabei die hier benötigten Intel-Programme. Deshalb begegnet dir die Rosetta-Prüfung auch beim nativen Weg.

## Modell, Runtime, Konsole und ScummVM

Bei der Einrichtung lädt der Patch Sprachmodelle, also KI-Dateien für Erzeugung und Erkennung, sowie die benötigten Hilfsprogramme. Wenn eine Meldung dabei „Runtime“ nennt, ist diese Programmumgebung gemeint. Die [Einrichtung](launcher.setup-dashboard.md) nimmt dir deren Aufbau ab.

Wie weit die Arbeit ist und ob eine Eingabe fehlt, siehst du in der [Konsole](launcher.console.md), dem Status- und Protokollbereich unten im Fenster. Ist deine Sprachfassung fertig, startest du ihren Spielordner anschließend mit [ScummVM](review.finish.md), einem separaten Programm für klassische Adventures.

## Weiterführende Hilfe

- [Schritt 5: eine Stimme für jede Rolle wählen](launcher.voices.md)
- [Was der Prewarm praktisch bewirkt](launcher.prewarm.md)
- [GPU oder ANE: den Rendermodus wählen](launcher.render-mode.md)
- [Mit gleichem oder neuem Seed vergleichen](review.seed.md)
- [Vorschau und aktuelle Ausgabe](review.candidate.md)
