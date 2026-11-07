"""Local help assets, allowlist and packaged entry points; no models or jobs."""
import ast
import json
from pathlib import Path
from patch_review.http import dispatch
ROOT = Path(__file__).resolve().parents[1]
ASSETS = ('index.html','data.js','help.js','help.css','wiki.js','wiki.css','app-help.js','help-index.json',
          *(f'status-{kind}.svg' for kind in ('manual','clean','repaired','open','warning','invalid','active')))


def test_help_dispatch_without_review_or_models():
    for name in ASSETS:
        result = dispatch(None, 'GET', '/wiki/'+name)
        assert result[0] == 200
        assert result[1] == (ROOT/'docs/wiki/site'/name).read_bytes()
    for name in ('../content/core.json', '../README.md', 'coverage.json', '%2e%2e/README.md', 'absent.js'):
        assert dispatch(None, 'GET', '/wiki/'+name)[0] == 404


def test_all_entry_points_load_local_overlay():
    tree = ast.parse((ROOT/'generator_launcher/host.py').read_text())
    page = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id=='PAGE' for t in n.targets))
    for html, prefix in [((ROOT/'generator_launcher/native/start.html').read_text(), '../../docs/wiki/site/'),
                         (page, '/wiki/'), ((ROOT/'patch_review/index.html').read_text(), '/wiki/')]:
        assert 'data-help-source=' in html
        for name in ('data.js','help.js','app-help.js','help.css'): assert prefix+name in html
    assert 'Shift+Klick' in (ROOT/'docs/wiki/site/app-help.js').read_text()
    adapter=(ROOT/'docs/wiki/site/app-help.js').read_text()
    assert "new URL('index.html', scriptURL)" in adapter and 'wikiURL});' in adapter
    assert 'mi1-help-launch' not in page


def test_distribution_contains_complete_wiki():
    from generator_launcher.package_native import own_files
    packaged = own_files()
    for name in ASSETS: assert 'docs/wiki/site/'+name in packaged


def test_review_status_signs_are_indexed_linked_and_locally_available():
    data=json.loads((ROOT/'docs/wiki/site/help-index.json').read_text())
    topics={topic['id']:topic for topic in data['topics']}
    topic=topics['review.status-signs']
    assert topic['title']=='Review-Statuszeichen und Arbeitslisten'
    assert 'review.status-signs' in topics['review.filters']['related']
    assert 'review.status-signs' in topics['review.good']['related']
    body=' '.join(topic['body'])
    for value in ('Manuell bestätigt','Automatisch unauffällig','Automatisch repariert','Shift+Klick','F1','Vorschau'):
        assert value in body
    assert any(binding['source']=='patch_review/app.js' and binding['selector']=='[data-review-status]' for binding in topic['bindings'])
    assert (ROOT/'docs/wiki/pages/review.status-signs.md').is_file()
    assert 'review.status-signs.md' in (ROOT/'docs/wiki/pages/README.md').read_text()
    for kind in ('manual','clean','repaired','open','warning','invalid','active'):
        assert '{{review-status:'+kind+'}}' in body
        assert f'../site/status-{kind}.svg' in (ROOT/'docs/wiki/pages/review.status-signs.md').read_text()
