"""Model-free backend profiles and immutable per-attempt bindings."""
from __future__ import annotations

import json
from pathlib import Path

from .audio import resolve_manifest_path, sha256_file
from .manifest import canonical_json, sha256
from .runner import ArtifactError, _exclusive_json


ANE_RUNTIME_ASSETS = (
    "work/ane_runtime_contract/voxcpmane2-0.1.3b1-py3-none-any.whl",
    "generator_launcher/provenance/voxcpmane-pypi.json",
    "generator_launcher/provenance/voxcpmane-beta-files.json",
    "generator_launcher/provenance/models/preview.json",
    "generator_launcher/provenance/preview-config.json",
    "generator_launcher/provenance/models/fallback.json",
    "generator_launcher/provenance/fallback-config.json",
    "generator_launcher/provenance/models/tokenizer.json",
)


def profile(backend: str, *, operating_mode: str = "single", facts: dict | None = None) -> dict:
    """Return a stable profile selection; operating mode never duplicates attempts."""
    if backend not in {"gpu", "cpu", "ane"}:
        raise ValueError(f"Unbekanntes Backendprofil: {backend}")
    if operating_mode not in {"single", "parallel_gpu_ane", "adaptive_ane", "adaptive_gpu_ane"}:
        raise ValueError(f"Unbekannter Betriebsmodus: {operating_mode}")
    if operating_mode == 'adaptive_ane' and backend != 'ane':
        raise ValueError('Adaptive ANE requires the ANE backend')
    if operating_mode == "parallel_gpu_ane" and backend not in {"gpu", "ane"}:
        raise ValueError("Parallelbetrieb benötigt GPU und ANE")
    if operating_mode == 'adaptive_gpu_ane' and backend not in {'gpu', 'ane'}:
        raise ValueError('Adaptive GPU+ANE requires GPU and ANE profiles')
    device = {"gpu": "metal", "cpu": "cpu", "ane": "coreml_cpu_and_ne"}[backend]
    parameters = {
        "device": device,
        "seed": "attempt.seed",
        "output": "wav,mono,pcm_s16le,48000",
    }
    if backend in {"gpu", "cpu"}:
        parameters.update(steps=200, timesteps=10, cfg=2.0, temperature=1.0)
    else:
        parameters.update(cfg=2.0, inference_timesteps=10, min_len=2,
                          max_len="min(6*target_tokens+10,2048,kv_remaining)", temperature=1.0)
    result = {
        "profile_id": backend,
        "operating_mode": operating_mode,
        "dispatch_profiles": ["gpu", "ane"] if operating_mode in {"parallel_gpu_ane", "adaptive_gpu_ane"} else [backend],
        "effective_parameters": parameters,
        "runtime_contract": "mi1:voxcpm2-cli-v1" if backend in {"gpu", "cpu"} else "voxcpmane2:0.1.3b1",
        "assets": [],
    }
    if operating_mode in {'adaptive_ane', 'adaptive_gpu_ane'}:
        from .adaptive_ane import effective_policy
        from .hardware import host_facts
        result['adaptive_policy'] = effective_policy(host_facts() if facts is None else facts)
    return result


def profile_assets(root: Path, selected: dict) -> list[dict]:
    """Hash only assets belonging to the selected backend profile."""
    if selected["profile_id"] != "ane" and selected["operating_mode"] not in {"parallel_gpu_ane", "adaptive_gpu_ane"}:
        return []
    result = []
    for relative in ANE_RUNTIME_ASSETS:
        path = resolve_manifest_path(root, relative)
        if not path.is_file():
            raise ValueError(f"ANE-Profilasset fehlt: {relative}")
        result.append({"path": relative, "sha256": sha256_file(path)})
    return result


def bind_attempt(root: Path, manifest: dict, attempt: dict, *, selected: dict | None = None,
                 manifest_sha256: str | None = None, persist: bool = True) -> dict:
    """Build a backend receipt and optionally atomically persist its legacy file."""
    selected = selected or manifest.get("render_execution")
    if not selected:
        selected = profile("gpu")
    assets = selected.get("assets") or []
    receipt = {
        "binding_version": "1",
        "manifest_sha256": manifest_sha256 or sha256(canonical_json(manifest)),
        "dialog_id": attempt["dialog_id"],
        "variant_id": attempt["variant_id"],
        "method": attempt["method"],
        "seed": attempt["seed"],
        "attempt_fingerprint": attempt["fingerprint"],
        "profile_id": selected["profile_id"],
        "operating_mode": selected["operating_mode"],
        "runtime_contract": selected["runtime_contract"],
        "model_identity": selected.get("model_identity"),
        "runtime": attempt["runtime"],
        "models": attempt["models"],
        "effective_parameters": selected["effective_parameters"],
        "backend_assets": assets,
    }
    if attempt.get("role_id") is not None:
        receipt['role_id'] = attempt['role_id']
    routing_policy=selected.get('routing_policy')
    if routing_policy is not None:
        receipt['routing_policy']=routing_policy
    record = {**receipt, "binding_sha256": sha256(canonical_json(receipt))}
    if not persist:
        return record
    path = resolve_manifest_path(root, manifest["output"]["root"] + "/bindings/" + attempt["fingerprint"] + ".json")
    if path.exists():
        return _read_same(path, record)
    try:
        _exclusive_json(path, record)
    except FileExistsError:
        return _read_same(path, record)
    return record


def _read_same(path: Path, expected: dict) -> dict:
    try:
        if path.is_symlink():
            raise ValueError("Symlink")
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ArtifactError("Backendzuordnung fehlt, ist beschädigt oder manipuliert") from exc
    if existing != expected:
        raise ArtifactError("Backendzuordnung weicht ab; wird nicht überschrieben")
    return existing
