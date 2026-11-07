"""Project existing Wizard metadata into the launcher's portable recipe fields."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from .contracts import ContractError, canonical_json, digest

FORMAT = "mi1-local-preparation/1"
PREFIX = "mi1-ute-1.02:vctl:"
SEED = 20260926


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def project_manifest(manifest: dict, draft: dict, component: dict, state: dict,
                     metadata_root: Path, draft_sha256: str | None = None) -> tuple[dict, dict]:
    """Return a copied manifest with exact role/source metadata and provenance."""
    if component.get("status") != "complete":
        raise ContractError("Stimmanker-Komponente ist nicht vollständig")
    draft_sha = draft_sha256 or hashlib.sha256(canonical_json(draft)).hexdigest()
    if (component.get("source_manifest", {}).get("sha256") != draft_sha or
            state.get("source_manifest_sha256") != draft_sha):
        raise ContractError("Draft-, Component- und Wizardzustand sind nicht an dieselbe Quelle gebunden")
    draft_rows = {row.get("dialog_id"): row for row in draft.get("dialogues", [])}
    dialogues = manifest.get("dialogues", [])
    if (len(dialogues) != 4393 or len(draft_rows) != 4393 or
            len({row.get("dialog_id") for row in dialogues}) != 4393):
        raise ContractError("Vollständige eindeutige 4.393-Dialogquelle fehlt")
    sources = []
    for dialogue in dialogues:
        did = dialogue.get("dialog_id")
        source = draft_rows.get(did)
        offset = source.get("native_speech_id") if source else None
        archive_offset = source.get("source_rows", {}).get("speech_manifest_archive_offset") if source else None
        if (not isinstance(did, str) or not did.startswith(PREFIX) or
                not isinstance(offset, str) or not offset.isdigit() or
                offset != did[len(PREFIX):] or archive_offset != int(offset)):
            raise ContractError(f"Widersprüchliche VCTL-Quell-ID: {did}")
        sources.append({"dialog_id": did, "native_speech_id": offset,
            "source_kind": "ute_sou_vctl", "archive_stage": "ute_english", "archive_member": "monster.sou"})

    source_ids = {row["dialog_id"] for row in sources}
    exported_roles = {row.get("role_id"): row for row in manifest.get("roles", [])}
    component_roles = {row.get("role_id"): row for row in component.get("roles", [])}
    state_roles = state.get("roles", {})
    if len(exported_roles) != 42 or set(exported_roles) != set(component_roles):
        raise ContractError("Rollenbestand von Renderexport und Component stimmt nicht überein")
    if not set(exported_roles).issubset(set(state_roles) | set(state.get("role_rules", {}))):
        raise ContractError("Wizardzustand bindet nicht exakt alle Exportrollen")

    roles, report_receipts = [], []
    project_root = metadata_root.resolve(strict=True)
    policy = manifest.get("project_policy", {})
    duration = policy.get("composite_target_duration_ms")
    if duration != [4000, 15000]:
        raise ContractError("Fester Anchor-Dauerbereich weicht von der ausgewählten Manifestfassung ab")
    for role_id, exported in exported_roles.items():
        comp = component_roles[role_id]
        rules = state.get("role_rules", {}).get(role_id, {})
        selected = state_roles.get(role_id)
        anchor = comp.get("main_anchor")
        if rules.get("without_anchor") is True:
            if anchor or exported.get("main_anchor") or exported.get("prewarm") or selected:
                raise ContractError(f"Abweichende Ohne-Anchor-Regel für {role_id}")
            roles.append({"role_id": role_id, "without_anchor": True, "without_prewarm": True})
            continue
        if not isinstance(anchor, dict) or not isinstance(exported.get("main_anchor"), dict) or not isinstance(selected, dict):
            raise ContractError(f"Ausgewählter Anchor oder Wizardzustand fehlt: {role_id}")
        selected_anchors = selected.get("anchors") or []
        if len(selected_anchors) != 1:
            raise ContractError(f"Anzahl ausgewählter Anchors weicht ab: {role_id}")
        selected_anchor = selected_anchors[0]
        ordered_speech_ids = [anchor.get("anchor_dialog_id"), *anchor.get("anchor_component_dialog_ids", [])]
        wanted_component_ids = [PREFIX + str(value) for value in selected_anchor.get("component_speech_ids", [])]
        if (not ordered_speech_ids or any(value not in source_ids for value in ordered_speech_ids) or
                ordered_speech_ids != wanted_component_ids or
                selected_anchor.get("kind") != anchor.get("kind") or
                selected_anchor.get("join_silence_ms") != anchor.get("join_silence_ms") or
                type(selected_anchor.get("small_role_fallback")) is not bool or
                exported["main_anchor"].get("prompt_text") != anchor.get("prompt_text")):
            raise ContractError(f"Anchor-Reihenfolge/Join/Prompt stimmt nicht mit Wizardauswahl überein: {role_id}")
        recipe = {"role_id": role_id, "reference": {"kind": anchor["kind"],
            "source_dialog_ids": ordered_speech_ids, "prompt_text": anchor["prompt_text"],
            "join_silence_ms": anchor["join_silence_ms"],
            "minimum_duration_ms": duration[0], "maximum_duration_ms": duration[1],
            "small_role_fallback": bool(selected_anchor.get("small_role_fallback"))}}
        prewarm = exported.get("prewarm")
        prewarm_id = selected.get("prewarm_id")
        if not isinstance(prewarm, dict) or prewarm_id is None or prewarm.get("selection", {}).get("status") != "selected":
            raise ContractError(f"Gewählter Rollenprewarm fehlt: {role_id}")
        target_id = str(prewarm_id)
        target_text = prewarm.get("text")
        target_hash = hashlib.sha256(target_text.encode("utf-8")).hexdigest() if isinstance(target_text, str) else None
        if (prewarm.get("text_sha256") != target_hash or
                (prewarm.get("source") == "custom_text" and target_id != "text:" + target_hash)):
            raise ContractError(f"Prewarm-Text oder Custom-ID stimmt nicht: {role_id}")
        audio_path = prewarm.get("audio", {}).get("path")
        if not isinstance(audio_path, str) or Path(audio_path).is_absolute() or ".." in Path(audio_path).parts:
            raise ContractError(f"Prewarmreportpfad ist nicht relativ gebunden: {role_id}")
        report_path = project_root / audio_path
        if report_path.is_symlink() or not report_path.is_file():
            raise ContractError(f"Gewählter Prewarmreport fehlt: {role_id}")
        report_file = report_path.parent / "report.json"
        if report_file.is_symlink() or not report_file.is_file():
            raise ContractError(f"Gewählter Prewarmreport fehlt: {role_id}")
        report = _read(report_file)
        verified = report.get("verified_inputs", {})
        target = verified.get("target", {})
        anchor_audio_hash = exported["main_anchor"].get("audio", {}).get("sha256")
        component_hashes = [item.get("sha256") for item in anchor.get("source_audio", [])]
        if (report.get("status") != "ok" or verified.get("kind") != "prewarm" or
                verified.get("seed") != SEED or target.get("speech_id") != target_id or
                target.get("target_text_sha256") != target_hash or report.get("target_text_sha256") != target_hash or
                verified.get("reference_sha256") != anchor_audio_hash or
                verified.get("reference_component_sha256") != component_hashes or
                not isinstance(verified.get("binaries"), dict) or not verified["binaries"] or
                not isinstance(verified.get("models"), dict) or not verified["models"]):
            raise ContractError(f"Prewarmreport ist nicht an Seed/Target/Text/Anchor gebunden: {role_id}")
        source = prewarm.get("source")
        if source not in {"dialog", "neutral_standard", "custom_text"}:
            raise ContractError(f"Nicht unterstützte gewählte Prewarmquelle: {role_id}")
        recipe["prewarm"] = {"source": source, "target_id": target_id, "text": target_text,
            "reference_mode": "shared_anchor", "seed": verified["seed"],
            "generation_report_sha256": hashlib.sha256(report_file.read_bytes()).hexdigest(),
            "runtime_inputs": {"binaries": verified["binaries"], "models": verified["models"]}}
        if source == "neutral_standard":
            recipe["prewarm"]["neutral_version"] = "mi1-neutral-prewarm/2"
        roles.append(recipe)
        report_receipts.append({"role_id": role_id, "sha256": recipe["prewarm"]["generation_report_sha256"]})

    result = copy.deepcopy(manifest)
    evidence = result.setdefault("source_evidence", {})
    if "launcher_preparation" in evidence:
        raise ContractError("Manifest enthält bereits eine Launcher-Projektion")
    evidence["launcher_preparation"] = {"format": FORMAT, "sources": sources, "roles": roles,
        "stages": {"builder": "UTE 1.02 original install_flac.bat", "patch": "German UTE 1.01 patch"},
        "runtime": {"recipe": "P17", "prewarm_seed": SEED,
                    "role_report_sha256": digest(sorted(report_receipts, key=lambda item: item["role_id"]))}}
    provenance = {"format": "mi1-launcher-metadata-projection/1",
        "manifest_id": result["manifest_id"], "source_manifest_sha256": digest(manifest),
        "draft_sha256": draft_sha, "component_sha256": digest(component),
        "state_sha256": digest(state), "dialogues": len(sources), "roles": len(roles),
        "verified_prewarm_reports": len(report_receipts),
        "prewarm_report_sha256": report_receipts}
    return result, provenance


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--component", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--metadata-root", type=Path, required=True,
        help="Projektwurzel für bereits exportierte Audio-Metadatenreports; Dateien werden nicht kopiert")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    base = _read(args.manifest)
    projected, evidence = project_manifest(base, _read(args.draft), _read(args.component),
        _read(args.state), args.metadata_root, hashlib.sha256(args.draft.read_bytes()).hexdigest())
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "manifest.json").write_bytes(canonical_json(projected) + b"\n")
    (args.output / "projection-evidence.json").write_bytes(canonical_json(evidence) + b"\n")
    print(json.dumps({"manifest": str(args.output / "manifest.json"),
        "projection_evidence": str(args.output / "projection-evidence.json"),
        "dialogues": evidence["dialogues"], "roles": evidence["roles"],
        "verified_prewarm_reports": evidence["verified_prewarm_reports"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
