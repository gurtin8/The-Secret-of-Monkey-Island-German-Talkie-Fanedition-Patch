"""Own, narrowly scoped acquisition/build recipes for the MI1 launcher.

No inferred inference, production, EULA acceptance or developer-venv copies.
Transfer follows tools/download_ane_models_resumable.sh: preserve .part,
resume via curl, verify size/hash, activate with a same-directory rename.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import os
import shutil

RECIPES = Path(__file__).with_name('recipes')


def digest(path: Path, kind='sha256') -> str:
    h = hashlib.new('sha1' if kind == 'gitblob' else kind)
    if kind == 'gitblob':
        h.update(f'blob {path.stat().st_size}\0'.encode())
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    root = root.absolute()
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ValueError('unsafe recipe path')
    path = root / rel
    for item in (root, *path.parents, path):
        if item.is_symlink():
            raise ValueError('symlink in recipe path')
    return path


def verify(path: Path, artifact: dict) -> None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size != artifact['bytes']:
        raise ValueError(f'size/type mismatch: {path}')
    if digest(path, artifact.get('integrity_kind', 'sha256')) != artifact['hash']:
        raise ValueError(f'hash mismatch: {path}')


def transfer(artifact: dict, target: Path, *, consent_ids=(), runner=subprocess.run) -> Path:
    """One resumable attempt; caller retries after reporting interruption.

    Catalog consent is not obtained by this function. Native UI must record
    the user's explicit action before passing the corresponding consent ID.
    """
    consent = artifact.get('consent_id')
    if consent and consent not in consent_ids:
        raise ValueError(f'user action required: {consent}')
    target = Path(target).absolute()
    safe_path(target.parent, target.name)
    part = target.with_name(target.name+'.part')
    safe_path(part.parent, part.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        verify(target, artifact)
        return target
    if part.exists() and part.stat().st_size > artifact['bytes']:
        raise ValueError('oversized .part retained')
    if not part.exists() or part.stat().st_size < artifact['bytes']:
        runner(['/usr/bin/curl', '--http1.1', '--location', '--fail', '--show-error',
                '--silent', '--connect-timeout', '30', '--speed-limit', '1024',
                '--speed-time', '120', '--continue-at', '-', '--output', str(part),
                artifact['url']], check=True)
    verify(part, artifact)  # wrong or interrupted bytes remain resumable, never active
    with part.open('rb') as f:
        os.fsync(f.fileno())
    part.replace(target)
    return target


def transform(source: Path, recipe: dict) -> str:
    """Validate every input and output before replacing any source file.

    Inputs are upstream bytes downloaded separately. Recipe ships only own
    insertion text, offsets, delete counts and hashes, never foreign context.
    """
    staged = []
    states = set()
    for entry in recipe['files']:
        path = safe_path(source, entry['path'])
        actual = digest(path) if path.is_file() else None
        if actual == entry['output_sha256']:
            states.add('done')
            continue
        if actual != entry['input_sha256'] or (path.exists() and not path.is_file()):
            raise ValueError(f'unexpected input hash: {path}')
        states.add('input')
        lines = path.read_bytes().splitlines(keepends=True) if path.exists() else []
        for op in reversed(entry['operations']):
            start, count = op['start_line'], op['delete_lines']
            if start < 0 or count < 0 or start+count > len(lines):
                raise ValueError('invalid transform offsets')
            lines[start:start+count] = op['insert'].encode().splitlines(keepends=True)
        output = b''.join(lines)
        if hashlib.sha256(output).hexdigest() != entry['output_sha256']:
            raise ValueError('transformed output hash mismatch')
        staged.append((path, output))
    if len(states) != 1:
        raise ValueError('partial transformation; no changes made')
    if states == {'done'}:
        return 'same'
    for path, output in staged:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.'+path.name, delete=False) as f:
            tmp = Path(f.name)
            try:
                f.write(output); f.flush(); os.fsync(f.fileno())
                if path.exists():
                    tmp.chmod(path.stat().st_mode & 0o777)
                tmp.replace(path)
            finally:
                tmp.unlink(missing_ok=True)
    return 'transformed'


def checkout(source: dict, destination: Path, *, runner=subprocess.run) -> Path:
    """Fetch an exact Git object into staging and atomically activate checkout."""
    destination = destination.absolute()
    safe_path(destination.parent, destination.name)
    revision = source['revision']
    if destination.exists():
        head = runner(['/usr/bin/git','-C',str(destination),'rev-parse','HEAD'],
                      check=True, capture_output=True, text=True).stdout.strip()
        if head != revision:
            raise ValueError('unexpected checkout revision')
        # transformations are separately hash-guarded
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.with_name(destination.name+'.part')
    safe_path(stage.parent, stage.name)
    if not stage.exists():
        stage.mkdir()
        runner(['/usr/bin/git','init',str(stage)],check=True)
        runner(['/usr/bin/git','-C',str(stage),'remote','add','origin',source['url']],check=True)
    origin = runner(['/usr/bin/git','-C',str(stage),'remote','get-url','origin'],check=True,
                    capture_output=True,text=True).stdout.strip()
    if origin != source['url']:
        raise ValueError('unexpected partial checkout origin')
    runner(['/usr/bin/git','-C',str(stage),'fetch','--depth','1','origin',revision],check=True)
    runner(['/usr/bin/git','-C',str(stage),'checkout','--detach',revision],check=True)
    head = runner(['/usr/bin/git','-C',str(stage),'rev-parse','HEAD'],check=True,
                  capture_output=True,text=True).stdout.strip()
    if head != revision:
        raise ValueError('checkout identity mismatch')
    stage.replace(destination)
    return destination


def build_tool(name: str, source: Path, build: Path, *, cmake: Path, prefix: Path,
               runner=subprocess.run) -> None:
    """Concrete C++/audio recipes, after OS toolchain completion; no inference."""
    if name == 'ffmpeg':
        build.mkdir(parents=True,exist_ok=True)
        # FFmpeg's configure and generated makefiles reject whitespace in
        # source/build paths. The user's chosen volume must remain unrestricted.
        # Keep the original checkout on that volume, compile a temporary copy
        # with a whitespace-free prefix, then publish only the finished binaries.
        temp_parent = '/private/tmp' if Path('/private/tmp').is_dir() else None
        with tempfile.TemporaryDirectory(prefix='mi1-ffmpeg-', dir=temp_parent) as temporary:
            stage = Path(temporary).resolve()
            if any(char.isspace() for char in str(stage)):
                raise ValueError('FFmpeg benötigt einen temporären Buildpfad ohne Leerzeichen')
            local_source, local_build, install = stage/'source', stage/'build', stage/'install'
            shutil.copytree(source,local_source,ignore=shutil.ignore_patterns('.git'))
            local_build.mkdir()
            try:
                runner([str(local_source/'configure'), '--prefix='+str(install), '--disable-autodetect',
                        '--disable-doc', '--disable-ffplay', '--disable-debug'],cwd=local_build,check=True)
                runner(['/usr/bin/make','-j','4'],cwd=local_build,check=True)
                runner(['/usr/bin/make','install'],cwd=local_build,check=True)
            except subprocess.CalledProcessError as exc:
                log = local_build/'ffbuild/config.log'
                saved = build/'configure.log'
                if log.is_file():
                    shutil.copy2(log,saved)
                detail = f' Diagnose: {saved}' if log.is_file() else ' configure hat keine config.log erzeugt.'
                raise RuntimeError(f'FFmpeg-Einrichtung fehlgeschlagen (Exit {exc.returncode}).{detail}') from exc
            binaries = [install/'bin'/name for name in ('ffmpeg','ffprobe')]
            if not all(path.is_file() for path in binaries):
                raise RuntimeError('FFmpeg-Build hat ffmpeg/ffprobe nicht vollständig erzeugt')
            destination = prefix/'bin'
            destination.mkdir(parents=True,exist_ok=True)
            for binary in binaries:
                with tempfile.NamedTemporaryFile(dir=destination, prefix='.'+binary.name, delete=False) as stream:
                    pending = Path(stream.name)
                try:
                    shutil.copy2(binary,pending)
                    pending.replace(destination/binary.name)
                finally:
                    pending.unlink(missing_ok=True)
        return
    if name not in {'vox','whisper','scummtr'}:
        raise ValueError('unknown concrete build recipe')
    if name in {'vox','scummtr'}:
        recipe = json.loads((RECIPES/('vox-combined-speed.json' if name=='vox' else 'scummtr-actor.json')).read_text())
        transform(source,recipe)
    flags = ['-DCMAKE_BUILD_TYPE=Release','-DCMAKE_OSX_ARCHITECTURES=arm64']
    target = {'vox':'voxcpm2-cli','whisper':'whisper-cli','scummtr':'scummtr'}[name]
    if name in {'vox','whisper'}:
        # Runtime shader compilation avoids demanding full Xcode Metal tooling.
        flags += ['-DGGML_METAL=ON','-DGGML_METAL_EMBED_LIBRARY=ON','-DGGML_METAL_USE_BF16=OFF']
    if name == 'vox':
        flags += ['-DGGML_CUDA=OFF','-DLLAMA_CURL=OFF','-DFETCHCONTENT_FULLY_DISCONNECTED=ON']
    runner([str(cmake),'-S',str(source),'-B',str(build),*flags],check=True)
    runner([str(cmake),'--build',str(build),'--config','Release','--target',target,'-j','4'],check=True)
    if name == 'scummtr':
        # Both tools come from the same pinned, hash-guarded ScummTR checkout.
        runner([str(cmake),'--build',str(build),'--config','Release','--target','scummrp','-j','4'],check=True)
