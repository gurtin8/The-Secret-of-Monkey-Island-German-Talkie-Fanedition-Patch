"""Textless manifest recipes and deterministic local reconstruction.

Recipes carry stable IDs, source locations, hashes and context-free edits.  They
never carry the source sentence or deleted text.  Local export indexes are
expected as ``{locale: {speech_id: text_or_occurrence}}``; occurrence lists are
supported when a source format contains multiple rows for an ID.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from difflib import SequenceMatcher
from typing import Any, Mapping


FORMAT = "mi1-textless-recipe/1"
MARKER = "text_reconstruction"
NEUTRAL_PREWARM_TEXTS = {
    "Ich bin bereit.",
    "Jetzt kann es losgehen.",
    "Alles klar, wir können beginnen.",
    "Wir sind jetzt bereit und können gemeinsam beginnen.",
}
_TEXT_VARIANT_KEYS = {"display_text", "tts_text", "qa_expected_text"}


class ReconstructionError(ValueError):
    """The recipe cannot be reconstructed from the supplied local sources."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _pointer(parts: tuple[str | int, ...]) -> str:
    def escape(part: str | int) -> str:
        return str(part).replace("~", "~0").replace("/", "~1")
    return "/" + "/".join(escape(part) for part in parts)


def _at(root: Any, parts: tuple[str | int, ...]) -> Any:
    value = root
    for part in parts:
        value = value[part]
    return value


def _set(root: Any, parts: tuple[str | int, ...], value: Any) -> None:
    parent = _at(root, parts[:-1])
    parent[parts[-1]] = value


def _speech_id(dialogue: Mapping[str, Any]) -> str:
    return str(dialogue.get("output_stem") or str(dialogue["dialog_id"]).rsplit(":", 1)[-1])


def _source_evidence_by_dialog(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    evidence = manifest.get("source_evidence", {}).get("launcher_preparation", {})
    for row in evidence.get("sources", []) if isinstance(evidence, dict) else []:
        dialog_id = row.get("dialog_id")
        if dialog_id:
            result[str(dialog_id)] = {k: row[k] for k in
                                      ("archive_stage", "archive_member", "native_speech_id",
                                       "script_export_locations") if k in row}
    return result


def _script_locations(location_by_dialog: Mapping[str, Mapping[str, Any]],
                      dialog_ids: list[str], locale: str) -> list[dict[str, Any]]:
    result = []
    for dialog_id in dialog_ids:
        record = location_by_dialog.get(str(dialog_id), {})
        location = record.get("script_export_locations", {}).get(locale)
        if isinstance(location, Mapping):
            result.append({"speech_id": str(dialog_id).rsplit(":", 1)[-1],
                           "location": dict(location)})
    return result


def _manifest_text_fields(manifest: Mapping[str, Any]) -> list[tuple[tuple[str | int, ...], dict[str, Any]]]:
    """Enumerate every source-derived or explicitly text-bearing manifest value."""
    fields: list[tuple[tuple[str | int, ...], dict[str, Any]]] = []
    location_by_dialog = _source_evidence_by_dialog(manifest)
    dialogues = manifest.get("dialogues", [])
    for index, dialogue in enumerate(dialogues):
        sid, did = _speech_id(dialogue), str(dialogue["dialog_id"])
        source_location = location_by_dialog.get(did, {"dialog_id": did, "native_speech_id": sid})
        base = {"speech_id": sid, "dialog_id": did, "location": source_location}
        original = dialogue.get("original_text", {})
        if isinstance(original.get("text"), str):
            fields.append((("dialogues", index, "original_text", "text"),
                           {**base, "locale": "en", "speech_ids": [sid], "separator": "",
                            "source_locations": _script_locations(location_by_dialog, [did], "en")}))
        for variant_index, variant in enumerate(dialogue.get("text_variants", [])):
            for key in _TEXT_VARIANT_KEYS:
                if isinstance(variant.get(key), str):
                    fields.append((("dialogues", index, "text_variants", variant_index, key),
                                   {**base, "locale": "de", "speech_ids": [sid], "separator": "",
                                    "source_locations": _script_locations(location_by_dialog, [did], "de")}))
            for allowed_index, allowed_text in enumerate(variant.get("qa_allowed_variants", [])):
                if isinstance(allowed_text, str):
                    fields.append((("dialogues", index, "text_variants", variant_index,
                                    "qa_allowed_variants", allowed_index),
                                   {**base, "locale": "de", "speech_ids": [sid], "separator": "",
                                    "source_locations": _script_locations(location_by_dialog, [did], "de")}))
            # Old normalization evidence can contain the source wording too.
            for step_index, step in enumerate(variant.get("normalization_steps", [])):
                for key in ("before", "after"):
                    if isinstance(step.get(key), str):
                        fields.append((("dialogues", index, "text_variants", variant_index,
                                        "normalization_steps", step_index, key),
                                       {**base, "locale": "de", "speech_ids": [sid], "separator": "",
                                        "source_locations": _script_locations(location_by_dialog, [did], "de")}))

    # Composite anchor prompts are rebuilt from their ordered speech IDs.
    for index, role in enumerate(manifest.get("roles", [])):
        anchor = role.get("main_anchor", {})
        if isinstance(anchor.get("prompt_text"), str):
            ordered_ids = ([anchor["anchor_dialog_id"]] if anchor.get("anchor_dialog_id") else [])
            ordered_ids.extend(anchor.get("anchor_component_dialog_ids", []))
            ids = [str(x).rsplit(":", 1)[-1] for x in ordered_ids]
            fields.append((("roles", index, "main_anchor", "prompt_text"),
                           {"locale": "en", "speech_ids": ids, "separator": " ",
                            "source_locations": _script_locations(location_by_dialog,
                                                                   [str(x) for x in ordered_ids], "en"),
                            "location": {"role_id": role.get("role_id"),
                                         "dialog_ids": anchor.get("anchor_component_dialog_ids", [])}}))

    evidence_roles = manifest.get("source_evidence", {}).get("launcher_preparation", {}).get("roles", [])
    for index, role in enumerate(evidence_roles):
        reference = role.get("reference", {})
        if isinstance(reference.get("prompt_text"), str):
            ids = [str(x).rsplit(":", 1)[-1] for x in reference.get("source_dialog_ids", [])]
            fields.append((("source_evidence", "launcher_preparation", "roles", index,
                            "reference", "prompt_text"),
                           {"locale": "en", "speech_ids": ids, "separator": " ",
                            "source_locations": _script_locations(location_by_dialog,
                                                                   reference.get("source_dialog_ids", []), "en"),
                            "location": {"role_id": role.get("role_id"),
                                         "dialog_ids": reference.get("source_dialog_ids", [])}}))
        # Forward-compatible support for per-component transcripts.
        for container in ("components",):
            for component_index, component in enumerate(role.get(container, [])):
                for key in ("speaker_text", "prompt_text"):
                    if isinstance(component.get(key), str):
                        raw_ids = component.get("source_dialog_ids", component.get("dialog_ids", []))
                        if component.get("dialog_id"):
                            raw_ids = [component["dialog_id"]]
                        ids = [str(x).rsplit(":", 1)[-1] for x in raw_ids]
                        fields.append((("source_evidence", "launcher_preparation", "roles", index,
                                        container, component_index, key),
                                       {"locale": "en", "speech_ids": ids, "separator": " ",
                                        "source_locations": _script_locations(location_by_dialog,
                                                                               [str(x) for x in raw_ids], "en"),
                                        "location": {"role_id": role.get("role_id"),
                                                     "component_id": component.get("component_id")}}))

    # Dialog prewarms are game-derived and must resolve from the local German
    # export by their selected prewarm speech ID. Only fixed standard-bank
    # copy is allowed to remain in the distributed skeleton.
    for role_index, role in enumerate(manifest.get("roles", [])):
        prewarm = role.get("prewarm", {})
        text = prewarm.get("text")
        if isinstance(text, str) and text and text not in NEUTRAL_PREWARM_TEXTS and prewarm.get("source") != "dialog":
            raise ReconstructionError("Nicht belegter Prewarm-Text darf nicht exportiert werden")
        if isinstance(text, str) and text not in NEUTRAL_PREWARM_TEXTS and prewarm.get("source") == "dialog":
            dialog_id = str(prewarm.get("prewarm_dialog_id", ""))
            sid = dialog_id.rsplit(":", 1)[-1] if dialog_id else ""
            fields.append((("roles", role_index, "prewarm", "text"),
                           {"locale": "de", "speech_ids": [sid] if sid else [], "separator": "",
                            "source_locations": _script_locations(location_by_dialog,
                                [dialog_id] if dialog_id else [], "de"),
                            "location": {"role_id": role.get("role_id"),
                                         "dialog_id": dialog_id, "kind": "dialog_prewarm"}}))
    for role_index, role in enumerate(evidence_roles):
        prewarm = role.get("prewarm", {})
        text = prewarm.get("text")
        if isinstance(text, str) and text and text not in NEUTRAL_PREWARM_TEXTS and prewarm.get("source") != "dialog":
            raise ReconstructionError("Nicht belegter Launcher-Prewarm darf nicht exportiert werden")
        if isinstance(text, str) and text not in NEUTRAL_PREWARM_TEXTS and prewarm.get("source") == "dialog":
            sid = str(prewarm.get("target_id", ""))
            fields.append((("source_evidence", "launcher_preparation", "roles", role_index,
                            "prewarm", "text"),
                           {"locale": "de", "speech_ids": [sid] if sid else [], "separator": "",
                            "location": {"role_id": role.get("role_id"),
                                         "speech_id": sid, "kind": "dialog_prewarm"}}))
    return fields


def _entries(indexes: Mapping[str, Any], locale: str, speech_id: str) -> list[dict[str, Any]]:
    locale_map = indexes.get(locale, indexes.get(locale.upper(), {}))
    aliases = {"en": ("english", "source", "original"), "de": ("german", "target")}
    if not locale_map:
        for alias in aliases.get(locale, ()):
            if alias in indexes:
                locale_map = indexes[alias]
                break
    if not isinstance(locale_map, Mapping):
        return []
    value = locale_map.get(str(speech_id))
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    result = []
    for item in items:
        if isinstance(item, str):
            result.append({"text": item})
        elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
            result.append(dict(item))
    return result


def _has_locale(indexes: Mapping[str, Any], locale: str) -> bool:
    aliases = {"en": ("en", "EN", "english", "source", "original"),
               "de": ("de", "DE", "german", "target")}
    return any(key in indexes and isinstance(indexes[key], Mapping) for key in aliases[locale])


def _location_matches(entry: Mapping[str, Any], wanted: Mapping[str, Any]) -> bool:
    entry_location = entry.get("location", entry)
    common = {k: v for k, v in wanted.items() if k in entry_location}
    return all(entry_location.get(k) == v for k, v in common.items()) if common else True


def _resolve_source(source: Mapping[str, Any], indexes: Mapping[str, Any],
                    hints: Mapping[str, str] | None = None,
                    expected_hash: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    locale = str(source.get("locale", ""))
    ids = source.get("speech_ids", [])
    if not ids:
        raise ReconstructionError(f"Keine lokale Textquelle für {source.get('location')}")
    texts: list[str] = []
    locations: list[dict[str, Any]] = []
    selected_by_id = {str(item["speech_id"]): item.get("location", {})
                      for item in source.get("source_locations", [])}
    for sid in ids:
        options = _entries(indexes, locale, str(sid))
        wanted_location = selected_by_id.get(str(sid), source.get("location", {}))
        if len(options) > 1 and wanted_location:
            located = [item for item in options if _location_matches(item, wanted_location)]
            if len(located) == 1:
                options = located
        if len(options) > 1 and hints and str(sid) in hints:
            matching_text = [item for item in options if item["text"] == hints[str(sid)]]
            if matching_text:
                options = matching_text
        if len(options) > 1 and expected_hash and len(ids) == 1:
            matching_hash = [item for item in options if _sha(item["text"]) == expected_hash]
            if matching_hash:
                options = matching_hash
        if len(options) > 1:
            raise ReconstructionError(f"Mehrdeutige Fundstelle {locale}:{sid}")
        if not options:
            raise ReconstructionError(f"Lokale Text-ID fehlt: {locale}:{sid}")
        texts.append(options[0]["text"])
        entry_location = options[0].get("location", {})
        locations.append({"speech_id": str(sid), "location": dict(entry_location)})
    return str(source.get("separator", "")).join(texts), locations


def _diff(base: str, target: str) -> list[dict[str, Any]]:
    operations = []
    for tag, a0, a1, b0, b1 in SequenceMatcher(a=base, b=target, autojunk=False).get_opcodes():
        if tag != "equal":
            operations.append({"start": a0, "delete_count": a1 - a0, "insert": target[b0:b1]})
    return operations


def export_recipe(manifest: Mapping[str, Any], local_texts: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a textless skeleton plus hash-guarded, context-free source edits."""
    local_texts = local_texts or {}
    skeleton = deepcopy(dict(manifest))
    slots = []
    seen = set()
    hints: dict[str, dict[str, str]] = {"en": {}, "de": {}}
    for dialogue in manifest.get("dialogues", []):
        sid = _speech_id(dialogue)
        original = dialogue.get("original_text", {}).get("text")
        if isinstance(original, str) and original:
            hints["en"][sid] = original
        variants = dialogue.get("text_variants", [])
        if variants and isinstance(variants[0].get("display_text"), str):
            hints["de"][sid] = variants[0]["display_text"]
    for parts, source in _manifest_text_fields(manifest):
        path = _pointer(parts)
        if path in seen:
            raise ReconstructionError(f"Doppelter Textpfad: {path}")
        seen.add(path)
        target = _at(manifest, parts)
        base = None
        source_locations = []
        if source["speech_ids"] and target:
            try:
                base, source_locations = _resolve_source(source, local_texts,
                                                         hints.get(source["locale"], {}))
            except ReconstructionError:
                if (_has_locale(local_texts, source["locale"]) and
                        any(not _entries(local_texts, source["locale"], str(sid))
                            for sid in source["speech_ids"])):
                    raise
                # Preserve only a reference hash when no local export is present.
                # This still requires the runtime source to match the known value.
                base = None
            if source["locale"] == "en" and base is not None and base != target:
                # Never turn a source-language mismatch into a distributable
                # English insertion. Keep only the original ID/hash reference.
                base = None
                source_locations = []
        if base is None:
            if not source["speech_ids"]:
                # This is explicitly project-authored copy, never source text.
                base = ""
                delta = _diff(base, target)
            else:
                base = target
                delta = []
        else:
            delta = _diff(base, target)
        slot = {"path": path, "locale": source["locale"],
                "speech_ids": source["speech_ids"], "location": source["location"],
                "expected_base_sha256": _sha(base), "operations": delta,
                "expected_target_sha256": _sha(target)}
        if source_locations:
            slot["source_locations"] = source_locations
        if source.get("separator"):
            slot["separator"] = source["separator"]
        if not source["speech_ids"]:
            slot["source"] = "project-authored-text"
            slot["allow_empty_source"] = True
        slots.append(slot)
        _set(skeleton, parts, "")
    recipe = {"format": FORMAT, "slots": slots,
              "expected_manifest_sha256": _sha(_canonical(dict(manifest)))}
    skeleton[MARKER] = recipe
    validate_recipe_structure(skeleton)
    return skeleton


def _parse_pointer(pointer: str) -> tuple[str | int, ...]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ReconstructionError("Ungültiger JSON-Pointer")
    parts: list[str | int] = []
    for part in pointer[1:].split("/"):
        unescaped = part.replace("~1", "/").replace("~0", "~")
        parts.append(int(unescaped) if re.fullmatch(r"0|[1-9][0-9]*", unescaped) else unescaped)
    return tuple(parts)


def validate_recipe_structure(recipe: Mapping[str, Any]) -> None:
    """Validate the release contract without needing any local source texts."""
    contract = recipe.get(MARKER)
    if not isinstance(contract, Mapping) or contract.get("format") != FORMAT:
        raise ReconstructionError("Unbekanntes Textrekonstruktionsformat")
    if set(contract) != {"format", "slots", "expected_manifest_sha256"}:
        raise ReconstructionError("Unerwartete Felder im Textrezept")
    if not isinstance(contract.get("expected_manifest_sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", contract["expected_manifest_sha256"]):
        raise ReconstructionError("Ungültiger Manifest-Prüfhash")
    if not isinstance(contract.get("slots"), list):
        raise ReconstructionError("Textslots fehlen")

    skeleton = deepcopy(dict(recipe))
    skeleton.pop(MARKER, None)
    preparation_roles = (skeleton.get("source_evidence", {})
                         .get("launcher_preparation", {}).get("roles", []))
    for role in list(skeleton.get("roles", [])) + list(preparation_roles):
        prewarm = role.get("prewarm")
        if isinstance(prewarm, Mapping) and prewarm and prewarm.get("source") not in {
                "dialog", "neutral_standard"}:
            raise ReconstructionError("Textrezept enthält einen unbelegten Prewarm-Ursprung")
    expected = {_pointer(parts) for parts, _ in _manifest_text_fields(skeleton)}
    slots = contract["slots"]
    paths = []
    allowed_slot_fields = {"path", "locale", "speech_ids", "location", "source_locations",
                           "separator", "expected_base_sha256", "operations",
                           "expected_target_sha256", "source", "allow_empty_source"}
    hash_fields = ("expected_base_sha256", "expected_target_sha256")
    for slot in slots:
        if not isinstance(slot, Mapping) or set(slot) - allowed_slot_fields:
            raise ReconstructionError("Unerwartete Felder in einem Textslot")
        path = slot.get("path")
        if not isinstance(path, str):
            raise ReconstructionError("Textslot ohne JSON-Pfad")
        paths.append(path)
        if slot.get("locale") not in {"en", "de"}:
            raise ReconstructionError(f"Ungültige Slot-Locale: {path}")
        if any(not isinstance(slot.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", slot[key])
               for key in hash_fields):
            raise ReconstructionError(f"Ungültiger Basis-/Zielhash: {path}")
        speech_ids = slot.get("speech_ids")
        if not isinstance(speech_ids, list) or any(not isinstance(sid, str) or not sid for sid in speech_ids):
            raise ReconstructionError(f"Ungültige Speech-ID-Referenz: {path}")
        if not speech_ids and (slot.get("source") != "project-authored-text" or
                               slot.get("allow_empty_source") is not True):
            raise ReconstructionError(f"Textslot ohne nachvollziehbare Quelle: {path}")
        if slot.get("allow_empty_source") not in (None, True):
            raise ReconstructionError(f"Ungültige Leerquellenfreigabe: {path}")
        if slot.get("separator", "") not in ("", " "):
            raise ReconstructionError(f"Ungültiger Textseparator: {path}")
        operations = slot.get("operations")
        if not isinstance(operations, list):
            raise ReconstructionError(f"Ungültige Operationen: {path}")
        if slot["locale"] == "en" and operations:
            raise ReconstructionError(f"EN-Text darf keine Einfügeoperationen enthalten: {path}")
        last_end = -1
        for operation in operations:
            if not isinstance(operation, Mapping) or set(operation) != {"start", "delete_count", "insert"}:
                raise ReconstructionError(f"Operation enthält Basis-/Kontexttext: {path}")
            start, delete_count, insert = operation["start"], operation["delete_count"], operation["insert"]
            if (not isinstance(start, int) or isinstance(start, bool) or
                    not isinstance(delete_count, int) or isinstance(delete_count, bool) or
                    start < last_end or delete_count < 0 or not isinstance(insert, str)):
                raise ReconstructionError(f"Ungültige oder überlappende Operation: {path}")
            last_end = start + delete_count
        try:
            parts = _parse_pointer(path)
            if _at(skeleton, parts) != "":
                raise ReconstructionError(f"Textfeld im Ausgangsrezept ist nicht leer: {path}")
        except (KeyError, IndexError, TypeError) as exc:
            raise ReconstructionError(f"Textpfad fehlt im Ausgangsrezept: {path}") from exc
        source_locations = slot.get("source_locations", [])
        if not isinstance(source_locations, list):
            raise ReconstructionError(f"Fundstellenliste ungültig: {path}")
        seen_sids = set()
        for occurrence in source_locations:
            if (not isinstance(occurrence, Mapping) or set(occurrence) != {"speech_id", "location"} or
                    occurrence.get("speech_id") not in speech_ids or
                    occurrence.get("speech_id") in seen_sids or
                    not isinstance(occurrence.get("location"), Mapping)):
                raise ReconstructionError(f"Fundstelle ungültig: {path}")
            seen_sids.add(occurrence["speech_id"])
            if any(key in occurrence["location"] for key in {"text", "base", "base_text", "context", "before", "after", "insert"}):
                raise ReconstructionError(f"Fundstelle enthält Textkontext: {path}")
    if len(paths) != len(set(paths)):
        raise ReconstructionError("Textpfad kommt mehrfach vor")
    if set(paths) != expected:
        raise ReconstructionError("Slots decken die bekannten Textfelder nicht exakt ab")


def _apply_ops(base: str, operations: Any, expected_hash: str, path: str) -> str:
    if _sha(base) != expected_hash:
        raise ReconstructionError(f"Ausgangstext stimmt nicht mit erwarteter Basis überein: {path}")
    if not isinstance(operations, list):
        raise ReconstructionError(f"Ungültige Änderungsoperationen: {path}")
    result = base
    last_start = len(base) + 1
    for operation in reversed(operations):
        if not isinstance(operation, Mapping):
            raise ReconstructionError(f"Ungültige Änderungsoperation: {path}")
        start, delete_count, insert = operation.get("start"), operation.get("delete_count"), operation.get("insert")
        if (not isinstance(start, int) or not isinstance(delete_count, int) or
                not isinstance(insert, str) or start < 0 or delete_count < 0 or
                start + delete_count > len(base) or start + delete_count > last_start):
            raise ReconstructionError(f"Änderungsoperation außerhalb der Basis: {path}")
        result = result[:start] + insert + result[start + delete_count:]
        last_start = start
    return result


def _dialogue_identity_check(manifest: Mapping[str, Any]) -> None:
    ids = [d.get("dialog_id") for d in manifest.get("dialogues", [])]
    if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        raise ReconstructionError("Dialog-IDs fehlen oder sind mehrdeutig")


def reconstruct(recipe: Mapping[str, Any], indexes: Mapping[str, Any]) -> dict[str, Any]:
    """Hydrate a textless recipe and verify source, target, IDs and full digest."""
    validate_recipe_structure(recipe)
    result = deepcopy(dict(recipe))
    contract = result.pop(MARKER)
    _dialogue_identity_check(result)
    seen_paths: set[str] = set()
    for slot in contract.get("slots", []):
        path = slot.get("path")
        if not isinstance(path, str) or path in seen_paths:
            raise ReconstructionError("Textpfad fehlt oder kommt mehrfach vor")
        seen_paths.add(path)
        parts = _parse_pointer(path)
        try:
            current = _at(result, parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise ReconstructionError(f"Textpfad fehlt im Rezept: {path}") from exc
        if current != "":
            raise ReconstructionError(f"Textfeld im Ausgangsrezept ist nicht leer: {path}")
        source = {"locale": slot.get("locale"), "speech_ids": slot.get("speech_ids", []),
                  "separator": slot.get("separator", ""), "location": slot.get("location", {})}
        if not source["speech_ids"] and slot.get("source") == "project-authored-text":
            if not slot.get("allow_empty_source"):
                raise ReconstructionError(f"Unbekannte lokale Textquelle: {path}")
            base = ""
        elif slot.get("allow_empty_source") and not source["speech_ids"]:
            base = ""
        else:
            # Empty source records in the known manifest are unavailable IDs;
            # preserve that fact without demanding a fabricated export row.
            if (not source["speech_ids"] or
                    slot.get("expected_base_sha256") == _sha("") and
                    slot.get("expected_target_sha256") == _sha("") and
                    not slot.get("operations")):
                base = ""
            else:
                source["source_locations"] = slot.get("source_locations", [])
                base, _ = _resolve_source(source, indexes,
                                          expected_hash=slot.get("expected_base_sha256"))
        target = _apply_ops(base, slot.get("operations"), slot.get("expected_base_sha256", ""), path)
        if _sha(target) != slot.get("expected_target_sha256"):
            raise ReconstructionError(f"Zieltext-Prüfsumme stimmt nicht: {path}")
        _set(result, parts, target)
    # Schema's variant identity hashes the canonical text tuple, not UTF-8 text.
    for dialogue in result.get("dialogues", []):
        for variant in dialogue.get("text_variants", []):
            payload = [variant.get(key) for key in
                       ("display_text", "tts_text", "qa_expected_text", "qa_allowed_variants")]
            variant["sha256"] = _sha(_canonical(payload))
    expected = contract.get("expected_manifest_sha256")
    if expected and _sha(_canonical(result)) != expected:
        raise ReconstructionError("Rekonstruiertes Manifest stimmt nicht exakt mit dem Referenzhash überein")
    return result


def ensure_reconstructed_manifest(root: Any, manifest: Mapping[str, Any],
                                  config: Mapping[str, Any] | None,
                                  installation: Any | None) -> dict[str, Any]:
    """Launcher adapter entry point; indexes are supplied by local preparation.

    ``installation`` or ``config`` may provide ``text_indexes`` directly, or
    ``text_indexes_path`` pointing to the normalized local index JSON. The
    generated manifest remains in memory for the caller to validate and save.
    """
    if MARKER not in manifest:
        return deepcopy(dict(manifest))
    config = config or {}
    installation = installation or {}

    def value(container: Any, name: str) -> Any:
        if isinstance(container, Mapping):
            return container.get(name)
        return getattr(container, name, None)

    indexes = value(installation, "text_indexes") or value(config, "text_indexes")
    if indexes is None:
        path = (value(installation, "text_indexes_path") or
                value(config, "text_indexes_path"))
        if path:
            from pathlib import Path
            index_path = Path(path)
            if not index_path.is_absolute():
                index_path = Path(root) / index_path
            try:
                indexes = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ReconstructionError(f"Lokaler Textindex kann nicht gelesen werden: {index_path}") from exc
    if not isinstance(indexes, Mapping):
        raise ReconstructionError("Lokale EN/DE-Textindizes für das Textrezept fehlen")
    return reconstruct(manifest, indexes)
