"""Keep the published setup and app on the native preparation path."""
from pathlib import Path

from generator_launcher.package_native import own_files
from generator_launcher.setup_dashboard import catalogue
from tools.package_publication import publication_sources


def test_public_package_omits_sdk_notices():
    sdk_notices = {'sdk-eula.txt', 'sdk-redist.txt'}

    for files in (own_files(), publication_sources()):
        assert sdk_notices.isdisjoint(Path(name).name for name in files)


def test_setup_catalogue_keeps_native_game_dependencies(tmp_path):
    rows = catalogue(tmp_path, gog_installer=True)
    packages = {row['id'] for row in rows}

    assert {'preparation/ute', 'preparation/wine', 'preparation/german_patch'} <= packages
    assert {'preparation/sdk', 'preparation/7zz'}.isdisjoint(packages)
    assert any(row['kind'] == 'gog_tool' for row in rows)
