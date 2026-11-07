"""Acquire a pinned, isolated ARM64 GOG Inno extractor in the work directory.

Only acquisition recipes are shipped. No brew, PATH search, system install,
installer execution, or import-time downloads are used.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import subprocess
import tarfile
import tempfile

from .contracts import ContractError, digest
from .local_preparation import _fetch_original, _sha
from .setup_recipes import safe_path

RECIPE = Path(__file__).with_name('recipes') / 'gog-extractor.json'
RELATIVE_HOME = 'MI1 Generator/foreign/preparation/gog-extractor'
SYSTEM_PREFIXES = ('/usr/lib/', '/System/Library/')


def gog_tool_downloads() -> dict:
    return {a['id']: dict(a) for a in json.loads(RECIPE.read_text())['artifacts']}


def fetch_gog_tool_artifact(artifact, target, *, runner=subprocess.run) -> Path:
    """Resume and hash-check public GHCR blobs without exposing auth tokens."""
    target = Path(target).absolute()
    safe_path(target.parent, target.name)
    if not artifact['url'].startswith('https://ghcr.io/v2/homebrew/core/'):
        return _fetch_original(artifact, target, runner)
    # Reuse complete downloads without a network request.
    if target.exists():
        if target.is_file() and target.stat().st_size == artifact['bytes'] and _sha(target) == artifact['sha256']:
            return target
        raise ContractError('GOG-Werkzeugdownload stimmt nicht mit dem Rezept überein: ' + target.name)
    formula = artifact['formula']
    if not formula or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789@-.' for c in formula):
        raise ContractError('Ungültiges GHCR-Originalrezept')
    token_url = f'https://ghcr.io/token?service=ghcr.io&scope=repository:homebrew/core/{formula}:pull'
    # capture_output prevents token data from appearing in the process log.
    result = runner(['/usr/bin/curl', '--fail', '--silent', '--show-error', '--connect-timeout', '30', token_url],
                    check=True, capture_output=True, text=True)
    try:
        token = json.loads(result.stdout)['token']
        if not isinstance(token, str) or not token or any(c in token for c in '\r\n"\\'):
            raise ValueError('invalid token')
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError('Öffentliche GHCR-Downloadfreigabe fehlt') from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    # Authorization goes in a private ephemeral file, never argv or a receipt.
    with tempfile.NamedTemporaryFile(mode='w', dir=target.parent, prefix='.ghcr-', delete=False) as stream:
        config = Path(stream.name)
        os.chmod(config, 0o600)
        stream.write('header = "Authorization: Bearer ' + token + '"\n')
    def authenticated(args, **kwargs):
        return runner([args[0], '--config', str(config), *args[1:]], **kwargs)
    try:
        return _fetch_original(artifact, target, authenticated)
    finally:
        config.unlink(missing_ok=True)


def _member_map(bundle):
    """Validate names/types/links before reading any selected archive member."""
    members = {}
    for member in bundle.getmembers():
        name = member.name.rstrip('/')
        p = PurePosixPath(name)
        if not name or p.is_absolute() or '..' in p.parts or '\\' in name or name in members:
            raise ContractError('Unsicherer oder doppelter GOG-Werkzeugarchivpfad')
        if not (member.isfile() or member.isdir() or member.issym()):
            raise ContractError('Nicht unterstützter GOG-Werkzeugarchivtyp')
        if member.issym():
            link = PurePosixPath(member.linkname)
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), member.linkname))
            if link.is_absolute() or '\\' in member.linkname or resolved.startswith('../') or resolved == '..':
                raise ContractError('GOG-Werkzeugarchiv enthält einen ausbrechenden Link')
        members[name] = member
    return members


def _copy_member(bundle, members, name, target, *, executable=False):
    seen = set()
    while True:
        if name in seen or name not in members:
            raise ContractError('GOG-Werkzeugarchivmitglied fehlt oder Linkzyklus: ' + name)
        seen.add(name)
        member = members[name]
        if member.issym():
            name = posixpath.normpath(posixpath.join(posixpath.dirname(name), member.linkname))
            continue
        if not member.isfile():
            raise ContractError('GOG-Werkzeugarchivmitglied ist keine Datei: ' + name)
        stream = bundle.extractfile(member)
        if stream is None:
            raise ContractError('GOG-Werkzeugarchivmitglied ist unlesbar: ' + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with stream, target.open('xb') as output:
            shutil.copyfileobj(stream, output)
        target.chmod(0o755 if executable else 0o644)
        return


def _dependencies(path, runner):
    result = runner(['/usr/bin/otool', '-L', str(path)], check=True, capture_output=True, text=True)
    return [line.strip().split(' (compatibility version', 1)[0]
            for line in result.stdout.splitlines()[1:] if line.strip()]


def _verify_dependencies(path, installed, runner):
    """Every non-system load must resolve to a regular file in our own tree."""
    for dependency in _dependencies(path, runner):
        if dependency.startswith(SYSTEM_PREFIXES):
            continue
        if not dependency.startswith('@loader_path/'):
            raise ContractError('GOG-Werkzeug hat eine nicht isolierte Bibliothek: ' + dependency)
        resolved = (path.parent / dependency.removeprefix('@loader_path/')).resolve(strict=True)
        if not resolved.is_relative_to(installed) or not resolved.is_file() or resolved.is_symlink():
            raise ContractError('GOG-Werkzeugbibliothek liegt außerhalb der lokalen Installation')


def _verify_installed(destination, recipe, runner):
    receipt_path = safe_path(destination, 'receipt.json')
    try:
        receipt = json.loads(receipt_path.read_text())
        if receipt['recipe_sha256'] != digest(recipe) or receipt['format'] != recipe['format']:
            raise ContractError('GOG-Werkzeugrezept wurde geändert; Werkzeug erneut einrichten')
        outputs = receipt['outputs']
        if 'bin/innoextract' not in outputs or not any(k.startswith('licenses/boost/') for k in outputs):
            raise ContractError('GOG-Werkzeugbeleg ist unvollständig')
        for relative, sha in outputs.items():
            path = safe_path(destination, relative)
            if not path.is_file() or _sha(path) != sha:
                raise ContractError('Isoliertes GOG-Werkzeug wurde verändert: ' + relative)
            if relative.startswith(('bin/', 'lib/')):
                _verify_dependencies(path, destination, runner)
    except ContractError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ContractError('Isolierter GOG-Werkzeugbeleg fehlt oder ist ungültig') from exc
    return destination / 'bin/innoextract'


def acquire_gog_extractor(work_root, *, runner=subprocess.run) -> Path:
    """Acquire exactly this recipe; return only the verified isolated binary."""
    root = Path(work_root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ContractError('GOG-Werkzeug benötigt ein reguläres Arbeitsverzeichnis')
    home = safe_path(root, RELATIVE_HOME)
    recipe = json.loads(RECIPE.read_text())
    destination = safe_path(home, 'installed')
    if destination.exists():
        return _verify_installed(destination, recipe, runner)
    home.mkdir(parents=True, exist_ok=True)
    archives = {a['id']: fetch_gog_tool_artifact(a, safe_path(home, a['name']), runner=runner)
                for a in recipe['artifacts']}
    stage = Path(tempfile.mkdtemp(dir=home, prefix='.install-'))
    bundles = []
    try:
        # Index upstream archive content without extracting foreign headers or tools.
        library_members = {}
        for artifact in recipe['artifacts']:
            if artifact['formula'] == 'boost-license':
                target = stage / 'licenses/boost/LICENSE_1_0.txt'
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(archives[artifact['id']], target)
                continue
            bundle = tarfile.open(archives[artifact['id']], 'r:gz')
            bundles.append(bundle)
            members = _member_map(bundle)
            prefix = artifact['root'] + '/lib/'
            for name in members:
                if name.startswith(prefix) and name.endswith('.dylib'):
                    basename = PurePosixPath(name).name
                    if basename in library_members:
                        raise ContractError('Mehrdeutige GOG-Werkzeugbibliothek: ' + basename)
                    library_members[basename] = (bundle, members, name)
            for name in artifact['licenses']:
                _copy_member(bundle, members, artifact['root'] + '/' + name,
                             stage / 'licenses' / artifact['formula'] / name)
            if artifact['formula'] == 'innoextract':
                _copy_member(bundle, members, recipe['entrypoint'], stage / 'bin/innoextract', executable=True)
        pending = [stage / 'bin/innoextract']
        installed_paths = []
        while pending:
            path = pending.pop(0)
            if path in installed_paths:
                continue
            installed_paths.append(path)
            for dependency in _dependencies(path, runner):
                if dependency.startswith(SYSTEM_PREFIXES):
                    continue
                basename = PurePosixPath(dependency).name
                # Only known original bottle load conventions are accepted.
                if not (dependency.startswith(('@loader_path/', '@@HOMEBREW_PREFIX@@/opt/', '/opt/homebrew/opt/'))):
                    raise ContractError('Unbekannte GOG-Werkzeugbibliothek: ' + dependency)
                if basename not in library_members:
                    raise ContractError('GOG-Werkzeugbibliothek fehlt im Rezept: ' + basename)
                lib = stage / 'lib' / basename
                if not lib.exists():
                    _copy_member(*library_members[basename], lib, executable=True)
                    pending.append(lib)
                replacement = ('@loader_path/../lib/' if path.parent.name == 'bin' else '@loader_path/') + basename
                if dependency != replacement:
                    runner(['/usr/bin/install_name_tool', '-change', dependency, replacement, str(path)], check=True,
                           capture_output=True, text=True)
            if path.parent.name == 'lib':
                runner(['/usr/bin/install_name_tool', '-id', '@loader_path/' + path.name, str(path)], check=True,
                       capture_output=True, text=True)
        for path in installed_paths:
            runner(['/usr/bin/codesign', '--force', '--sign', '-', str(path)], check=True, capture_output=True, text=True)
            _verify_dependencies(path, stage, runner)
        # No inference and no Windows installer execution: only the tool version.
        runner([str(stage / 'bin/innoextract'), '--version'], check=True, capture_output=True, text=True)
        outputs = {p.relative_to(stage).as_posix(): _sha(p) for p in stage.rglob('*') if p.is_file()}
        (stage / 'receipt.json').write_text(json.dumps(dict(format=recipe['format'],
            recipe_sha256=digest(recipe), outputs=outputs), indent=2) + '\n')
        stage.rename(destination)
        return destination / 'bin/innoextract'
    finally:
        for bundle in bundles:
            bundle.close()
        if stage.exists():
            shutil.rmtree(stage)
