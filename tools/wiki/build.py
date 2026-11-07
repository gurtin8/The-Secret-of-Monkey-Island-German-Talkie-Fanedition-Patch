#!/usr/bin/env python3
"""Build offline help and Pages site without dependencies or application imports."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIKI = ROOT / 'docs/wiki'
# Explicit product boundary: auxiliary hearing interfaces and historical work/ are excluded.
SOURCE_FILES = sorted(set(
    list((ROOT / 'generator_launcher').glob('*.py')) +
    list((ROOT / 'generator_launcher').glob('*.js')) +
    list((ROOT / 'generator_launcher/native').glob('*.js')) +
    list((ROOT / 'generator_launcher/native').glob('*.html')) +
    list((ROOT / 'generator_launcher/native').glob('*.swift')) +
    list((ROOT / 'patch_review').glob('*.py')) +
    list((ROOT / 'patch_review').glob('*.js')) +
    list((ROOT / 'patch_review').glob('*.html')) +
    [ROOT / 'mi1_patcher.py', ROOT / 'tools/patch_mi1_intro.py']))
# Developer fixtures and benchmarks are outside the regular generator app.
DEVELOPER_FILES = {
    'generator_launcher/prewarm_ab.py',
    'generator_launcher/package_prewarm_ab.py',
    'generator_launcher/native/PrewarmBenchmark.swift',
    'generator_launcher/native/PatchWindowDockFixture.swift',
}
SOURCE_FILES = [path for path in SOURCE_FILES if path.relative_to(ROOT).as_posix() not in DEVELOPER_FILES]
INLINE_LINK = re.compile(r'\[([^\]\n]+)\]\(([a-z][a-z0-9.-]+)\)')


def markdown(text):
    text = re.sub(r'\{\{review-status:(manual|clean|repaired|open|warning|invalid|active)\}\}',
                  lambda m: f'![Status {m[1]}](../site/status-{m[1]}.svg)', text)
    return INLINE_LINK.sub(lambda m: f'[{m[1]}]({m[2]}.md)', text)


def literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(v.value if isinstance(v, ast.Constant) else '{' + ast.unparse(v.value) + '}' for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        left = literal(node.left)
        right = literal(node.right)
        if left is None: return None
        if isinstance(node.op, ast.Mod): return re.sub(r'%[sdri]', '{value}', left)
        return left + (right if right is not None else '{' + ast.unparse(node.right) + '}')
    return None


def messages():
    found = {}
    unresolved = []
    def add(message, path, line, kind):
        if not message or len(message.strip()) < 3: return
        message = message.strip()
        key = hashlib.sha256(message.encode()).hexdigest()[:16]
        record = found.setdefault(key, {'id': 'message.' + key, 'message': message, 'kind': kind, 'locations': []})
        location = {'source': str(path.relative_to(ROOT)), 'line': line}
        if location not in record['locations']: record['locations'].append(location)
    for path in SOURCE_FILES:
        source = path.read_text(encoding='utf-8')
        if path.suffix == '.py':
            tree = ast.parse(source)
            for node in ast.walk(tree):
                expr = None
                kind = 'error'
                if isinstance(node, ast.Raise) and node.exc:
                    if isinstance(node.exc, ast.Call) and node.exc.args: expr = node.exc.args[0]
                    else: continue  # bare rethrow: keeps original failure
                elif isinstance(node, ast.Call):
                    name = ast.unparse(node.func)
                    if name == '_require' and len(node.args) > 1: expr = node.args[1]
                    elif name.endswith(('.warning', '.error', '.warn')) and node.args:
                        expr = node.args[0]; kind = 'warning' if name.endswith(('.warning', '.warn')) else 'error'
                if expr is not None:
                    text = literal(expr)
                    if text: add(text, path, node.lineno, kind)
                    else: unresolved.append({'source': str(path.relative_to(ROOT)), 'line': node.lineno, 'expression': ast.unparse(expr)})
        else:
            # Static JS exceptions and native alert text; dynamic host/platform exceptions are recorded as classes in diagnosis.
            patterns = [r'(?:throw\s+(?:new\s+)?(?:Error|\w+Error)|(?:new\s+)?Error)\(\s*([\'"`])((?:\\.|(?!\1).)*?)\1',
                        r'(?:messageText|informativeText)\s*=\s*([\'"])((?:\\.|(?!\1).)*?)\1',
                        r'return\s+([\'"])((?:\\.|(?!\1).)*?)\1(?=\s*\n)'] if path.suffix == '.swift' else [r'(?:throw\s+(?:new\s+)?(?:Error|\w+Error)|new\s+Error)\(\s*([\'"`])((?:\\.|(?!\1).)*?)\1']
            for pattern in patterns:
                for match in re.finditer(pattern, source):
                    text = match.group(2).replace('\\n', '\n').replace('\\"', '"').replace("\\'", "'")
                    add(text, path, source.count('\n', 0, match.start()) + 1, 'error')
    return sorted(found.values(), key=lambda m: m['message'].casefold()), unresolved


def classify(message, topics):
    value = message.casefold()
    rules = [
        ('launcher.text-reconstruction', r'textrekonstruktion|textrezept|textslot|textpfad|speech.id.referenz|lokale text.id|fundstelle|basis./zielhash|basis.hash|zieltext.prüfsumme|textfeld im ausgangsrezept'),
        ('diagnosis.memory', r'ram|memory|speicher(?:druck|mangel)|out.of.memory|swap|ressourcenbudget'),
        ('diagnosis.disk', r'platz|space|read.only|permission|zugriff|schreib|laufwerk|symlink|pfad|path|directory|ordner'),
        ('diagnosis.prewarm', r'prewarm|vorwärm'),
        ('diagnosis.anchor', r'anker|anchor|voice|stimm|speaker|rolle|role'),
        ('diagnosis.qa', r'whisper|stt|qa|transcript|transkript|aussprache'),
        ('diagnosis.evidence', r'beleg|receipt|hash|signature|signatur|binding|bindung|fingerprint|prüfsumme|checksum'),
        ('diagnosis.download', r'download|transfer|http|https|netz|network|paket|sdk|inno|gog|wine|rosetta|extract|extrak'),
        ('diagnosis.busy', r'busy|läuft|laufend|aktiv|paused|pause|running|auftrag|job'),
        ('diagnosis.selection', r'auswahl|selected|selection|kandidat|candidate|warning|warnung|vorherig|übernomm|vorschau'),
        ('diagnosis.manifest', r'manifest|schema|format|json|version|revision|rezept|recipe|dialog|text|seed|backend'),
        ('diagnosis.export', r'build|export|monkey|ute|scumm|intro|flac|patch|spiel'),
    ]
    for topic, pattern in rules:
        if re.search(pattern, value): return topic
    return 'diagnosis.general'


def surface(path):
    if path.startswith('patch_review/'): return 'review'
    if path.startswith('generator_launcher/native/'): return 'native-start'
    return 'launcher'


def controls(bindings, topic_ids):
    """Lexical control inventory; dynamic DOM structure still needs rendered checks."""
    inventory = []
    for relative in ('generator_launcher/native/start.html', 'generator_launcher/host.py',
                     'generator_launcher/launcher_flow.js', 'patch_review/app.js', 'patch_review/index.html'):
        source = (ROOT / relative).read_text()
        for match in re.finditer(r'<(button|input|select|textarea|audio|summary)\b[^>]*>', source):
            attrs = [(k, v or v2) for k, v, v2 in re.findall(r'(id|data-[\w-]+)=(?:"([^"\s]+)"|\'([^\'\s]+)\')', match.group())]
            candidates = [value for name, value in attrs if name in ('data-help-id', 'data-help-open') and value in topic_ids]
            for binding in bindings:
                if surface(relative) != surface(binding['source']): continue
                selector = binding['selector']
                if any(re.search('#' + re.escape(value) + r'(?![\w-])', selector) if name == 'id'
                       else '[' + name in selector for name, value in attrs):
                    candidates.append(binding['topic'])
            inventory.append({'source': relative, 'line': source.count('\n', 0, match.start()) + 1,
                              'tag': match.group(1), 'identities': dict(attrs),
                              'matching_help_ids': sorted(set(candidates)),
                              'needs_context_validation': not bool(candidates)})
    missing = [row for row in inventory if row['identities'] and not row['matching_help_ids']]
    if missing: raise ValueError('Controls without direct help mapping: ' + str(missing))
    return inventory


def build(check=False):
    topics = []
    for path in sorted((WIKI / 'content').glob('*.json')):
        topics.extend(json.loads(path.read_text())['topics'])
    ids = [t['id'] for t in topics]
    if len(ids) != len(set(ids)): raise ValueError('Duplicate topic IDs')
    for topic in topics:
        for required in ('id', 'title', 'category', 'summary', 'body'):
            if not topic.get(required): raise ValueError(f"Missing {required}: {topic.get('id')}")
        if not re.fullmatch(r'[a-z][a-z0-9.-]+', topic['id']): raise ValueError('Invalid help ID: ' + topic['id'])
        for other in topic.get('related', []):
            if other not in ids: raise ValueError(f"Broken cross-reference {topic['id']} -> {other}")
        paragraphs = [topic['summary'], *topic['body'], *topic.get('steps', [])]
        for section in topic.get('sections', []):
            paragraphs += [*section['body'], *section.get('steps', [])]
        for paragraph in paragraphs:
            for match in INLINE_LINK.finditer(paragraph):
                if match[2] not in ids:
                    raise ValueError(f"Broken inline link {topic['id']} -> {match[2]}")
        for section in topic.get('sections', []):
            if not section.get('title') or not section.get('body'):
                raise ValueError('Incomplete section: ' + topic['id'])
            if not all(isinstance(p, str) and p.strip() for p in section['body'] + section.get('steps', [])):
                raise ValueError('Invalid section text: ' + topic['id'])
        for link in topic.get('links', []):
            if not link.get('title') or not re.match(r'^https?://', link.get('url', '')):
                raise ValueError('Invalid source link: ' + topic['id'])
        for binding in topic.get('bindings', []):
            if not (ROOT / binding['source']).is_file(): raise ValueError('Missing binding source: ' + binding['source'])
            if not binding.get('selector'): raise ValueError('Missing binding selector')
    corpus, unresolved = messages()
    # Include curated status/warning texts even where the application composes them outside raise/Error.
    indexed = {record['message'] for record in corpus}
    for topic in topics:
        for message in topic.get('messages', []):
            if message not in indexed:
                corpus.append({'id': 'message.' + hashlib.sha256(message.encode()).hexdigest()[:16],
                               'message': message, 'kind': 'documented', 'locations': []})
                indexed.add(message)
    corpus.sort(key=lambda record: record['message'].casefold())
    for record in corpus:
        exact = [t for t in topics if record['message'] in t.get('messages', [])]
        record['topic'] = exact[0]['id'] if exact else classify(record['message'], topics)
        record['explanation_level'] = 'specific' if exact else 'category'
        if record['topic'] not in ids: raise ValueError('Missing diagnosis topic ' + record['topic'])
    version_tree = ast.parse((ROOT / 'generator_launcher/version.py').read_text())
    version = next(literal(node.value) for node in version_tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == 'LAUNCHER_VERSION' for target in node.targets))
    bindings = [dict(b, topic=t['id']) for t in topics for b in t.get('bindings', [])]
    native_bindings = [dict(b, topic=t['id']) for t in topics for b in t.get('native_bindings', [])]
    inventory = controls(bindings, ids)
    signatures = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in SOURCE_FILES}
    data = {'format': 'mi1-help/1', 'locale': 'de', 'app_version': version, 'topics': topics, 'messages': corpus, 'bindings': bindings, 'native_bindings': native_bindings}
    report = {'format': 'mi1-wiki-coverage/1', 'app_version': version, 'topic_count': len(topics), 'binding_count': len(bindings), 'message_count': len(corpus), 'source_sha256': signatures, 'dynamic_exception_sites': unresolved,
              'limits': ['Meldungsextraktion: Python raise/_require/logger warning/error, statische JS-Error-Texte und native Alerttexte.', 'Dynamische OS-, Fremdtool- und Modellmeldungen werden über Diagnoseklassen erklärt; keine vollständige Liste möglicher externer Texte.', 'Bindings ordnen die lokal mitgelieferte Hilfe zu; dynamische Controls und native Dialoge benötigen zusätzliche Bedienprüfung.']}
    outputs = {
        WIKI / 'site/help-index.json': json.dumps(data, ensure_ascii=False, indent=2) + '\n',
        WIKI / 'site/data.js': 'window.MI1_WIKI = ' + json.dumps(data, ensure_ascii=False).replace('</', '<\\/') + ';\n',
        WIKI / 'coverage.json': json.dumps(report, ensure_ascii=False, indent=2) + '\n',
        WIKI / 'control-inventory.json': json.dumps({'format': 'mi1-controls/1', 'controls': inventory,
            'limits': 'Lexikalische Zuordnung über IDs/data-Attribute; DOM-Hierarchie, deaktivierte Controls, native Menüs und dynamische Strukturen müssen bei Appintegration gerendert geprüft werden.'}, ensure_ascii=False, indent=2) + '\n',
    }
    for topic in topics:
        lines = [f"# {topic['title']}", '', markdown(topic['summary']), '']
        lines += [markdown(p) + '\n' for p in topic['body']]
        for section in topic.get('sections', []):
            lines += ['## ' + section['title'], '']
            lines += [markdown(p) + '\n' for p in section['body']]
            if section.get('steps'):
                lines += [f'{i}. {markdown(s)}' for i, s in enumerate(section['steps'], 1)] + ['']
        if topic.get('steps'):
            lines += ['## Vorgehen', ''] + [f'{i}. {markdown(s)}' for i, s in enumerate(topic['steps'], 1)] + ['']
        if topic.get('messages'):
            lines += ['<details>', '<summary>Meldungen zu diesem Thema nachschlagen</summary>', '']
            lines += ['- ' + m.replace('\n', ' ') for m in topic['messages']] + ['', '</details>', '']
        if topic.get('related'):
            lines += ['## Weiterführende Hilfe', ''] + [f"- [{next(t['title'] for t in topics if t['id'] == other)}]({other}.md)" for other in topic['related']] + ['']
        if topic.get('links'):
            lines += ['## Originalquellen und Bedingungen', ''] + [f"- [{link['title']}]({link['url']})" for link in topic['links']] + ['']
        outputs[WIKI / 'pages' / (topic['id'] + '.md')] = '\n'.join(lines)
    guides = sorted((t for t in topics if 'navigation_order' in t), key=lambda t: t['navigation_order'])
    index = ['# Anleitung zum MI German Talkie Patch', '',
             'Beginne mit [Vom ersten Start zum spielbaren Monkey Island](wiki.start.md). Die Anleitung führt durch alle sechs Schritte des Patchs bis zur Spieleerzeugung und zum Start in ScummVM.', '',
             '## Praktische Anleitungen', '']
    index += [f"- [{t['title']}]({t['id']}.md)" for t in guides]
    index += ['', '## Einzelhilfen zum Nachschlagen', '']
    for category in sorted({t['category'] for t in topics}):
        index += ['### ' + category, '']
        index += [f"- [{t['title']}]({t['id']}.md)" for t in sorted(topics, key=lambda t: t['title'])
                  if t['category'] == category and 'navigation_order' not in t]
        index += ['']
    outputs[WIKI / 'pages/README.md'] = '\n'.join(index)
    outputs[WIKI / 'site/.nojekyll'] = ''
    if check:
        stale = [str(path.relative_to(ROOT)) for path, text in outputs.items() if not path.is_file() or path.read_text() != text]
        if stale: raise ValueError('Wiki neu bauen: ' + ', '.join(stale[:10]))
    else:
        for path, text in outputs.items():
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text, encoding='utf-8')
        # Only generated Markdown lives here; remove renamed/retired generated pages.
        for path in (WIKI / 'pages').glob('*.md'):
            if path not in outputs: path.unlink()
    print(f'{len(topics)} Themen, {len(bindings)} Bindings, {len(corpus)} Meldungen; {len(unresolved)} dynamische Python-Ausnahmen separat ausgewiesen.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--check', action='store_true')
    build(parser.parse_args().check)
