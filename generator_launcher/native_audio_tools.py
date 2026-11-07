"""Lazy, hash-bound native SoX build. Called only by explicit game preparation.

Own code/recipe ships with the launcher; upstream sources and locally built
GPL executable stay in the chosen runtime, never in the launcher package.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import struct
import subprocess
import tarfile
import tempfile
import wave

from .setup_recipes import safe_path, transfer

RECIPE = Path(__file__).with_name('recipes') / 'native-audio.json'


def _sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _archive_members(archive, recipe):
    """Reject links/special files, escapes, duplicates and oversized archives."""
    members = archive.getmembers()
    if len(members) > recipe['max_members'] or sum(m.size for m in members) > recipe['max_unpacked_bytes']:
        raise ValueError('SoX archive exceeds extraction budget')
    names = set()
    for member in members:
        path = PurePosixPath(member.name)
        if (path.is_absolute() or '..' in path.parts or not path.parts or
                path.parts[0] != recipe['directory'] or '\\' in member.name or
                member.name in names or not (member.isfile() or member.isdir())):
            raise ValueError(f'Unsafe SoX archive member: {member.name}')
        names.add(member.name)
    return members


def _source_cache(archive_path, cache, recipe):
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = _archive_members(archive, recipe)
        if cache.exists():
            expected = set()
            for member in members:
                if member.isfile():
                    relative = PurePosixPath(member.name).relative_to(recipe['directory'])
                    path = safe_path(cache, str(relative))
                    expected.add(str(relative))
                    if not path.is_file() or path.stat().st_size != member.size:
                        raise ValueError('SoX cached source differs from verified archive')
                    with archive.extractfile(member) as stream:
                        wanted = hashlib.sha256(stream.read()).hexdigest()
                    if _sha(path) != wanted:
                        raise ValueError('SoX cached source hash differs from verified archive')
            actual = {str(p.relative_to(cache)) for p in cache.rglob('*') if p.is_file() or p.is_symlink()}
            if actual != expected:
                raise ValueError('Unexpected files in SoX source cache')
        else:
            with tempfile.TemporaryDirectory(prefix='source-', dir=cache.parent) as name:
                stage = Path(name)
                # Already validated: only regular files/directories under one prefix.
                for member in members:
                    target = stage / member.name
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.extractfile(member) as source, target.open('xb') as dest:
                            shutil.copyfileobj(source, dest)
                        target.chmod(member.mode & 0o777)
                (stage / recipe['directory']).replace(cache)
    return cache


def _patch(source, patch):
    path = source / patch['path']
    if _sha(path) != patch['input_sha256']:
        raise ValueError('SoX portability patch input hash mismatch')
    data = path.read_bytes()
    output = data[:patch['offset']] + patch['insert'].encode() + data[patch['offset']:]
    if hashlib.sha256(output).hexdigest() != patch['output_sha256']:
        raise ValueError('SoX portability patch output hash mismatch')
    path.write_bytes(output)


def _run(argv, *, cwd=None, env=None, log=None):
    env = dict(os.environ if env is None else env)
    env.pop('SOX_OPTS', None)
    return subprocess.run(argv, cwd=cwd, env=env, check=True,
                          stdout=log if log else subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=not bool(log))


def _verify_version(binary, recipe):
    version = _run([str(binary), '--version']).stdout.strip()
    if version != f'{binary}: SoX v{recipe["version"]}':
        # 14.3.1 uses program basename/path in version output.
        if not version.endswith(f': SoX v{recipe["version"]}'):
            raise ValueError(f'Unexpected native SoX version: {version}')
    return version


def _smoke(binary, directory, recipe):
    version = _verify_version(binary, recipe)
    original = directory / 'original.wav'
    with wave.open(str(original), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(22050)
        audio.writeframes(b''.join(struct.pack('<h', (i * 271) % 24001 - 12000) for i in range(2205)))
    changed = directory / 'effects.wav'
    _run([str(binary), '-R', str(original), str(changed), 'trim', '0', '0.05',
          'gain', '-3', 'compand', '0.01,0.1', '-60,-60,-20,-10,0,-5', 'rate', '11025', 'dither'])
    mixed = directory / 'mixed.wav'
    _run([str(binary), '-R', '-m', str(original), str(original), str(mixed), 'gain', '-6'])
    raw = directory / 'audio.raw'
    restored = directory / 'restored.wav'
    _run([str(binary), str(original), '-t', 'raw', str(raw)])
    _run([str(binary), '-t', 'raw', '-r', '22050', '-c', '1', '-b', '16', '-e', 'signed-integer', str(raw), str(restored)])
    with wave.open(str(changed), 'rb') as audio:
        if audio.getframerate() != 11025 or audio.getnchannels() != 1 or not 540 <= audio.getnframes() <= 560:
            raise ValueError('Native SoX rate/trim smoke failed')
    with wave.open(str(original), 'rb') as left, wave.open(str(restored), 'rb') as right:
        if left.readframes(left.getnframes()) != right.readframes(right.getnframes()):
            raise ValueError('Native SoX raw/WAV roundtrip failed')
    return {'version': version, 'effects': ['wav', 'raw', 'rate', 'trim', 'gain', 'mix', 'compand', 'dither'], 'passed': True}


def ensure_native_sox(work_root: Path, progress=None) -> Path:
    """Publish work_root/bin/sox lazily; reject unreceipted/changed installations.

    progress, when supplied, receives concise German status strings. No
    download or compilation occurs at module import or launcher startup.
    """
    root = Path(work_root).absolute()
    recipe_bytes = RECIPE.read_bytes()
    recipe = json.loads(recipe_bytes)
    recipe_sha = hashlib.sha256(recipe_bytes).hexdigest()
    if platform.system() != 'Darwin' or platform.machine() not in ('arm64', 'x86_64'):
        raise ValueError('Native SoX preparation supports macOS arm64/x86_64 only')
    binary = safe_path(root, 'bin/sox')
    foreign = safe_path(root, 'foreign/runtime/native-sox')
    receipt_path = safe_path(foreign, 'receipt.json')
    identity = {'source_sha256': recipe['source']['sha256'], 'recipe_sha256': recipe_sha,
                'build_revision': recipe['build_revision'], 'machine': platform.machine(), 'version': recipe['version']}
    if binary.exists():
        if not receipt_path.is_file():
            raise ValueError('Native SoX exists without a build receipt')
        receipt = json.loads(receipt_path.read_text())
        if any(receipt.get(key) != value for key, value in identity.items()) or receipt.get('binary_sha256') != _sha(binary):
            raise ValueError('Native SoX binary/receipt identity mismatch; changed tool is not executed')
        _verify_version(binary, recipe)
        return binary
    if receipt_path.exists():
        raise ValueError('Native SoX receipt exists but executable is missing')
    foreign.mkdir(parents=True, exist_ok=True)
    lock = safe_path(foreign, 'build.lock')
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise RuntimeError(f'Native SoX build already active (or interrupted lock): {lock}') from exc
    def notify(message):
        if progress:
            progress(message)
    try:
        for directory in (foreign, Path('/private/tmp')):
            if shutil.disk_usage(directory).free < 128 * 1024 * 1024:
                raise ValueError('Native SoX build needs at least 128 MiB free runtime/temporary space')
        notify('Native SoX 14.3.1: offizielle Quelle prüfen/herunterladen')
        archive_path = safe_path(foreign, 'sox-14.3.1.tar.gz')
        artifact = dict(recipe['source'], hash=recipe['source']['sha256'])
        transfer(artifact, archive_path)
        source = _source_cache(archive_path, safe_path(foreign, 'source'), recipe['source'])
        for name, wanted in recipe['license']['files'].items():
            if _sha(source / name) != wanted:
                raise ValueError('Native SoX license identity mismatch')
        with tempfile.TemporaryDirectory(prefix='mi1-native-sox-', dir='/private/tmp') as name:
            temporary = Path(name).resolve()
            if any(char.isspace() for char in str(temporary)):
                raise ValueError('Native SoX requires a whitespace-free temporary build path')
            local_source, build = temporary / 'source', temporary / 'build'
            shutil.copytree(source, local_source)
            _patch(local_source, recipe['portability_patch'])
            build.mkdir()
            env = os.environ.copy()
            env.update(CC='/usr/bin/clang', CFLAGS=recipe['cflags'], CPPFLAGS='', LDFLAGS='', LIBS='', PKG_CONFIG='/usr/bin/false')
            env['CONFIG_SITE'] = '/dev/null'
            env.pop('SOX_OPTS', None)
            env['PATH'] = '/usr/bin:/bin:/usr/sbin:/sbin'
            triple = ('aarch64' if platform.machine() == 'arm64' else 'x86_64') + '-apple-darwin'
            argv = [str(local_source / 'configure'), '--build=' + triple,
                    '--prefix=' + str(temporary / 'unused-install')] + recipe['configure_flags']
            log_path = safe_path(foreign, 'build.log')
            notify('Native SoX 14.3.1: lokal mit Apple clang bauen')
            with log_path.open('w') as log:
                _run(argv, cwd=build, env=env, log=log)
                _run(['/usr/bin/make', '-j', str(min(4, os.cpu_count() or 1))], cwd=build, env=env, log=log)
            compiled = build / 'src/sox'
            deps = _run(['/usr/bin/otool', '-L', str(compiled)]).stdout.splitlines()[1:]
            if any(not line.strip().startswith(('/usr/lib/', '/System/Library/')) for line in deps if line.strip()):
                raise ValueError('Native SoX unexpectedly depends on non-system libraries')
            notify('Native SoX: WAV/raw und UTE-Effekte prüfen')
            smoke = _smoke(compiled, temporary, recipe)
            receipt = dict(identity, binary_sha256=_sha(compiled), source_url=recipe['source']['url'],
                           configure_argv=argv, cflags=recipe['cflags'], license=recipe['license'],
                           portability_patch=recipe['portability_patch'], smoke=smoke)
            binary.parent.mkdir(parents=True, exist_ok=True)
            # Own temporary file in bin: completed executable activated by same-dir rename.
            with tempfile.NamedTemporaryFile(prefix='.sox-', dir=binary.parent, delete=False) as handle:
                staged = Path(handle.name)
                try:
                    with compiled.open('rb') as stream:
                        shutil.copyfileobj(stream, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                except BaseException:
                    staged.unlink(missing_ok=True)
                    raise
            try:
                staged.chmod(0o755)
                # Receipt first: interruption never leaves an unreceipted runnable tool.
                receipt_stage = safe_path(foreign, 'receipt.json.part')
                receipt_stage.write_text(json.dumps(receipt, indent=2) + '\n')
                receipt_stage.replace(receipt_path)
                staged.replace(binary)
            finally:
                staged.unlink(missing_ok=True)
        notify('Native SoX 14.3.1 bereit')
        return binary
    except BaseException as exc:
        error_path = safe_path(foreign, 'last-error.json')
        error_path.write_text(json.dumps(dict(identity, status='error', error=f'{type(exc).__name__}: {exc}',
                                              build_log=str(foreign / 'build.log')), indent=2) + '\n')
        raise
    finally:
        lock.rmdir()
