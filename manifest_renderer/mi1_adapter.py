"""Map the MI1 source draft and reviewed role component to renderer records.

This adapter deliberately carries source evidence forward without treating the
draft's project-root labels as renderer paths.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _variant_sha(candidate: dict[str, Any]) -> str:
    values = [candidate["display_text"], candidate["tts_text"], candidate["qa_expected_text"], candidate["qa_allowed_variants"]]
    serial = json.dumps(values, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(serial).hexdigest()


def _path_asset(asset: dict[str, Any], hash_key: str = "sha256") -> dict[str, Any]:
    path = asset.get("path")
    if not isinstance(path, str) or not path or "\\" in path:
        raise ValueError("asset path must be a non-empty POSIX relative path")
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"unsafe asset path: {path!r}")
    digest = asset.get(hash_key)
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"asset is missing a valid {hash_key}")
    result = {"path": path, "sha256": digest, "media_type": asset.get("media_type")}
    for key in ("duration_ms", "sample_rate_hz", "channels"):
        if isinstance(asset.get(key), int) and asset[key] > 0:
            result[key] = asset[key]
    if not isinstance(result["media_type"], str) or not result["media_type"]:
        raise ValueError("asset is missing media_type")
    return result


def _source_text(text: str | None, provenance: dict[str, Any], verification: str) -> dict[str, Any]:
    if not text:
        return {
            "text": "",
            "sha256": _sha_text(""),
            "provenance": {"kind": "inferred", "source_ref": "MI1 source draft", "source_version": "1"},
            "verification": "unavailable",
        }
    return {
        "text": text,
        "sha256": _sha_text(text),
        "provenance": provenance,
        "verification": verification,
    }


def _variant(candidate: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(candidate, dict):
        return []
    # Keep only renderer-schema fields, preserving their source values verbatim.
    keys = (
        "variant_id", "variant_revision", "display_text", "tts_text", "qa_expected_text",
        "qa_allowed_variants", "sha256", "provenance", "creation_version", "edit_version",
        "edit_status", "review_status", "normalization_steps",
    )
    row = {key: candidate[key] for key in keys if key in candidate}
    if not all(key in row for key in keys) or not row.get("display_text") or not row.get("tts_text"):
        return []
    return [row]


def _prewizard_variant(exported: dict[str, Any] | None, fallback: dict[str, Any] | None,
                       dialog_id: str) -> list[dict[str, Any]]:
    """Project the optional text-prewizard export into the existing variant contract."""
    if not isinstance(exported, dict):
        return _variant(fallback)
    display = exported.get("display_text")
    tts = exported.get("tts_text")
    if not isinstance(display, str) or not display or not isinstance(tts, str) or not tts:
        return _variant(fallback)
    row = {
        "variant_id": exported.get("variant_id") or f"{dialog_id}:de:prewizard-{_sha_text(tts)[:16]}",
        "variant_revision": exported.get("variant_revision", 1),
        "display_text": display,
        "tts_text": tts,
        "qa_expected_text": exported.get("qa_expected_text") or tts,
        "qa_allowed_variants": exported.get("qa_allowed_variants", []),
        "provenance": exported.get("provenance") or {
            "kind": "normalization_rule", "source_ref": "text_prewizard", "source_version": "1"},
        "creation_version": exported.get("creation_version", "mi1-text-prewizard:1"),
        "edit_version": exported.get("edit_version", "1"),
        "edit_status": exported.get("edit_status", "imported"),
        "review_status": exported.get("review_status", "unreviewed"),
        "normalization_steps": exported.get("normalization_steps", []),
    }
    if type(row["variant_revision"]) is not int or row["variant_revision"] < 1:
        raise ValueError(f"invalid prewizard variant revision for {dialog_id}")
    row["sha256"] = _variant_sha(row)
    return [row]


def _decision_map(component: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for decision in component.get("dialogue_decisions", []):
        dialog_id = decision.get("dialog_id")
        if dialog_id in result:
            raise ValueError(f"duplicate component decision: {dialog_id}")
        if decision.get("action") not in {"copy_original", "assign_existing", "exclude", "technical_role"}:
            raise ValueError(f"unsupported decision for {dialog_id}: {decision.get('action')!r}")
        result[dialog_id] = decision
    return result


def _source_draft_sha256(draft: dict[str, Any]) -> str:
    # Source drafts are emitted as UTF-8, two-space JSON with a final newline.
    payload = json.dumps(draft, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    return hashlib.sha256(payload).hexdigest()


def _map_role(source: dict[str, Any]) -> dict[str, Any]:
    role = {
        "role_id": source["role_id"],
        "name": source["name"],
        "speaker_evidence": "; ".join(source.get("speaker_evidence", {}).get("evidence", [])) or source["role_id"],
        "prewarm_for_long_text": bool(source.get("prewarm_for_long_text", False)),
        "use_voice_anchor_for_long_text": bool(source.get("use_voice_anchor_for_long_text", False)),
    }
    anchor_data = source.get("main_anchor")
    without_anchor = source.get("without_anchor", False)
    without_prewarm = source.get("without_prewarm", False)
    if isinstance(anchor_data, dict) and not without_anchor:
        materialized = anchor_data.get("materialized_audio")
        if not isinstance(materialized, dict):
            raise ValueError(f"role {source['role_id']} has no materialized main anchor")
        role["main_anchor"] = {
            "kind": anchor_data["kind"],
            "anchor_dialog_id": anchor_data["anchor_dialog_id"],
            "anchor_component_dialog_ids": list(anchor_data.get("anchor_component_dialog_ids", [])),
            "audio": _path_asset(materialized),
            "source_audio": [_path_asset(asset) for asset in anchor_data["source_audio"]],
            "prompt_text": anchor_data["prompt_text"],
            "prompt_text_sha256": anchor_data["prompt_text_sha256"],
            "selection_evidence": anchor_data.get("selection_evidence", "component role selection"),
        }
        selected = source.get("main_anchor_selection", {})
        selection = {"status": "selected", "evidence_ref": selected.get("evidence_ref", "voice_anchor_wizard_data/state.json")}
        preview = selected.get("preview_audio")
        if isinstance(preview, dict):
            selection["preview_audio"] = _path_asset(preview)
        role["main_anchor_selection"] = selection
        role["backup_anchors"] = []
        prewarm = source.get("prewarm")
        if isinstance(prewarm, dict) and prewarm.get("technical_check") == "pass" and isinstance(prewarm.get("audio"), dict):
            data = {
                "source": prewarm.get("source", "neutral_standard"),
                "generated_with_anchor_dialog_id": anchor_data["anchor_dialog_id"],
                "generated_with_anchor_audio_sha256": materialized["sha256"],
                "audio": _path_asset(prewarm["audio"]),
                "text": prewarm["text"],
                "text_sha256": prewarm["text_sha256"],
                "technical_check": "pass",
                "selection": {"status": "selected", "evidence_ref": prewarm.get("selection", {}).get("evidence_ref", "voice_anchor_wizard_data/state.json")},
            }
            if prewarm.get("source") == "dialog" and prewarm.get("prewarm_dialog_id"):
                data["prewarm_dialog_id"] = prewarm["prewarm_dialog_id"]
            role["prewarm"] = data
        elif without_prewarm is True:
            role["without_prewarm"] = True
    else:
        if without_anchor is False:
            raise ValueError(f"role {source['role_id']} has no main anchor and does not waive it")
        role["without_anchor"] = True
        if without_prewarm is True:
            role["without_prewarm"] = True
        line_prewarm = source.get("prewarm")
        if isinstance(line_prewarm, dict):
            descriptor = {
                "source": line_prewarm.get("source"),
                "text": line_prewarm.get("text"),
                "text_sha256": line_prewarm.get("text_sha256"),
                "reference_mode": "line_original",
            }
            if line_prewarm.get("prewarm_dialog_id"):
                descriptor["prewarm_dialog_id"] = line_prewarm["prewarm_dialog_id"]
            selection = line_prewarm.get("selection")
            if isinstance(selection, dict):
                descriptor["selection"] = {"status": "selected", "evidence_ref": selection.get("evidence_ref", "voice_anchor_wizard_data/state.json")}
            if descriptor["source"] and descriptor["text"] and descriptor["text_sha256"]:
                role["prewarm"] = descriptor
    return role


def map_mi1_inputs(draft: dict[str, Any], component: dict[str, Any]) -> dict[str, Any]:
    """Return renderer ``scope``, ``roles``, ``dialogues`` and evidence receipts.

    New components account for the complete source inventory as render,
    copy-original, or explicit exclusion. Older components retain subset mode.
    """
    if not isinstance(draft, dict) or not isinstance(component, dict):
        raise ValueError("draft and component must be objects")
    if draft.get("draft_format") != "mi1-manifest-v1-source-draft/1":
        raise ValueError("unsupported MI1 source draft format")
    if component.get("format") != "mi1-manifest-role-component/2":
        raise ValueError("unsupported MI1 role component format")
    if component.get("status") != "complete":
        raise ValueError("MI1 role component is not complete")
    source_receipt = component.get("source_manifest", {})
    expected_hash = source_receipt.get("sha256")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("role component is missing a valid source draft SHA-256")
    if expected_hash != _source_draft_sha256(draft):
        raise ValueError("role component was produced from a different source draft")
    source_rows = draft.get("dialogues", [])
    rows = {row["dialog_id"]: row for row in source_rows}
    if len(rows) != len(source_rows):
        raise ValueError("source draft contains duplicate dialogue IDs")
    scope_component = component.get("scope", {})
    render_ids = scope_component.get("dialog_ids")
    if not isinstance(render_ids, list) or len(render_ids) != scope_component.get("dialog_count"):
        raise ValueError("component render scope is incomplete")
    if len(set(render_ids)) != len(render_ids) or any(dialog_id not in rows for dialog_id in render_ids):
        raise ValueError("component render scope contains duplicate or missing source IDs")
    decisions = _decision_map(component)
    full_inventory = "source_dialog_ids" in scope_component
    if full_inventory:
        source_ids = scope_component["source_dialog_ids"]
        if not isinstance(source_ids, list) or len(source_ids) != len(rows) or len(set(source_ids)) != len(source_ids) or set(source_ids) != set(rows):
            raise ValueError("component source inventory does not exactly match source draft IDs")
        if any(dialog_id not in rows for dialog_id in decisions):
            raise ValueError("component decision has no source row")
        for dialog_id, decision in decisions.items():
            if decision["action"] == "exclude" and not decision.get("reason"):
                raise ValueError(f"explicit exclusion has no reason: {dialog_id}")
        render_set = set(render_ids)
        for dialog_id in source_ids:
            decision = decisions.get(dialog_id)
            if dialog_id not in render_set and not (decision and decision["action"] in {"copy_original", "exclude"}):
                raise ValueError(f"source dialogue has no render, copy, or exclusion decision: {dialog_id}")
            if dialog_id in render_set and decision and decision["action"] in {"copy_original", "exclude"}:
                raise ValueError(f"render scope conflicts with {decision['action']} decision: {dialog_id}")
    included = list(render_ids)
    for dialog_id, decision in decisions.items():
        if decision["action"] == "copy_original":
            if dialog_id not in rows:
                raise ValueError(f"copy-original decision has no source row: {dialog_id}")
            if dialog_id not in included:
                included.append(dialog_id)
    excluded_decisions = {did: decision for did, decision in decisions.items() if decision["action"] == "exclude"}
    included = [did for did in included if did not in excluded_decisions]
    role_sources = {role["role_id"]: role for role in component.get("roles", [])}
    if len(role_sources) != len(component.get("roles", [])):
        raise ValueError("role component contains duplicate role IDs")
    roles = [_map_role(role) for role in component.get("roles", [])]
    text_block = component.get("text_prewizard")
    text_rows = {}
    if text_block is not None:
        if not isinstance(text_block, dict) or text_block.get("format") != "mi1-text-pipeline-export/1":
            raise ValueError("unsupported text_prewizard export")
        for item in text_block.get("dialogues", []):
            sid = item.get("speech_id")
            if not isinstance(sid, str) or sid in text_rows:
                raise ValueError(f"invalid or duplicate text_prewizard speech ID: {sid!r}")
            text_rows[sid] = item
    override_rows = {}
    for item in component.get("dialogue_overrides", []):
        sid = item.get("speech_id")
        if not isinstance(sid, str) or sid in override_rows:
            raise ValueError(f"invalid or duplicate dialogue override speech ID: {sid!r}")
        override_rows[sid] = item
    role_ids = {role["role_id"] for role in roles}
    dialogues = []
    for dialog_id in included:
        row = rows[dialog_id]
        decision = decisions.get(dialog_id)
        processing = "copy_original" if decision and decision["action"] == "copy_original" else "render"
        source_audio = row.get("original_audio")
        if not isinstance(source_audio, dict):
            raise ValueError(f"dialogue has no original audio: {dialog_id}")
        audio = {"path": source_audio["path"], "sha256": source_audio.get("sha256_from_extraction"), "media_type": source_audio.get("media_type")}
        for key, source_key in (("duration_ms", "duration_ms_from_extraction"), ("sample_rate_hz", "sample_rate_hz_from_extraction"), ("channels", "channels_from_extraction")):
            if source_audio.get(source_key):
                audio[key] = source_audio[source_key]
        audio = _path_asset(audio)
        english = row.get("english_text_candidate") or {}
        text = english.get("text")
        status = english.get("status")
        unclear = row.get("unclear_resolution") or {}
        verification = "exact" if status in {"unique", "exact"} else "inferred_reviewed" if status in {"inferred_reviewed", "reviewed"} else "unavailable" if not text else "inferred_unreviewed"
        provenance = {"kind": "game", "source_ref": english.get("source", "MI1 English source"), "source_version": "MI1 Ultimate Talkie source"}
        original_text = _source_text(text, provenance, verification)
        speech_id = str(row.get("native_speech_id", ""))
        variant = _prewizard_variant(text_rows.get(speech_id), row.get("text_variant_candidate"), dialog_id)
        if not variant and unclear.get("confirmed_processing") == "render":
            decision_text = unclear.get("text_decision", {})
            tts_text = decision_text.get("tts_text") or decision_text.get("audition_tts_text")
            display = decision_text.get("display_text_from_review") or tts_text
            if tts_text and display:
                variant = [{"variant_id": dialog_id + ":de:reviewed", "variant_revision": 1, "display_text": display,
                    "tts_text": tts_text, "qa_expected_text": tts_text, "qa_allowed_variants": [], "sha256": "",
                    "provenance": {"kind": "manual", "source_ref": "unclear_resolution.text_decision", "source_version": "reviewed"},
                    "creation_version": "mi1-source-adapter:1", "edit_version": "1", "edit_status": "user_edited",
                    "review_status": "approved", "normalization_steps": []}]
                variant[0]["sha256"] = _variant_sha(variant[0])
        chosen = variant[0] if variant else None
        if processing == "render" and chosen is None:
            raise ValueError(f"render dialogue has no reviewed or imported German variant: {dialog_id}")
        role_id = (decision.get("target_role_id") if decision and decision.get("action") in {"assign_existing", "technical_role"} else None)
        if role_id is None:
            role_id = row.get("speaker_candidate", {}).get("role_id") or next((rid for rid, role in role_sources.items() if role.get("speaker_evidence", {}).get("speaker_code") == row.get("speaker_candidate", {}).get("code")), None)
        if role_id not in role_ids and processing == "render":
            # The role component uses the stable speaker-code namespace.
            role_id = next((rid for rid in role_ids if rid.endswith(":voice-code:" + str(row.get("speaker_candidate", {}).get("code", "")))), None)
        if processing == "render" and role_id not in role_ids:
            raise ValueError(f"render dialogue has no selected role: {dialog_id}")
        if processing == "copy_original" and role_id is None:
            role_id = (decision or {}).get("source_role_id")
            if not role_id:
                code = row.get("speaker_candidate", {}).get("code") or "unclassified"
                role_id = f"mi1:source-speaker:{code}"
        explicit_content_type = decision.get("content_type") if decision else None
        out = {"dialog_id": dialog_id, "role_id": role_id,
            "content_type": explicit_content_type or ("speech" if processing == "render" else "unknown"), "processing": processing,
            "original_audio": audio, "original_text": original_text,
            "performance_prompt": "original_audio_and_text" if verification in {"exact", "inferred_reviewed"} else "none",
            "performance_prompt_missing_reason": "none" if verification in {"exact", "inferred_reviewed"} else "performance_prompt_missing",
            "output_stem": row.get("native_speech_id", dialog_id.rsplit(":", 1)[-1])}
        if chosen:
            out["selected_variant_id"] = chosen["variant_id"]
            out["text_variants"] = variant
        else:
            out["selected_variant_id"] = None
            out["text_variants"] = []
        override = override_rows.get(speech_id)
        if override is not None:
            values = {key: override.get(key, "inherit") for key in ("anchor", "prewarm")}
            if any(value not in {"inherit", "use", "skip"} for value in values.values()):
                raise ValueError(f"invalid render override for {dialog_id}")
            if values != {"anchor": "inherit", "prewarm": "inherit"}:
                out["render_overrides"] = values
        dialogues.append(out)
    outside = sorted(set(rows) - set(included) - set(excluded_decisions))
    excluded = ([{"dialog_id": did, "reason": "deferred_by_user", "note": decision["reason"]}
                 for did, decision in excluded_decisions.items()] if full_inventory else [])
    excluded.extend({"dialog_id": did, "reason": "outside_scope"} for did in outside)
    return {
        "scope": {"kind": "complete_game" if full_inventory else "wizard_subset", "selection_complete": True, "included_dialog_ids": included,
                  "excluded": excluded},
        "roles": roles,
        "dialogues": dialogues,
            "source_evidence": {
            "draft_format": draft.get("draft_format"),
            "component_format": component.get("format"),
            "source_manifest": component.get("source_manifest"),
            "scope_render_count": len(render_ids),
            "source_inventory_complete": full_inventory,
            **({"source_dialog_ids": list(component["scope"]["source_dialog_ids"])} if full_inventory else {}),
            "copy_original_dialog_ids": sorted(did for did, decision in decisions.items() if decision["action"] == "copy_original"),
        },
    }
