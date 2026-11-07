"""Pure deterministic run-plan generation; never loads assets or models."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from .manifest import canonical_json, resolve_manifest, sha256


def _word_count(text: str) -> int:
    # unicode_whitespace_v1: split on Python's Unicode whitespace semantics.
    return len(text.split())


def _attempt(dialogue: dict, variant: dict, role: dict, method: str, seed: int | None,
             manifest: dict, artifacts: dict) -> dict:
    policy = manifest["project_policy"]
    anchor = role.get("main_anchor")
    prewarm = role.get("prewarm")
    short = artifacts["short"]
    use_emotion = method == "standard"
    long_anchor = bool(role.get("use_voice_anchor_for_long_text", False))
    long_prewarm = bool(role.get("prewarm_for_long_text", False))
    without_anchor = role.get("without_anchor", False)
    without_prewarm = role.get("without_prewarm", False)
    overrides = dialogue.get("render_overrides", {})
    anchor_override = overrides.get("anchor", "inherit")
    prewarm_override = overrides.get("prewarm", "inherit")
    anchor_reference = overrides.get("anchor_reference")
    prewarm_reference = overrides.get("prewarm_reference")
    if anchor_reference is not None:
        anchor = anchor_reference
    if prewarm_reference is not None:
        prewarm = prewarm_reference
    if method == "standard":
        use_fixed_anchor = short or long_anchor or long_prewarm
    else:
        use_fixed_anchor = method != "without_fixed_anchor"
    if without_anchor:
        use_fixed_anchor = False
    if anchor_override == "use":
        use_fixed_anchor = True
    elif anchor_override == "skip":
        use_fixed_anchor = False
    use_line_original = not use_fixed_anchor or (use_emotion and dialogue["performance_prompt"] == "original_audio_and_text")
    use_prewarm = (use_fixed_anchor or without_anchor or anchor_override == "skip") and not without_prewarm and (short or long_prewarm) and prewarm is not None and method != "main_anchor_only"
    if prewarm_override == "use":
        use_prewarm = prewarm is not None
    elif prewarm_override == "skip":
        use_prewarm = False
    per_line_prewarm = None
    if use_prewarm and not use_fixed_anchor and prewarm_reference is None:
        generation = {"reference_mode": "line_original", "reference_audio": dialogue["original_audio"],
                      "text": prewarm["text"], "text_sha256": prewarm["text_sha256"],
                      "seed": seed, "runtime": manifest["runtime"], "models": manifest["models"],
                      "target_duration_ms": [3000, 4000]}
        if manifest.get("render_execution") is not None:
            generation["render_execution"] = manifest["render_execution"]
        generation_hash = sha256(canonical_json(generation))
        cache = PurePosixPath(manifest["output"]["root"]) / manifest["output"]["wav_directory"] / "line-prewarm" / generation_hash
        per_line_prewarm = {**generation, "fingerprint": generation_hash,
                           "artifacts": {"wav": str(cache) + ".wav", "log": str(cache) + ".log", "report": str(cache) + ".json"}}
    inputs = []
    if use_fixed_anchor:
        part = {"kind": "main_anchor", "path": anchor["audio"]["path"], "sha256": anchor["audio"]["sha256"]}
        if anchor_reference is not None:
            part["reference"] = anchor_reference
        inputs.append(part)
    if use_line_original:
        inputs.append({"kind": "line_original", "path": dialogue["original_audio"]["path"], "sha256": dialogue["original_audio"]["sha256"]})
    if use_prewarm:
        if per_line_prewarm:
            part = {"kind": "german_prewarm", "generation_fingerprint": per_line_prewarm["fingerprint"], "path": per_line_prewarm["artifacts"]["wav"]}
        else:
            part = {"kind": "german_prewarm", "path": prewarm["audio"]["path"], "sha256": prewarm["audio"]["sha256"]}
            if prewarm_reference is not None:
                part["reference"] = prewarm_reference
        inputs.append(part)
    prompt_parts = []
    # The anchor is part of the prompt audio only for short/prewarm paths;
    # ordinary lines use it solely as the fixed -r reference.
    if use_fixed_anchor and use_prewarm and method != "main_anchor_only":
        prompt_parts.append({"input_kind": "main_anchor", "text": anchor["prompt_text"]})
    if (use_fixed_anchor and use_line_original and method != "without_fixed_anchor"
            and dialogue["performance_prompt"] == "original_audio_and_text"):
        prompt_parts.append({"input_kind": "line_original", "text": dialogue["original_text"]["text"]})
    if use_prewarm:
        prompt_parts.append({"input_kind": "german_prewarm", "text": prewarm["text"]})
    if method == "main_anchor_only":
        prompt_parts = []

    fixed_anchor = {"path": anchor["audio"]["path"], "sha256": anchor["audio"]["sha256"], "prompt_text": anchor["prompt_text"]} if use_fixed_anchor else None
    if fixed_anchor is not None and anchor_reference is not None:
        fixed_anchor["reference"] = anchor_reference
    planned_prewarm = (per_line_prewarm or {"path": prewarm["audio"]["path"], "sha256": prewarm["audio"]["sha256"], "text": prewarm["text"]}) if use_prewarm else None
    if planned_prewarm is not None and prewarm_reference is not None and not per_line_prewarm:
        planned_prewarm["reference"] = prewarm_reference
    body = {
        "dialog_id": dialogue["dialog_id"],
        "variant_id": variant["variant_id"],
        "variant_revision": variant["variant_revision"],
        "manifest_id": manifest["manifest_id"],
        "manifest_revision": manifest["manifest_revision"],
        "method": method,
        "reference_mode": "fixed_anchor" if use_fixed_anchor else "line_original",
        "runtime_mode": "combined" if use_fixed_anchor or use_prewarm else "legacy_isolated",
        "seed": seed,
        "input_parts": inputs,
        "prompt_parts": prompt_parts,
        "target_text": variant["tts_text"],
        "qa_expected_text": variant["qa_expected_text"],
        "qa_allowed_variants": variant["qa_allowed_variants"],
        "content_type": dialogue["content_type"],
        "qa": manifest["qa"],
        "performance_direction": dialogue.get("performance_direction") if use_emotion else None,
        "performance_prompt_missing": dialogue["performance_prompt"] == "none",
        "fixed_anchor": fixed_anchor,
        "prewarm": planned_prewarm,
        "runtime": manifest["runtime"],
        "models": manifest["models"],
        "requirements": manifest["requirements"],
        "project_policy": manifest["project_policy"],
        "manifest_sha256": artifacts["manifest_sha256"],
    }
    if without_anchor or without_prewarm:
        body["role_rules"] = {"without_anchor": without_anchor, "without_prewarm": without_prewarm}
    if manifest.get("render_execution") is not None:
        body["render_execution"] = manifest["render_execution"]
        if manifest["render_execution"].get("routing_policy") == "fixed-character-v1":
            body["role_id"] = dialogue["role_id"]
    fingerprint = sha256(canonical_json(body))
    base = PurePosixPath(manifest["output"]["root"]) / manifest["output"]["wav_directory"] / dialogue["output_stem"] / fingerprint
    return {**body, "fingerprint": fingerprint,
            "artifacts": {"wav": str(base) + ".wav", "log": str(base) + ".log",
                          "report": str(base) + ".json"}}


def replay_conditioning(attempt, dialogue, recipe, manifest):
    """Bind the exact exported conditioning and backend, then rehash locally."""
    import copy
    from .backend import profile
    result=copy.deepcopy(attempt)
    conditioning=recipe['conditioning']
    parts={part['kind']:part for part in result['input_parts']}
    if conditioning['line_original']:
        parts['line_original']={'kind':'line_original',**{key:dialogue['original_audio'][key] for key in ('path','sha256')}}
    else:parts.pop('line_original',None)
    if not recipe['anchor']:parts.pop('main_anchor',None);result['fixed_anchor']=None
    if not recipe['prewarm']:parts.pop('german_prewarm',None);result['prewarm']=None
    result['input_parts']=[parts[k] for k in ('main_anchor','line_original','german_prewarm') if k in parts]
    result.update(prompt_parts=copy.deepcopy(conditioning['prompt_parts']),
        runtime_mode=conditioning['runtime_mode'],performance_direction=conditioning.get('performance_direction'),
        performance_prompt_missing=conditioning['performance_prompt_missing'])
    selected=manifest.get('render_execution',{})
    if selected.get('operating_mode') in {'parallel_gpu_ane','adaptive_gpu_ane'}:
        from .production import _parallel_profiles
        execution=_parallel_profiles(manifest)[recipe['backend']]
    elif selected.get('profile_id')==recipe['backend']:execution=copy.deepcopy(selected)
    else:execution=profile(recipe['backend'])
    if recipe.get('effective_parameters'):
        execution['effective_parameters']=copy.deepcopy(recipe['effective_parameters'])
    result['render_execution']=execution
    body={k:v for k,v in result.items() if k not in ('fingerprint','artifacts')}
    fingerprint=sha256(canonical_json(body))
    base=PurePosixPath(manifest['output']['root'])/manifest['output']['wav_directory']/dialogue['output_stem']/fingerprint
    return {**body,'fingerprint':fingerprint,'artifacts':{'wav':str(base)+'.wav','log':str(base)+'.log','report':str(base)+'.json'}}

def build_run_plan(manifest_or_resolved: dict[str, Any]) -> dict[str, Any]:
    """Create a deterministic plan and its digest from a validated manifest."""
    resolved = manifest_or_resolved if "manifest" in manifest_or_resolved and "manifest_sha256" in manifest_or_resolved else resolve_manifest(manifest_or_resolved)
    manifest = resolved["manifest"]
    strategy = manifest["project_policy"]["render_strategy"]
    rows = []
    for item in resolved["dialogues"]:
        dialogue, variant, role = item["dialogue"], item["variant"], item["role"]
        if dialogue["processing"] == "copy_original":
            rows.append({"dialog_id": dialogue["dialog_id"], "processing": "copy_original", "selected_variant_id": variant["variant_id"] if variant else None, "source_audio": {"path": dialogue["original_audio"]["path"], "sha256": dialogue["original_audio"]["sha256"]}, "attempts": []})
            continue
        is_short = dialogue["content_type"] == "speech" and (
            dialogue["original_audio"].get("duration_ms", 0) < manifest["project_policy"]["short_if_original_duration_below_ms"]
            or _word_count(variant["tts_text"]) < manifest["project_policy"]["short_if_tts_words_below"]
        )
        data = {"short": is_short, "manifest_sha256": resolved["manifest_sha256"]}
        imported=manifest.get('source_evidence',{}).get('portable_render_recipe',{}).get('dialogues',{}).get(dialogue['dialog_id'])
        if imported and imported.get('processing')=='render':
            attempt=_attempt(dialogue,variant,role,imported['method'],imported['seed'],manifest,data)
            attempt=replay_conditioning(attempt,dialogue,imported,manifest)
            rows.append({'dialog_id':dialogue['dialog_id'],'processing':'render','selected_variant_id':variant['variant_id'],'role_id':role['role_id'],
                         'classification':{'short':is_short,'word_count':_word_count(variant['tts_text']),'word_count_rule':manifest['project_policy']['word_count_rule']},'attempts':[attempt]})
            continue
        launcher_inputs=manifest.get('source_evidence',{}).get('launcher_render_inputs',{})
        if dialogue['dialog_id'] in launcher_inputs:
            declared=launcher_inputs[dialogue['dialog_id']]
            seeds=declared.get('standard_seeds') if isinstance(declared,dict) else None
        else:
            seeds=strategy['standard_seeds']
        # A one-seed declaration is an approved fixed choice. Two seeds retain
        # the ordinary standard choice pair. Once a per-dialogue declaration
        # exists it must be complete; never fall through to project defaults.
        if (not isinstance(seeds,list) or len(seeds) not in (1,2)
                or any(type(seed) is not int or seed<0 for seed in seeds)
                or len(set(seeds))!=len(seeds)):
            raise ValueError('Launcher-Seedrecipe benötigt einen festen oder zwei verschiedene konkrete Seeds')
        attempts = [_attempt(dialogue, variant, role, "standard", seed, manifest, data) for seed in seeds]
        # Escalations still need a reproducible CLI seed; they reuse the first
        # manifest seed exactly once instead of introducing another variation.
        overrides = dialogue.get("render_overrides", {})
        forced_anchor = overrides.get("anchor") == "use"
        forced_prewarm = overrides.get("prewarm") == "use"
        attempts.extend(_attempt(dialogue, variant, role, method,
                                 seeds[0], manifest, data)
                        for method in strategy["escalation_order"]
                        if not (role.get("without_anchor", False) and method == "main_anchor_only")
                        and not (forced_anchor and method == "without_fixed_anchor")
                        and not (forced_prewarm and method == "main_anchor_only"))
        portable=manifest.get('source_evidence',{}).get('portable_render_recipe',{})
        if portable.get('pipeline') in {'ane','gpu'} and manifest.get('render_execution',{}).get('operating_mode') in {'parallel_gpu_ane','adaptive_gpu_ane'}:
            from .production import _parallel_profiles
            adjusted=[]
            for attempt in attempts:
                recipe={'backend':portable['pipeline'],'anchor':attempt['fixed_anchor'] is not None,'prewarm':attempt['prewarm'] is not None,
                        'conditioning':{'line_original':any(p['kind']=='line_original' for p in attempt['input_parts']),
                            'prompt_parts':attempt['prompt_parts'],'runtime_mode':attempt['runtime_mode'],
                            'performance_direction':attempt['performance_direction'],'performance_prompt_missing':attempt['performance_prompt_missing']}}
                adjusted.append(replay_conditioning(attempt,dialogue,recipe,manifest))
            attempts=adjusted
        rows.append({"dialog_id": dialogue["dialog_id"], "processing": "render", "selected_variant_id": variant["variant_id"], "role_id": role["role_id"], "classification": {"short": is_short, "word_count": _word_count(variant["tts_text"]), "word_count_rule": manifest["project_policy"]["word_count_rule"], "duration_ms": dialogue["original_audio"].get("duration_ms"), "short_if_original_duration_below_ms": manifest["project_policy"]["short_if_original_duration_below_ms"], "short_if_tts_words_below": manifest["project_policy"]["short_if_tts_words_below"]}, "attempts": attempts})
    body = {"plan_version": "1", "manifest_id": manifest["manifest_id"], "manifest_revision": manifest["manifest_revision"], "manifest_sha256": resolved["manifest_sha256"], "project_policy": manifest["project_policy"], "excluded": manifest["scope"]["excluded"], "dialogues": rows}
    return {**body, "sha256": sha256(canonical_json(body))}
