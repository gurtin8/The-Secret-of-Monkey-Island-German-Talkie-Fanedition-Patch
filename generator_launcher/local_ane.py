"""Same-installation ANE bindings from pinned setup assets; never load a model."""
from __future__ import annotations
import json
import hashlib
from pathlib import Path
import shutil
import subprocess

from .contracts import ContractError, digest, load_or_create_installation
from .setup_recipes import RECIPES, safe_path, verify


def resolve_local_ane(root, *, runner=subprocess.run, read_only=False):
    from manifest_renderer.ane_worker import _path_digest, _path_snapshot, _PATH_DIGEST_CACHE, resolve_bindings
    from manifest_renderer.backend import ANE_RUNTIME_ASSETS
    from manifest_renderer.production import atomic_json
    from .local_preparation import preparation_receipt, verify_preparation_receipt
    root=Path(root).resolve(strict=True)
    from .contracts import load_existing_installation
    installation=load_existing_installation(root) if read_only else load_or_create_installation(root)
    project=json.loads((root/'.launcher/project-id').read_text())['project_id']
    recipe=json.loads((RECIPES/'runtime.json').read_text())
    model_root=safe_path(root,'models/ane-runtime')
    python=root/'work/ane-runtime-venv/bin/python'
    if not python.is_file() or not python.resolve().is_relative_to(root):
        raise ContractError('Lokale ANE-Pythonumgebung fehlt; Paketeinrichtung abschließen: '+str(python))
    assets=[]
    for artifact in recipe['artifacts']:
        aid=artifact['id']
        if aid=='ane-wheel': relative='work/ane_runtime_contract/voxcpmane2-0.1.3b1-py3-none-any.whl'
        elif aid.startswith('ane-preview/'): relative='models/ane-runtime/preview/'+aid.split('/',1)[1]
        elif aid.startswith('ane-split/'): relative='models/ane-runtime/split/'+aid.split('/',1)[1]
        elif aid.startswith('ane-tokenizer/'): relative='models/ane-runtime/tokenizer/'+aid.split('/',1)[1]
        else: continue
        path=safe_path(root,relative)
        if not path.is_file(): raise ContractError('Lokales ANE-Asset fehlt; Paketeinrichtung abschließen: '+str(path))
        assets.append((path,artifact))
    # These are packaged provenance metadata, not third-party model weights.
    source=Path(__file__).parents[1]
    for name in ANE_RUNTIME_ASSETS:
        if name.endswith('.whl'): continue
        origin=source/name;target=safe_path(root,name)
        if not target.is_file() or target.read_bytes()!=origin.read_bytes():
            if read_only: raise ContractError("ANE-Paketmetadaten verändert; Einrichtung erforderlich")
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(origin,target)
    profile=root/'generator_launcher/provenance/models'
    preview=json.loads((profile/'preview.json').read_text())
    fallback=json.loads((profile/'fallback.json').read_text())
    tokenizer=json.loads((profile/'tokenizer.json').read_text())
    versions=runner([str(python),'-c',"from importlib.metadata import version; print(version('voxcpmane2')); print(version('coremltools'))"],
        check=True,capture_output=True,text=True).stdout.splitlines()
    if versions!=['0.1.3b1','9.0']: raise ContractError('Lokale ANE-Versionen passen nicht: '+repr(versions))
    stats={str(path.relative_to(root)):_path_snapshot(path) for path,_ in assets}
    stats['python']=_path_snapshot(python.resolve())
    inputs=dict(root=str(root),recipe_sha256=digest(recipe),file_stats=stats,versions=versions,
        model_tree_stats=_path_snapshot(model_root),
        profile_sha256={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ANE_RUNTIME_ASSETS if not name.endswith('.whl')})
    saved_path=root/'.launcher/ane-worker.json'
    if saved_path.is_file():
        saved=json.loads(saved_path.read_text())
        expected={**inputs,'config':saved.get('config')}
        if verify_preparation_receipt(saved.get('receipt',{}),installation,project_id=project,inputs=expected,work_root=root):
            config=saved['config']
            # Only signed, same-host, unchanged stat snapshots can seed this cache.
            for receipt in [*config['model_paths'].values(),config['tokenizer']]:
                for item in receipt if isinstance(receipt,list) else [receipt]:
                    path=model_root/item['path']
                    _PATH_DIGEST_CACHE[str(path.absolute())]=(_path_snapshot(path),item['sha256'])
            resolve_bindings(config,verify_hashes=True)
            return config
    if read_only: raise ContractError("Aktueller ANE-Einrichtungsbeleg fehlt")
    expected_files={path for path,_ in assets if path.is_relative_to(model_root)}
    actual_files={path for path in model_root.rglob('*') if path.is_file()}
    if actual_files!=expected_files:
        raise ContractError('ANE-Modellverzeichnis enthält nicht katalogisierte oder fehlende Dateien: '+str(model_root))
    for path,artifact in assets: verify(path,artifact)
    cache=safe_path(root,'generated/ane-voice-cache');cache.mkdir(parents=True,exist_ok=True)
    names=dict(model_dir='preview',base_lm_path=['split/base_lm_multifunction_part0_of_2.mlmodelc','split/base_lm_multifunction_part1_of_2.mlmodelc'],
        residual_lm_path='preview/residual_lm_fused_multifunction.mlmodelc',locdit_path='preview/locdit_p4_c4.mlmodelc',
        vae_encoder_path='preview/audio_vae_encoder.mlmodelc',feat_encoder_path='preview/feat_encoder.mlmodelc',
        vae_decoder_path='preview/audio_vae_decoder_lf4.mlmodelc',fsq_path='preview/fsq_s4.mlmodelc',
        projections_path='preview/projections.mlmodelc',embedding_path='preview/embed_tokens.npy')
    def bind(name): return dict(path=name,sha256=_path_digest(model_root/name))
    config=dict(model_root=str(model_root),cache_dir=str(cache),split_base_lm=True,
        model_paths={key:[bind(n) for n in value] if isinstance(value,list) else bind(value) for key,value in names.items()},
        tokenizer=bind('tokenizer/tokenizer.json'),decoder_context_mode='prompt_context',
        model_revisions=dict(model_id=preview['id'],version=preview['sha'],fallback_model_id=fallback['id'],
            fallback_version=fallback['sha'],tokenizer_version=tokenizer['sha']),
        worker_command=[str(python),'-m','manifest_renderer.ane_worker_process','--config','{config}'])
    resolve_bindings(config,verify_hashes=True)
    receipt=preparation_receipt(installation,project_id=project,inputs={**inputs,'config':config},outputs={'python':dict(path=python.resolve().relative_to(root).as_posix(),sha256=hashlib.sha256(python.resolve().read_bytes()).hexdigest())})
    atomic_json(saved_path,dict(format='launcher-local-ane/1',config=config,receipt=receipt))
    return config
