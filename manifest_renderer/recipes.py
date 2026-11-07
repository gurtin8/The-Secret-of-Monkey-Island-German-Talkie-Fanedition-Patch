"""Portable synthesis instructions, independent of a sender's files and receipts.

Only explicit materialization reads audio or starts synthesis. Validation and
application are pure; local hashes remain useful for the new local cache only.
"""
from __future__ import annotations
import copy
from pathlib import Path
from .manifest import canonical_json, sha256

FORMAT = 'mi1-render-recipe/1'
PIPELINES = {'ane_gpu': ('fast', 'ANE+GPU'), 'ane': ('medium', 'ANE'), 'gpu': ('slow', 'GPU')}
METHODS = {'standard', 'without_emotion', 'without_fixed_anchor', 'main_anchor_only'}


def recipe_mode(recipe):
    return PIPELINES[validate_recipe(recipe)['pipeline']][0]


def recipe_warnings(recipe):
    recipe=validate_recipe(recipe)
    if recipe['pipeline']=='ane_gpu':return []
    return ['Dialog '+did+': Backend '+entry['backend'].upper()+' weicht von Pipeline '+pipeline_label(recipe)+' ab; das Dialogbackend wird verwendet.'
            for did,entry in recipe['dialogues'].items() if entry.get('processing')=='render' and entry['backend']!=recipe['pipeline']]

def pipeline_label(recipe):
    pipeline = recipe['pipeline'] if isinstance(recipe, dict) else recipe
    if pipeline not in PIPELINES: raise ValueError('Unbekannte Rezeptpipeline: '+str(pipeline))
    return PIPELINES[pipeline][1]


def _text(value, name):
    if not isinstance(value, str) or not value.strip(): raise ValueError('Rezept benötigt '+name)


def _seed(value):
    if type(value) is not int or not 0 <= value < 2**31: raise ValueError('Rezept benötigt konkreten numerischen Seed (0–2147483647)')


def _conditioning(value):
    if not isinstance(value, dict) or type(value.get('line_original')) is not bool:
        raise ValueError('Rezept benötigt konkrete Originalaudio-Konditionierung')
    if set(value)-{'line_original','prompt_parts','performance_direction','performance_prompt_missing','runtime_mode'}:
        raise ValueError('Unbekannte Rezeptkonditionierung')
    if not isinstance(value.get('prompt_parts'), list): raise ValueError('Rezept benötigt konkrete Promptteile')
    for part in value['prompt_parts']:
        if not isinstance(part,dict) or set(part)!={'input_kind','text'} or part['input_kind'] not in {'main_anchor','line_original','german_prewarm'} or not isinstance(part['text'],str):
            raise ValueError('Ungültiger Rezeptprompt')
    if value.get('runtime_mode') not in ('combined','legacy_isolated'): raise ValueError('Rezept benötigt konkreten Runtime-Modus')
    if value.get('performance_direction') is not None and not isinstance(value['performance_direction'],str): raise ValueError('Ungültige Spielanweisung')
    if type(value.get('performance_prompt_missing')) is not bool: raise ValueError('Rezept benötigt Performance-Promptstatus')


def _render(entry, ids, depth):
    required={'processing','tts_text','seed','backend','anchor','prewarm','method','conditioning'}
    if not isinstance(entry,dict) or required-set(entry): raise ValueError('Dem Rezept fehlen konkrete Erzeugungsangaben')
    if set(entry)-required-{'anchor_source','prewarm_source','role_id','effective_parameters'}: raise ValueError('Unbekannte Felder im Rezept')
    if entry['processing']!='render': raise ValueError('Unbekannte Rezeptverarbeitung')
    _text(entry['tts_text'],'TTS-Text'); _seed(entry['seed'])
    if not isinstance(entry['backend'],str) or entry['backend'] not in {'gpu','ane'}: raise ValueError('Nicht verfügbares Rezeptbackend: '+str(entry['backend']))
    if not isinstance(entry['method'],str) or entry['method'] not in METHODS: raise ValueError('Nicht unterstützte Rendermethode')
    if any(type(entry[k]) is not bool for k in ('anchor','prewarm')): raise ValueError('Anker/Vorwärmen benötigen boolesche Werte')
    _conditioning(entry['conditioning'])
    if 'effective_parameters' in entry and not isinstance(entry['effective_parameters'],dict): raise ValueError('Ungültige Syntheseparameter')
    for key in ('anchor_source','prewarm_source'):
        if key in entry: _source(entry[key],ids,depth+1)
    inputs={'line_original'} if entry['conditioning']['line_original'] else set()
    if entry['anchor']: inputs.add('main_anchor')
    if entry['prewarm']: inputs.add('german_prewarm')
    if not entry['anchor'] and not entry['conditioning']['line_original']: raise ValueError('Rezept ohne Stimmreferenz')
    if any(p['input_kind'] not in inputs for p in entry['conditioning']['prompt_parts']): raise ValueError('Prompt benötigt fehlende Referenzeingabe')


def _source(source, ids=None, depth=0):
    if depth>12: raise ValueError('Referenzrezept zu tief verschachtelt')
    if not isinstance(source,dict): raise ValueError('Referenzrezept fehlt')
    kind=source.get('kind')
    if kind=='original_dialogs':
        allowed={'kind','source_dialog_ids','prompt_text','join_silence_ms','minimum_duration_ms','maximum_duration_ms','small_role_fallback'}
        if set(source)-allowed or allowed-{'small_role_fallback'}-set(source): raise ValueError('Originalreferenz benötigt vollständiges Quellrezept')
        components=source['source_dialog_ids']
        if not isinstance(components,list) or not 1<=len(components)<=8 or any(not isinstance(d,str) or not d for d in components) or len(set(components))!=len(components): raise ValueError('Originalreferenz benötigt geordnete Dialog-IDs')
        if ids is not None and any(d not in ids for d in components): raise ValueError('Unbekannte Referenz-Dialog-ID: '+next(d for d in components if d not in ids))
        if not isinstance(source['prompt_text'],str): raise ValueError('Originalreferenz benötigt Prompttext')
        for key in ('join_silence_ms','minimum_duration_ms','maximum_duration_ms'):
            if type(source[key]) is not int or source[key]<0: raise ValueError('Ungültige Referenzvorbereitung: '+key)
        if not 0<source['minimum_duration_ms']<=source['maximum_duration_ms']<=15000: raise ValueError('Ungültiges Referenzdauerfenster')
    elif kind=='generated':
        if set(source)!={'kind','dialog_id','recipe'}: raise ValueError('Generierte Referenz benötigt vollständiges Erzeugungsrezept')
        if ids is not None and source['dialog_id'] not in ids: raise ValueError('Unbekannter generierter Referenzdialog: '+str(source['dialog_id']))
        _render(source['recipe'],ids,depth)
        for key,flag in (('anchor_source','anchor'),('prewarm_source','prewarm')):
            if source['recipe'][flag] and key not in source['recipe']: raise ValueError('Generierter Referenz fehlt '+key)
    elif kind=='shared_prewarm':
        allowed={'kind','tts_text','seed','backend','reference_mode','source','target_id','neutral_version','anchor_source','effective_parameters'}
        required={'kind','tts_text','seed','backend','reference_mode','source','target_id','anchor_source'}
        if required-set(source) or set(source)-allowed: raise ValueError('Vorwärmreferenz benötigt vollständiges Erzeugungsrezept')
        _text(source['tts_text'],'Vorwärmtext');_seed(source['seed'])
        if source['backend']!='gpu': raise ValueError('Vorwärmrekonstruktion unterstützt derzeit nur GPU')
        if source['reference_mode']!='shared_anchor': raise ValueError('Unbekannter Vorwärmreferenzmodus')
        _source(source['anchor_source'],ids,depth+1)
    else: raise ValueError('Nicht rekonstruierbare Referenzquelle: '+str(kind))


def validate_recipe(recipe, manifest=None):
    if not isinstance(recipe,dict) or recipe.get('format')!=FORMAT: raise ValueError('Unbekanntes Rezeptformat')
    if set(recipe)!={'format','game_id','pipeline','roles','dialogues'}: raise ValueError('Rezept enthält fehlende oder unbekannte Felder')
    _text(recipe['game_id'],'Spielkennung')
    if not isinstance(recipe['pipeline'],str) or recipe['pipeline'] not in PIPELINES: raise ValueError('Unbekannte Rezeptpipeline')
    if not isinstance(recipe['roles'],dict) or not isinstance(recipe['dialogues'],dict) or not recipe['dialogues']: raise ValueError('Rezept benötigt Rollen und Dialoge')
    ids={d['dialog_id'] for d in manifest['dialogues']} if manifest else None
    roles={r['role_id'] for r in manifest['roles']} if manifest else None
    if manifest and recipe['game_id']!=manifest['game']['game_id']: raise ValueError('Rezept gehört zu einem anderen Spiel')
    for rid,role in recipe['roles'].items():
        if roles is not None and rid not in roles: raise ValueError('Unbekannte Rezeptrolle: '+rid)
        if not isinstance(role,dict) or set(role)-{'reference','prewarm'}: raise ValueError('Ungültiges Rollenrezept')
        for source in role.values(): _source(source,ids)
    for did,entry in recipe['dialogues'].items():
        if ids is not None and did not in ids: raise ValueError('Unbekannter Rezeptdialog: '+did)
        if entry=={'processing':'copy_original'}: continue
        _render(entry,ids,0)
        if manifest and entry.get('role_id') is not None and entry['role_id']!=next(d.get('role_id') for d in manifest['dialogues'] if d['dialog_id']==did):raise ValueError('Rezeptdialog nennt eine andere Stimme: '+did)
        rid=entry.get('role_id') or (next(d.get('role_id') for d in manifest['dialogues'] if d['dialog_id']==did) if manifest else None)
        role=recipe['roles'].get(rid,{})
        if entry['anchor'] and not (entry.get('anchor_source') or role.get('reference')): raise ValueError('Fehlende Ankerreferenz für '+did)
        if entry['prewarm'] and not (entry.get('prewarm_source') or role.get('prewarm')): raise ValueError('Fehlende Vorwärmreferenz für '+did)
    if manifest:
        source_rows={d['dialog_id']:d for d in manifest['dialogues']}
        def same_voice(source,rid):
            if source['kind']=='original_dialogs':
                if any(source_rows[did].get('role_id')!=rid for did in source['source_dialog_ids']):raise ValueError('Referenzdialog gehört zu einer anderen Stimme: '+str(rid))
                expected=' '.join(source_rows[did]['original_text']['text'] for did in source['source_dialog_ids'])
                if source['prompt_text']!=expected:raise ValueError('Referenzprompt passt nicht zu den Originaldialogen')
            elif source['kind']=='generated':
                if source_rows[source['dialog_id']].get('role_id')!=rid:raise ValueError('Generierte Referenz gehört zu einer anderen Stimme')
                for key in ('anchor_source','prewarm_source'):
                    if key in source['recipe']:same_voice(source['recipe'][key],rid)
            else:same_voice(source['anchor_source'],rid)
        for rid,role in recipe['roles'].items():
            for source in role.values():same_voice(source,rid)
        for did,entry in recipe['dialogues'].items():
            for key in ('anchor_source','prewarm_source'):
                if key in entry:same_voice(entry[key],source_rows[did].get('role_id'))
    return copy.deepcopy(recipe)


def apply_recipe(manifest,recipe):
    recipe=validate_recipe(recipe,manifest);m=copy.deepcopy(manifest)
    from voice_anchor_wizard.text_rules import prepare_text
    for d in m['dialogues']:
        entry=recipe['dialogues'].get(d['dialog_id'])
        if entry is None: continue
        d['processing']=entry['processing']
        if entry['processing']=='copy_original': continue
        variant=next((v for v in d['text_variants'] if v['variant_id']==d['selected_variant_id']),None)
        if variant is None: raise ValueError('Rezept benötigt deutsche Textvariante: '+d['dialog_id'])
        variant.update(tts_text=entry['tts_text'],qa_expected_text=prepare_text(entry['tts_text'])['qa_expected_text'],qa_allowed_variants=[])
        variant['sha256']=sha256(canonical_json([variant[k] for k in ('display_text','tts_text','qa_expected_text','qa_allowed_variants')]))
        d['render_overrides']={'anchor':'use' if entry['anchor'] else 'skip','prewarm':'use' if entry['prewarm'] else 'skip'}
    evidence=m.setdefault('source_evidence',{});evidence['portable_render_recipe']=recipe
    for role in evidence.get('launcher_preparation',{}).get('roles',[]):
        portable=recipe['roles'].get(role['role_id'])
        if not portable:
            candidates=[entry for did,entry in recipe['dialogues'].items() if entry.get('processing')=='render' and next(d.get('role_id') for d in m['dialogues'] if d['dialog_id']==did)==role['role_id']]
            if candidates:
                first=candidates[0]
                portable={'reference':first.get('anchor_source'),'prewarm':first.get('prewarm_source')}
                if not all(entry.get('anchor_source')==first.get('anchor_source') and entry.get('prewarm_source')==first.get('prewarm_source') for entry in candidates):portable=None
        if not portable: continue
        source=portable.get('reference')
        if source and source['kind']=='original_dialogs':
            role['reference']={k:copy.deepcopy(v) for k,v in source.items() if k!='kind'}
            role['reference']['kind']='composite' if len(source['source_dialog_ids'])>1 else 'single'
            role.pop('without_anchor',None)
        prewarm=portable.get('prewarm')
        if prewarm and prewarm['kind']=='shared_prewarm':
            role['prewarm']={k:copy.deepcopy(v) for k,v in prewarm.items() if k not in {'kind','anchor_source','backend','effective_parameters','tts_text'}}
            role['prewarm']['text']=prewarm['tts_text'];role['prewarm']['exact_seed']=True
    normalize_recipe_roles(m)
    return m


def normalize_recipe_roles(manifest):
    for role in manifest['roles']:
        rows=[d for d in manifest['dialogues'] if d.get('role_id')==role['role_id'] and d['processing']=='render']
        def consumes(d):
            overrides=d.get('render_overrides',{})
            if overrides.get('prewarm')=='skip' or overrides.get('prewarm_reference') is not None:return False
            if overrides.get('prewarm')=='use':return True
            variant=next((v for v in d.get('text_variants',[]) if v['variant_id']==d.get('selected_variant_id')),None)
            policy=manifest['project_policy']
            short=bool(variant) and d['content_type']=='speech' and (d['original_audio'].get('duration_ms',0)<policy['short_if_original_duration_below_ms'] or len(variant['tts_text'].split())<policy['short_if_tts_words_below'])
            return not role.get('without_prewarm',False) and (short or role.get('prewarm_for_long_text',False))
        if rows and not any(consumes(d) for d in rows):
            role.pop('prewarm',None);role['without_prewarm']=True
    active={d.get('role_id') for d in manifest['dialogues'] if d['processing']=='render' and d['content_type']=='speech'}
    manifest['roles']=[role for role in manifest['roles'] if role['role_id'] in active]
    for excluded in manifest.get('scope',{}).get('excluded',[]):
        if excluded.get('role_id') not in active:excluded.pop('role_id',None)
    return manifest


def source_from_reference(manifest,reference,kind='anchor',attempt=None):
    """Translate a *local* reference into source instructions; never export paths."""
    if reference.get('reference') is not None:
        reference=reference['reference']
    if reference.get('source_recipe'):
        source=copy.deepcopy(reference['source_recipe']);_source(source);return source
    recipe=manifest.get('source_evidence',{}).get('portable_reference_sources',{}).get(sha256(canonical_json(reference)))
    if recipe: return copy.deepcopy(recipe)
    roles=manifest.get('roles',[])
    for role in roles:
        candidates=[role.get('main_anchor'),*role.get('backup_anchors',[])] if kind=='anchor' else [role.get('prewarm')]
        for candidate in candidates:
            if not candidate: continue
            asset=candidate.get('audio',{})
            target=reference.get('audio',reference)
            if asset.get('path')!=target.get('path') or asset.get('sha256')!=target.get('sha256'): continue
            prep=next((r for r in manifest.get('source_evidence',{}).get('launcher_preparation',{}).get('roles',[]) if r['role_id']==role['role_id']),{})
            if kind=='anchor':
                p=prep.get('reference') if candidate is role.get('main_anchor') else None
                if p: return {'kind':'original_dialogs',**{k:copy.deepcopy(v) for k,v in p.items() if k!='kind'}}
                ids=[candidate.get('anchor_dialog_id'),*candidate.get('anchor_component_dialog_ids',[])]
                known={d['dialog_id'] for d in manifest['dialogues']}
                if not ids or any(d not in known for d in ids): raise ValueError('Ankeraufnahme hat kein rekonstruierbares Dialogrezept')
                return dict(kind='original_dialogs',source_dialog_ids=ids,prompt_text=candidate['prompt_text'],join_silence_ms=manifest['project_policy']['composite_join_silence_ms'],minimum_duration_ms=1,maximum_duration_ms=15000,small_role_fallback=True)
            p=prep.get('prewarm')
            if not p or type(p.get('seed')) is not int: raise ValueError('Vorwärmaudio hat kein vollständiges Erzeugungsrezept')
            anchor=source_from_reference(manifest,role['main_anchor'])
            actual=manifest.get('source_evidence',{}).get('launcher_prewarm_attempts',{}).get(role['role_id'],{})
            return dict(kind='shared_prewarm',tts_text=p['text'],seed=actual.get('seed',p['seed']),backend='gpu',anchor_source=anchor,
                **{k:copy.deepcopy(p[k]) for k in ('reference_mode','source','target_id','neutral_version') if k in p})
    # A per-line prewarm uses the dialogue original and its actual seed/backend.
    if kind=='prewarm' and reference.get('reference_mode')=='line_original' and attempt:
        did=attempt['dialog_id'];row=next(d for d in manifest['dialogues'] if d['dialog_id']==did)
        render=dict(processing='render',tts_text=reference['text'],seed=reference['seed'],backend=(attempt.get('render_execution') or manifest.get('render_execution',{})).get('profile_id'),anchor=False,prewarm=False,method='without_fixed_anchor',
            conditioning=dict(line_original=True,prompt_parts=[],runtime_mode='legacy_isolated',performance_direction=None,performance_prompt_missing=False))
        execution=attempt.get('render_execution') or manifest.get('render_execution',{})
        if execution.get('effective_parameters'):render['effective_parameters']=copy.deepcopy(execution['effective_parameters'])
        return dict(kind='generated',dialog_id=did,recipe=render)
    raise ValueError('Referenz hat kein vollständiges portables Quellrezept')


def _entry(manifest,attempt):
    execution=attempt.get('render_execution') or manifest.get('render_execution',{})
    backend=execution.get('profile_id')
    if backend not in {'gpu','ane'}: raise ValueError('Tatsächliches Dialogbackend fehlt oder ist nicht unterstützt')
    result=dict(processing='render',tts_text=attempt['target_text'],seed=attempt['seed'],backend=backend,
        anchor=attempt.get('fixed_anchor') is not None,prewarm=attempt.get('prewarm') is not None,method=attempt['method'],
        conditioning=dict(line_original=any(p['kind']=='line_original' for p in attempt['input_parts']),prompt_parts=copy.deepcopy(attempt['prompt_parts']),
            runtime_mode=attempt['runtime_mode'],performance_direction=attempt.get('performance_direction'),performance_prompt_missing=attempt['performance_prompt_missing']))
    if execution.get('effective_parameters'):result['effective_parameters']=copy.deepcopy(execution['effective_parameters'])
    for key,kind,part in (('anchor_source','anchor',attempt.get('fixed_anchor')),('prewarm_source','prewarm',attempt.get('prewarm'))):
        if part: result[key]=source_from_reference(manifest,part,kind,attempt)
    return result


def export_recipe(manifest,entries):
    dialogues={};roles={};backends=set()
    for did,attempt in entries.items():
        if attempt.get('processing')=='copy_original': dialogues[did]={'processing':'copy_original'};continue
        context=attempt.get('_recipe_manifest',manifest);entry=_entry(context,attempt);dialogues[did]=entry;backends.add(entry['backend'])
        row=next(d for d in context['dialogues'] if d['dialog_id']==did);rid=row.get('role_id')
        if rid: entry['role_id']=rid
    # Store repeated sources once per role; alternate voices and their paired
    # prewarms remain explicit on each dialogue. A one-row export keeps its
    # self-contained sources so accepting one candidate never changes defaults.
    for rid in {entry.get('role_id') for entry in dialogues.values()}-{None}:
        group=[entry for entry in dialogues.values() if entry.get('role_id')==rid]
        for field,default in (('anchor_source','reference'),('prewarm_source','prewarm')):
            counts={};values={}
            for entry in group:
                if field not in entry:continue
                key=sha256(canonical_json(entry[field]));counts[key]=counts.get(key,0)+1;values[key]=entry[field]
            if not counts:continue
            common=max(counts,key=counts.get)
            if counts[common]<2:continue
            roles.setdefault(rid,{})[default]=copy.deepcopy(values[common])
            for entry in group:
                if field in entry and sha256(canonical_json(entry[field]))==common:entry.pop(field)
    execution=manifest.get('render_execution',{})
    pipeline='ane_gpu' if len(backends)>1 or execution.get('operating_mode') in {'parallel_gpu_ane','adaptive_gpu_ane'} else next(iter(backends),execution.get('profile_id','gpu'))
    pipeline=manifest.get('source_evidence',{}).get('portable_render_recipe',{}).get('pipeline',pipeline)
    return validate_recipe(dict(format=FORMAT,game_id=manifest['game']['game_id'],pipeline=pipeline,roles=roles,dialogues=dialogues),manifest)


def materialize_recipe_references(manifest,recipe,resolver=None,*,asset_root=None,state_root=None,executor_factory=None):
    """Resolve portable sources only at explicit render start.

    The built-in resolver normalizes ordered original components and synthesizes
    complete generated sources. An injected resolver supports model-free tests.
    """
    recipe=validate_recipe(recipe,manifest);m=copy.deepcopy(manifest)
    if resolver is None:
        resolver=_LocalResolver(m,Path(asset_root),Path(state_root),executor_factory)
    roles={r['role_id']:r for r in m['roles']}
    for d in m['dialogues']:
        entry=recipe['dialogues'].get(d['dialog_id'])
        if not entry or entry['processing']=='copy_original':continue
        rid=d.get('role_id');role=recipe['roles'].get(rid,{})
        overrides=d.setdefault('render_overrides',{})
        for field,flag,default in (('anchor_source','anchor','reference'),('prewarm_source','prewarm','prewarm')):
            if not entry[flag]: continue
            source=entry.get(field) or role.get(default)
            configured=roles.get(rid,{})
            existing=configured.get('main_anchor' if flag=='anchor' else 'prewarm')
            same=False
            if existing:
                try:same=source_from_reference(m,existing,flag)==source
                except ValueError:pass
            # Reuse a local reference only when its complete source instruction
            # matches. Audio receipts are still checked by the local renderer.
            if same and (flag=='anchor' or 'anchor_reference' not in overrides):
                overrides.pop('anchor_reference' if flag=='anchor' else 'prewarm_reference',None)
                continue
            value=resolver(copy.deepcopy(source),kind=flag,role_id=rid)
            if flag=='prewarm':
                paired=overrides.get('anchor_reference') or configured.get('main_anchor')
                if paired:
                    value['generated_with_anchor_dialog_id']=paired['anchor_dialog_id']
                    value['generated_with_anchor_audio_sha256']=paired['audio']['sha256']
            overrides['anchor_reference' if flag=='anchor' else 'prewarm_reference']=value
            overrides[flag]='use'
            m.setdefault('source_evidence',{}).setdefault('portable_reference_sources',{})[sha256(canonical_json(value))]=copy.deepcopy(source)
    normalize_recipe_roles(m)
    return m


class _LocalResolver:
    def __init__(self,manifest,asset_root,state_root,executor_factory):
        self.manifest=manifest;self.assets=asset_root.resolve(strict=True);self.root=state_root.resolve(strict=True);self.factory=executor_factory;self.memo={}

    def __call__(self,source,*,kind,role_id=None):
        from .audio import resolve_manifest_path,sha256_file,validate_wav
        from . import preparation_core as core
        from .audio_cache import AudioPreparationCache
        key=sha256(canonical_json(source))
        if key in self.memo:
            audio=self.memo[key]
        else:
            rows={d['dialog_id']:d for d in self.manifest['dialogues']}
            ffmpeg=resolve_manifest_path(self.assets,self.manifest['runtime']['artifacts']['ffmpeg']['path'])
            if source['kind']=='original_dialogs':
                components=[dict(speech_id=did,audio=copy.deepcopy(rows[did]['original_audio']),english_text=rows[did]['original_text']['text']) for did in source['source_dialog_ids']]
                verified=dict(component_audio_sha256=[sha256_file(resolve_manifest_path(self.assets,c['audio']['path'])) for c in components],component_prompt_text_sha256=[sha256(c['english_text'].encode()) for c in components],binaries={'ffmpeg':sha256_file(ffmpeg)})
                for c,h in zip(components,verified['component_audio_sha256']):
                    if h!=c['audio']['sha256']:raise ValueError('Lokale Originalreferenz verändert: '+c['speech_id'])
                local_id=sha256(canonical_json(dict(source=source,verified=verified)))
                folder=self.root/'portable-references'/local_id;folder.mkdir(parents=True,exist_ok=True)
                # Source files remain under assets; output/cache under state.
                core.materialize_role_reference(self.assets,dict(anchor={'fingerprint':local_id},components=components,join_silence_ms=source['join_silence_ms'],main_anchor={'small_role_fallback':source.get('small_role_fallback')}),verified,ffmpeg,folder/'reference.wav',folder/'reference.json',AudioPreparationCache(self.root/'cache/audio_preparation_v1',ffmpeg))
                info=validate_wav(folder/'reference.wav')
                if not source['minimum_duration_ms']<=info['duration_ms']<=source['maximum_duration_ms']:raise ValueError('Rekonstruierte Referenz außerhalb des Rezeptdauerfensters')
                audio=dict(path=(folder/'reference.wav').relative_to(self.root).as_posix(),sha256=info['sha256'],media_type='audio/wav',duration_ms=info['duration_ms'],sample_rate_hz=48000,channels=1)
            else:
                if self.factory is None:raise ValueError('Generierte Referenz benötigt einen lokalen Syntheseadapter')
                if source['kind']=='shared_prewarm':
                    entry=dict(processing='render',tts_text=source['tts_text'],seed=source['seed'],backend=source['backend'],anchor=True,prewarm=False,method='main_anchor_only',anchor_source=source['anchor_source'],conditioning=dict(line_original=False,prompt_parts=[],runtime_mode='combined',performance_direction=None,performance_prompt_missing=False))
                    if 'effective_parameters' in source:entry['effective_parameters']=source['effective_parameters']
                    did=next(d['dialog_id'] for d in self.manifest['dialogues'] if d.get('role_id')==role_id)
                else:entry=source['recipe'];did=source['dialog_id']
                anchor=self(entry['anchor_source'],kind='anchor',role_id=role_id) if entry['anchor'] else None
                prewarm=self(entry['prewarm_source'],kind='prewarm',role_id=role_id) if entry['prewarm'] else None
                m=copy.deepcopy(self.manifest)
                m.pop('render_execution',None)
                from .backend import profile
                selected=self.factory.backend_profile(entry['backend'],m) if hasattr(self.factory,'backend_profile') else profile(entry['backend'])
                if entry.get('effective_parameters'):selected['effective_parameters']=copy.deepcopy(entry['effective_parameters'])
                m['render_execution']=selected
                if hasattr(self.factory,'runtime_for_attempt'):m['runtime']=self.factory.runtime_for_attempt(entry['backend'],m)
                local_id=sha256(canonical_json(dict(source=source,original=rows[did]['original_audio'],anchor=anchor,prewarm=prewarm,runtime=m['runtime'],models=m['models'],execution=selected)))
                folder=self.root/'portable-references'/local_id;folder.mkdir(parents=True,exist_ok=True)
                m['output']['root']='portable-references/'+local_id
                d=copy.deepcopy(rows[did]);d['render_overrides']={'anchor':'use' if anchor else 'skip','prewarm':'use' if prewarm else 'skip'}
                if anchor:d['render_overrides']['anchor_reference']=anchor
                if prewarm:d['render_overrides']['prewarm_reference']=prewarm
                role=next((r for r in m['roles'] if r['role_id']==d.get('role_id')),{'role_id':role_id})
                variant=copy.deepcopy(next(v for v in d['text_variants'] if v['variant_id']==d['selected_variant_id']));variant['tts_text']=entry['tts_text']
                from .plan import _attempt, replay_conditioning
                attempt=_attempt(d,variant,role,entry['method'],entry['seed'],m,{'short':True,'manifest_sha256':sha256(canonical_json(m))})
                attempt=replay_conditioning(attempt,d,entry,m)
                execution=self.factory.for_manifest(m,self.root) if hasattr(self.factory,'for_manifest') else self.factory
                synthesizer=getattr(execution,'executor',execution)
                output=folder/'generated.wav';log=folder/'generated.log';report=folder/'generated.json'
                identity=dict(source=source,attempt_fingerprint=attempt['fingerprint'])
                try:
                    if output.exists() or report.exists():
                        import json
                        prior=json.loads(report.read_text()) if report.exists() else {}
                        if prior.get('identity')!=identity or not output.is_file() or prior.get('sha256')!=sha256_file(output):raise ValueError('Lokale Referenzrekonstruktion unvollständig oder verändert')
                    else:
                        if not hasattr(synthesizer,'synthesize'):raise ValueError('Lokaler Adapter unterstützt keine Referenzsynthese')
                        synthesizer.synthesize(attempt,output,log)
                        core.atomic_json(report,dict(identity=identity,sha256=sha256_file(output)))
                finally:
                    if hasattr(execution,'close'):execution.close()
                info=validate_wav(output)
                if kind=='prewarm' and not 2000<=info['duration_ms']<=5000:raise ValueError('Exaktes Vorwärmrezept außerhalb 2000–5000 ms; kein Ersatzseed')
                audio=dict(path=output.relative_to(self.root).as_posix(),sha256=info['sha256'],media_type='audio/wav',duration_ms=info['duration_ms'],sample_rate_hz=48000,channels=1)
            self.memo[key]=audio
        if kind=='anchor':
            ids=source.get('source_dialog_ids') or [source.get('dialog_id') or next(d['dialog_id'] for d in self.manifest['dialogues'] if d.get('role_id')==role_id)]
            rows={d['dialog_id']:d for d in self.manifest['dialogues']}
            prompt=source.get('prompt_text') or source.get('tts_text') or source.get('recipe',{}).get('tts_text','')
            return dict(anchor_dialog_id=ids[0] if ids else 'generated:'+key,anchor_component_dialog_ids=ids[1:],kind='composite' if len(ids)>1 else 'single',audio=copy.deepcopy(audio),source_audio=[copy.deepcopy(rows[did]['original_audio']) for did in ids],prompt_text=prompt,prompt_text_sha256=sha256(prompt.encode()),selection_evidence='local_portable_recipe_reconstruction')
        text=source.get('tts_text') or source.get('recipe',{}).get('tts_text')
        return dict(audio=copy.deepcopy(audio),text=text,text_sha256=sha256(text.encode()),source='custom_text',technical_check='pass',selection=dict(status='selected',evidence_ref=audio['path']),generated_with_anchor_dialog_id='portable:'+key,generated_with_anchor_audio_sha256=audio['sha256'])
