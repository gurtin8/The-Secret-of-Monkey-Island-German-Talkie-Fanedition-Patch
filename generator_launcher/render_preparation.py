"""Materialize the selected portable recipes through P19's real audio core."""
from __future__ import annotations
import copy
import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import shutil
import sys

from .contracts import ContractError, digest, resolve_relative
from .local_preparation import (materialize_preparation, materialize_role_audio,
    preparation_receipt, resolve_preparation, verify_preparation_receipt)


PREWARM_MIN_MS = 2000
PREWARM_MAX_MS = 5000
PREWARM_EXTRA_ATTEMPTS = 3
# Shipped legacy IDs retained after their text was replaced by neutral:4.
LEGACY_NEUTRAL_FOUR_IDS = frozenset('text:' + value for value in (
    '7a1592cd177d8caa17d9b0c465231a2d91ac372b0ea5aad4e4853aa7d072a5fd',
    'e8669668d38dc10bfd7f8730f9ab1aa59bb69e0ac987ff853f83a5a1cf1963b3',
    '1197f3483c5cb8b69477fc4364b22d059dd2a845860f6544f832118c9d981ffc',
    '663987cc2cbd36e2078b6c6629ce13a84a5824fa172e6d4491b958ec02e06008',
    '49bf165d958139ff89b8caf27b53e9ed856efc030c52c84cfab440833304c4ff',
    'a7b535b3a4f581b55171e78369b4fd0f3c3031572a9bef82a3ad28c7ee2df79f'))


def normalize_prewarm_recipe(recipe):
    """Resolve the exact legacy text-hash alias; never change text or seed."""
    from manifest_renderer.preparation_core import NEUTRAL_PREWARM_TEXTS
    result = copy.deepcopy(recipe)
    if not isinstance(result, dict) or result.get('source') != 'neutral_standard':
        return result
    bank = {text: f'neutral:{i}' for i, text in enumerate(NEUTRAL_PREWARM_TEXTS, 1)}
    text = result.get('text')
    target = bank.get(text) if isinstance(text, str) else None
    if target and result.get('neutral_version') == 'mi1-neutral-prewarm/2' and result.get('target_id') == target:
        return result
    if (target and 'neutral_version' not in result
            and (result.get('target_id') == 'text:' + hashlib.sha256(text.encode()).hexdigest()
                 or target == 'neutral:4' and result.get('target_id') in LEGACY_NEUTRAL_FOUR_IDS)):
        result.update(target_id=target, neutral_version='mi1-neutral-prewarm/2')
        return result
    raise ContractError('Neutraler Prewarm weicht von fester Bank/Version ab')


def _prewarm_check(audio, report, *, reference, prewarm, fingerprint, verified):
    """Check the actual frames and original core report, never clamp metadata."""
    from manifest_renderer import preparation_core as core
    from manifest_renderer.audio import validate_wav
    info = validate_wav(audio)
    record = json.loads(report.read_text())
    target = dict(speech_id=prewarm['target_id'], german_text=prewarm['text'], audio=None)
    identity = core.build_render_identity(fingerprint, 'prewarm', prewarm['seed'], target,
                                         core.digest_file(reference), verified)
    expected = identity.pop('fingerprint')
    prior = {k: v for k, v in record.get('verified_inputs', {}).items() if k != 'verified_input_fingerprint'}
    if (record.get('status') != 'ok' or record.get('output_sha256') != info['sha256']
            or record.get('selection_fingerprint') != fingerprint
            or record.get('target_text') != prewarm['text']
            or record.get('target_text_sha256') != hashlib.sha256(prewarm['text'].encode()).hexdigest()
            or not (record.get('fingerprint') == expected or prior == identity)):
        raise ContractError('Prewarm-Erzeugungsreport passt nicht zu Stimme, Text, Seed oder WAV: ' + str(report))
    # Frame bounds avoid accepting 1999.9 ms merely rounded to 2000 ms.
    valid = PREWARM_MIN_MS * 48 <= info['frames'] <= PREWARM_MAX_MS * 48
    return info, valid


def _role_evidence(root, folder, inputs, installation, project_id, *, write=False):
    """An invalid base is signed only as attempt evidence, never a valid role."""
    from manifest_renderer.audio import sha256_file
    from manifest_renderer.production import immutable_json
    path = folder / 'prewarm-attempts' / 'base-receipt.json'
    evidence_inputs = dict(role_inputs=inputs, purpose='prewarm_base_attempt/1')
    if path.exists():
        prior = json.loads(path.read_text())
        if not verify_preparation_receipt(prior, installation, project_id=project_id,
                inputs=evidence_inputs, work_root=root):
            raise ContractError('Prewarm-Basisversuchsbeleg ungültig')
        return True
    if write:
        outputs = {name: dict(path=(folder/name).relative_to(root).as_posix(), sha256=sha256_file(folder/name))
                   for name in ('reference.wav', 'reference.json', 'prewarm.wav', 'prewarm.json')}
        path.parent.mkdir(parents=True, exist_ok=True)
        immutable_json(path, preparation_receipt(installation, project_id=project_id,
            inputs=evidence_inputs, outputs=outputs))
        return True
    return False


def _select_prewarm(root, folder, inputs, prewarm, verified, *, installation, project_id,
                    cli, ffmpeg, models, progress=None, runner=None):
    """Keep every base/attempt immutable, select at most three same-voice retries."""
    from manifest_renderer import preparation_core as core
    from manifest_renderer.audio import sha256_file
    from manifest_renderer.production import immutable_json
    class RunnerSession:
        def run(self,command,stream):return runner(command,stream)
    sessions=RunnerSession() if runner is not None else None
    reference = folder / 'reference.wav'; fingerprint = digest(inputs)
    if (folder / 'prewarm-attempts' / 'base-receipt.json').exists():
        _role_evidence(root,folder,inputs,installation,project_id)
    durations = []
    policy = dict(format='launcher-prewarm-bounds/1', minimum_ms=PREWARM_MIN_MS,
                  maximum_ms=PREWARM_MAX_MS, additional_attempts=PREWARM_EXTRA_ATTEMPTS)
    used_seeds = {prewarm['seed']}
    for attempt in range(1 if prewarm.get('exact_seed') else PREWARM_EXTRA_ATTEMPTS + 1):
        recipe = copy.deepcopy(prewarm)
        if attempt:
            seed = int(digest(dict(role_inputs=inputs, policy=policy, attempt=attempt))[:8], 16) % 2**31
            while seed in used_seeds: seed = (seed + 1) % 2**31
            used_seeds.add(seed); recipe['seed'] = seed
            attempt_folder = folder / 'prewarm-attempts' / f'{attempt:02d}'
        else:
            attempt_folder = folder
        output = attempt_folder / 'prewarm.wav'; report = attempt_folder / 'prewarm.json'
        attempt_inputs = dict(role_inputs=inputs, policy=policy, attempt=attempt, seed=recipe['seed'],
                              reference_sha256=sha256_file(reference))
        attempt_receipt = attempt_folder / 'attempt-receipt.json'
        def core_progress(stage, kind, speech_id, log=None):
            if progress: progress(dict(phase='preparation_prewarm', preparation_attempt=attempt + 1,
                preparation_detail=stage, preparation_duration_ms=None, log_path=str(log) if log else None))
        if attempt:
            if attempt_receipt.exists():
                if not verify_preparation_receipt(json.loads(attempt_receipt.read_text()), installation,
                        project_id=project_id, inputs=attempt_inputs, work_root=root):
                    raise ContractError('Prewarm-Versuchsbeleg ungültig: ' + str(attempt_receipt))
            else:
                # The core validates its own resume identity and refuses to overwrite.
                core.render_one(root, cli, ffmpeg, models, reference,
                    dict(speech_id=recipe['target_id'], german_text=recipe['text'], audio=None),
                    output, report, fingerprint, 'prewarm', recipe['seed'], verified,
                    **({'progress': core_progress} if progress else {}),
                    **({'sessions':sessions} if sessions is not None else {}))
        info, valid = _prewarm_check(output, report, reference=reference,
            prewarm=recipe, fingerprint=fingerprint, verified=verified)
        durations.append(info['frames'] * 1000 / info['sample_rate_hz'] if info['frames'] % 48 else info['duration_ms'])
        if progress: progress(dict(phase='preparation_prewarm', preparation_attempt=attempt + 1,
            preparation_detail='Dauer geprüft' if valid else 'Dauer außerhalb 2000–5000 ms',
            preparation_duration_ms=info['duration_ms'], log_path=None))
        if attempt and not attempt_receipt.exists():
            outputs = {name: dict(path=p.relative_to(root).as_posix(), sha256=sha256_file(p))
                       for name,p in (('audio',output),('report',report))}
            immutable_json(attempt_receipt, preparation_receipt(installation, project_id=project_id,
                inputs=attempt_inputs, outputs=outputs))
        if valid:
            evidence = dict(attempt=attempt, seed=recipe['seed'], duration_ms=info['duration_ms'],
                            audio_path=output.relative_to(root).as_posix(),
                            report_path=report.relative_to(root).as_posix())
            if attempt:
                selected = folder / 'prewarm-attempts' / 'selected-receipt.json'
                selection_inputs = dict(role_inputs=inputs, policy=policy, selected=evidence)
                if selected.exists():
                    if not verify_preparation_receipt(json.loads(selected.read_text()), installation,
                            project_id=project_id, inputs=selection_inputs, work_root=root):
                        raise ContractError('Prewarm-Auswahlbeleg ungültig')
                else:
                    immutable_json(selected, preparation_receipt(installation, project_id=project_id,
                        inputs=selection_inputs, outputs=dict(audio=dict(path=evidence['audio_path'],sha256=info['sha256']),
                        report=dict(path=evidence['report_path'],sha256=sha256_file(report)))))
                evidence['selection_receipt'] = selected.relative_to(root).as_posix()
            return output, report, evidence
    if prewarm.get('exact_seed'):
        raise ContractError('Exaktes importiertes Vorwärmrezept außerhalb 2000–5000 ms; Seed wird nicht geändert')
    raise ContractError('Prewarm für Rolle '+str(inputs.get('role_id') or inputs.get('role',{}).get('role_id'))+
        ' außerhalb 2000–5000 ms nach Basisversuch und 3 zusätzlichen Seedversuchen; gemessene Dauern: '+
        ', '.join(str(v)+' ms' for v in durations)+'. Stimme und Text unverändert; Audios und Reports erhalten unter: '+str(folder / 'prewarm-attempts'))


def materialize_custom_role(root, role_recipe, choice, *, installation, project_id,
                            ffmpeg, cli, models, cache, progress=None, runner=None):
    """Bind an imported reference to the original role's shared prewarm recipe.

    Kept public for the repair adapter's optional second same-voice recording.
    Its presence never invalidates materialization of the primary recording.
    """
    from manifest_renderer import preparation_core as core
    from manifest_renderer.audio import sha256_file, validate_wav
    from manifest_renderer.production import immutable_json
    from .voice_choices import verify_custom
    role_recipe = copy.deepcopy(role_recipe)
    if 'prewarm' in role_recipe:
        role_recipe['prewarm'] = normalize_prewarm_recipe(role_recipe['prewarm'])
    class RunnerSession:
        def run(self,command,stream):return runner(command,stream)
    sessions=RunnerSession() if runner is not None else None
    root = Path(root).resolve(strict=True)
    audio = verify_custom(root, role_recipe['role_id'], choice)
    prewarm = copy.deepcopy(role_recipe.get('prewarm'))
    if prewarm is not None:
        if prewarm['reference_mode'] != 'shared_anchor':
            raise ContractError('Eigener Rollenanker benötigt shared_anchor-Prewarm')
        if prewarm['source'] == 'neutral_standard':
            bank = {f'neutral:{i}': text for i, text in enumerate(core.NEUTRAL_PREWARM_TEXTS, 1)}
            if prewarm.get('neutral_version') != 'mi1-neutral-prewarm/2' or bank.get(prewarm['target_id']) != prewarm['text']:
                raise ContractError('Neutraler Prewarm weicht von fester Bank/Version ab')
        elif prewarm['source'] == 'custom_text' and prewarm['target_id'] != 'text:' + hashlib.sha256(prewarm['text'].encode()).hexdigest():
            raise ContractError('Customtext-ID abweichend')
    verified = dict(component_audio_sha256=[audio['sha256']],
        component_prompt_text_sha256=[hashlib.sha256(choice['transcript'].encode()).hexdigest()],
        binaries={cli.name: sha256_file(cli), 'ffmpeg': sha256_file(ffmpeg)},
        models={path.name: sha256_file(path) for path in models}, cases=[])
    if prewarm is not None:
        verified['cases'] = [dict(speech_id=prewarm['target_id'], audio_path=None, audio_sha256=None,
            prompt_text_sha256=hashlib.sha256(b'').hexdigest(), target_text_sha256=hashlib.sha256(prewarm['text'].encode()).hexdigest(), source_kind=prewarm['source'])]
    inputs = dict(role_id=role_recipe['role_id'], custom_reference_sha256=choice['semantic_sha256'],
                  prewarm=prewarm, binaries=verified['binaries'], models=verified['models'])
    folder = root / 'generated/roles' / installation.installation_id / digest(inputs)
    signed = folder / 'local-receipt.json'
    if signed.exists():
        prior = json.loads(signed.read_text())
        if not verify_preparation_receipt(prior, installation, project_id=project_id, inputs=inputs, work_root=root):
            raise ContractError('Eigener Rollenbeleg ungültig')
    else:
        if folder.exists() and not _role_evidence(root, folder, inputs, installation, project_id):
            raise ContractError('Unvollständiger eigener Rollenbeleg wird nicht überschrieben')
        folder.mkdir(parents=True, exist_ok=True)
        # The importer already normalized and validated this exact local WAV.
        # Copying it retains a genuine 3-second reference; the original-game
        # composite core intentionally keeps its existing 4-second boundary.
        if not (folder / 'reference.wav').exists():
            shutil.copyfile(resolve_relative(root, choice['path'], must_exist=True), folder / 'reference.wav')
        reference_info = validate_wav(folder / 'reference.wav')
        if reference_info['sha256'] != audio['sha256']: raise ContractError('Eigener Stimmanker änderte sich während der Vorbereitung')
        plan = dict(anchor={'fingerprint': digest(inputs)})
        if not (folder / 'reference.json').exists():
            core.atomic_json(folder / 'reference.json', core.reference_report_value(plan, verified, folder / 'reference.wav', core.wav_info(folder / 'reference.wav')))
        if prewarm is not None and not (folder / 'prewarm.json').exists():
            core.render_one(root, cli, ffmpeg, models, folder / 'reference.wav',
                dict(speech_id=prewarm['target_id'], german_text=prewarm['text'], audio=None),
                folder / 'prewarm.wav', folder / 'prewarm.json', digest(inputs), 'prewarm', prewarm['seed'], verified,
                **({'progress': lambda stage,kind,speech_id,log=None: progress(dict(phase='preparation_prewarm',preparation_attempt=1,preparation_detail=stage,log_path=str(log) if log else None))} if progress else {}),
                **({'sessions':sessions} if sessions is not None else {}))
        if prewarm is not None:
            _role_evidence(root, folder, inputs, installation, project_id, write=True)
    reference_info = validate_wav(folder / 'reference.wav')
    if reference_info['sha256'] != audio['sha256'] or not 3000 <= reference_info['duration_ms'] <= 15000:
        raise ContractError('Eigener Rollenanker passt nicht zur unveränderten gewählten Aufnahme')
    selected_audio, selected_report, selected_evidence = (None, None, None)
    if prewarm is not None:
        selected_audio, selected_report, selected_evidence = _select_prewarm(root, folder, inputs, prewarm, verified, installation=installation, project_id=project_id,cli=cli,ffmpeg=ffmpeg,models=models,progress=progress,runner=runner)
    if not signed.exists():
        names = ('reference', 'prewarm') if prewarm is not None else ('reference',)
        outputs = {name: dict(path=(folder / (name + '.wav')).relative_to(root).as_posix(),
                    sha256=sha256_file(folder / (name + '.wav'))) for name in names}
        if prewarm is not None:
            outputs['prewarm'] = dict(path=selected_audio.relative_to(root).as_posix(),sha256=sha256_file(selected_audio))
            outputs['prewarm_report'] = dict(path=selected_report.relative_to(root).as_posix(),sha256=sha256_file(selected_report))
        immutable_json(signed, preparation_receipt(installation, project_id=project_id, inputs=inputs, outputs=outputs))
    def asset(name):
        path = selected_audio if name == 'prewarm' else folder / (name + '.wav'); info = validate_wav(path)
        return dict(path=path.relative_to(root).as_posix(), sha256=info['sha256'], media_type='audio/wav',
                    duration_ms=info['duration_ms'], sample_rate_hz=48000, channels=1)
    anchor = dict(anchor_dialog_id='custom-anchor:' + choice['semantic_sha256'], anchor_component_dialog_ids=[],
                  kind='single', audio=asset('reference'), source_audio=[audio],
                  prompt_text=choice['transcript'], prompt_text_sha256=verified['component_prompt_text_sha256'][0],
                  selection_evidence='local_wizard_direct_selection')
    result = dict(anchor=anchor, selection=dict(status='selected', evidence_ref=choice['metadata_path']))
    if prewarm is not None:
        result['prewarm'] = dict(audio=asset('prewarm'), text=prewarm['text'], source=prewarm['source'],
            text_sha256=hashlib.sha256(prewarm['text'].encode()).hexdigest(), technical_check='pass',
            selection=dict(status='selected', evidence_ref=selected_report.relative_to(root).as_posix()),
            generated_with_anchor_dialog_id=anchor['anchor_dialog_id'], generated_with_anchor_audio_sha256=anchor['audio']['sha256'])
        result['prewarm_evidence'] = selected_evidence
        if prewarm['source'] == 'dialog': result['prewarm']['prewarm_dialog_id'] = prewarm['target_id']
    return result


def apply_custom_role(manifest, role_id, assets):
    """Own-voice selection overrides portable original-reference preferences."""
    configured = next(role for role in manifest['roles'] if role['role_id'] == role_id)
    configured['main_anchor'] = assets['anchor']; configured['main_anchor_selection'] = assets['selection']
    configured.pop('without_anchor', None)
    configured['use_voice_anchor_for_long_text'] = True
    configured['backup_anchors'] = []
    for dialogue in manifest['dialogues']:
        if dialogue.get('role_id') == role_id and dialogue.get('processing') == 'render':
            overrides = dialogue.setdefault('render_overrides', {})
            if overrides.get('anchor') == 'skip':
                manifest['source_evidence'].setdefault('launcher_custom_anchor_overrides', []).append(dialogue['dialog_id'])
            overrides['anchor'] = 'use'
    if 'prewarm' in assets: configured['prewarm'] = assets['prewarm']
    if assets.get('prewarm_evidence'): manifest['source_evidence'].setdefault('launcher_prewarm_attempts',{})[role_id]=assets['prewarm_evidence']


def effective_manifest(manifest, correction, *, local_history=None):
    """Apply text/settings from the existing correction export, never its audio."""
    m=copy.deepcopy(manifest)
    if correction is None: return m
    from manifest_renderer.recipes import FORMAT as RECIPE_FORMAT, apply_recipe
    if correction.get('format')==RECIPE_FORMAT:
        return apply_recipe(manifest,correction)
    if (correction.get('format')!='patch-review-extension/1' or
        correction.get('manifest_id')!=m['manifest_id'] or
        correction.get('game_id')!=m['game']['game_id']):
        raise ContractError('Korrekturen gehören nicht zur gewählten Manifest-/Spielidentität')
    rows={d['dialog_id']:d for d in m['dialogues']}
    from voice_anchor_wizard.text_rules import prepare_text
    for did,state in correction.get('rows',{}).items():
        if did not in rows: raise ContractError('Unbekannter Korrekturdialog: '+did)
        d=rows[did]
        if state.get('generation_mode')=='copy_original': d['processing']='copy_original'
        variant=next((v for v in d['text_variants'] if v['variant_id']==d['selected_variant_id']),None)
        if variant is None:
            if state.get('tts_text'): raise ContractError('Originalübernahme ohne feste deutsche Textvariante: '+did)
            continue
        if state.get('qa_expected_text') != prepare_text(state['tts_text'])['qa_expected_text']:
            raise ContractError('Korrektur-Solltext passt nicht zum Sprachausgabetext: '+did)
        if variant['tts_text']!=state['tts_text']: variant['qa_allowed_variants']=[]
        variant.update(tts_text=state['tts_text'],qa_expected_text=state['qa_expected_text'])
        variant['sha256']=digest([variant[k] for k in ('display_text','tts_text','qa_expected_text','qa_allowed_variants')])
        preset=state.get('next_preset',{})
        if preset.get('seed','keep') not in ('keep','fresh'): raise ContractError('Unbekannte Seedentscheidung')
        baseline=state.get('action_baseline',{}).get('next_preset',{})
        if preset.get('seed')=='keep' and state.get('selection'):
            if local_history is None: raise ContractError('Seed behalten benötigt einen gültigen lokalen Erzeugungsbeleg: '+did)
            previous=local_history(did,state['selection'])
            if previous['method']!='standard': raise ContractError('Beibehalten eines Eskalationskandidaten benötigt dessen portables wirksames Rezept: '+did)
            # An accepted output fixes this dialogue to its actual seed. Do not
            # append a project-default alternative, even when the seed equals
            # one of those defaults; every retry must replay the approval.
            m['source_evidence'].setdefault('launcher_render_inputs',{})[did]={'standard_seeds':[previous['seed']]}
        if preset.get('seed')=='fresh' and (state.get('marked') is True or baseline.get('seed')!='fresh'):
            # Resolve an explicitly requested fresh recipe once for this
            # immutable correction. Loading and saving still perform no work.
            request={'dialog_id':did,'tts_text':state['tts_text'],'text_revision':state.get('text_revision',1),
                     'previous_audio':(state.get('selection') or {}).get('wav_sha256'),'seed_choice':'fresh'}
            first=int(digest({**request,'seed_slot':0})[:8],16) % 2**31
            second=int(digest({**request,'seed_slot':1})[:8],16) % 2**31
            if second==first: second=(first+1) % 2**31
            m['source_evidence'].setdefault('launcher_render_inputs',{})[did]={'standard_seeds':[first,second]}
        for key in ('anchor','prewarm'):
            value=preset.get(key,'keep')
            if value not in ('keep','on','off'): raise ContractError('Unbekannte Korrektureinstellung')
            if value!='keep': d.setdefault('render_overrides',{})[key]='use' if value=='on' else 'skip'
        # An alternate reference must have a portable source recipe. Imported
        # candidate paths/host claims are never silently materialized as local.
        if state.get('anchor_ref'): raise ContractError('Alternativanker benötigt ein festes portables Quellrecipe: '+did)
    return m


def benchmark_subset(manifest, completed_roles):
    """Freeze only finished role inputs; never expose a role's pending files."""
    rows=[d for d in manifest['dialogues'] if d['processing']=='render' and d.get('role_id') in completed_roles]
    if len(rows)<2: return None
    subset=copy.deepcopy(manifest)
    ids={d['dialog_id'] for d in manifest['dialogues'] if d.get('role_id') in completed_roles}
    subset['dialogues']=[d for d in subset['dialogues'] if d['dialog_id'] in ids]
    subset['roles']=[r for r in subset['roles'] if r['role_id'] in completed_roles]
    subset['scope']['included_dialog_ids']=[d['dialog_id'] for d in subset['dialogues']]
    if subset.get('manifest_profile')!='local_render': subset['scope']['kind']='wizard_subset'
    # local_render requires complete_game and its entire source inventory.
    # These exclusions belong only to the explicitly requested benchmark copy.
    subset['scope']['excluded']=[{k:v for k,v in item.items() if k!='role_id'} for item in subset['scope'].get('excluded',[])]+[
        dict(dialog_id=d['dialog_id'],reason='deferred_by_user',
             note='Nur Benchmark: Rolle noch nicht vollständig vorbereitet; Produktionsauftrag unverändert')
        for d in manifest['dialogues'] if d['dialog_id'] not in ids]
    subset['source_evidence']['launcher_benchmark_subset']=dict(source_manifest_sha256=digest(manifest),
        completed_role_ids=sorted(completed_roles))
    return subset


def prepare_render_manifest(root, manifest, correction, config, installation, progress=None, *, ready=None, preparation_step=None,
                           approved_review_projection=None):
    from manifest_renderer.audio import sha256_file, validate_wav
    from manifest_renderer.audio_cache import AudioPreparationCache
    from manifest_renderer.production import immutable_json
    from .render import validate_launcher_host
    validate_launcher_host(root) # Separate launcher boundary; no M1/chip/RAM guard.
    root=Path(root).resolve(strict=True)
    from .runtime_setup import verify_installed_runtime, install_own_code
    verify_installed_runtime(root)
    install_own_code(root)
    project_id=config['project_state']['project_id']
    def local_history(did,selection):
        import hmac
        from .contracts import authorize_audio, canonical_json
        for path in (root/'.launcher/audio-receipts').glob('*/*.json'):
            entry=json.loads(path.read_text())
            body={key:value for key,value in entry.items() if key!='signature'}
            signed=hmac.new(installation.receipt_key,canonical_json(body),hashlib.sha256).hexdigest()
            generation=entry.get('generation',{})
            if generation.get('synthesis',{}).get('inputs',{}).get('dialog_id')!=did or generation.get('audio',{}).get('sha256')!=selection.get('wav_sha256'): continue
            if not hmac.compare_digest(entry.get('signature',''),signed): continue
            authorize_audio(generation,installation,generation['synthesis'],project_id=project_id,work_root=root)
            return generation['synthesis']['inputs']
        raise ContractError('Gewählter Korrekturkandidat hat keinen lokalen Seedbeleg: '+did)
    m=effective_manifest(manifest,correction,local_history=local_history)
    for role in m.get('source_evidence', {}).get('launcher_preparation', {}).get('roles', []):
        if 'prewarm' in role:
            role['prewarm'] = normalize_prewarm_recipe(role['prewarm'])
    if approved_review_projection is not None:
        reviewed=approved_review_projection.get('source_evidence',{})
        approved_inputs=reviewed.get('launcher_render_inputs',{})
        if isinstance(approved_inputs,dict):
            target_inputs=m.setdefault('source_evidence',{}).setdefault('launcher_render_inputs',{})
            known={d['dialog_id'] for d in m['dialogues'] if d.get('processing')=='render'}
            for did,value in approved_inputs.items():
                if did not in known or not isinstance(value,dict):continue
                seeds=value.get('standard_seeds')
                if isinstance(seeds,list) and len(seeds)==1:
                    if type(seeds[0]) is not int or seeds[0]<0:
                        raise ContractError('Persistierter Reviewseed ist ungültig: '+did)
                    target_inputs[did]={'standard_seeds':[seeds[0]]}
        reviewed_recipe=reviewed.get('portable_render_recipe')
        if reviewed_recipe:
            from manifest_renderer.recipes import validate_recipe
            m.setdefault('source_evidence',{})['portable_render_recipe']=validate_recipe(reviewed_recipe,m)
    resolved=resolve_preparation(m)
    needed={d.get('role_id') for d in m['dialogues'] if d['processing']=='render'}
    completed_roles=0
    def emit(phase, detail, completed=0, total=None, **extra):
        if progress: progress(dict(phase=phase, preparation_detail=detail, preparation_completed=completed,
            preparation_total=len(needed) if total is None else total, log_path=None, **extra))
    emit('preparation_sources','Originalquellen prüfen',total=len(resolved['sources']))
    from .voice_choices import verify_choices
    voice_choices = verify_choices(root, m, config.get('voice_choices'))
    m['source_evidence']['launcher_voice_choices'] = voice_choices
    game=root/'generated/game-preparation/preparation.json'
    evidence=json.loads(game.read_text())
    receipt=evidence['preparation_receipt']
    if not verify_preparation_receipt(receipt,installation,project_id=project_id,
            inputs=receipt['inputs'],work_root=root.parent):
        raise ContractError('Lokale PC-SE/UTE-Vorbereitungsreceipt fehlt oder ist ungültig')
    if receipt['inputs'].get('development_only') is True or evidence.get('development_only') is True:
        raise ContractError('Alter Entwicklungsbeleg ohne Originalbuilder wird nicht mehr verwendet; Vorbereitung erneut ausführen')
    m['source_evidence']['launcher_game_preparation'] = dict(
        development_only=False, preparation_receipt_sha256=digest(receipt),
        mode='pc_se_original_builder', gog_import=evidence.get('gog_import'))
    from .original_audio import english_archive, scan_archive
    archive=english_archive(root.parent,receipt['outputs'])
    ffmpeg=resolve_relative(root,'bin/ffmpeg',must_exist=True)
    ffprobe=resolve_relative(root,'bin/ffprobe',must_exist=True)
    cli=resolve_relative(root,'bin/voxcpm2_cli_speed',must_exist=True)
    models=[resolve_relative(root,a['path'],must_exist=True) for a in m['models']['tts']['files']]
    for path,a in zip(models,m['models']['tts']['files']):
        if sha256_file(path)!=a['sha256']: raise ContractError('TTS-Modellhash abweichend: '+a['path'])
    # Raw VCTL scanning stays in the extractor. The own SOF index adapter
    # preserves those original script offsets while locating compressed audio.
    loader=importlib.machinery.SourceFileLoader('launcher_mi1_extractor',str(Path(__file__).parents[1]/'mi1-dubbing-extractor'))
    spec=importlib.util.spec_from_loader(loader.name,loader);extractor=importlib.util.module_from_spec(spec)
    sys.modules[loader.name]=extractor;loader.exec_module(extractor)
    samples={s.speech_id:s for s in scan_archive(archive,extractor)}
    source_ids=[int(a['native_speech_id']) for a in resolved['sources']]
    if any(sid not in samples for sid in source_ids): raise ContractError('Quell-ID fehlt im lokalen englischen SOU-/SOF-Archivindex')
    extraction_inputs=dict(archive_sha256=sha256_file(archive),source_ids=source_ids,
        ffmpeg_sha256=sha256_file(ffmpeg),ffprobe_sha256=sha256_file(ffprobe))
    output=root/'generated/extraction'/installation.installation_id/digest(extraction_inputs)
    extraction_receipt=output/'local-receipt.json'
    if extraction_receipt.exists():
        prior=json.loads(extraction_receipt.read_text())
        if not verify_preparation_receipt(prior,installation,project_id=project_id,
                inputs=extraction_inputs,work_root=root): raise ContractError('Extraktionsbeleg ungültig')
        records=json.loads((output/'audio.json').read_text())
    else:
        if output.exists(): raise ContractError('Unbelegte alte/unvollständige Extraktion wird nicht aktiviert')
        output.mkdir(parents=True)
        emit('preparation_sources','Originalquellen extrahieren',total=len(source_ids))
        records=extractor.audio_manifest([samples[sid] for sid in source_ids],archive,output,output,
            str(ffmpeg),str(ffprobe),1,False,{})
        immutable_json(output/'audio.json',records)
        outputs={str(r['speech_id']):dict(path=(output/r['output_file']).relative_to(root).as_posix(),sha256=r['sha256']) for r in records}
        outputs['records']=dict(path=(output/'audio.json').relative_to(root).as_posix(),sha256=sha256_file(output/'audio.json'))
        immutable_json(extraction_receipt,preparation_receipt(installation,project_id=project_id,inputs=extraction_inputs,outputs=outputs))
    emit('preparation_sources','Originalquellen bereit',len(source_ids),len(source_ids))
    local={}
    for r in records:
        local['mi1-ute-1.02:vctl:'+str(r['speech_id'])]=dict(path=(output/r['output_file']).relative_to(root).as_posix(),
            sha256=r['sha256'],media_type='audio/ogg',duration_ms=int(r['duration_ms']),sample_rate_hz=int(r['sample_rate']),channels=int(r['channels']))
    rows={d['dialog_id']:d for d in m['dialogues']}
    for d in m['dialogues']:
        if d['dialog_id'] not in local: raise ContractError('Dialog ohne feste lokale Originalquelle: '+d['dialog_id'])
        d['original_audio']=local[d['dialog_id']]
    cache=AudioPreparationCache(root/'cache/audio_preparation_v1',ffmpeg)
    roles={r['role_id']:r for r in m['roles']}
    def materializer(*, role, resolved):
        def role_progress(details):
            value=dict(role_id=role['role_id'],role_name=roles[role['role_id']].get('name',role['role_id']),
                preparation_completed=completed_roles,preparation_total=len(needed),log_path=None)
            value.update(details)
            if progress: progress(value)
        role_progress(dict(phase='preparation_roles',preparation_detail='Rollenreferenz und Prewarm prüfen',preparation_attempt=1,preparation_duration_ms=None))
        choice = voice_choices['roles'][role['role_id']] if voice_choices else None
        if choice and choice['source'] == 'custom':
            assets = materialize_custom_role(root, role, choice, installation=installation, project_id=project_id,
                ffmpeg=ffmpeg, cli=cli, models=models, cache=cache, **({'progress':role_progress} if progress else {}))
            apply_custom_role(m, role['role_id'], assets)
            return dict(status='completed', role_id=role['role_id'],preparation_duration_ms=assets.get('prewarm',{}).get('audio',{}).get('duration_ms'))
        from manifest_renderer import preparation_core as core
        reference_recipe=role['reference'];prewarm=role['prewarm']
        if prewarm['reference_mode']!='shared_anchor': raise ContractError('Rollenprewarm benötigt shared_anchor; line_original folgt im bestehenden Renderer')
        if prewarm['source']=='neutral_standard':
            bank={f'neutral:{i}':t for i,t in enumerate(core.NEUTRAL_PREWARM_TEXTS,1)}
            if prewarm.get('neutral_version')!='mi1-neutral-prewarm/2' or bank.get(prewarm['target_id'])!=prewarm['text']:
                raise ContractError('Neutraler Prewarm weicht von fester Bank/Version ab')
        elif prewarm['source']=='custom_text':
            if prewarm['target_id']!='text:'+hashlib.sha256(prewarm['text'].encode()).hexdigest(): raise ContractError('Customtext-ID abweichend')
        components=[]
        for did in reference_recipe['source_dialog_ids']:
            row=rows[did]
            components.append(dict(speech_id=did.rsplit(':',1)[-1],audio=local[did],english_text=row['original_text']['text']))
        verified=dict(component_audio_sha256=[c['audio']['sha256'] for c in components],
            component_prompt_text_sha256=[hashlib.sha256(c['english_text'].encode()).hexdigest() for c in components],
            binaries={cli.name:sha256_file(cli),'ffmpeg':sha256_file(ffmpeg)},models={p.name:sha256_file(p) for p in models},
            cases=[dict(speech_id=prewarm['target_id'],audio_path=None,audio_sha256=None,prompt_text_sha256=hashlib.sha256(b'').hexdigest(),target_text_sha256=hashlib.sha256(prewarm['text'].encode()).hexdigest(),source_kind=prewarm['source'])])
        inputs=dict(role=role,source_hashes=verified['component_audio_sha256'],prompt_hashes=verified['component_prompt_text_sha256'],binaries=verified['binaries'],models=verified['models'])
        folder=root/'generated/roles'/installation.installation_id/digest(inputs)
        signed=folder/'local-receipt.json'
        if signed.exists():
            prior=json.loads(signed.read_text())
            if not verify_preparation_receipt(prior,installation,project_id=project_id,inputs=inputs,work_root=root): raise ContractError('Rollenbeleg ungültig')
        else:
            if folder.exists() and not _role_evidence(root,folder,inputs,installation,project_id):
                raise ContractError('Unvollständige/unbelegte Rollenmaterialisierung wird nicht überschrieben')
            plan=dict(anchor={'fingerprint':digest(inputs)},components=components,
                join_silence_ms=reference_recipe['join_silence_ms'],main_anchor={'small_role_fallback':reference_recipe.get('small_role_fallback')})
            materialize_role_audio(root=root,role_plan=plan,verified_inputs=verified,ffmpeg=ffmpeg,cli=cli,
                models=models,cache=cache,reference=folder/'reference.wav',reference_report=folder/'reference.json',
                target_case=dict(speech_id=prewarm['target_id'],german_text=prewarm['text']),
                output=folder/'prewarm.wav',report=folder/'prewarm.json',seed=prewarm['seed'],
                **({'progress': lambda stage,kind,speech_id,log=None: role_progress(dict(phase='preparation_prewarm',preparation_attempt=1,preparation_detail=stage,log_path=str(log) if log else None))} if progress else {}))
            _role_evidence(root,folder,inputs,installation,project_id,write=True)
        reference_info=validate_wav(folder/'reference.wav')
        if not reference_recipe.get('small_role_fallback') and not reference_recipe['minimum_duration_ms']<=reference_info['duration_ms']<=reference_recipe['maximum_duration_ms']:
            raise ContractError('Rollenreferenz außerhalb ihres festen Dauerfensters')
        selected_audio, selected_report, selected_evidence = _select_prewarm(root,folder,inputs,prewarm,verified,installation=installation,project_id=project_id,cli=cli,ffmpeg=ffmpeg,models=models,progress=role_progress if progress else None)
        if not signed.exists():
            outputs={name:dict(path=(folder/(name+'.wav')).relative_to(root).as_posix(),sha256=sha256_file(folder/(name+'.wav'))) for name in ('reference','prewarm')}
            outputs['prewarm']=dict(path=selected_audio.relative_to(root).as_posix(),sha256=sha256_file(selected_audio))
            outputs['prewarm_report']=dict(path=selected_report.relative_to(root).as_posix(),sha256=sha256_file(selected_report))
            immutable_json(signed,preparation_receipt(installation,project_id=project_id,inputs=inputs,outputs=outputs))
        def asset(name):
            p=selected_audio if name=='prewarm' else folder/(name+'.wav');info=validate_wav(p)
            return dict(path=p.relative_to(root).as_posix(),sha256=info['sha256'],media_type='audio/wav',duration_ms=info['duration_ms'],sample_rate_hz=48000,channels=1)
        configured=roles[role['role_id']]
        configured['main_anchor']['anchor_dialog_id']=reference_recipe['source_dialog_ids'][0]
        configured['main_anchor']['anchor_component_dialog_ids']=reference_recipe['source_dialog_ids'][1:]
        configured['main_anchor']['kind']='composite' if len(reference_recipe['source_dialog_ids'])>1 else 'single'
        configured['main_anchor']['audio']=asset('reference')
        configured['main_anchor']['source_audio']=[local[did] for did in reference_recipe['source_dialog_ids']]
        configured['main_anchor_selection'].pop('preview_audio',None)
        configured['main_anchor']['prompt_text']=reference_recipe['prompt_text']
        configured['main_anchor']['prompt_text_sha256']=hashlib.sha256(reference_recipe['prompt_text'].encode()).hexdigest()
        configured.setdefault('prewarm',{})['audio']=asset('prewarm');configured['prewarm']['text']=prewarm['text']
        configured['prewarm']['source']=prewarm['source']
        configured['prewarm']['technical_check']='pass'
        configured['prewarm']['selection']=dict(status='selected',evidence_ref=selected_report.relative_to(root).as_posix())
        m['source_evidence'].setdefault('launcher_prewarm_attempts',{})[role['role_id']]=selected_evidence
        if prewarm['source']!='dialog': configured['prewarm'].pop('prewarm_dialog_id',None)
        configured['prewarm']['text_sha256']=hashlib.sha256(prewarm['text'].encode()).hexdigest()
        configured['prewarm']['generated_with_anchor_dialog_id']=configured['main_anchor']['anchor_dialog_id']
        configured['prewarm']['generated_with_anchor_audio_sha256']=configured['main_anchor']['audio']['sha256']
        return dict(status='completed',role_id=role['role_id'],preparation_duration_ms=selected_evidence['duration_ms'])
    for role in m['roles']:
        for anchor in role.get('backup_anchors',[]):
            ids=[anchor['anchor_dialog_id'],*anchor.get('anchor_component_dialog_ids',[])]
            if len(ids)!=1: raise ContractError('Backupkomposit benötigt festes lokales Referenzrecipe')
            anchor['audio']=local[ids[0]]
    # Replace local runtime/audio receipts in the derived copy only.
    m['runtime']['artifacts']={k:dict(path=p.relative_to(root).as_posix(),sha256=sha256_file(p),media_type='application/octet-stream') for k,p in [('ffmpeg',ffmpeg),('combined_cli',cli),('isolated_cli',cli)]}
    m['source_evidence']['launcher_selection']=dict(manifest_sha256=config['project_state']['version']['manifest_sha256'],correction_sha256=digest(correction) if correction else None,extraction_receipt=extraction_receipt.relative_to(root).as_posix())
    completed_ids=set()
    for role_id in sorted(needed):
        from contextlib import nullcontext
        with preparation_step() if preparation_step else nullcontext():
            recipe = next((role for role in resolved['roles'] if role['role_id'] == role_id), None)
            if voice_choices and voice_choices['roles'][role_id]['source'] == 'custom' and recipe is not None:
                result=materializer(role=recipe, resolved=resolved)
            else:
                result=materialize_preparation(resolved=resolved,role_id=role_id,materializer=materializer)
            completed_roles+=1
            completed_ids.add(role_id)
            if ready:
                subset=benchmark_subset(m,completed_ids)
                if subset is not None: ready(subset)
            emit('preparation_roles','Rolle vorbereitet',completed_roles,role_id=role_id,role_name=roles[role_id].get('name',role_id),preparation_attempt=None,preparation_duration_ms=result.get('preparation_duration_ms'))
    from manifest_renderer.recipes import FORMAT as RECIPE_FORMAT, materialize_recipe_references
    if correction and correction.get('format')==RECIPE_FORMAT:
        from patch_review.render import ProductionAdapter
        needs_ane=any(entry.get('backend')=='ane' for entry in correction['dialogues'].values())
        from .local_ane import resolve_local_ane
        adapter=ProductionAdapter(root,resolve_local_ane(root) if needs_ane else None)
        try:
            m=materialize_recipe_references(m,correction,asset_root=root,state_root=root,executor_factory=adapter)
        finally:adapter.close()
    from manifest_renderer.manifest import resolve_manifest
    resolve_manifest(m)
    return m
