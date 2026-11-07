"""Compose a local MI1 render manifest; no subprocesses or model loading."""
from __future__ import annotations

import json
from pathlib import Path

from .manifest import ManifestError, canonical_json, resolve_manifest, sha256
from .mi1_adapter import map_mi1_inputs
from validate_dubbing_manifest import file_digest


def compose_mi1_manifest(draft: dict, component: dict, asset_root: Path) -> dict:
    if "source_dialog_ids" not in component.get("scope", {}):
        raise ManifestError("Wizard export does not cover all source IDs; restart the updated Wizard and finish open IDs before exporting")
    root = asset_root.resolve(strict=True)
    mapped = map_mi1_inputs(draft, component)
    receipt = {k: v for k, v in component.items() if k != "generated_at"}
    component_hash = sha256(canonical_json(receipt))

    def binary(relative: str) -> dict:
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ManifestError(f"runtime artifact is outside asset root: {relative}")
        return {"path": path.relative_to(root).as_posix(), "sha256": file_digest(path),
                "media_type": "application/octet-stream"}

    artifacts = {key: binary(path) for key, path in {
        "isolated_cli": "bin/voxcpm2_cli", "combined_cli": "bin/voxcpm2_cli_combined", "ffmpeg": "bin/ffmpeg"}.items()}
    models = []
    # Reuse the source draft's recorded model fingerprints. --check-assets
    # verifies the large weights once during preflight, never loads the model.
    for evidence in draft["runtime_requirements_local_evidence"]["model_files_from_report"]:
        item = evidence["file"]
        path = root / item["path"]
        if not path.is_file() or path.stat().st_size != item["size_bytes"]:
            raise ManifestError(f"model file missing or size changed: {item['path']}")
        models.append({"path": item["path"], "sha256": item["sha256"], "media_type": "application/octet-stream"})
    model_version = "sha256:" + sha256(canonical_json(models))
    policy = {"short_if_original_duration_below_ms": 3000, "short_if_tts_words_below": 4,
        "word_count_rule": "unicode_whitespace_v1", "composite_join_silence_ms": 300,
        "composite_target_duration_ms": [4000, 15000], "composite_max_duration_ms": 15000,
        "prewarm_target_duration_ms": [3000, 4000], "prompt_order": ["main_anchor", "line_original", "german_prewarm"],
        "review_method_rules": ["seed", "anchor", "prewarm", "backend"],
        "render_strategy": {"standard_seeds": [101, 202],
            "escalation_order": ["without_emotion", "without_fixed_anchor", "main_anchor_only"],
            "stop_on_first_pass": True, "warn_regenerates": False}}
    manifest = {"schema_version": "1.0.0", "manifest_profile": "local_render",
        "manifest_id": "mi1:local-render", "manifest_revision": int(component_hash[:12], 16) or 1,
        "game": {"game_id": "mi1:ute", "title": draft["game_evidence"]["title"],
            "edition": "Ultimate Talkie Edition", "distribution": "local_extracted_audio",
            "game_version": draft["game_evidence"]["ute_version_from_readme"],
            "source_language": "en", "target_language": "de", "adapter_id": "mi1:source-adapter", "adapter_version": "1",
            "required_inputs": {"match_policy": "exact_files_sha256_v1", "base_files": [], "patches": [], "final_files": []}},
        **mapped, "project_policy": policy,
        "hardware": {"selection_policy": "first_satisfied_in_order", "profiles": []}, "requirements": [],
        "models": {"tts": {"model_id": "mi1:voxcpm2", "component_id": "mi1:voxcpm2-model-files", "version": model_version, "files": models}},
        "runtime": {"runtime_id": "mi1:voxcpm2-runtime", "runtime_version": "sha256:" + sha256(canonical_json(artifacts)),
            "backend": "metal", "sample_rate_hz": 48000, "channels": 1, "sample_format": "pcm_s16le",
            "tts_parameters": {}, "artifacts": artifacts},
        "qa": {"status": "not_configured", "policy_id": "mi1:qa-pending", "policy_version": "not_configured",
            "normalization_version": "not_configured", "decoder_parameters": {}, "thresholds": {}},
        "output": {"root": "work/manifest-render", "wav_directory": "wav", "render_report": "render-report.json",
            "qa_directory": "qa", "qa_summary": "qa-summary.json"}}
    manifest["source_evidence"].update({"component_content_sha256": component_hash,
        "pending_product_evidence": ["patch_chain", "hardware_minimums", "dependency_distribution", "stt_and_qa_policy"],
        "profile_limit": "all source IDs are accounted for; local render planning; QA acceptance and game patch construction are not configured"})
    resolve_manifest(manifest)
    return manifest


def prepare_mi1_manifest(draft_path: Path, component_path: Path, asset_root: Path) -> dict:
    try:
        return compose_mi1_manifest(json.loads(draft_path.read_text(encoding="utf-8")),
                                    json.loads(component_path.read_text(encoding="utf-8")), asset_root)
    except (OSError, ValueError, KeyError) as exc:
        raise ManifestError(f"cannot prepare MI1 render manifest: {exc}") from exc
