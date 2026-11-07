"""Create portable standard generator defaults from corrected MI1 metadata.

This is a deterministic stdlib-only data projection. It never copies audio,
models, saved review rows, or prior render receipts into the distributed data.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def read(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def portable_manifest(source: dict, recipes_from: dict, state: dict) -> tuple[dict, dict, dict]:
    result = json.loads(json.dumps(source, ensure_ascii=False))
    old_evidence = recipes_from.get("source_evidence", {})
    recipes = old_evidence.get("launcher_preparation")
    if not isinstance(recipes, dict) or recipes.get("format") != "mi1-local-preparation/1":
        raise ValueError("Keine bestehende launcher_preparation/1-Rollenprojektion vorhanden")
    if len(result.get("dialogues", [])) != 4393 or len(result.get("roles", [])) != 42:
        raise ValueError("Die korrigierte Quelle muss 4.393 Dialoge und 42 Rollen enthalten")

    # The corrected source already contains every selected text change and
    # original choice. Keep its actual effective runtime, QA, scope and policy.
    result["output"]["root"] = "output"
    # This is a previous renderer execution receipt (profile/model-loaded
    # state), not a durable recipe for the distributed app. Setup and the
    # user's later run choice own that state.
    result.pop("render_execution", None)
    # A repaired declaration is a future recipe, not a past execution receipt.
    # Preserve its selection while setup supplies this host's assets/worker.
    execution_repair = source.get("source_evidence", {}).get("execution_repair")
    seed_inputs = source.get("source_evidence", {}).get("launcher_render_inputs", {})
    if execution_repair:
        selection = source["render_execution"]
        result["render_execution"] = {key: selection[key] for key in (
            "profile_id", "operating_mode", "dispatch_profiles", "effective_parameters",
            "runtime_contract", "routing_policy")}
        result["render_execution"]["assets"] = []
    # These statuses describe past text/audio review and cannot authorize any
    # output from a clean distributed installation.
    reset_review_statuses = 0
    for dialogue in result["dialogues"]:
        for variant in dialogue.get("text_variants", []):
            reset_review_statuses += variant.get("review_status") != "unreviewed"
            variant["review_status"] = "unreviewed"
    result["source_evidence"] = {
        "draft_format": source.get("source_evidence", {}).get("draft_format"),
        "source_inventory_complete": True,
        "source_dialog_ids": list(source.get("source_evidence", {}).get("source_dialog_ids", [])),
        "copy_original_dialog_ids": [d["dialog_id"] for d in result["dialogues"]
                                      if d.get("processing") == "copy_original"],
        "scope_render_count": sum(d.get("processing") == "render" for d in result["dialogues"]),
        "profile_limit": source.get("source_evidence", {}).get("profile_limit"),
        "source_manifest": {
            "root_id": source.get("source_evidence", {}).get("source_manifest", {}).get("root_id"),
            "sha256": source.get("source_evidence", {}).get("source_manifest", {}).get("sha256"),
        },
        "text_correction": {
            key: value for key, value in source.get("source_evidence", {}).get("text_correction", {}).items()
            if key != "normalizer_path"
        },
        "reviewwizard_import": {
            "format": "mi1-reviewwizard-import-summary/1",
            "revision": state.get("revision"),
            "saved_rows": len(state.get("rows", {})),
            "state_file_sha256": source.get("source_evidence", {}).get("reviewwizard_import", {}).get("state_file_sha256"),
            "review_manifest_file_sha256": source.get("source_evidence", {}).get("reviewwizard_import", {}).get("review_manifest_file_sha256"),
            "source_manifest_sha256": state.get("source_manifest_sha256"),
            "additional_saved_text_imports": source.get("source_evidence", {}).get("reviewwizard_import", {}).get("additional_saved_text_imports"),
            "pending_presets": "encoded in the identity-bound correction rows where supported; saved audio selections and attempt history omitted",
        },
        "launcher_preparation": recipes,
    }
    if execution_repair:
        result["source_evidence"]["launcher_render_inputs"] = json.loads(json.dumps(seed_inputs))
        result["source_evidence"]["execution_repair"] = {
            key: execution_repair[key] for key in (
                "version", "source_r1_manifest_sha256", "seed_projection_contract",
                "operating_mode", "routing_policy")}

    # Patch-review's effective manifest supports the pending next-preset
    # controls. Drop selection/candidate/audio fields so no legacy WAV, seed
    # receipt, run, or reviewed-audio binding can be reused.
    by_id = {d["dialog_id"]: d for d in result["dialogues"]}
    rows = state.get("rows", {})
    if not isinstance(rows, dict):
        raise ValueError("Gespeicherter Reviewzustand enthält keine Dialogtabelle")
    correction_rows: dict[str, dict] = {}
    preset_counts: collections.Counter[str] = collections.Counter()
    skipped_anchor_refs = 0
    backend_counts: collections.Counter[str] = collections.Counter()
    actionable_fresh_seeds = 0
    for dialog_id, saved in rows.items():
        if dialog_id not in by_id:
            raise ValueError(f"Reviewzustand enthält unbekannte Dialog-ID: {dialog_id}")
        preset = saved.get("next_preset", {})
        if not isinstance(preset, dict):
            raise ValueError(f"Ungültige next_preset-Angabe: {dialog_id}")
        if saved.get("anchor_ref"):
            skipped_anchor_refs += 1
            continue
        # Do not carry the selected audio, candidate list, prior seed, backend
        # request, or attempt data. Text itself is already in the corrected
        # base; equal text fields below let the current resolver validate the
        # row while applying only its supported settings.
        dialogue = by_id[dialog_id]
        selected = next((v for v in dialogue.get("text_variants", [])
                         if v.get("variant_id") == dialogue.get("selected_variant_id")), None)
        row: dict = {}
        generation_mode = saved.get("generation_mode", "synthesize")
        if generation_mode not in ("synthesize", "copy_original"):
            raise ValueError(f"Nicht unterstützter Ausgabemodus für {dialog_id}")
        row["generation_mode"] = generation_mode
        row["method"] = saved.get("method", "seed")
        if selected:
            row["tts_text"] = selected["tts_text"]
            row["qa_expected_text"] = selected["qa_expected_text"]
            text_revision = saved.get("text_revision", 1)
            if (selected["tts_text"] != saved.get("tts_text") or
                    selected["qa_expected_text"] != saved.get("qa_expected_text")):
                text_revision = max(text_revision + 1, selected.get("variant_revision", 1))
            row["text_revision"] = text_revision
        safe_preset = {key: preset.get(key, "keep") for key in ("seed", "anchor", "prewarm")}
        if (safe_preset["seed"] not in ("keep", "fresh") or
                any(safe_preset[key] not in ("keep", "on", "off") for key in ("anchor", "prewarm"))):
            raise ValueError(f"Nicht unterstützte Presetangabe: {dialog_id}")
        row["next_preset"] = safe_preset
        backend = saved.get("backend")
        if backend in ("gpu", "cpu", "ane"):
            row["backend"] = backend
            backend_counts[backend] += 1
        # Complete sanitized saved() shape, so import_extension remains a
        # normal editable review import. No saved selection/candidate/audio
        # binding or note content crosses the package boundary.
        row.setdefault("tts_text", selected["tts_text"] if selected else "")
        row.setdefault("qa_expected_text", selected["qa_expected_text"] if selected else "")
        row.setdefault("text_revision", 1)
        row.update(note="", note_binding=None, note_history=[], marked=saved.get("marked") is True,
                   anchor_ref=None, backend=row.get("backend"), good_binding=None,
                   selection=None, candidates=[], revisions=[], releases=[],
                   processing_binding=None, accepted_probe=None, seed_defaults_version=2)
        saved_baseline = saved.get("action_baseline", {})
        saved_baseline_preset = saved_baseline.get("next_preset", {})
        baseline_preset = {
            "seed": saved_baseline_preset.get("seed", "fresh"),
            "anchor": saved_baseline_preset.get("anchor", "keep"),
            "prewarm": saved_baseline_preset.get("prewarm", "keep"),
        }
        if (baseline_preset["seed"] not in ("keep", "fresh") or
                any(baseline_preset[key] not in ("keep", "on", "off") for key in ("anchor", "prewarm"))):
            raise ValueError(f"Nicht unterstützte Aktionsbasis: {dialog_id}")
        if safe_preset["seed"] == "fresh" and (saved.get("marked") is True or baseline_preset["seed"] != "fresh"):
            actionable_fresh_seeds += 1
        row["action_baseline"] = {
            "tts_text": row["tts_text"],
            "qa_expected_text": row["qa_expected_text"],
            "next_preset": baseline_preset,
            "anchor_ref": None,
            "generation_mode": "synthesize",
            "backend": None,
        }
        if execution_repair and dialog_id in seed_inputs:
            # Fresh has already been resolved into this immutable seed pair.
            # Reapplying a sanitized export would choose a different pair.
            row["next_preset"]["seed"] = "keep"
            row["action_baseline"]["next_preset"]["seed"] = "keep"
        correction_rows[dialog_id] = row
        preset_counts[json.dumps(safe_preset, sort_keys=True)] += 1

    correction = {
        "format": "patch-review-extension/1",
        "manifest_id": result["manifest_id"],
        "source_manifest_sha256": sha(result),
        "game_id": result["game"]["game_id"],
        "method_rules": result["project_policy"]["review_method_rules"],
        "rows": correction_rows,
    }
    summary = {
        "format": "mi1-portable-default-projection/1",
        "source_manifest_revision": source.get("manifest_revision"),
        "source_manifest_canonical_sha256": sha(source),
        "manifest_id": result["manifest_id"],
        "manifest_revision": result.get("manifest_revision"),
        "manifest_canonical_sha256": sha(result),
        "correction_canonical_sha256": sha(correction),
        "correction_source_manifest_sha256": correction["source_manifest_sha256"],
        "dialogues": len(result["dialogues"]),
        "render_choices": sum(d.get("processing") == "render" for d in result["dialogues"]),
        "original_choices": len(result["source_evidence"]["copy_original_dialog_ids"]),
        "roles": len(result["roles"]),
        "portable_role_recipes": len(recipes.get("roles", [])),
        "portable_source_mappings": len(recipes.get("sources", [])),
        "correction_rows": len(correction_rows),
        "saved_review_rows": len(rows),
        "reset_text_review_statuses": reset_review_statuses,
        "pending_presets": dict(preset_counts),
        "actionable_fresh_seed_requests": actionable_fresh_seeds,
        "pending_backends": dict(backend_counts),
        "unsupported_alternate_anchor_rows_skipped": skipped_anchor_refs,
        "omitted": ["saved_state row history", "prior audio selections and approvals", "WAV/model payloads", "attempt reports and logs", "host-local absolute paths"],
    }
    return result, correction, summary


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True,
                        help="Corrected 2026-10-09 asset-full manifest")
    parser.add_argument("--recipes-from", type=Path, required=True,
                        help="Existing portable projection carrying exact role recipes")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    source = read(args.source)
    prior = read(args.recipes_from)
    state = source.get("source_evidence", {}).get("reviewwizard_import", {}).get("saved_state")
    if not isinstance(source, dict) or not isinstance(prior, dict) or not isinstance(state, dict):
        raise ValueError("Korrigierte Quelle, Recipebasis oder gespeicherter Zustandsbeleg fehlt")
    manifest, correction, summary = portable_manifest(source, prior, state)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (("manifest.json", manifest), ("correction.json", correction),
                        ("projection-evidence.json", summary)):
        (args.output_dir / name).write_bytes(canonical(value) + b"\n")


if __name__ == "__main__":
    main()
