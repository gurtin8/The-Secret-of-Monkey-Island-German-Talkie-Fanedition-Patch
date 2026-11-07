"""Explicit original-source runtime setup using P17's pinned helper recipes."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

from .contracts import ContractError, digest
from .setup_recipes import RECIPES, build_tool, checkout, transfer


def install_runtime(root, *, runner=subprocess.run, phase=None):
    """No PATH/developer tool/model fallback, and no model invocation."""
    root=Path(root).resolve(strict=True)
    # Reuse only locally bound, recipe-matching and hash-verified tools/models.
    if (root / '.launcher/runtime.json').is_file():
        if phase: phase('verify_runtime', 'vorhandene Arbeitsumgebung prüfen')
        try:
            existing = verify_installed_runtime(root)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Incomplete or changed installation needs the explicit setup.
        else:
            if phase: phase('runtime_reused', 'geprüfte Arbeitsumgebung wiederverwenden')
            return existing
    recipe=json.loads((RECIPES/'runtime.json').read_text())
    catalog={a['id']:a for a in recipe['artifacts']}
    # Toolchain is an OS action; detect it, never invent or auto-accept terms.
    runner(['/usr/bin/xcrun','--find','clang'],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    foreign=root/'foreign/runtime';foreign.mkdir(parents=True,exist_ok=True)
    cmake_archive=transfer(catalog['cmake'],foreign/'cmake.tar.gz',runner=runner)
    cmake_home=foreign/'cmake'
    if not cmake_home.exists():
        with tarfile.open(cmake_archive) as archive: archive.extractall(cmake_home,filter='data')
    candidates=list(cmake_home.rglob('CMake.app/Contents/bin/cmake'))
    if len(candidates)!=1: raise ContractError('Original-CMake-Binary fehlt oder ist mehrdeutig')
    cmake=candidates[0]
    installed={}
    targets={'vox':('voxcpm2-cli','bin/voxcpm2_cli_speed'),
             'whisper':('whisper-cli','work/whisper.cpp/build/bin/whisper-cli')}
    for name in ('ffmpeg','vox','whisper','scummtr'):
        if phase: phase('build', name)
        source=checkout(catalog[{'vox':'vox-source','whisper':'whisper-source','ffmpeg':'ffmpeg-source',
                                'scummtr':'scummtr-source'}[name]],foreign/(name+'-source'),runner=runner)
        build=foreign/(name+'-build')
        build_tool(name,source,build,cmake=cmake,prefix=root,runner=runner)
        if name=='ffmpeg':
            for binary in ('ffmpeg','ffprobe'):
                target=root/'bin'/binary
                if not target.is_file(): raise ContractError('Lokaler FFmpeg-Build erzeugte '+binary+' nicht')
                installed['bin/'+binary]=target
        elif name == 'scummtr':
            for binary in ('scummtr','scummrp'):
                paths=[p for p in build.rglob(binary) if p.is_file() and not p.is_symlink()]
                if len(paths)!=1: raise ContractError('Buildbinary fehlt/mehrdeutig: '+binary)
                target=root/'bin'/binary;target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(paths[0],target)
                installed['bin/'+binary]=target
        else:
            binary,relative=targets[name]
            paths=[p for p in build.rglob(binary) if p.is_file() and not p.is_symlink()]
            if len(paths)!=1: raise ContractError('Buildbinary fehlt/mehrdeutig: '+binary)
            target=root/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(paths[0],target)
            installed[relative]=target
    for artifact_id,relative in [('vox-baselm','models/voxcpm2/VoxCPM2-BaseLM-F16.gguf'),
        ('vox-acoustic','models/voxcpm2/VoxCPM2-Acoustic-F16.gguf'),
        ('whisper-base','models/asr/ggml-base.bin'),('whisper-medium','models/asr/ggml-medium.bin')]:
        installed[relative]=transfer(catalog[artifact_id],root/relative,runner=runner)
    from manifest_renderer.audio import sha256_file
    from manifest_renderer.production import atomic_json
    result=dict(format='launcher-installed-runtime/1',recipe_sha256=digest(recipe),
        artifacts=[dict(path=relative,sha256=sha256_file(path)) for relative,path in installed.items()])
    from .contracts import load_or_create_installation
    from .local_preparation import preparation_receipt
    installation=load_or_create_installation(root)
    project=json.loads((root/'.launcher/project-id').read_text())
    result['local_receipt']=preparation_receipt(installation,project_id=project['project_id'],
        inputs={'recipe_sha256':result['recipe_sha256']},outputs={a['path']:a for a in result['artifacts']})
    atomic_json(root/'.launcher/runtime.json',result)
    return result


def verify_installed_runtime(root, *, installation=None):
    from manifest_renderer.audio import sha256_file
    from .contracts import resolve_relative
    value=json.loads((Path(root)/'.launcher/runtime.json').read_text())
    if value.get('format')!='launcher-installed-runtime/1': raise ContractError('Runtimeeinrichtungsbeleg fehlt')
    active=json.loads((RECIPES/'runtime.json').read_text())
    if value.get('recipe_sha256')!=digest(active): raise ContractError('Runtimequellenrezept wurde geändert; Einrichtung erneut ausdrücklich ausführen')
    required={'bin/ffmpeg','bin/ffprobe','bin/scummtr','bin/scummrp',
        'bin/voxcpm2_cli_speed','work/whisper.cpp/build/bin/whisper-cli',
        'models/voxcpm2/VoxCPM2-BaseLM-F16.gguf','models/voxcpm2/VoxCPM2-Acoustic-F16.gguf',
        'models/asr/ggml-base.bin','models/asr/ggml-medium.bin'}
    if not required <= {a['path'] for a in value['artifacts']}:
        raise ContractError('Runtimebeleg für benötigte Werkzeuge/Modelle fehlt; Einrichtung erneut ausdrücklich ausführen')
    for artifact in value['artifacts']:
        path=resolve_relative(root,artifact['path'],must_exist=True)
        if sha256_file(path)!=artifact['sha256']: raise ContractError('Eingerichtetes Runtimeasset verändert: '+artifact['path'])
    from .contracts import load_or_create_installation
    from .local_preparation import verify_preparation_receipt
    installation=installation or load_or_create_installation(root)
    project=json.loads((Path(root)/'.launcher/project-id').read_text())
    if not verify_preparation_receipt(value['local_receipt'],installation,project_id=project['project_id'],
        inputs={'recipe_sha256':value['recipe_sha256']},work_root=root): raise ContractError('Runtimebeleg stammt nicht von dieser lokalen Installation')
    return value


def install_own_code(root):
    """Only packaged own implementation; no user data or third-party payload."""
    from manifest_renderer.production import CODE_FILES
    from manifest_renderer.audio import sha256_file
    from .contracts import resolve_relative
    source=Path(__file__).parents[1]
    for name in dict.fromkeys(CODE_FILES):
        origin=source/name
        if not origin.is_file(): raise ContractError('Eigenes Paketmodul fehlt: '+name)
        target=resolve_relative(root,name)
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists() or sha256_file(target)!=sha256_file(origin): shutil.copy2(origin,target)
