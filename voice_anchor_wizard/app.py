#!/usr/bin/env python3
"""Local, persistent MI1 voice-anchor and German prewarm review wizard."""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import itertools
import json
import mimetypes
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import threading
import unicodedata
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from anchor_selection import pick_candidates, anchor_duration, automatic_composite
from custom_anchors import AnchorImportError, import_custom_anchor
from reviewed_transcripts import REVIEW_PATH, commit_manifest_import, prepare_manifest_import
import text_state
import text_rules
import text_review
from dictionary import GermanDictionary

FORMAT = "mi1-voice-anchor-wizard/1"
EXPORT_FORMAT = "mi1-manifest-role-component/2"
JOIN_MS = 300
TARGET_MIN_MS = 4000
TARGET_MAX_MS = 15000
MAX_COMPOSITE_COMPONENTS = 8
PREWARM_MIN_MS = 3000
PREWARM_MAX_MS = 4000
SEED = 20260926
NEUTRAL_PREWARM_TEXTS = (
    "Ich bin bereit.",
    "Jetzt kann es losgehen.",
    "Alles klar, wir können beginnen.",
    "Wir sind jetzt bereit und können gemeinsam beginnen.",
)
LOCK = threading.Lock()
ID = re.compile(r"^[0-9]+$")


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_relative(path: str) -> str:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or not path or "\\" in path:
        raise ValueError(f"Unsicherer relativer Pfad: {path!r}")
    return str(candidate)


def german_text(dialogue: dict) -> str:
    variant = dialogue.get("text_variant_candidate") or {}
    resolution = dialogue.get("unclear_resolution") or {}
    return (variant.get("tts_text") or resolution.get("text_decision", {}).get("tts_text") or "").strip()


def english_text(dialogue: dict) -> tuple[str, str]:
    value = dialogue.get("english_text_candidate") or {}
    if isinstance(value, dict):
        return (value.get("text") or "").strip(), value.get("status") or "missing"
    return "", "missing"


def in_render_scope(dialogue: dict) -> bool:
    if dialogue.get("extractor_status") == "todo":
        return True
    return (dialogue.get("unclear_resolution") or {}).get("confirmed_processing") == "render"


def role_id_for(dialogue: dict) -> str:
    code = (dialogue.get("speaker_candidate") or {}).get("code") or "unclassified"
    return "mi1-ute-1.02:voice-code:" + code


def candidate_from(dialogue: dict, effective_german: str | None = None) -> dict | None:
    text, text_status = english_text(dialogue)
    audio = dialogue.get("original_audio") or {}
    de = german_text(dialogue) if effective_german is None else effective_german.strip()
    speech_id = str(dialogue.get("native_speech_id") or "")
    if not ID.fullmatch(speech_id) or text_status not in {"unique", "inferred_reviewed"} or not text or not de:
        return None
    if not audio.get("path") or not audio.get("sha256_from_extraction"):
        return None
    return {
        "speech_id": speech_id,
        "dialog_id": dialogue["dialog_id"],
        "english_text": text,
        "german_text": de,
        "duration_ms": int(audio["duration_ms_from_extraction"]),
        "audio": {
            "root_id": audio.get("root_id", "dubbing_project"),
            "path": safe_relative(audio["path"]),
            "sha256": audio["sha256_from_extraction"],
            "media_type": audio.get("media_type", "audio/ogg"),
            "sample_rate_hz": audio.get("sample_rate_hz_from_extraction"),
            "channels": audio.get("channels_from_extraction"),
        },
    }




def prewarm_suggestions(candidates: list[dict], selected_id: str | None = None,
                        limit: int | None = 5) -> list[dict]:
    def score(item: dict) -> tuple:
        duration = item["duration_ms"]
        in_range = PREWARM_MIN_MS <= duration <= PREWARM_MAX_MS
        # The duration window is strict. A previously selected ID only wins
        # within its duration class and never displaces a 3–4 s source.
        return (0 if in_range else 1, 0 if item["speech_id"] == selected_id else 1,
                abs(duration - 3500), int(item["speech_id"]))
    result = []
    ordered = sorted(candidates, key=score)
    for rank, item in enumerate(ordered if limit is None else ordered[:limit], 1):
        in_range = PREWARM_MIN_MS <= item["duration_ms"] <= PREWARM_MAX_MS
        result.append(dict(item, prewarm_rank=rank,
                           prewarm_source_window_match=in_range,
                           prewarm_suggestion_kind="target_3_4s" if in_range else "nearest_duration_fallback"))
    return result






def render_capability(dubbing_root: Path) -> dict:
    available = platform.system() == "Darwin" and platform.machine() == "arm64" and str(dubbing_root).startswith("/Volumes/")
    if available:
        try:
            cpu = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
            available = cpu.startswith("Apple M1")
        except (OSError, subprocess.SubprocessError):
            available = False
    return {"available": available,
            "message": "Rendering kann auf diesem M1 gestartet werden" if available else "Rendering auf M1 erforderlich"}


class Wizard:
    def __init__(self, draft_path: Path, dubbing_root: Path, data_root: Path):
        self.draft_path = draft_path.resolve()
        self.dubbing_root = dubbing_root.resolve()
        self.data_root = data_root.resolve()
        self.source_bytes = self.draft_path.read_bytes()
        self.source_sha256 = digest_bytes(self.source_bytes)
        self.draft = json.loads(self.source_bytes)
        if self.draft.get("draft_format") != "mi1-manifest-v1-source-draft/1":
            raise ValueError("Unbekanntes MI1-Manifestentwurfsformat")
        # Keep the complete manifest inventory visible; processing is a decision
        # about export behavior, not about whether an ID exists in the wizard.
        self.dialogues = list(self.draft["dialogues"])
        self.dialogue_by_speech = {str(d["native_speech_id"]): d for d in self.dialogues}
        self.state_path = self.data_root / "state.json"
        self.export_path = self.data_root / "mi1-voice-anchors.manifest-component.json"
        self.state = self._load_state()
        self._text_dictionary = None
        self._text_dictionary_words = None
        self._effective_text_cache = {}
        self._text_findings_cache = {}
        self._text_projection_cache = None
        self._text_cache_stats = {"prepared": 0, "detected": 0}
        self.refresh_roles()

    def refresh_roles(self) -> None:
        self.roles = self._roles()
        self.role_by_id = {r["role_id"]: r for r in self.roles}

    def effective_role_id(self, dialogue: dict) -> str:
        speech_id = str(dialogue.get("native_speech_id") or "")
        action = self.state.get("dialogue_actions", {}).get(speech_id, {})
        if action.get("action") in {"assign_existing", "technical_role"} or (action.get("content_type") == "nonverbal" and action.get("target_role_id")):
            return action["target_role_id"]
        return role_id_for(dialogue)

    def effective_processing(self, dialogue: dict) -> str:
        legacy = self._legacy_effective_processing(dialogue)
        if legacy == "exclude":
            return legacy
        speech_id = str(dialogue.get("native_speech_id") or "")
        review = self.state.get("patch_review_processing", {}).get(speech_id)
        if review:
            original = dialogue.get("original_audio", {})
            english = (dialogue.get("english_text_candidate") or dialogue.get("original_text") or {}).get("text", "")
            from patch_review.evidence import digest as review_digest
            if review.get("original_audio_sha256") == original.get("sha256_from_extraction", original.get("sha256")) and review.get("original_text_sha256") == review_digest([english, ""]):
                return review["processing"]
        block = self.state.get("text_prewizard") or {}
        if speech_id not in block.get("ids", {}):
            return legacy
        decision = text_state.resolve(block, speech_id, {
            "processing": self.role_processing(self.effective_role_id(dialogue)), "anchor": "use", "prewarm": "use"}, validated=True)
        resolved = decision["processing"]
        if resolved["source"] in {"manual", "lists", "last_consistent"} and resolved["value"] != "inherit":
            return resolved["value"]
        return legacy

    def effective_text(self, speech_id: str) -> dict:
        """Shared German reading projection for dialogue views and role candidates."""
        block = self.state["text_prewizard"]
        record = block["ids"].get(str(speech_id))
        if not record:
            return {"source_text": "", "display_text": "", "tts_text": "", "qa_expected_text": "",
                    "changes": [], "source_span_map": [], "review_cases": []}
        rules = self._effective_preparation_rules(block, str(speech_id))
        source = record["original_text"]
        dialogue = self.dialogue_by_speech.get(str(speech_id))
        variant = (dialogue or {}).get("text_variant_candidate") or {}
        # Legacy callers can edit an unversioned in-memory draft row directly.
        # Honor that unsaved source only when no explicit ID reading exists.
        if (dialogue and not record.get("manual_reading") and not dialogue.get("text_versions")
                and variant.get("tts_text") and variant.get("tts_text") != source):
            source = variant["tts_text"]
        saved_actions = block.get("text_actions", {}).get(str(speech_id), [])
        review_tts = self.state.get("patch_review_tts", {}).get(str(speech_id))
        key = canonical([str(speech_id), source, rules, record.get("display_text"), record.get("manual_reading"), saved_actions, review_tts])
        cached = self._effective_text_cache.get(str(speech_id))
        if cached is not None and cached[0] == key:
            return json.loads(json.dumps(cached[1], ensure_ascii=False))
        self._text_cache_stats["prepared"] += 1
        prepared = text_rules.prepare_text(source, rules,
                    record["display_text"] if record.get("manual_reading") else None)
        prepared["display_text"] = record["display_text"]
        blockers = []
        if saved_actions and record.get("manual_reading"):
            blockers.append("Manuelle Lesefassung hat Vorrang; gespeicherte Fundaktionen sind pausiert. Lesefassung zurücksetzen oder Fundaktionen ausdrücklich zurücksetzen.")
        if saved_actions and not record.get("manual_reading"):
            if any(item.get("source_identity") != record.get("source_sha256") for item in saved_actions):
                blockers.append("Textaktion gehört zu einer anderen Quellfassung; Aktionen zurücksetzen.")
            else:
                base_prepared = prepared
                applied = text_review.apply_text_actions(source,
                    [{"finding": item["finding"], "action": item["action"], "replacement": item["replacement"]}
                     for item in saved_actions], reading_text=prepared["tts_text"],
                    source_span_map=prepared["source_span_map"])
                if applied["stale"] or applied["warnings"] or len(applied["applied"]) != len(saved_actions):
                    blockers.append("Gespeicherte Fundaktion ist veraltet oder kollidiert; Aktionen zurücksetzen.")
                else:
                    prepared = text_rules.prepare_text(applied["text"], rules)
                    prepared["source_text"] = base_prepared["source_text"]
                    prepared["display_text"] = prepared["tts_text"]
        prepared["text_action_blockers"] = blockers
        if review_tts:
            override = text_rules.prepare_text(review_tts["tts_text"], rules, manual_text=review_tts["tts_text"])
            prepared["tts_text"] = override["tts_text"]
            prepared["qa_expected_text"] = override["qa_expected_text"]
            prepared["changes"] = [*prepared.get("changes", []), {"kind": "patch_review_tts", "version": review_tts["revision"]}]
        self._effective_text_cache[str(speech_id)] = (key, json.loads(json.dumps(prepared, ensure_ascii=False)))
        return prepared

    @staticmethod
    def _effective_preparation_rules(block: dict, speech_id: str) -> dict:
        return text_rules._validate_rules({**block["preparation_rules"],
                                           **block.get("rule_overrides", {}).get(str(speech_id), {})})

    def save_text_prewizard_api(self, operation: str, payload: object) -> dict:
        if not isinstance(payload, dict) or set(payload) != {"revision", "operation", "data"}:
            raise ValueError("Vorwizard-Aktion braucht revision, operation und data")
        if type(payload["revision"]) is not int or payload["revision"] != self.state["revision"]:
            raise RuntimeError("Zustand wurde zwischenzeitlich geändert; Seite neu laden")
        if payload["operation"] != operation or not isinstance(payload["data"], dict):
            raise ValueError("Vorwizard-Operation oder Daten sind ungültig")
        if operation == "actions":
            return self._text_actions_api(payload["data"], payload["revision"])
        if operation == "workflow":
            return self._text_workflow_api(payload["data"], payload["revision"])
        block = self.state["text_prewizard"]
        data = payload["data"]
        updated = json.loads(json.dumps(block))
        if operation == "rules":
            if set(data) != {"rules"}:
                raise ValueError("Regeländerung braucht genau rules")
            updated["preparation_rules"] = text_rules._validate_rules(data["rules"])
        elif operation == "vocabulary":
            if set(data) != {"words"} or not isinstance(data["words"], list):
                raise ValueError("Wortliste braucht words als Liste")
            GermanDictionary._validate_project_words(data["words"])
            updated["project_words"] = list(dict.fromkeys(data["words"]))
        elif operation in {"sounds", "patterns"}:
            field = "pronunciation_templates" if operation == "sounds" else "search_templates"
            key = "templates"
            if set(data) != {key} or not isinstance(data[key], list):
                raise ValueError("Vorlagenänderung braucht templates als Liste")
            values = data[key]
            if operation == "sounds":
                values = [{"id": item.id, "base": item.base, "repeat_limit": item.repeat_limit}
                          for item in (text_review.VocalizationRule.create(x) for x in values)]
            else:
                values = [text_review.validate_marker_template(x) for x in values]
            ids = [x["id"] for x in values]
            if len(ids) != len(set(ids)):
                raise ValueError("Vorlagen-IDs müssen eindeutig sein")
            updated[field] = values
        elif operation == "id":
            action = data.get("action", "set_text")
            if action == "set_text":
                if set(data) not in ({"speech_id", "text"}, {"action", "speech_id", "text"}) or not isinstance(data["text"], str) or len(data["text"]) > 4000:
                    raise ValueError("ID-Text braucht speech_id und text mit höchstens 4000 Zeichen")
                sid = data["speech_id"]
                if sid not in updated["ids"]:
                    raise ValueError("Unbekannte native_speech_id")
                updated["ids"][sid]["display_text"] = data["text"]
                updated["ids"][sid]["manual_reading"] = True
            elif action in {"set_decision", "reset_decision"}:
                expected = {"action", "speech_id", "feature", "value"} if action == "set_decision" else {"action", "speech_id", "feature"}
                if set(data) != expected or data.get("speech_id") not in updated["ids"] or data.get("feature") not in text_state.ENUMS:
                    raise ValueError("ID-Entscheidung braucht eine bekannte ID und ein unterstütztes Merkmal")
                sid, feature = data["speech_id"], data["feature"]
                if action == "set_decision":
                    value = data["value"]
                    if not isinstance(value, str) or value not in text_state.ENUMS[feature]:
                        raise ValueError("Ungültiger Wert für die ID-Entscheidung")
                    updated["rules"].setdefault(sid, {})[feature] = value
                else:
                    updated["rules"].setdefault(sid, {}).pop(feature, None)
            elif action == "reset_text":
                if set(data) != {"action", "speech_id"} or data["speech_id"] not in updated["ids"]:
                    raise ValueError("Textreset braucht eine bekannte speech_id")
                sid = data["speech_id"]
                updated["ids"][sid]["display_text"] = updated["ids"][sid]["original_text"]
                updated["ids"][sid]["manual_reading"] = False
            else:
                raise ValueError("Unbekannte ID-Aktion")
        elif operation == "lists":
            action = data.get("action")
            if action not in {"create", "rename", "delete", "set_members", "remove_members", "preview"}:
                raise ValueError("Unbekannte Listenaktion")
            list_payload = {k: v for k, v in data.items() if k != "action"}
            if action == "create": updated = text_state.create_list(updated, list_payload.get("list_id"), list_payload.get("name"))
            elif action == "rename": updated = text_state.rename_list(updated, list_payload.get("list_id"), list_payload.get("name"))
            elif action == "delete": updated = text_state.delete_list(updated, list_payload.get("list_id"))
            elif action == "set_members": updated = text_state.set_members(updated, list_payload.get("list_id"), list_payload.get("members", []))
            elif action == "remove_members": updated = text_state.remove_members(updated, list_payload.get("list_id"), list_payload.get("speech_ids", []))
            else:
                preview = text_state.preview(updated, list_payload.get("speech_ids", []), list_payload.get("decisions", {}), list_payload.get("role_values", {}))
                return {"revision": self.state["revision"], "preview": preview}
        elif operation == "review":
            if set(data) != {"finding_id", "status"} or not isinstance(data["finding_id"], str) or data["status"] not in {"complete", "reopen"}:
                raise ValueError("Review braucht finding_id und complete/reopen")
            previous_block = self.state["text_prewizard"]
            self.state["text_prewizard"] = updated
            try:
                valid_ids = {x["id"] for x in self._text_findings()}
            finally:
                self.state["text_prewizard"] = previous_block
            if data["status"] == "complete" and data["finding_id"] not in valid_ids:
                raise ValueError("Befund ist veraltet oder unbekannt; Befundliste neu laden")
            completed = {x.get("finding_id"): x for x in updated["completed_reviews"]}
            if data["status"] == "complete":
                completed[data["finding_id"]] = {"finding_id": data["finding_id"], "status": "complete"}
            else:
                completed.pop(data["finding_id"], None)
            updated["completed_reviews"] = list(completed.values())
        else:
            raise ValueError("Unbekannte Vorwizard-Operation")
        text_state.validate(updated)
        if updated != block:
            for sid in updated["ids"]:
                decision = text_state.resolve(updated, sid, {"processing": "render", "anchor": "use", "prewarm": "use"}, validated=True)
                saved = updated["last_consistent_decisions"].setdefault(sid, {})
                saved.update(decision["last_consistent_decisions"])
            previous_block = self.state["text_prewizard"]
            self.state["text_prewizard"] = updated
            try:
                live = {x["id"] for x in self._text_findings()}
            finally:
                self.state["text_prewizard"] = previous_block
            still_complete = [x for x in updated["completed_reviews"] if x.get("finding_id") in live]
            to_archive = [dict(x, status="archived") for x in updated["completed_reviews"] if x.get("finding_id") not in live]
            updated["completed_reviews"] = still_complete
            archived = {x.get("finding_id"): x for x in updated["archived_reviews"]}
            archived.update({x.get("finding_id"): x for x in to_archive})
            updated["archived_reviews"] = list(archived.values())
            self.state["text_prewizard"] = updated
            self.state["revision"] += 1
            self.state["updated_at"] = utc_now()
            atomic_json(self.state_path, self.state)
            self.refresh_roles()
        return {"revision": self.state["revision"], "text_prewizard": self.text_prewizard_state()}

    def _text_actions_api(self, data: dict, revision: int) -> dict:
        if isinstance(data, dict) and data.get("action") == "reset":
            if set(data) != {"action", "speech_ids"} or not isinstance(data["speech_ids"], list):
                raise ValueError("Fundaktionen zurücksetzen braucht eine explizite speech_ids-Liste")
            speech_ids = data["speech_ids"]
            if not speech_ids or any(not isinstance(sid, str) for sid in speech_ids):
                raise ValueError("Fundaktionen zurücksetzen braucht bekannte IDs")
            if any(sid not in self.state["text_prewizard"]["ids"] for sid in speech_ids):
                raise ValueError("Fundaktionen zurücksetzen braucht bekannte IDs")
            if len(speech_ids) != len(set(speech_ids)):
                raise ValueError("IDs müssen eindeutig sein")
            updated = json.loads(json.dumps(self.state["text_prewizard"], ensure_ascii=False))
            for sid in speech_ids:
                updated.setdefault("text_actions", {}).pop(sid, None)
            text_state.validate(updated)
            previous = (self.state["text_prewizard"], self.state["revision"], self.state["updated_at"])
            try:
                self.state["text_prewizard"] = updated
                self.state["revision"] += 1
                self.state["updated_at"] = utc_now()
                atomic_json(self.state_path, self.state)
            except Exception:
                self.state["text_prewizard"], self.state["revision"], self.state["updated_at"] = previous
                raise
            self.refresh_roles()
            self._text_projection_cache = None
            return {"revision": self.state["revision"], "committed": True,
                    "text_prewizard": self.text_prewizard_state()}
        required = {"action", "finding_ids", "text_action", "replacement"}
        if set(data) != required or data["action"] not in {"preview", "commit"}:
            raise ValueError("Fundstellenaktion braucht action, finding_ids, text_action und replacement")
        finding_ids = data["finding_ids"]
        text_action = data["text_action"]
        replacement = data["replacement"]
        allowed = {"remove_delimiters", "remove_passage", "replace_passage"}
        if (not isinstance(finding_ids, list) or not finding_ids
                or any(not isinstance(item, str) for item in finding_ids)
                or len(finding_ids) != len(set(finding_ids)) or text_action not in allowed
                or not isinstance(replacement, str) or len(replacement) > 4000):
            raise ValueError("Fundstellen oder Textaktion sind ungültig")
        if text_action == "replace_passage" and not replacement:
            raise ValueError("Für das Ersetzen fehlt der Ersatztext")
        block = self.state["text_prewizard"]
        current = {item["id"]: item for item in self._text_findings()}
        chosen = []
        for finding_id in finding_ids:
            finding = current.get(finding_id)
            if not finding:
                raise ValueError(f"Fundstelle {finding_id} ist veraltet; Befundliste neu laden")
            if finding.get("kind") != "marker" or finding.get("status") != "complete":
                raise ValueError(f"Fundstelle {finding_id} ist keine vollständige Markierung und kann nicht geändert werden")
            chosen.append(finding)
        by_sid: dict[str, list[dict]] = {}
        for finding in chosen:
            by_sid.setdefault(finding["speech_id"], []).append(finding)
        rows = []
        commits: dict[str, list[dict]] = {}
        for sid, findings in by_sid.items():
            record = block["ids"].get(sid)
            prepared = self.effective_text(sid)
            if not record or record.get("manual_reading"):
                raise ValueError(f"ID {sid} hat Vorrang durch eine manuelle Lesefassung; Lesefassung zurücksetzen oder Fundstellen prüfen")
            if hashlib.sha256(prepared["source_text"].encode("utf-8")).hexdigest() != record.get("source_sha256"):
                raise ValueError(f"ID {sid}: Fundstelle stammt nicht aus der unveränderten Ausgangsfassung; Fassung zuerst prüfen")
            if prepared.get("text_action_blockers"):
                raise ValueError(f"ID {sid}: {prepared['text_action_blockers'][0]}")
            source_findings = []
            seen_spans = set()
            marker_hits = text_review.find_markers(sid, prepared["source_text"], prepared["tts_text"], block["search_templates"])
            source_markers = [item for item in marker_hits if item["basis"] == "source"]
            reading_markers = [item for item in marker_hits if item["basis"] == "reading"]
            for finding in findings:
                span, error = text_review.resolve_finding_span(finding, prepared["source_text"],
                    prepared["tts_text"], prepared["source_span_map"])
                inner = finding.get("inner_span") or finding["span"]
                inner_error = None
                if not error and finding.get("basis") == "reading" and finding.get("inner_span"):
                    inner_finding = {**finding, "span": finding["inner_span"],
                        "text": prepared["tts_text"][finding["inner_span"][0]:finding["inner_span"][1]]}
                    inner, inner_error = text_review.resolve_finding_span(inner_finding, prepared["source_text"],
                        prepared["tts_text"], prepared["source_span_map"])
                if error or span is None or inner_error or inner is None:
                    if finding.get("basis") == "reading":
                        same_rule = lambda item: item.get("rule_id") == finding.get("rule_id")
                        reading_group = sorted((item for item in reading_markers if same_rule(item)), key=lambda item: item["span"])
                        source_group = sorted((item for item in source_markers if same_rule(item)), key=lambda item: item["span"])
                        index = next((i for i, item in enumerate(reading_group) if item["id"] == finding["id"]), -1)
                        if (index >= 0 and index < len(source_group)
                                and source_group[index].get("status") == "complete"
                                and source_group[index].get("delimiters") == finding.get("delimiters")):
                            span = source_group[index]["span"]
                            inner = source_group[index]["inner_span"]
                        else:
                            raise ValueError(f"ID {sid}: Lesefassungsfundstelle ist nicht eindeutig auf die Quelle abbildbar (rule={finding.get('rule_id')}, index={index}, reading={len(reading_group)}, source={len(source_group)})")
                    else:
                        raise ValueError(f"ID {sid}: Fundstelle kann nicht sicher auf den unveränderten Ausgangstext abgebildet werden")
                key = (tuple(span), tuple(inner))
                if key in seen_spans:
                    continue
                seen_spans.add(key)
                source_findings.append({**finding, "basis": "source", "span": span,
                    "inner_span": inner, "text": prepared["source_text"][span[0]:span[1]],
                    "basis_identity": record["source_sha256"]})
            actions = [{"finding": finding, "action": text_action, "replacement": replacement}
                       for finding in source_findings]
            result = text_review.apply_text_actions(prepared["source_text"], actions)
            if result["stale"] or result["warnings"] or len(result["applied"]) != len(source_findings):
                raise ValueError(f"ID {sid}: Fundstellen sind veraltet, unzuordenbar oder überschneiden sich; nichts wurde übernommen")
            previous = block.get("text_actions", {}).get(sid, [])
            existing_spans = [item["finding"].get("span", []) for item in previous]
            for finding in source_findings:
                span = finding["span"]
                if prepared["source_text"][span[0]:span[1]] != finding["text"]:
                    raise ValueError(f"ID {sid}: Fundstelle kann nicht sicher auf den unveränderten Ausgangstext abgebildet werden")
                if any(span[0] < old[1] and old[0] < span[1] for old in existing_spans if len(old) == 2):
                    raise ValueError(f"ID {sid}: Fundstelle überschneidet eine gespeicherte Aktion; Aktionen zurücksetzen")
                commits.setdefault(sid, []).append({"finding": dict(finding), "action": text_action,
                    "replacement": replacement, "source_identity": record["source_sha256"]})
            after = text_rules.prepare_text(result["text"], self._effective_preparation_rules(block, sid))["tts_text"]
            rows.append({"speech_id": sid, "source_text": prepared["source_text"],
                         "before": prepared["tts_text"], "after": after,
                         "finding_ids": [item["id"] for item in findings]})
        if data["action"] == "preview":
            return {"revision": revision, "preview": rows, "committed": False}
        updated = json.loads(json.dumps(block, ensure_ascii=False))
        for sid, entries in commits.items():
            updated.setdefault("text_actions", {}).setdefault(sid, []).extend(entries)
        text_state.validate(updated)
        previous = (self.state["text_prewizard"], self.state["revision"], self.state["updated_at"])
        try:
            self.state["text_prewizard"] = updated
            self.state["revision"] += 1
            self.state["updated_at"] = utc_now()
            atomic_json(self.state_path, self.state)
        except Exception:
            self.state["text_prewizard"], self.state["revision"], self.state["updated_at"] = previous
            raise
        self.refresh_roles()
        self._text_projection_cache = None
        return {"revision": self.state["revision"], "committed": True,
                "text_prewizard": self.text_prewizard_state()}

    def _text_findings(self, dictionary: GermanDictionary | None = None) -> list[dict]:
        block = self.state["text_prewizard"]
        dictionary = dictionary or self._get_text_dictionary(block["project_words"])
        return [item for sid in block["ids"] for item in self._text_findings_for_id(sid, dictionary)]

    _FINDING_CATEGORIES = {
        "unknown_sentence": "unknown_sentence", "number": "number",
        "character_passage": "character_passage", "vocalization": "vocalization",
        "marker": "marker", "rule_conflict": "rule_conflict",
    }

    def _original_english(self, speech_id: str) -> str | None:
        dialogue = self.dialogue_by_speech.get(str(speech_id), {})
        versions = dialogue.get("text_versions") or {}
        original = versions.get("original") or {}
        value = original.get("english_text")
        if not isinstance(value, str):
            value = (dialogue.get("original_texts") or {}).get("english_text")
        return value if isinstance(value, str) and value else None

    def _id_review_projection(self, speech_id: str, findings: list[dict] | None = None) -> dict:
        block = self.state["text_prewizard"]
        record = block["ids"][speech_id]
        prepared = self.effective_text(speech_id)
        dialogue = self.dialogue_by_speech.get(speech_id, {})
        policy = text_state.resolve(block, speech_id, {
            "processing": self.role_processing(self.effective_role_id(dialogue)),
            "anchor": "use", "prewarm": "use"}, validated=True)
        findings = self._text_findings_for_id(speech_id) if findings is None else findings
        live_identity = [{key: finding.get(key) for key in
                          ("id", "kind", "rule_id", "basis", "span", "inner_span", "status", "reason", "text")}
                         for finding in sorted(findings, key=lambda item: item.get("id", ""))]
        payload = {"speech_id": speech_id,
            "source_de": {"text": record.get("original_text"), "identity": record.get("source_sha256"),
                          "source_identity": record.get("source_identity_sha256")},
            "source_en": self._original_english(speech_id),
            "reading": prepared.get("tts_text"), "policy": policy,
            "review_cases": sorted(prepared.get("review_cases", []), key=lambda item: canonical(item).decode("utf-8")),
            "findings": live_identity}
        fingerprint = digest_bytes(canonical(payload))
        return {"fingerprint": fingerprint, "policy": policy, "prepared": prepared,
                "findings": findings, "source_en": payload["source_en"]}

    def _special_case_row(self, speech_id: str, status: dict | None = None,
                          live: list[dict] | None = None) -> dict | None:
        block = self.state["text_prewizard"]
        live = self._text_findings_for_id(speech_id) if live is None else live
        projection = self._id_review_projection(speech_id, live)
        old = block.get("id_reviews", {}).get(speech_id, {})
        live_categories = {self._FINDING_CATEGORIES.get(item.get("kind")) for item in live}
        live_categories.discard(None)
        live_ids = {item["id"] for item in live}
        # Review cases are part of the existing review detector contract even if
        # no standalone detector row was emitted for them.
        for case in projection["prepared"].get("review_cases", []):
            category = self._FINDING_CATEGORIES.get(case.get("kind"))
            if category:
                live_categories.add(category)
        history_categories = set(old.get("categories", []))
        history_ids = set(old.get("finding_ids", []))
        if not live_categories and not history_categories:
            return None
        current_matches = (old.get("status") == "released" and
                           old.get("fingerprint") == projection["fingerprint"])
        categories = sorted(live_categories | history_categories)
        finding_ids = sorted(live_ids | history_ids)
        row = {"speech_id": speech_id,
            "role_id": self.effective_role_id(self.dialogue_by_speech.get(speech_id, {})),
            "role_name": self.role_by_id.get(self.effective_role_id(self.dialogue_by_speech.get(speech_id, {})), {}).get("display_name"),
            "categories": categories, "finding_ids": finding_ids,
            "review_status": "released" if current_matches else "open",
            "original_de": block["ids"][speech_id].get("original_text", ""),
            "original_en": projection["source_en"],
            "current_reading": projection["prepared"].get("tts_text", ""),
            "original_url": "/audio/original/" + speech_id,
            "policy": projection["policy"],
            "manual_reading": bool(block["ids"][speech_id].get("manual_reading")),
            "text_action_blockers": list(projection["prepared"].get("text_action_blockers", [])),
            "audition": None,
            "review_fingerprint": projection["fingerprint"],
            "findings": live}
        if status is not None:
            row["audition"] = status
        return row

    def text_audition_status(self, speech_id: str, *, busy: bool = False,
                             job: dict | None = None) -> dict:
        dialogue = self.dialogue_by_speech.get(speech_id)
        if dialogue is None:
            return {"available": False, "state": "roles_unready", "message": "Dialog-ID ist nicht vorhanden.",
                    "role_id": None, "parameters_label": "", "variants": []}
        role_id = self.effective_role_id(dialogue)
        if role_id not in self.role_by_id:
            return {"available": False, "state": "roles_unready", "message": "Für diese ID ist keine gespeicherte Rolle gewählt.",
                    "role_id": role_id, "parameters_label": "", "variants": []}
        if self.role_processing(role_id) == "copy_original":
            return {"available": False, "state": "roles_unready", "message": "Die gewählte Rolle ist auf vollständige Originalübernahme eingestellt und nicht für TTS eingerichtet.",
                    "role_id": role_id, "parameters_label": "", "variants": []}
        try:
            role_plan = self.dialogue_test_plan(role_id, speech_id, allow_copy_original=True)
        except ValueError as exc:
            return {"available": False, "state": "roles_unready", "message": str(exc),
                    "role_id": role_id, "parameters_label": "", "variants": []}
        try:
            from render_tests_m1 import selected_cli
            root = self.dubbing_root
            cli_name = ("voxcpm2_cli_combined" if role_plan["use_voice_anchor"] or role_plan["use_prewarm"]
                        else "voxcpm2_cli")
            cli = selected_cli(self, root / "bin" / cli_name)
            required = [cli, root / "bin/ffmpeg",
                root / "models/voxcpm2/VoxCPM2-BaseLM-F16.gguf",
                root / "models/voxcpm2/VoxCPM2-Acoustic-F16.gguf"]
            if not all(path.is_file() for path in required):
                raise FileNotFoundError
        except (OSError, ValueError, FileNotFoundError):
            return {"available": False, "state": "model_unavailable", "message": "VoxCPM2-CLI, ffmpeg oder Modelle fehlen auf dem M1.",
                    "role_id": role_id, "parameters_label": "", "variants": []}
        parameters = []
        parameters.append("gewählter Stimmanker" if role_plan["use_voice_anchor"] else "Rollenregel ohne festen Anker")
        parameters.append("gewählter Prewarm" if role_plan["use_prewarm"] and not role_plan.get("per_line_prewarm")
                          else "zeilenweiser Original-Prewarm" if role_plan.get("per_line_prewarm") else "ohne Prewarm")
        parameters.append("englischer Originaltext als Performanceprompt" if not role_plan["performance_prompt_missing"]
                          else "kein eindeutiger englischer Performanceprompt")
        capability = render_capability(self.dubbing_root)
        if not capability.get("available"):
            return {"available": False, "state": "model_unavailable", "message": "Die deutsche TTS-Probe kann nur mit der bereitgestellten Modellruntime auf dem M1 gestartet werden.",
                    "role_id": role_id, "parameters_label": "; ".join(parameters), "variants": []}
        if busy:
            return {"available": False, "state": "busy", "message": "Ein anderer Rollen-, Kandidaten- oder Textjob verwendet die Runtime.",
                    "role_id": role_id, "parameters_label": "; ".join(parameters), "variants": []}
        audition = {"available": True, "state": "ready", "message": "Bereit mit den aktuell gespeicherten Rollenparametern.",
                    "role_id": role_id, "parameters_label": "; ".join(parameters), "variants": []}
        if job and job.get("speech_id") == speech_id and job.get("mode") == "role":
            texts = [item.get("text") for item in (job.get("plan") or {}).get("variants", [])]
            try:
                current_fingerprints = [self.dialogue_test_plan(role_id, speech_id,
                    text_override=text, allow_copy_original=True)["fingerprint"] for text in texts]
                current_job_identity = digest_bytes(canonical(current_fingerprints))
            except (ValueError, TypeError):
                current_job_identity = None
            if (job.get("role_plan_fingerprint") == current_job_identity and current_job_identity and
                    job.get("revision") == self.state["revision"]):
                audition["state"] = job.get("status", "ready")
                audition["message"] = job.get("error") or audition["message"]
                audition["variants"] = job.get("variants", [])
            else:
                audition.update(state="stale", available=False,
                                message="Die Rollenwahl oder Textrevision wurde seit dieser Probe geändert.")
        return audition

    def _workflow_rows(self, candidate: dict, speech_ids: list[str]) -> dict[str, dict]:
        previous_state = self.state
        previous_text_cache, previous_finding_cache = self._effective_text_cache, self._text_findings_cache
        previous_projection = self._text_projection_cache
        self.state = candidate
        self._effective_text_cache = {}
        self._text_findings_cache = {}
        self._text_projection_cache = None
        try:
            return {sid: self._special_case_row(sid) for sid in speech_ids}
        finally:
            self.state = previous_state
            self._effective_text_cache, self._text_findings_cache = previous_text_cache, previous_finding_cache
            self._text_projection_cache = previous_projection

    def _workflow_marker_sources(self, speech_id: str, findings: list[dict]) -> list[dict]:
        """Resolve current live marker findings back to immutable source spans."""
        block = self.state["text_prewizard"]
        record = block["ids"][speech_id]
        prepared = self.effective_text(speech_id)
        markers = text_review.find_markers(speech_id, prepared["source_text"], prepared["tts_text"], block["search_templates"])
        source_markers = [item for item in markers if item.get("basis") == "source"]
        reading_markers = [item for item in markers if item.get("basis") == "reading"]
        result, seen = [], set()
        for finding in findings:
            if finding.get("kind") != "marker" or finding.get("status") != "complete":
                raise ValueError(f"Fund {finding.get('id')} ist keine vollständige Markierung")
            span, error = text_review.resolve_finding_span(finding, prepared["source_text"],
                prepared["tts_text"], prepared["source_span_map"])
            inner = finding.get("inner_span") or finding.get("span")
            inner_error = None
            if not error and finding.get("basis") == "reading" and finding.get("inner_span"):
                inner_finding = {**finding, "span": finding["inner_span"],
                    "text": prepared["tts_text"][finding["inner_span"][0]:finding["inner_span"][1]]}
                inner, inner_error = text_review.resolve_finding_span(inner_finding, prepared["source_text"],
                    prepared["tts_text"], prepared["source_span_map"])
            if error or span is None or inner_error or inner is None:
                if finding.get("basis") == "reading":
                    same_rule = lambda item: item.get("rule_id") == finding.get("rule_id")
                    read_group = sorted((item for item in reading_markers if same_rule(item)), key=lambda item: item["span"])
                    source_group = sorted((item for item in source_markers if same_rule(item)), key=lambda item: item["span"])
                    index = next((i for i, item in enumerate(read_group) if item["id"] == finding["id"]), -1)
                    if (index >= 0 and index < len(source_group) and source_group[index].get("status") == "complete"
                            and source_group[index].get("delimiters") == finding.get("delimiters")):
                        span, inner = source_group[index]["span"], source_group[index]["inner_span"]
                    else:
                        raise ValueError(f"ID {speech_id}: Lesefassungsfundstelle ist nicht eindeutig auf die Quelle abbildbar")
                else:
                    raise ValueError(f"ID {speech_id}: Fundstelle kann nicht sicher auf den unveränderten Ausgangstext abgebildet werden")
            key = tuple(span), tuple(inner)
            if key in seen:
                continue
            seen.add(key)
            result.append({**finding, "basis": "source", "span": span, "inner_span": inner,
                "text": prepared["source_text"][span[0]:span[1]], "basis_identity": record["source_sha256"]})
        return result

    def _text_workflow_api(self, data: dict, revision: int) -> dict:
        required = {"action", "speech_ids", "decision", "resolutions", "preview_fingerprint"}
        if set(data) != required or data.get("action") not in {"preview", "commit"}:
            raise ValueError("Arbeitsablauf braucht action, speech_ids, decision, resolutions und preview_fingerprint")
        block = self.state["text_prewizard"]
        speech_ids = data["speech_ids"]
        if (not isinstance(speech_ids, list) or not speech_ids or any(not isinstance(sid, str) for sid in speech_ids)
                or len(speech_ids) != len(set(speech_ids)) or any(sid not in block["ids"] for sid in speech_ids)):
            raise ValueError("Explizite IDs müssen bekannt, eindeutig und nicht leer sein")
        live_findings = {finding["id"]: finding for finding in self._text_findings()}
        allowed_cases = set(block.get("id_reviews", {}))
        for sid in block["ids"]:
            if any(finding.get("speech_id") == sid for finding in live_findings.values()):
                allowed_cases.add(sid)
        if set(speech_ids) - allowed_cases:
            raise ValueError("Nur aktuelle oder zuvor bearbeitete Sonderfall-IDs dürfen geändert werden")
        decision = data["decision"]
        if not isinstance(decision, dict) or set(decision) != {"processing", "anchor", "prewarm", "review", "text"}:
            raise ValueError("Entscheidung braucht processing, anchor, prewarm, review und text")
        choices = {"processing": {"keep", "inherit", "render", "copy_original"},
                   "anchor": {"keep", "inherit", "use", "skip"}, "prewarm": {"keep", "inherit", "use", "skip"},
                   "review": {"keep", "release", "open"}}
        if any(not isinstance(decision[key], str) or decision[key] not in allowed
               for key, allowed in choices.items()):
            raise ValueError("Unbekannte Verarbeitungs-, Anker-, Prewarm- oder Prüfentscheidung")
        text_decision = decision["text"]
        if not isinstance(text_decision, dict) or set(text_decision) != {"marker", "rules", "reading", "reset_actions", "reset_rules"}:
            raise ValueError("Textentscheidung enthält fehlende oder unbekannte Felder")
        if type(text_decision["reset_actions"]) is not bool or type(text_decision["reset_rules"]) is not bool:
            raise ValueError("Textresets müssen boolesch sein")
        rule_values = text_decision["rules"]
        if rule_values is not None:
            if not isinstance(rule_values, dict):
                raise ValueError("ID-Regeln müssen ein Objekt sein")
            rule_fields = {"uppercase_enabled", "integer_enabled", "uppercase_exceptions", "quantity_units", "replacements"}
            if set(rule_values) - rule_fields:
                raise ValueError("Unbekannte ID-Regelfelder")
            text_rules._validate_rules({**block["preparation_rules"], **rule_values})
        if text_decision["reset_rules"] and rule_values is not None:
            raise ValueError("ID-Regeln können nicht zugleich zurückgesetzt und geändert werden")
        reading = text_decision["reading"]
        if (not isinstance(reading, dict) or set(reading) != {"action", "text"}
                or not isinstance(reading["action"], str) or reading["action"] not in {"keep", "set", "reset"}):
            raise ValueError("Lesefassung braucht keep, set oder reset und text")
        if not isinstance(reading["text"], str) or (reading["action"] == "set" and
                (len(speech_ids) != 1 or not 1 <= len(reading["text"]) <= 4000)):
            raise ValueError("Eine manuelle Lesefassung gilt für genau eine ID und 1–4000 Zeichen")
        marker = text_decision["marker"]
        if marker is not None:
            if (not isinstance(marker, dict) or set(marker) != {"finding_ids", "action", "replacement"}
                    or not isinstance(marker["action"], str)
                    or marker["action"] not in {"remove_delimiters", "remove_passage", "replace_passage"}
                    or not isinstance(marker["finding_ids"], list) or not isinstance(marker["replacement"], str)
                    or any(not isinstance(fid, str) for fid in marker["finding_ids"])
                    or len(marker["replacement"]) > 4000):
                raise ValueError("Markierungsaktion enthält ungültige Fund-IDs, Aktion oder Ersatztext")
            if marker["action"] == "replace_passage" and not marker["replacement"]:
                raise ValueError("Für das Ersetzen fehlt der Ersatztext")
            if (len(marker["finding_ids"]) != len(set(marker["finding_ids"])) or
                    any(fid not in live_findings for fid in marker["finding_ids"])):
                raise ValueError("Markierungsaktion enthält unbekannte oder veraltete Fund-IDs")
            if any(live_findings[fid].get("kind") != "marker" or live_findings[fid].get("speech_id") not in speech_ids
                   for fid in marker["finding_ids"]):
                raise ValueError("Markierungs-Fund-IDs müssen zu ausgewählten IDs gehören")
        resolutions = data["resolutions"]
        if not isinstance(resolutions, dict) or set(resolutions) - set(speech_ids):
            raise ValueError("Konfliktlösungen dürfen nur ausgewählte IDs enthalten")
        for sid, resolution in resolutions.items():
            if not isinstance(resolution, dict) or set(resolution) != {"replace_policy_features"}:
                raise ValueError("Konfliktlösung braucht replace_policy_features")
            values = resolution["replace_policy_features"]
            if (not isinstance(values, list) or any(not isinstance(value, str) for value in values)
                    or len(values) != len(set(values)) or set(values) - {"processing", "anchor", "prewarm"}):
                raise ValueError("Konfliktlösung nennt ungültige oder doppelte Merkmale")
        if reading["action"] == "set" and (marker is not None or rule_values is not None or text_decision["reset_rules"]):
            raise ValueError("Eine manuelle Lesefassung kann nicht mit Marker- oder Regelaktionen kombiniert werden")
        if data["action"] == "preview" and data["preview_fingerprint"] is not None:
            raise ValueError("Vorschau braucht preview_fingerprint:null")
        if data["action"] == "commit" and (not isinstance(data["preview_fingerprint"], str) or
                not re.fullmatch(r"[a-f0-9]{64}", data["preview_fingerprint"])):
            raise ValueError("Übernahme braucht einen gültigen Vorschau-Fingerprint")

        candidate = json.loads(json.dumps(self.state, ensure_ascii=False))
        candidate_block = candidate["text_prewizard"]
        pre_rows = self._workflow_rows(self.state, speech_ids)
        selected_markers: dict[str, list[dict]] = {}
        if marker:
            for finding_id in marker["finding_ids"]:
                selected_markers.setdefault(live_findings[finding_id]["speech_id"], []).append(live_findings[finding_id])
        conflicts: dict[str, list[str]] = {sid: [] for sid in speech_ids}
        warnings: dict[str, list[str]] = {sid: [] for sid in speech_ids}
        # Require conscious replacement of an existing manual policy value.
        for sid in speech_ids:
            requested_features = {feature: decision[feature] for feature in ("processing", "anchor", "prewarm")
                                  if decision[feature] != "keep"}
            resolved_features = set(resolutions.get(sid, {}).get("replace_policy_features", []))
            manual_before = block.get("rules", {}).get(sid, {})
            replaceable = {feature for feature, requested in requested_features.items()
                           if feature in manual_before and manual_before[feature] != requested}
            if resolved_features - replaceable:
                raise ValueError(f"ID {sid}: Konfliktlösung darf nur tatsächlich geänderte manuelle Merkmale bestätigen")
            for feature, requested in requested_features.items():
                if feature in manual_before and manual_before[feature] != requested and feature not in resolved_features:
                    conflicts[sid].append(f"Vorhandene ID-Entscheidung für {feature} wird ersetzt; Konflikt in der Vorschau ausdrücklich auflösen.")
            action = self.state.get("dialogue_actions", {}).get(sid, {})
            if action.get("action") == "exclude" and decision["processing"] not in {"keep"}:
                conflicts[sid].append("Die bestehende Ausschlussentscheidung bleibt wirksam; Verarbeitung kann hier nicht ersetzt werden.")
            if pre_rows[sid]["manual_reading"] and (marker or rule_values is not None or text_decision["reset_rules"]):
                if reading["action"] != "reset":
                    conflicts[sid].append("Manuelle Lesefassung behalten oder ausdrücklich zurücksetzen.")
            if resolved_features:
                warnings[sid].append("Vorhandene ID-Entscheidung wird durch die ausdrücklich bestätigte Auswahl ersetzt.")
            if any(pre_rows[sid]["policy"][feature].get("conflict") for feature in resolved_features):
                warnings[sid].append("Der bisherige Listen-/Rollenstreit wird durch diese ID-Entscheidung aufgelöst; Listenmitgliedschaft bleibt erhalten.")
        if any(conflicts.values()):
            rows = [{"speech_id": sid, "categories": pre_rows[sid]["categories"],
                     "original_de": pre_rows[sid]["original_de"], "original_en": pre_rows[sid]["original_en"],
                     "before": pre_rows[sid]["current_reading"], "after": pre_rows[sid]["current_reading"],
                     "policy_before": pre_rows[sid]["policy"], "policy_after": pre_rows[sid]["policy"],
                     "review_before": pre_rows[sid]["review_status"], "review_after": pre_rows[sid]["review_status"],
                     "finding_ids": pre_rows[sid]["finding_ids"], "warnings": warnings[sid], "conflicts": conflicts[sid]}
                    for sid in speech_ids]
            return {"revision": revision, "committed": False, "can_commit": False,
                    "preview_fingerprint": None, "preview": rows}

        # Mutate only a candidate copy. No call below writes state or exports.
        for sid in speech_ids:
            row = candidate_block["ids"][sid]
            if text_decision["reset_actions"]:
                candidate_block.get("text_actions", {}).pop(sid, None)
            if text_decision["reset_rules"]:
                candidate_block.get("rule_overrides", {}).pop(sid, None)
            elif rule_values is not None:
                candidate_block.setdefault("rule_overrides", {})[sid] = json.loads(json.dumps(rule_values, ensure_ascii=False))
            if reading["action"] == "reset":
                row["display_text"] = row["original_text"]
                row["manual_reading"] = False
            elif reading["action"] == "set":
                row["display_text"] = reading["text"]
                row["manual_reading"] = True
            if marker:
                chosen = selected_markers.get(sid, [])
                if not chosen:
                    warnings[sid].append("Keine passende Markierung; Text unverändert.")
                elif row.get("manual_reading"):
                    conflicts[sid].append("Manuelle Lesefassung behalten oder ausdrücklich zurücksetzen.")
                else:
                    current_sources = self._workflow_marker_sources(sid, chosen)
                    existing = candidate_block.get("text_actions", {}).get(sid, [])
                    existing_spans = [item.get("finding", {}).get("span", []) for item in existing]
                    for finding in current_sources:
                        span = finding["span"]
                        if any(span[0] < old[1] and old[0] < span[1] for old in existing_spans if len(old) == 2):
                            conflicts[sid].append("Die neue Passage überschneidet eine gespeicherte Textaktion; Fundaktionen zuerst zurücksetzen.")
                    if not conflicts[sid]:
                        candidate_block.setdefault("text_actions", {}).setdefault(sid, []).extend(
                            {"finding": dict(finding), "action": marker["action"],
                             "replacement": marker["replacement"],
                             "source_identity": candidate_block["ids"][sid]["source_sha256"]}
                            for finding in current_sources)
            for feature in ("processing", "anchor", "prewarm"):
                choice = decision[feature]
                if choice == "keep":
                    continue
                target = "inherit" if choice == "inherit" else choice
                if target == "inherit":
                    candidate_block.setdefault("rules", {}).setdefault(sid, {}).pop(feature, None)
                else:
                    candidate_block.setdefault("rules", {}).setdefault(sid, {})[feature] = target
            effective_role = self.effective_role_id(self.dialogue_by_speech[sid])
            resolved = text_state.resolve(candidate_block, sid, {
                "processing": self.role_processing(effective_role), "anchor": "use", "prewarm": "use"},
                validated=True)
            candidate_block.setdefault("last_consistent_decisions", {}).setdefault(sid, {}).update(
                resolved["last_consistent_decisions"])
        if any(conflicts.values()):
            post_rows = self._workflow_rows(candidate, speech_ids)
            rows = [{"speech_id": sid, "categories": sorted(set(pre_rows[sid]["categories"]) | set(post_rows[sid]["categories"])),
                     "original_de": pre_rows[sid]["original_de"], "original_en": pre_rows[sid]["original_en"],
                     "before": pre_rows[sid]["current_reading"], "after": post_rows[sid]["current_reading"],
                     "policy_before": pre_rows[sid]["policy"], "policy_after": post_rows[sid]["policy"],
                     "review_before": pre_rows[sid]["review_status"], "review_after": post_rows[sid]["review_status"],
                     "finding_ids": sorted(set(pre_rows[sid]["finding_ids"]) | set(post_rows[sid]["finding_ids"])),
                     "warnings": warnings[sid], "conflicts": conflicts[sid]}
                    for sid in speech_ids]
            return {"revision": revision, "committed": False, "can_commit": False,
                    "preview_fingerprint": None, "preview": rows}
        # Seed the selected IDs with their known history before taking the
        # after-projection. A text action may remove the last live finding,
        # but the review/fingerprint still needs a row for that ID.
        for sid in speech_ids:
            old_review = block.get("id_reviews", {}).get(sid, {})
            candidate_block.setdefault("id_reviews", {}).setdefault(sid, {
                "status": "open", "fingerprint": "",
                "categories": sorted(set(old_review.get("categories", [])) | set(pre_rows[sid]["categories"])),
                "finding_ids": sorted(set(old_review.get("finding_ids", [])) | set(pre_rows[sid]["finding_ids"])),
                "updated_at": utc_now()})
        text_state.validate(candidate_block)
        post_rows = self._workflow_rows(candidate, speech_ids)
        changes_text = (marker is not None or rule_values is not None or text_decision["reset_rules"]
                        or text_decision["reset_actions"] or reading["action"] != "keep")
        if changes_text:
            for sid in speech_ids:
                conflicts[sid].extend(post_rows[sid]["text_action_blockers"])
        if any(conflicts.values()):
            rows = [{"speech_id": sid,
                     "categories": sorted(set(pre_rows[sid]["categories"]) | set(post_rows[sid]["categories"])),
                     "original_de": pre_rows[sid]["original_de"], "original_en": pre_rows[sid]["original_en"],
                     "before": pre_rows[sid]["current_reading"], "after": post_rows[sid]["current_reading"],
                     "policy_before": pre_rows[sid]["policy"], "policy_after": post_rows[sid]["policy"],
                     "review_before": pre_rows[sid]["review_status"], "review_after": pre_rows[sid]["review_status"],
                     "finding_ids": sorted(set(pre_rows[sid]["finding_ids"]) | set(post_rows[sid]["finding_ids"])),
                     "warnings": warnings[sid], "conflicts": conflicts[sid]}
                    for sid in speech_ids]
            return {"revision": revision, "committed": False, "can_commit": False,
                    "preview_fingerprint": None, "preview": rows}
        rows = []
        for sid in speech_ids:
            before, after = pre_rows[sid], post_rows[sid]
            old_review = block.get("id_reviews", {}).get(sid, {})
            categories = sorted(set(old_review.get("categories", [])) | set(before["categories"]) | set(after["categories"]))
            finding_ids = sorted(set(old_review.get("finding_ids", [])) | set(before["finding_ids"]) | set(after["finding_ids"]))
            current_projection = self._workflow_rows(candidate, [sid])[sid]
            status = decision["review"]
            if status == "release":
                status = "released"
            if status == "keep":
                status = "released" if (old_review.get("status") == "released" and
                    old_review.get("fingerprint") == current_projection["review_fingerprint"]) else "open"
            candidate_block.setdefault("id_reviews", {})[sid] = {
                "status": status, "fingerprint": current_projection["review_fingerprint"],
                "categories": categories, "finding_ids": finding_ids, "updated_at": utc_now()}
            rows.append({"speech_id": sid, "categories": categories,
                "original_de": before["original_de"], "original_en": before["original_en"],
                "before": before["current_reading"], "after": after["current_reading"],
                "policy_before": before["policy"], "policy_after": after["policy"],
                "review_before": before["review_status"], "review_after": status,
                "finding_ids": finding_ids, "warnings": warnings[sid], "conflicts": []})
        payload_fingerprint = digest_bytes(canonical({"revision": revision, "speech_ids": speech_ids,
            "decision": decision, "resolutions": resolutions, "preview": rows}))
        if data["action"] == "preview":
            return {"revision": revision, "committed": False, "can_commit": True,
                    "preview_fingerprint": payload_fingerprint, "preview": rows}
        if data["preview_fingerprint"] != payload_fingerprint:
            raise RuntimeError("Vorschau wurde geändert oder ist veraltet; erneut prüfen")
        candidate["revision"] = revision + 1
        candidate["updated_at"] = utc_now()
        previous_state = self.state
        previous_roles, previous_role_by_id = self.roles, self.role_by_id
        try:
            self.state = candidate
            self.refresh_roles()
            atomic_json(self.state_path, candidate)
            self._effective_text_cache = {}
            self._text_findings_cache = {}
            self._text_projection_cache = None
        except Exception:
            self.state = previous_state
            self.roles, self.role_by_id = previous_roles, previous_role_by_id
            raise
        return {"revision": candidate["revision"], "committed": True,
                "text_prewizard": self.text_prewizard_state()}

    def _text_findings_for_id(self, sid: str, dictionary: GermanDictionary | None = None) -> list[dict]:
        block = self.state["text_prewizard"]
        dictionary = dictionary or self._get_text_dictionary(block["project_words"])
        prepared = self.effective_text(sid)
        key = canonical([sid, prepared["source_text"], prepared["tts_text"],
                         self._effective_preparation_rules(block, sid), block["project_words"],
                         dictionary.identity, block["pronunciation_templates"], block["search_templates"]])
        cached_entry = self._text_findings_cache.get(sid)
        cached = cached_entry[1] if cached_entry is not None and cached_entry[0] == key else None
        if cached is None:
            self._text_cache_stats["detected"] += 1
            cached = (text_review.find_text_reviews(sid, prepared["source_text"], prepared["tts_text"], dictionary) +
                      text_review.find_vocalizations(sid, prepared["source_text"], prepared["tts_text"], block["pronunciation_templates"]) +
                      text_review.find_markers(sid, prepared["source_text"], prepared["tts_text"], block["search_templates"]))
            for finding in cached:
                basis = "im unveränderten Ausgangstext" if finding["basis"] == "source" else "in der aktuellen Lesefassung"
                if finding["kind"] == "unknown_sentence":
                    rule_label = "Wörterbuchprüfung"
                    reason = f"Alle erkannten Wörter fehlen im gebundenen Wörterbuch {basis}; das ist ein Prüfhinweis, keine automatische Korrektur."
                elif finding["kind"] == "number":
                    rule_label = "Zahlenprüfung"
                    reason = f"Zahl im Text {basis}; Schreibweise und gewünschte Aussprache prüfen."
                elif finding["kind"] == "character_passage":
                    rule_label = "Zeichenpassage"
                    reason = f"Zeichenfolge ohne Wörter {basis}; prüfen, ob sie gesprochen oder als Geräusch übernommen werden soll."
                elif finding["kind"] == "vocalization":
                    rule = next((x for x in block["pronunciation_templates"] if x.get("id") == finding.get("rule_id")), None)
                    rule_label = f"Lautvorlage {rule['id']}" if rule else "Lautvorlage"
                    reason = f"Passt zur Lautvorlage {rule['id']} ({rule['base']}) {basis}." if rule else f"Passt zu einer konfigurierten Lautvorlage {basis}."
                elif finding["kind"] == "marker":
                    rule = next((x for x in block["search_templates"] if x.get("id") == finding.get("rule_id")), None)
                    delimiters = rule or finding.get("delimiters", {})
                    start, end = delimiters.get("start", ""), delimiters.get("end", "")
                    is_default = str(finding.get("rule_id", "")).startswith("default:")
                    inner_glob = rule.get("inner_glob") if rule else None
                    boundary = f"{start} TEXT {end}"
                    rule_label = (f"Standardmarkierung „{boundary}“" if is_default else f"Markierung „{boundary}“")
                    if inner_glob is not None:
                        rule_label += f" · Inhalt: {inner_glob or 'leer'}"
                    status_reason = {"complete": "Die Markierung ist vollständig.",
                        "unclosed": "Die öffnende Markierung hat kein passendes Ende.",
                        "unmatched_close": "Die schließende Markierung hat keinen passenden Anfang.",
                        "nested": "Die Markierung ist verschachtelt; ihre Grenzen müssen geprüft werden.",
                        "overlapping": "Die Markierung überschneidet sich mit einer anderen; ihre Grenzen müssen geprüft werden."}.get(
                            finding.get("status"), "Die Markierung braucht eine manuelle Prüfung.")
                    pattern_reason = (f" Der Inhalt entspricht exakt {inner_glob!r} (Groß-/Kleinschreibung zählt)."
                                      if inner_glob is not None else " Der Inhalt innerhalb der Begrenzungen ist beliebig.")
                    reason = (f"{status_reason} Fundstelle {basis}: {start!r} … {end!r}."
                              f"{pattern_reason} Prüfen, ob Begrenzungszeichen oder Passage bearbeitet werden sollen.")
                else:
                    rule_label = "Regelkonflikt"
                    reason = f"Regelkonflikt {basis}; den letzten konsistenten Wert prüfen."
                finding["reason"] = reason
                finding["rule_label"] = rule_label
            self._text_findings_cache[sid] = (key, json.loads(json.dumps(cached, ensure_ascii=False)))
        return json.loads(json.dumps(cached, ensure_ascii=False))

    def _get_text_dictionary(self, project_words: list[str]) -> GermanDictionary:
        words = tuple(project_words)
        if self._text_dictionary is None or words != self._text_dictionary_words:
            self._text_dictionary = GermanDictionary(project_words=words)
            self._text_dictionary_words = words
        return self._text_dictionary

    def text_prewizard_state(self, client_revision: int | None = None, *, detail_id: str | None = None,
                             audition_busy: bool = False, audition_job: dict | None = None) -> dict:
        if (detail_id is None and client_revision == self.state["revision"] and self._text_projection_cache
                and self._text_projection_cache.get("revision") == self.state["revision"]):
            return {"revision": self.state["revision"], "unchanged": True}
        block = self.state["text_prewizard"]
        dictionary = self._get_text_dictionary(block["project_words"])
        ids = {}
        conflicts = []
        for sid, row in block["ids"].items():
            prepared = self.effective_text(sid)
            policy = text_state.resolve(block, sid,
                {"processing": self.role_processing(self.effective_role_id(self.dialogue_by_speech[sid])),
                 "anchor": "use", "prewarm": "use"}, validated=True)
            ids[sid] = {**prepared, "policy": policy}
            conflicts.extend({"speech_id": sid, "feature": feature, **policy[feature]}
                             for feature in ("processing", "anchor", "prewarm") if policy[feature]["conflict"])
            conflicts.extend({"speech_id": sid, **item} for item in prepared["review_cases"]
                             if item.get("kind") == "rule_conflict")
        findings = self._text_findings(dictionary)
        findings_by_id: dict[str, list[dict]] = {}
        for finding in findings:
            findings_by_id.setdefault(finding["speech_id"], []).append(finding)
        special_cases = {}
        for sid in sorted(set(findings_by_id) | set(block.get("id_reviews", {})), key=int):
            row = self._special_case_row(sid, live=findings_by_id.get(sid, []))
            if row is not None:
                special_cases[sid] = row
        categories = sorted({category for row in special_cases.values() for category in row["categories"]})
        case_counts = {"ids": len(special_cases),
                       "open": sum(row["review_status"] == "open" for row in special_cases.values()),
                       "released": sum(row["review_status"] == "released" for row in special_cases.values()),
                       "categories": {category: sum(category in row["categories"] for row in special_cases.values())
                                      for category in categories}}
        if detail_id is not None:
            if detail_id not in special_cases:
                raise ValueError("Die angeforderte ID ist kein aktueller oder gespeicherter Sonderfall")
            special_cases[detail_id] = self._special_case_row(detail_id, live=findings_by_id.get(detail_id, []),
                status=self.text_audition_status(detail_id, busy=audition_busy, job=audition_job))
        by_id = {x["finding_id"]: x for x in block["completed_reviews"]}
        original_urls = {}
        for sid in self.dialogue_by_speech:
            try:
                self.resolve_audio(sid)
            except (ValueError, FileNotFoundError):
                continue
            original_urls[sid] = "/audio/original/" + sid
        result = {"revision": self.state["revision"], "rules": block["preparation_rules"] or text_rules.default_rules(), "id_policies": block["rules"], "results": ids,
                "counts": {"ids": len(ids), "findings": len(findings), "open": sum(x["id"] not in by_id for x in findings)},
                "findings": [{**x, "review_status": "complete" if x["id"] in by_id else "open"} for x in findings],
                "special_cases": special_cases, "case_counts": case_counts,
                "archived_reviews": block["archived_reviews"], "project_words": block["project_words"],
                "sounds": block["pronunciation_templates"], "patterns": block["search_templates"],
                "lists": block["lists"], "conflicts": conflicts, "original_urls": original_urls,
                "dictionary": {"available": dictionary.status.available,
                    "message": dictionary.status.message, "identity": dictionary.identity}}
        self._text_projection_cache = json.loads(json.dumps(result, ensure_ascii=False))
        return result

    def _legacy_effective_processing(self, dialogue: dict) -> str:
        role_id = self.effective_role_id(dialogue)
        # Preserve the established whole-role override: once a role is set to
        # original audio, all IDs currently assigned to it follow that choice.
        if self.role_processing(role_id) == "copy_original":
            return "copy_original"
        action = self.state.get("dialogue_actions", {}).get(str(dialogue.get("native_speech_id") or ""), {})
        if action.get("action") in {"copy_original", "exclude"}:
            return action["action"]
        if action.get("action") in {"assign_existing", "technical_role"}:
            return "render"
        confirmed = (dialogue.get("unclear_resolution") or {}).get("confirmed_processing")
        if confirmed == "copy_original":
            return "copy_original"
        if dialogue.get("extractor_status") == "todo" or confirmed == "render":
            return "render"
        return "pending"

    def role_processing(self, role_id: str) -> str:
        return self.state.get("role_processing", {}).get(role_id, {}).get("processing", "render")

    def role_members(self, role_id: str) -> list[dict]:
        return [dialogue for dialogue in self.dialogues if self.effective_role_id(dialogue) == role_id]

    def require_render_role(self, role_id: str) -> None:
        if self.role_processing(role_id) == "copy_original":
            raise ValueError("Diese Rolle übernimmt vollständig die Originalaufnahmen; zuerst auf Neuvertonung umstellen")

    def role_rules(self, role_id: str) -> dict:
        stored = self.state.get("role_rules", {}).get(role_id, {})
        return {"without_anchor": bool(stored.get("without_anchor", False)),
                "without_prewarm": bool(stored.get("without_prewarm", False))}

    def save_role_rules(self, payload: object) -> dict:
        required = {"role_id", "revision", "without_anchor", "without_prewarm"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise ValueError("Rollenregeln brauchen role_id, revision, without_anchor und without_prewarm")
        role_id = payload["role_id"]
        role = self.role_by_id.get(role_id) if isinstance(role_id, str) else None
        if not role or role.get("work_queue"):
            raise ValueError("Rollenregeln brauchen eine geklärte Rolle")
        self.require_render_role(role_id)
        if type(payload["revision"]) is not int or payload["revision"] != self.state["revision"]:
            raise RuntimeError("Zustand wurde zwischenzeitlich geändert; Seite neu laden")
        rules = {key: payload[key] for key in ("without_anchor", "without_prewarm")}
        if any(type(value) is not bool for value in rules.values()):
            raise ValueError("Rollenregeln müssen boolesch sein")
        if rules != self.role_rules(role_id):
            if any(rules.values()):
                self.state.setdefault("role_rules", {})[role_id] = rules
            else:
                self.state.setdefault("role_rules", {}).pop(role_id, None)
            self.state["revision"] += 1
            self.state["updated_at"] = utc_now()
            atomic_json(self.state_path, self.state)
            self.refresh_roles()
        return self.public_state()

    def dialogue_render_flags(self, role_id: str, record: dict, short: bool) -> tuple[bool, bool]:
        rules = self.role_rules(role_id)
        use_prewarm = not rules["without_prewarm"] and (short or bool(record.get("prewarm_for_long_text", False)))
        use_voice_anchor = not rules["without_anchor"] and (short or use_prewarm or bool(record.get("use_voice_anchor_for_long_text", False)))
        return use_voice_anchor, use_prewarm

    def dialogue_text_policy(self, dialogue: dict, role_id: str | None = None) -> dict:
        """Resolve manual, list, then legacy role policy for one source ID."""
        speech_id = str(dialogue.get("native_speech_id") or "")
        role_id = role_id or self.effective_role_id(dialogue)
        record = self.state.get("roles", {}).get(role_id) or {}
        text = self.effective_text(speech_id)["tts_text"]
        audio = dialogue.get("original_audio") or {}
        short = int(audio.get("duration_ms_from_extraction") or 0) < 3000 or len(text.split()) < 4
        legacy_anchor, legacy_prewarm = self.dialogue_render_flags(role_id, record, short)
        block = self.state.get("text_prewizard") or {}
        if speech_id not in block.get("ids", {}):
            return {"anchor": {"value": "use" if legacy_anchor else "skip", "source": "role", "conflict": False},
                    "prewarm": {"value": "use" if legacy_prewarm else "skip", "source": "role", "conflict": False}}
        return text_state.resolve(block, speech_id,
            {"processing": self.role_processing(role_id), "anchor": "use" if legacy_anchor else "skip",
             "prewarm": "use" if legacy_prewarm else "skip"}, validated=True)

    def effective_dialogue_render_flags(self, dialogue: dict, role_id: str | None = None) -> tuple[bool, bool]:
        policy = self.dialogue_text_policy(dialogue, role_id)
        return policy["anchor"]["value"] == "use", policy["prewarm"]["value"] == "use"

    def save_role_processing(self, payload: object) -> dict:
        if not isinstance(payload, dict) or set(payload) != {"role_id", "revision", "processing"}:
            raise ValueError("Rollenverarbeitung braucht role_id, revision und processing")
        role_id = payload["role_id"]
        role = self.role_by_id.get(role_id) if isinstance(role_id, str) else None
        if not role or role.get("work_queue"):
            raise ValueError("Originalübernahme braucht eine geklärte bestehende Rolle")
        if type(payload["revision"]) is not int or payload["revision"] != self.state["revision"]:
            raise RuntimeError("Zustand wurde zwischenzeitlich geändert; Seite neu laden")
        processing = payload["processing"]
        if not isinstance(processing, str) or processing not in {"copy_original", "render"}:
            raise ValueError("Rollenverarbeitung muss copy_original oder render sein")
        if processing == "copy_original" and not self.role_members(role_id):
            raise ValueError("Die Rolle enthält keine zugeordneten Dialoge")
        if self.role_processing(role_id) != processing:
            overrides = self.state.setdefault("role_processing", {})
            if processing == "copy_original":
                overrides[role_id] = {"processing": processing, "decided_at": utc_now()}
            else:
                overrides.pop(role_id, None)
            self.state["revision"] += 1
            self.state["updated_at"] = utc_now()
            atomic_json(self.state_path, self.state)
            self.refresh_roles()
        return self.public_state()

    def _roles(self) -> list[dict]:
        active_groups: dict[str, list[dict]] = {}
        original_groups: dict[str, list[dict]] = {}
        for dialogue in self.dialogues:
            original_groups.setdefault(role_id_for(dialogue), []).append(dialogue)
            if self.effective_processing(dialogue) == "render":
                active_groups.setdefault(self.effective_role_id(dialogue), []).append(dialogue)
        role_ids = set(original_groups) | set(active_groups) | set(self.state.get("technical_roles", {}))
        result = []
        for role_id in sorted(role_ids):
            dialogues = active_groups.get(role_id, [])
            original_dialogues = original_groups.get(role_id, [])
            evidence_dialogues = original_dialogues or dialogues
            speakers = [d.get("speaker_candidate") or {} for d in evidence_dialogues]
            codes = sorted({s.get("code") or "" for s in speakers})
            names = sorted({s.get("name") or "" for s in speakers if s.get("name")})
            statuses = sorted({s.get("status") or "" for s in speakers})
            evidence = sorted({s.get("evidence") or "" for s in speakers if s.get("evidence")})
            actor_ids = sorted({s.get("actor_id") or "" for s in speakers if s.get("actor_id")})
            eligible = [value for d in dialogues if (value := candidate_from(d, self.effective_text(str(d.get("native_speech_id") or ""))["tts_text"]))]
            candidates, selection_meta = pick_candidates(eligible)
            attention = []
            source_code = (original_dialogues[0].get("speaker_candidate") or {}).get("code") if original_dialogues else None
            work_queue = bool(original_dialogues) and (source_code in {None, "", "126", "133"}
                                                        or any((d.get("speaker_candidate") or {}).get("status") == "unclassified_audio"
                                                               for d in original_dialogues))
            if work_queue:
                attention.append("speaker_identity_unresolved")
            if not candidates:
                attention.append("no_verified_anchor_candidates")
            technical = self.state.get("technical_roles", {}).get(role_id)
            display_name = (technical["name"] if technical else names[0] if len(names) == 1
                            else codes[0] if codes and codes != [""] else "Unklassifiziert")
            result.append({
                "role_id": role_id, "display_name": display_name, "speaker_code": codes[0] if codes else "TECH",
                "speaker_names": names, "speaker_statuses": statuses, "speaker_evidence": evidence,
                "actor_ids_observed": actor_ids, "render_dialog_count": len(dialogues),
                "render_dialog_ids": [d["dialog_id"] for d in dialogues],
                "work_queue": work_queue, "work_queue_dialog_count": len(original_dialogues),
                "pending_dialog_count": sum(self.effective_processing(d) == "pending" for d in original_dialogues),
                "pending_speech_ids": [str(d.get("native_speech_id") or "") for d in original_dialogues
                                        if self.effective_processing(d) == "pending"],
                "technical_role": bool(technical),
                "candidates": candidates, "candidate_selection": selection_meta,
                "_eligible_candidates": eligible,
                "automatic_composite": automatic_composite(candidates),
                "blocked_reasons": [],
                "attention_reasons": attention,
                "small_role_fallback": bool(eligible) and anchor_duration(eligible) < TARGET_MIN_MS,
            })
        return result

    def _empty_state(self) -> dict:
        text_block, _ = text_state.migrate({}, self.dialogues)
        return {"format": FORMAT, "source_manifest_sha256": self.source_sha256,
                "revision": 0, "updated_at": utc_now(), "roles": {}, "candidate_views": {},
                "custom_anchors": {}, "custom_prewarm_texts": {}, "dialogue_actions": {}, "technical_roles": {},
                "role_processing": {}, "role_rules": {}, "role_fingerprint_source_sha256": {},
                "text_prewizard": text_block["text_prewizard"]}

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            state = self._empty_state()
            atomic_json(self.state_path, state)
            return state
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("format") != FORMAT:
            raise ValueError("Unbekanntes Wizard-Zustandsformat")
        if state.get("source_manifest_sha256") != self.source_sha256:
            raise ValueError("Wizard-Zustand gehört zu einem anderen Manifestentwurf; separat archivieren")
        original_state_bytes = self.state_path.read_bytes()
        state, migrated = text_state.migrate(state, self.dialogues)
        state, registered = text_state.register_dialogues(state, self.dialogues)
        migrated = migrated or registered
        if migrated:
            backup = self.state_path.with_name("state.json.pre-text-prewizard.bak")
            if not backup.exists():
                fd, temporary = tempfile.mkstemp(prefix="." + backup.name + "-", dir=backup.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(original_state_bytes); stream.flush(); os.fsync(stream.fileno())
                    os.replace(temporary, backup)
                finally:
                    if os.path.exists(temporary): os.unlink(temporary)
            state["revision"] += 1
            state["updated_at"] = utc_now()
            atomic_json(self.state_path, state)
        state.setdefault("candidate_views", {})
        state.setdefault("custom_anchors", {})
        state.setdefault("custom_prewarm_texts", {})
        state.setdefault("dialogue_actions", {})
        state.setdefault("technical_roles", {})
        state.setdefault("role_processing", {})
        state.setdefault("role_rules", {})
        state.setdefault("role_fingerprint_source_sha256", {})
        return state

    def save_text_prewizard(self, payload: object) -> dict:
        """Revision-guarded write access for named explicit ID lists and decisions."""
        if not isinstance(payload, dict) or set(payload) != {"revision", "operation", "data"}:
            raise ValueError("Vorwizard-Aktion braucht revision, operation und data")
        if type(payload["revision"]) is not int or payload["revision"] != self.state["revision"]:
            raise RuntimeError("Zustand wurde zwischenzeitlich geändert; Seite neu laden")
        data = payload["data"]
        if not isinstance(data, dict):
            raise ValueError("Vorwizard-Aktionsdaten müssen ein Objekt sein")
        block = self.state["text_prewizard"]
        op = payload["operation"]
        if op == "create_list":
            updated = text_state.create_list(block, data.get("list_id"), data.get("name"))
        elif op == "rename_list":
            updated = text_state.rename_list(block, data.get("list_id"), data.get("name"))
        elif op == "delete_list":
            updated = text_state.delete_list(block, data.get("list_id"))
        elif op == "set_members":
            members = data.get("members")
            if not isinstance(members, list):
                raise ValueError("Mitglieder müssen eine explizite ID-Liste sein")
            for member in members:
                sid = member.get("speech_id") if isinstance(member, dict) else None
                if sid not in block["ids"]:
                    raise ValueError(f"Unbekannte native_speech_id: {sid!r}")
                if not member.get("source_span") or not member.get("source_identity"):
                    raise ValueError("Fundstelle braucht Quellbereich und Quellidentität")
                span = member["source_span"]
                source_text = block["ids"][sid]["original_text"]
                if (not isinstance(span, list) or len(span) != 2 or any(type(x) is not int for x in span)
                        or not (0 <= span[0] < span[1] <= len(source_text))
                        or member["source_identity"] != block["ids"][sid]["source_sha256"]):
                    raise ValueError("Quellfundstelle ist ungültig oder veraltet")
                if member.get("reading_span") and not member.get("reading_source_identity"):
                    raise ValueError("Lesefassungsfundstelle braucht Herkunftsabbildung")
                if member.get("reading_span"):
                    read_span = member["reading_span"]
                    origin_span = member.get("reading_source_span")
                    reading = block["ids"][sid]["display_text"]
                    if (not isinstance(origin_span, list) or len(origin_span) != 2
                            or any(type(x) is not int for x in origin_span)
                            or not (0 <= origin_span[0] < origin_span[1] <= len(source_text))
                            or not isinstance(read_span, list) or len(read_span) != 2 or any(type(x) is not int for x in read_span)
                            or not (0 <= read_span[0] < read_span[1] <= len(reading))
                            or member["reading_source_identity"] != digest_bytes(reading.encode("utf-8"))):
                        raise ValueError("Lesefassungsfundstelle braucht gültige Herkunftsabbildung und Identität")
                if member.get("reading_stale"):
                    raise ValueError("Veraltete Lesefassungsfundstelle muss neu geprüft werden")
            updated = text_state.set_members(block, data.get("list_id"), members)
        elif op == "remove_members":
            updated = text_state.remove_members(block, data.get("list_id"), data.get("speech_ids", []))
        elif op == "set_manual_decision":
            sid, feature, value = data.get("speech_id"), data.get("feature"), data.get("value")
            if sid not in block["ids"] or feature not in text_state.ENUMS or value not in text_state.ENUMS[feature]:
                raise ValueError("Ungültige ID, Merkmal oder Entscheidung")
            updated = json.loads(json.dumps(block))
            updated["rules"].setdefault(sid, {})[feature] = value
        elif op == "reset_manual_decision":
            sid, feature = data.get("speech_id"), data.get("feature")
            if sid not in block["ids"] or feature not in text_state.ENUMS:
                raise ValueError("Ungültige ID oder Merkmal")
            updated = json.loads(json.dumps(block))
            updated["rules"].setdefault(sid, {}).pop(feature, None)
        else:
            raise ValueError("Unbekannte Vorwizard-Aktion")
        text_state.validate(updated)
        if updated != block:
            # Remember each unambiguous per-ID result. Conflicting list edits
            # then retain this value until the conflict is resolved.
            for sid in updated["ids"]:
                decision = text_state.resolve(updated, sid, {"processing": "render", "anchor": "use", "prewarm": "use"}, validated=True)
                saved = updated["last_consistent_decisions"].setdefault(sid, {})
                saved.update(decision["last_consistent_decisions"])
            self.state["text_prewizard"] = updated
            self.state["revision"] += 1
            self.state["updated_at"] = utc_now()
            atomic_json(self.state_path, self.state)
            self.refresh_roles()
        return self.public_state()

    def dialogue_processing(self, speech_id: str) -> str:
        dialogue = self.dialogue_by_speech.get(speech_id)
        return self.effective_processing(dialogue) if dialogue else "render"

    def active_role_dialogues(self, role: dict) -> list[dict]:
        return [d for d in self.dialogues if self.effective_processing(d) == "render"
                and self.effective_role_id(d) == role["role_id"]]

    def save_dialogue_action(self, payload: object) -> dict:
        required = {"role_id", "speech_ids", "action", "revision"}
        allowed = required | {"target_role_id", "technical_role_name", "reason"}
        if not isinstance(payload, dict) or not required.issubset(payload) or not set(payload).issubset(allowed):
            raise ValueError("Dialogentscheidung enthält unbekannte oder fehlende Felder")
        role_id, speech_ids, action = payload["role_id"], payload["speech_ids"], payload["action"]
        role = self.role_by_id.get(role_id)
        if not role:
            raise ValueError("Unbekannte Rolle")
        if not isinstance(speech_ids, list) or not speech_ids or len(speech_ids) != len(set(speech_ids)):
            raise ValueError("Mindestens eine eindeutige Dialog-ID auswählen")
        dialogues = []
        for speech_id in speech_ids:
            dialogue = self.dialogue_by_speech.get(speech_id) if isinstance(speech_id, str) else None
            if not dialogue or role_id not in {role_id_for(dialogue), self.effective_role_id(dialogue)}:
                raise ValueError("Dialog-ID gehört nicht zu dieser Rolle")
            if action == "reset" and not self.state.get("dialogue_actions", {}).get(speech_id):
                raise ValueError("Für diese Dialog-ID gibt es keine explizite Entscheidung")
            dialogues.append(dialogue)
        if type(payload["revision"]) is not int or payload["revision"] != self.state["revision"]:
            raise RuntimeError("Zustand wurde zwischenzeitlich geändert; Seite neu laden")
        if action not in {"reset", "copy_original", "nonverbal_original", "assign_existing", "technical_role", "exclude"}:
            raise ValueError("Unbekannte Arbeitskorbaktion")
        target_role_id = None
        if action == "assign_existing":
            target_role_id = payload.get("target_role_id")
            target = self.role_by_id.get(target_role_id)
            if not target or target.get("work_queue"):
                raise ValueError("Eine geklärte bestehende Zielrolle wählen")
        if action == "technical_role":
            name = str(payload.get("technical_role_name") or "").strip()
            if not 2 <= len(name) <= 80:
                raise ValueError("Technische Rolle braucht einen Namen mit 2–80 Zeichen")
            target_role_id = "mi1-wizard:technical:" + digest_bytes(name.casefold().encode("utf-8"))[:16]
            self.state.setdefault("technical_roles", {}).setdefault(
                target_role_id, {"name": name, "created_at": utc_now()})
        exclude_reason = None
        if action == "exclude":
            exclude_reason = payload.get("reason")
            if not isinstance(exclude_reason, str) or not exclude_reason.strip():
                raise ValueError("Ausschluss braucht eine Begründung")
        actions = self.state.setdefault("dialogue_actions", {})
        for speech_id, dialogue in zip(speech_ids, dialogues):
            if action == "reset":
                actions.pop(speech_id, None)
                continue
            decision = {"action": "copy_original" if action == "nonverbal_original" else action, "source_role_id": role_id,
                        "dialog_id": dialogue["dialog_id"], "decided_at": utc_now()}
            if action == "nonverbal_original":
                decision["content_type"] = "nonverbal"
                decision["source_role_id"] = role_id_for(dialogue)
                if role_id != decision["source_role_id"]:
                    decision["target_role_id"] = role_id
            if target_role_id:
                decision["target_role_id"] = target_role_id
            if action == "exclude":
                decision["reason"] = exclude_reason.strip()
            actions[speech_id] = decision
        self.state["revision"] += 1
        self.state["updated_at"] = utc_now()
        atomic_json(self.state_path, self.state)
        self.refresh_roles()
        return self.public_state()

    def reload_state(self) -> None:
        state = self._load_state()
        if state != self.state:
            self.state = state
            self._text_projection_cache = None
            self.refresh_roles()

    def save_original_transcript(self, payload: object) -> dict:
        if not isinstance(payload, dict) or set(payload) != {"role_id", "speech_id", "revision", "text"}:
            raise ValueError("Originaltranskript braucht Rolle, Dialog-ID, Revision und Text")
        dialogue = self.dialogue_by_speech.get(payload["speech_id"])
        if not dialogue:
            raise ValueError("Unbekannte Dialog-ID")
        return self.save_dialogue_text({"role_id": payload["role_id"], "speech_id": payload["speech_id"],
            "revision": payload["revision"], "english_text": payload["text"], "german_text": german_text(dialogue)})

    def save_review_tts(self, speech_id: str, text: str) -> None:
        """Version TTS/QA only in local Wizard state; preserve the display text."""
        sid = str(speech_id)
        if sid not in self.dialogue_by_speech or not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("Review-TTS braucht eine bekannte ID und 1–4000 Zeichen")
        before = self.effective_text(sid)
        prepared = text_rules.prepare_text(text, manual_text=text)
        candidate = json.loads(json.dumps(self.state))
        old = candidate.setdefault("patch_review_tts", {}).get(sid, {})
        history = list(old.get("history", []))
        history.append({"tts_text": before["tts_text"], "qa_expected_text": before["qa_expected_text"],
                        "display_text": before["display_text"], "revision": old.get("revision", 0)})
        candidate["patch_review_tts"][sid] = {"tts_text": prepared["tts_text"], "qa_expected_text": prepared["qa_expected_text"],
            "revision": old.get("revision", 0) + 1, "history": history}
        candidate["revision"] += 1
        candidate["updated_at"] = utc_now()
        atomic_json(self.state_path, candidate)
        self.state = candidate
        self._effective_text_cache.clear()
        self._text_projection_cache = None

    def save_review_processing(self, speech_id: str, processing: str, binding: dict, original_binding: dict) -> None:
        """Persist the desired manifest processing, independently of audio selection."""
        sid = str(speech_id)
        if sid not in self.dialogue_by_speech or processing not in {"render", "copy_original"}:
            raise ValueError("Reviewauswahl braucht eine bekannte Dialog-ID und Verarbeitung")
        dialogue = self.dialogue_by_speech[sid]
        original = dialogue.get("original_audio", {})
        english = (dialogue.get("english_text_candidate") or dialogue.get("original_text") or {}).get("text", "")
        from patch_review.evidence import digest as review_digest
        audio_hash = original.get("sha256_from_extraction", original.get("sha256"))
        text_hash = review_digest([english, ""])
        if original_binding.get("wav_sha256") != audio_hash or original_binding.get("text_sha256") != text_hash:
            raise ValueError("Revieworiginal stimmt nicht mit der aktuellen Wizardquelle überein")
        candidate = json.loads(json.dumps(self.state))
        previous = candidate.setdefault("patch_review_processing", {}).get(sid)
        history = list((previous or {}).get("history", []))
        if previous:history.append({k: v for k, v in previous.items() if k != "history"})
        candidate["patch_review_processing"][sid] = {"processing": processing, "binding": binding,
            "original_audio_sha256": audio_hash, "original_text_sha256": text_hash, "history": history, "decided_at": utc_now()}
        candidate["revision"] += 1
        candidate["updated_at"] = utc_now()
        atomic_json(self.state_path, candidate)
        self.state = candidate
        self._text_projection_cache = None

    def save_dialogue_text(self, payload: object) -> dict:
        if not isinstance(payload, dict) or set(payload) != {"role_id", "speech_id", "revision", "english_text", "german_text"}:
            raise ValueError("Dialogtext braucht Rolle, Dialog-ID, Revision sowie englischen und deutschen Text")
        role = self.role_by_id.get(payload["role_id"])
        dialogue = self.dialogue_by_speech.get(payload["speech_id"])
        if not role or not dialogue or payload["role_id"] not in {role_id_for(dialogue), self.effective_role_id(dialogue)}:
            raise ValueError("Dialog-ID gehört nicht zu dieser Rolle")
        if type(payload["revision"]) is not int or payload["revision"] != self.state["revision"]:
            raise RuntimeError("Zustand wurde zwischenzeitlich geändert; Seite neu laden")
        current_texts = {"english_text": english_text(dialogue)[0],
                         "german_text": self.effective_text(str(payload["speech_id"]))["tts_text"]}
        texts = {}
        for language in ("english_text", "german_text"):
            text = payload[language]
            if not isinstance(text, str) or len(text.strip()) > 4000 or (current_texts[language] and not text.strip()):
                raise ValueError("Vorhandene Texte können nicht geleert werden; höchstens 4000 Zeichen je Sprache")
            text = text.strip()
            if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} and char not in "\n\r\t" for char in text):
                raise ValueError("Text enthält ungültige Steuerzeichen")
            texts[language] = text
        if texts == current_texts:
            return self.public_state()
        if not any(texts.values()):
            raise ValueError("Mindestens einen englischen oder deutschen Text eingeben")
        review_path = self.dubbing_root / REVIEW_PATH
        previous = review_path.read_bytes() if review_path.is_file() else None
        reviews = json.loads(previous) if previous else {"format": "mi1-english-transcript-reviews/1", "transcripts": {}}
        global_code = reviews.pop("role_code", None)
        for review in reviews["transcripts"].values():
            review.setdefault("role_code", global_code)
        audio = dialogue["original_audio"]
        previous_entry = reviews["transcripts"].get(payload["speech_id"], {})
        versions = json.loads(json.dumps(dialogue.get("text_versions") or {
            "original": {**current_texts, "source_manifest_sha256": self.source_sha256}, "versions": []}))
        version_id = "text:" + digest_bytes(canonical([texts["english_text"], texts["german_text"]]))
        if not any(version["version_id"] == version_id for version in versions["versions"]):
            versions["versions"].append({"version_id": version_id, **texts, "created_at": utc_now()})
        versions["active_version_id"] = version_id
        entry = {**previous_entry, "status": "inferred_reviewed", "confirmed_by": "user", "source": "user_transcript_in_wizard",
            "role_code": (dialogue.get("speaker_candidate") or {}).get("code"),
            "original_audio_path": audio["path"], "original_audio_sha256": audio["sha256_from_extraction"],
            "confirmed_at": utc_now(), "text_versions": versions}
        if texts["english_text"] != current_texts["english_text"]:
            entry["text"] = texts["english_text"]
        if texts["german_text"] != current_texts["german_text"]:
            entry["german_text"] = texts["german_text"]
            entry["variant_revision"] = (dialogue.get("text_variant_candidate") or {}).get("variant_revision", 0) + 1
        reviews["transcripts"][payload["speech_id"]] = entry
        review_bytes = (json.dumps(reviews, ensure_ascii=False, indent=2) + "\n").encode()
        originals = {review_path: previous, self.draft_path: self.draft_path.read_bytes(),
                     self.state_path: self.state_path.read_bytes()}
        if digest_bytes(originals[self.draft_path]) != self.source_sha256:
            raise RuntimeError("Manifest wurde zwischenzeitlich geändert; Server neu laden")
        draft, state, _ = prepare_manifest_import(originals[self.draft_path], originals[self.state_path],
                                                 self.dubbing_root, review_bytes)
        state_value = json.loads(state)
        text_id = state_value["text_prewizard"]["ids"][str(payload["speech_id"])]
        text_id["display_text"] = texts["german_text"]
        text_id["manual_reading"] = True
        state = (json.dumps(state_value, ensure_ascii=False, indent=2) + "\n").encode()
        commit_manifest_import({review_path: review_bytes, self.draft_path: draft, self.state_path: state}, originals, self.data_root)
        self.source_bytes, self.source_sha256 = draft, digest_bytes(draft)
        self.draft = json.loads(draft)
        self.dialogues = list(self.draft["dialogues"])
        self.dialogue_by_speech = {str(d["native_speech_id"]): d for d in self.dialogues}
        self.state = self._load_state()
        self.refresh_roles()
        return self.public_state()

    def _custom_candidates(self, role: dict) -> list[dict]:
        result = []
        for record in self.state.get("custom_anchors", {}).get(role["role_id"], {}).values():
            result.append({
                "speech_id": "custom:" + record["import_id"],
                "dialog_id": "custom-anchor:" + record["import_id"],
                "english_text": record["transcript"],
                "german_text": record["transcript"] if record["language"] == "de" else "",
                "duration_ms": record["duration_ms"],
                "audio": record["audio"],
                "source_kind": "custom",
                "custom_name": record["name"],
                "language": record["language"],
                "original_filename": record["original_filename"],
                "import_id": record["import_id"],
                "buckets": ["custom"],
                "below_five_seconds": record["duration_ms"] < 5000,
            })
        return sorted(result, key=lambda item: (item["custom_name"].casefold(), item["import_id"]))

    def _neutral_prewarm_candidates(self, role: dict) -> list[dict]:
        return [{"speech_id": f"neutral:{index}", "dialog_id": None, "english_text": "",
                 "german_text": text, "duration_ms": 3500, "audio": None,
                 "source_kind": "neutral_standard", "neutral_version": "mi1-neutral-prewarm/2",
                 "prewarm_rank": 0 if index == 4 else index, "prewarm_source_window_match": False,
                 "prewarm_suggestion_kind": "neutral_standard"}
                for index, text in enumerate(NEUTRAL_PREWARM_TEXTS, 1)]

    def _custom_prewarm_candidates(self, role: dict) -> list[dict]:
        return [{"speech_id": text_id, "dialog_id": None, "english_text": "",
                 "german_text": record["text"], "text_sha256": record["text_sha256"],
                 "duration_ms": None, "audio": None, "source_kind": "custom_text",
                 "prewarm_source_window_match": False, "prewarm_suggestion_kind": "custom_text"}
                for text_id, record in self.state.get("custom_prewarm_texts", {}).get(role["role_id"], {}).items()]

    def _prewarm_pool(self, role: dict) -> list[dict]:
        return [*sorted(self._neutral_prewarm_candidates(role), key=lambda item: item["prewarm_rank"]),
                *self._custom_prewarm_candidates(role),
                *prewarm_suggestions(role["_eligible_candidates"], limit=None)]

    def _candidate_map(self, role: dict) -> dict[str, dict]:
        candidates = [*role["_eligible_candidates"], *self._custom_candidates(role),
                      *self._neutral_prewarm_candidates(role), *self._custom_prewarm_candidates(role)]
        by_id = {candidate["speech_id"]: candidate for candidate in candidates}
        # A saved role reference remains valid when its source ID is later
        # switched to whole-original delivery. Keep that source resolvable for
        # viewing/export; it does not re-enter the eligible candidate pool.
        record = self.state.get("roles", {}).get(role["role_id"], {})
        pinned = {str(sid) for anchor in record.get("anchors", [])
                  for sid in anchor.get("component_speech_ids", [])}
        if record.get("prewarm_id") is not None:
            pinned.add(str(record["prewarm_id"]))
        for speech_id in sorted(pinned, key=lambda value: (0, int(value)) if value.isdigit() else (1, value)):
            if speech_id in by_id or speech_id not in self.dialogue_by_speech:
                continue
            dialogue = self.dialogue_by_speech[speech_id]
            if role["role_id"] not in {role_id_for(dialogue), self.effective_role_id(dialogue)}:
                continue
            candidate = candidate_from(dialogue, self.effective_text(speech_id)["tts_text"])
            if candidate:
                by_id[speech_id] = candidate
        return by_id

    def visible_candidates(self, role: dict, kind: str) -> list[dict]:
        if kind == "anchor":
            initial = [*self._custom_candidates(role), *role["candidates"]]
            selected = self.state.get("roles", {}).get(role["role_id"], {})
            by_id = self._candidate_map(role)
            for anchor in selected.get("anchors", []):
                for speech_id in anchor.get("component_speech_ids", []):
                    if speech_id in by_id and not any(item["speech_id"] == speech_id for item in initial):
                        initial.append(by_id[speech_id])
            pool = sorted(role["_eligible_candidates"],
                          key=lambda item: (-item["duration_ms"], int(item["speech_id"])))
        elif kind == "prewarm":
            pool = self._prewarm_pool(role)
            initial = [*sorted(self._neutral_prewarm_candidates(role), key=lambda item: item["prewarm_rank"]),
                       *prewarm_suggestions(role["_eligible_candidates"], limit=5),
                       *self._custom_prewarm_candidates(role)]
            selected_id = self.state.get("roles", {}).get(role["role_id"], {}).get("prewarm_id")
            if selected_id and not any(item["speech_id"] == selected_id for item in initial):
                selected = self._candidate_map(role).get(selected_id)
                if selected:
                    initial.append(selected)
        else:
            raise ValueError("Kandidatenart muss anchor oder prewarm sein")
        extras = self.state.get("candidate_views", {}).get(role["role_id"], {}).get(kind, [])
        by_id = self._candidate_map(role)
        result = [dict(item) for item in initial]
        seen = {item["speech_id"] for item in result}
        for speech_id in extras:
            if speech_id in by_id and speech_id not in seen:
                result.append(dict(by_id[speech_id])); seen.add(speech_id)
        return result

    def save_prewarm_text(self, payload: object) -> dict:
        if not isinstance(payload, dict) or set(payload) != {"role_id", "revision", "text"}:
            raise ValueError("Prewarm-Text braucht role_id, revision und text")
        role = self.role_by_id.get(payload["role_id"]) if isinstance(payload["role_id"], str) else None
        if not role or role.get("work_queue"):
            raise ValueError("Prewarm-Text braucht eine geklärte Rolle")
        self.require_render_role(role["role_id"])
        if type(payload["revision"]) is not int or payload["revision"] != self.state["revision"]:
            raise RuntimeError("Zustand wurde zwischenzeitlich geändert; Seite neu laden")
        raw = payload["text"]
        if not isinstance(raw, str) or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} and char not in "\n\r\t" for char in raw):
            raise ValueError("Prewarm-Text darf keine Steuerzeichen enthalten")
        text = raw.strip()
        if not text or len(text) > 500:
            raise ValueError("Prewarm-Text braucht 1–500 Zeichen")
        record = self.state["roles"].get(role["role_id"])
        if (not record or not record.get("anchors")) and not self.role_rules(role["role_id"])["without_anchor"]:
            raise ValueError("Zuerst einen gültigen Stimmanker speichern")
        if not record:
            record = {"anchors": [], "prewarm_id": None, "prewarm_for_long_text": False,
                      "use_voice_anchor_for_long_text": False, "selected_at": utc_now(), "updated_at": utc_now()}
            self.state["roles"][role["role_id"]] = record
        if not self.role_rules(role["role_id"])["without_anchor"]:
            for index, anchor in enumerate(record["anchors"]):
                self.validate_anchor(role, {key: anchor[key] for key in ("kind", "component_speech_ids")}, backup=index > 0)
        text_sha256 = digest_bytes(text.encode("utf-8"))
        text_id = "text:" + text_sha256
        texts = self.state.setdefault("custom_prewarm_texts", {}).setdefault(role["role_id"], {})
        changed = text_id not in texts or record.get("prewarm_id") != text_id
        if changed:
            texts.setdefault(text_id, {"source_kind": "custom_text", "text": text,
                                       "text_sha256": text_sha256, "created_at": utc_now()})
            record["prewarm_id"] = text_id
            record["updated_at"] = utc_now()
            self.state["revision"] += 1
            self.state["updated_at"] = utc_now()
            atomic_json(self.state_path, self.state)
        return {**self.public_state(), "created_prewarm_id": text_id}

    def import_custom_anchor(self, payload: object) -> dict:
        required = {"role_id", "revision", "name", "language", "transcript", "filename", "data_base64"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise ValueError("Ankerimport enthält unbekannte oder fehlende Felder")
        role = self.role_by_id.get(payload["role_id"])
        if not role:
            raise ValueError("Unbekannte Rolle")
        self.require_render_role(role["role_id"])
        if type(payload["revision"]) is not int or payload["revision"] != self.state["revision"]:
            raise RuntimeError("Zustand wurde zwischenzeitlich geändert; Seite neu laden")
        filename = Path(str(payload["filename"])).name
        suffix = Path(filename).suffix.lower()
        if suffix not in {".wav", ".flac", ".ogg"}:
            raise ValueError("Unterstützte Formate sind WAV, FLAC und OGG")
        try:
            raw = base64.b64decode(payload["data_base64"], validate=True)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ValueError("Audiodatei ist nicht gültig kodiert") from exc
        if not raw or len(raw) > 16 * 1024 * 1024:
            raise ValueError("Audiodatei ist leer oder größer als 16 MiB")
        staging = self.data_root / "import-staging"
        staging.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="custom-anchor-", suffix=suffix, dir=staging)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            ffmpeg = self.dubbing_root / "bin/ffmpeg"
            ffprobe = self.dubbing_root / "bin/ffprobe"
            imported = import_custom_anchor(
                self.data_root, role["role_id"], Path(temporary), str(payload["name"]),
                str(payload["language"]), str(payload["transcript"]),
                ffmpeg=ffmpeg if ffmpeg.is_file() else (shutil.which("ffmpeg") or "ffmpeg"),
                ffprobe=ffprobe if ffprobe.is_file() else (shutil.which("ffprobe") or "ffprobe"),
            )
        except AnchorImportError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        destination = Path(imported["directory"]).resolve()
        audio = Path(imported["audio_path"]).resolve()
        if not destination.is_relative_to(self.data_root) or not audio.is_relative_to(destination):
            raise ValueError("Importziel liegt außerhalb des Wizard-Datenbereichs")
        record = {
            "import_id": imported["import_id"], "name": imported["name"],
            "language": imported["language"], "transcript": imported["transcript"],
            "original_filename": filename, "duration_ms": round(imported["duration_seconds"] * 1000),
            "directory": str(destination.relative_to(self.dubbing_root)).replace(os.sep, "/"),
            "audio": {"root_id": "dubbing_project",
                      "path": str(audio.relative_to(self.dubbing_root)).replace(os.sep, "/"),
                      "sha256": imported["sha256"], "media_type": "audio/wav",
                      "sample_rate_hz": 48000, "channels": 1},
            "imported_at": utc_now(),
        }
        self.state.setdefault("custom_anchors", {}).setdefault(role["role_id"], {})[record["import_id"]] = record
        self.state["revision"] += 1
        self.state["updated_at"] = utc_now()
        atomic_json(self.state_path, self.state)
        return self.public_state()

    def delete_custom_anchor(self, payload: object) -> dict:
        required = {"role_id", "revision", "import_id"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise ValueError("Löschanfrage enthält unbekannte oder fehlende Felder")
        role_id, import_id = payload["role_id"], payload["import_id"]
        if role_id not in self.role_by_id or type(import_id) is not str:
            raise ValueError("Unbekannter benutzerdefinierter Anker")
        if type(payload["revision"]) is not int or payload["revision"] != self.state["revision"]:
            raise RuntimeError("Zustand wurde zwischenzeitlich geändert; Seite neu laden")
        record = self.state.get("custom_anchors", {}).get(role_id, {}).get(import_id)
        if not record:
            raise ValueError("Unbekannter benutzerdefinierter Anker")
        selected = self.state.get("roles", {}).get(role_id, {}).get("anchors", [])
        if any("custom:" + import_id in anchor.get("component_speech_ids", []) for anchor in selected):
            raise ValueError("Aktiven Stimmanker zuerst durch einen anderen Anker ersetzen")
        directory = (self.dubbing_root / record["directory"]).resolve()
        custom_root = (self.data_root / "custom-anchors").resolve()
        if not directory.is_relative_to(custom_root) or directory.is_symlink():
            raise ValueError("Unsicheres Importverzeichnis")
        shutil.rmtree(directory)
        del self.state["custom_anchors"][role_id][import_id]
        self.state["revision"] += 1
        self.state["updated_at"] = utc_now()
        atomic_json(self.state_path, self.state)
        return self.public_state()

    def expand_candidates(self, payload: object) -> dict:
        required = {"role_id", "revision", "kind", "shown_ids", "count"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise ValueError("Kandidatenerweiterung enthält unbekannte oder fehlende Felder")
        role = self.role_by_id.get(payload["role_id"])
        kind = payload["kind"]
        if not role or kind not in {"anchor", "prewarm"}:
            raise ValueError("Unbekannte Rolle oder Kandidatenart")
        if type(payload["revision"]) is not int or payload["revision"] != self.state["revision"]:
            raise RuntimeError("Zustand wurde zwischenzeitlich geändert; Seite neu laden")
        if payload["count"] != 3:
            raise ValueError("Es können genau drei weitere Kandidaten geladen werden")
        visible = self.visible_candidates(role, kind)
        visible_ids = [item["speech_id"] for item in visible]
        if payload["shown_ids"] != visible_ids:
            raise RuntimeError("Sichtbare Kandidaten wurden zwischenzeitlich geändert; Seite neu laden")
        if kind == "anchor":
            pool = sorted(role["_eligible_candidates"],
                          key=lambda item: (-item["duration_ms"], int(item["speech_id"])))
        else:
            pool = self._prewarm_pool(role)
        additions = [item["speech_id"] for item in pool if item["speech_id"] not in set(visible_ids)][:3]
        if not additions:
            raise ValueError("Keine weiteren Kandidaten verfügbar")
        view = self.state.setdefault("candidate_views", {}).setdefault(role["role_id"], {})
        view.setdefault(kind, []).extend(additions)
        self.state["revision"] += 1
        self.state["updated_at"] = utc_now()
        atomic_json(self.state_path, self.state)
        return self.public_state()

    def validate_anchor(self, role: dict, value: object, *, backup: bool = False) -> dict:
        if not isinstance(value, dict) or set(value) != {"kind", "component_speech_ids"}:
            raise ValueError("Anker braucht kind und component_speech_ids")
        kind, ids = value["kind"], value["component_speech_ids"]
        if kind not in {"single", "composite"} or not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
            raise ValueError("Ungültiger Anker")
        if len(ids) != len(set(ids)):
            raise ValueError("Kompositkomponenten müssen verschieden sein")
        if kind == "single" and len(ids) != 1:
            raise ValueError("Einzelanker braucht genau eine Komponente")
        if kind == "composite" and not 2 <= len(ids) <= MAX_COMPOSITE_COMPONENTS:
            raise ValueError("Kompositanker braucht zwei bis acht Komponenten")
        if backup and kind != "single":
            raise ValueError("Backups sind in Wizard v1 Einzelanker")
        cmap = self._candidate_map(role)
        shown = {c["speech_id"] for c in self.visible_candidates(role, "anchor")}
        if any(sid not in shown for sid in ids):
            raise ValueError("Anker enthält eine fremde oder ungeprüfte ID")
        duration = anchor_duration([cmap[sid] for sid in ids])
        selected_candidates = [cmap[sid] for sid in ids]
        all_available_duration = anchor_duration(role["_eligible_candidates"]) if role["_eligible_candidates"] else 0
        small_role_fallback = (all_available_duration < TARGET_MIN_MS
                               and not any(item.get("source_kind") == "custom" for item in selected_candidates))
        if not small_role_fallback and not TARGET_MIN_MS <= duration <= TARGET_MAX_MS:
            raise ValueError("Hauptanker muss einschließlich 300-ms-Abständen 4–15 s lang sein")
        if small_role_fallback and not backup and set(ids) != {item["speech_id"] for item in role["_eligible_candidates"]}:
            raise ValueError("Kleinrollenanker muss alle sicheren eigenen Aufnahmen verwenden")
        if duration > TARGET_MAX_MS:
            raise ValueError("Stimmanker darf 15 s nicht überschreiten")
        return {"kind": kind, "component_speech_ids": ids, "duration_ms": duration,
                "join_silence_ms": JOIN_MS if kind == "composite" else 0,
                "small_role_fallback": small_role_fallback and not backup}

    def save_selection(self, payload: object) -> dict:
        required = {"role_id", "revision", "anchors", "prewarm_id"}
        allowed = required | {"prewarm_for_long_text", "use_voice_anchor_for_long_text"}
        if not isinstance(payload, dict) or not required.issubset(payload) or not set(payload).issubset(allowed):
            raise ValueError("Auswahl enthält unbekannte oder fehlende Felder")
        role = self.role_by_id.get(payload["role_id"])
        if not role:
            raise ValueError("Unbekannte Rolle")
        self.require_render_role(role["role_id"])
        if type(payload["revision"]) is not int or payload["revision"] != self.state["revision"]:
            raise RuntimeError("Zustand wurde zwischenzeitlich geändert; Seite neu laden")
        anchors = payload["anchors"]
        rules = self.role_rules(role["role_id"])
        if not isinstance(anchors, list) or not (0 if rules["without_anchor"] else 1) <= len(anchors) <= 3:
            raise ValueError("Es müssen ein bis drei Haupt-/Backupanker gewählt werden")
        if not anchors and rules["without_anchor"]:
            clean = self.state["roles"].get(role["role_id"], {}).get("anchors", [])
        else:
            clean = [self.validate_anchor(role, item, backup=index > 0) for index, item in enumerate(anchors)]
        flattened = [sid for anchor in clean for sid in anchor["component_speech_ids"]]
        if len(flattened) != len(set(flattened)):
            raise ValueError("Eine Spiel-ID darf nicht zugleich Haupt- und Backupanker sein")
        prewarm_id = payload["prewarm_id"]
        if prewarm_id is None and rules["without_prewarm"]:
            prewarm_id = self.state["roles"].get(role["role_id"], {}).get("prewarm_id")
        if prewarm_id is not None and (not isinstance(prewarm_id, str) or prewarm_id not in self._candidate_map(role)):
            raise ValueError("Eine der sichtbaren Prewarm-IDs muss gewählt werden")
        suggestions = {x["speech_id"] for x in self.visible_candidates(role, "prewarm")}
        if prewarm_id is not None and prewarm_id not in suggestions:
            raise ValueError("Prewarm-ID gehört nicht zu den aktuell sichtbaren Vorschlägen")
        prewarm_for_long_text = payload.get("prewarm_for_long_text", False)
        if type(prewarm_for_long_text) is not bool:
            raise ValueError("Prewarm-Regel für lange Texte muss boolesch sein")
        use_voice_anchor_for_long_text = payload.get("use_voice_anchor_for_long_text", False)
        if type(use_voice_anchor_for_long_text) is not bool:
            raise ValueError("Stimmanker-Regel für lange Texte muss boolesch sein")
        record = {"anchors": clean, "prewarm_id": prewarm_id,
                  "prewarm_for_long_text": prewarm_for_long_text,
                  "use_voice_anchor_for_long_text": use_voice_anchor_for_long_text,
                  "selected_at": utc_now(), "updated_at": utc_now()}
        self.state["roles"][role["role_id"]] = record
        self.state["revision"] += 1
        self.state["updated_at"] = utc_now()
        atomic_json(self.state_path, self.state)
        return self.public_state()

    def fingerprint_source_sha256(self, role_id: str) -> str:
        return self.state.get("role_fingerprint_source_sha256", {}).get(role_id, self.source_sha256)

    def anchor_fingerprint(self, role_id: str, record: dict) -> str:
        value = {"format": FORMAT, "source_manifest_sha256": self.fingerprint_source_sha256(role_id),
                 "role_id": role_id, "main_anchor": record["anchors"][0], "seed": SEED}
        return digest_bytes(canonical(value))

    def prewarm_fingerprint(self, role_id: str, record: dict) -> str:
        role = self.role_by_id.get(role_id)
        candidate = self._candidate_map(role).get(record.get("prewarm_id")) if role else None
        text = self.prewarm_text(candidate) if candidate else ""
        value = {"anchor_fingerprint": self.anchor_fingerprint(role_id, record),
                 "prewarm_id": record["prewarm_id"],
                 "prewarm_for_long_text": bool(record.get("prewarm_for_long_text", False)),
                 "seed": SEED}
        dialogue = self.dialogue_by_speech.get(candidate.get("speech_id")) if candidate else None
        source_id = candidate.get("speech_id") if candidate else None
        source_text = self.state.get("text_prewizard", {}).get("ids", {}).get(source_id, {}).get("original_text") if dialogue else None
        if dialogue and text != source_text:
            value["prewarm_text_sha256"] = digest_bytes(text.encode("utf-8"))
        return digest_bytes(canonical(value))

    def prewarm_text(self, candidate: dict) -> str:
        speech_id = candidate.get("speech_id")
        if speech_id in self.dialogue_by_speech:
            return self.effective_text(speech_id)["tts_text"]
        return candidate.get("german_text", "")

    def role_slug(self, role_id: str) -> str:
        return digest_bytes(role_id.encode("utf-8"))[:16]

    def artifact_paths(self, role_id: str, record: dict, kind: str) -> tuple[Path, Path]:
        fingerprint = self.anchor_fingerprint(role_id, record) if kind == "anchor" else self.prewarm_fingerprint(role_id, record)
        folder = self.data_root / "generated" / self.role_slug(role_id) / (kind + "-" + fingerprint)
        return folder / ("preview.wav" if kind == "anchor" else "prewarm.wav"), folder / "report.json"

    def artifact_status(self, role_id: str, record: dict, kind: str) -> dict:
        audio, report_path = self.artifact_paths(role_id, record, kind)
        expected = self.anchor_fingerprint(role_id, record) if kind == "anchor" else self.prewarm_fingerprint(role_id, record)
        result = {"ready": False, "playable": False, "selection_fingerprint": expected, "fingerprint": None, "duration_ms": None, "error": None}
        if kind == "prewarm" and record.get("prewarm_id") is None:
            result["error"] = "Zuerst einen Prewarm wählen und speichern"
            return result
        if not report_path.is_file():
            return result
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("status") != "ok" or report.get("selection_fingerprint") != expected or not report.get("fingerprint") or not audio.is_file():
                result["error"] = report.get("error") or "Report/WAV ist unvollständig oder veraltet"
                return result
            if digest_file(audio) != report.get("output_sha256"):
                result["error"] = "WAV-Hash stimmt nicht mit dem Report überein"
                return result
            result.update(ready=True, playable=True, fingerprint=report["fingerprint"], duration_ms=report.get("duration_ms"), sha256=report["output_sha256"],
                          target_speech_id=report.get("target_speech_id"), target_text_sha256=report.get("target_text_sha256"),
                          rendered_text=report.get("target_text"),
                          relative_path=str(audio.relative_to(self.dubbing_root)).replace(os.sep, "/"))
            if kind == "prewarm" and not PREWARM_MIN_MS <= int(report.get("duration_ms", 0)) <= PREWARM_MAX_MS:
                result.update(ready=False, error="Prewarm-Ausgabe liegt außerhalb 3–4 s")
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            result["error"] = str(exc)
        return result

    def reference_status(self, role_id: str, record: dict) -> dict:
        anchor_audio, _ = self.artifact_paths(role_id, record, "anchor")
        audio, report_path = anchor_audio.parent / "reference.wav", anchor_audio.parent / "reference.json"
        expected = self.anchor_fingerprint(role_id, record)
        result = {"ready": False, "selection_fingerprint": expected, "fingerprint": None, "duration_ms": None, "error": None}
        if not report_path.is_file():
            return result
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("selection_fingerprint") != expected or not report.get("fingerprint") or not audio.is_file():
                result["error"] = "Materialisierter Anker ist unvollständig oder veraltet"
                return result
            if digest_file(audio) != report.get("output_sha256"):
                result["error"] = "Materialisierter Anker-Hash stimmt nicht"
                return result
            result.update(ready=True, fingerprint=report["fingerprint"], duration_ms=report.get("duration_ms"), sha256=report["output_sha256"],
                          relative_path=str(audio.relative_to(self.dubbing_root)).replace(os.sep, "/"))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            result["error"] = str(exc)
        return result

    def role_status(self, role: dict) -> dict:
        members = self.role_members(role["role_id"])
        pending = [str(d.get("native_speech_id") or "") for d in members
                   if self.effective_processing(d) == "pending"]
        missing_german = [str(d.get("native_speech_id") or "") for d in members
                          if self.effective_processing(d) == "render" and not german_text(d)]
        if not self.active_role_dialogues(role):
            reasons = (["dialogue_decisions_pending"] if pending else [])
            return {"status": "incomplete" if reasons else "ready", "reasons": reasons,
                    "processing": "copy_original_only", "pending_speech_ids": pending,
                    "missing_german_speech_ids": missing_german}
        rules = self.role_rules(role["role_id"])
        record = self.state["roles"].get(role["role_id"])
        record = record or {}
        members = self.active_role_dialogues(role)
        uses = {str(d["native_speech_id"]): self.effective_dialogue_render_flags(d, role["role_id"]) for d in members}
        needs_anchor = any(value[0] for value in uses.values())
        needs_prewarm = any(value[1] for value in uses.values())
        needs_anchor_prewarm = any(anchor and warm for anchor, warm in uses.values())
        reasons = (["dialogue_decisions_pending"] if pending else [])
        if missing_german:
            reasons.append("german_text_missing")
        anchor = reference = prewarm = None
        if needs_anchor:
            if not record.get("anchors"):
                reasons.append("selection_missing")
            else:
                anchor = self.artifact_status(role["role_id"], record, "anchor")
                reference = self.reference_status(role["role_id"], record)
                if not reference["ready"]:
                    reasons.append("anchor_reference_missing")
        if needs_prewarm:
            candidate = self._candidate_map(role).get(record.get("prewarm_id"))
            if not candidate or not self.prewarm_text(candidate):
                reasons.append("prewarm_selection_missing")
            if needs_anchor_prewarm and record.get("anchors"):
                prewarm = self.artifact_status(role["role_id"], record, "prewarm")
                if not prewarm["ready"]:
                    reasons.append("prewarm_test_missing_or_not_3_4s")
        if not needs_anchor and not needs_prewarm:
            return {"status": "ready" if not reasons else "incomplete", "reasons": reasons,
                    "pending_speech_ids": pending, "missing_german_speech_ids": missing_german,
                    "anchor_reference": None, "anchor_artifact": None, "prewarm_artifact": None,
                    "without_anchor": True, "without_prewarm": True}
        if not record.get("anchors") and needs_anchor:
            return {"status": "incomplete", "reasons": reasons,
                    "pending_speech_ids": pending, "missing_german_speech_ids": missing_german,
                    "anchor_reference": reference, "anchor_artifact": anchor, "prewarm_artifact": prewarm}
        if needs_prewarm and record.get("prewarm_id") is None and "prewarm_selection_missing" not in reasons:
            reasons.append("prewarm_selection_missing")
        cmap = self._candidate_map(role)
        if anchor:
            components = [cmap[sid] for sid in record["anchors"][0]["component_speech_ids"]]
            targets = [(anchor, self._anchor_preview_target(role, components))]
        else:
            targets = []
        if prewarm and record.get("prewarm_id") is not None:
            targets.append((prewarm, cmap[record["prewarm_id"]]))
        for artifact, target in targets:
            source_id = artifact.get("target_speech_id") or target["speech_id"]
            source = cmap.get(source_id)
            text = artifact.get("rendered_text")
            if text is None:
                text = source["german_text"] if source else target["german_text"]
            expected_hash = artifact.get("target_text_sha256")
            if expected_hash and digest_bytes(text.encode("utf-8")) != expected_hash:
                artifact.update(ready=False, playable=False, error="Text der vorhandenen Probe passt nicht mehr zur Quellfassung", text=None)
            else:
                artifact.update(source_speech_id=source_id, text=text)
        return {"status": "ready" if not reasons else "incomplete", "reasons": reasons,
                "pending_speech_ids": pending, "missing_german_speech_ids": missing_german,
                "anchor_reference": reference, "anchor_artifact": anchor, "prewarm_artifact": prewarm,
                "without_anchor": not needs_anchor, "without_prewarm": not needs_prewarm,
                "prewarm_generation": "per_dialogue_original" if needs_prewarm and not needs_anchor_prewarm else None}

    def public_state(self) -> dict:
        roles = []
        for role in self.roles:
            record = self.state["roles"].get(role["role_id"])
            copy = {key: value for key, value in role.items() if not key.startswith("_")}
            copy["selection"] = record
            copy["rules"] = self.role_rules(role["role_id"])
            copy["candidates"] = self.visible_candidates(role, "anchor")
            copy["prewarm_suggestions"] = self.visible_candidates(role, "prewarm")
            copy["automatic_composite"] = automatic_composite(copy["candidates"])
            for kind, key in (("anchor", "candidate_selection"), ("prewarm", "prewarm_candidate_selection")):
                shown = copy["candidates"] if kind == "anchor" else copy["prewarm_suggestions"]
                meta = dict(copy.get(key, {}))
                eligible_count = (len(role["_eligible_candidates"]) + len(self._custom_candidates(role))
                                  if kind == "anchor" else len(self._prewarm_pool(role)))
                meta.update(shown_count=len(shown), eligible_count=eligible_count,
                            has_more=len(shown) < eligible_count)
                copy[key] = meta
            for kind, candidates in (("anchor", copy["candidates"]), ("prewarm", copy["prewarm_suggestions"])):
                for candidate in candidates:
                    if candidate.get("source_kind") == "custom":
                        candidate["preview_" + kind] = {"ready": False, "fingerprint": None,
                            "error": "Deutsche Kontextprobe nach direkter Auswahl rendern"}
                        candidate["audio_url"] = ("/audio/custom-anchor?role_id=" + quote(role["role_id"])
                            + "&import_id=" + quote(candidate["import_id"])
                            + "&audio_sha256=" + candidate["audio"]["sha256"])
                    elif candidate.get("source_kind") in {"neutral_standard", "custom_text"}:
                        candidate["preview_" + kind] = self.candidate_preview_status(role["role_id"], candidate["speech_id"], kind)
                        candidate["audio_url"] = None
                    else:
                        candidate["preview_" + kind] = self.candidate_preview_status(role["role_id"], candidate["speech_id"], kind)
                        candidate["audio_url"] = "/audio/original/" + candidate["speech_id"]
            copy["safe_prewarm_source_count"] = len(role["_eligible_candidates"])
            copy["render_dialog_count"] = len(self.active_role_dialogues(role))
            copy["processing"] = self.role_processing(role["role_id"])
            copy["role_dialog_count"] = len(self.role_members(role["role_id"]))
            decisions = [self.state.get("dialogue_actions", {}).get(str(d["native_speech_id"]), {})
                         for d in self.dialogues if role_id_for(d) == role["role_id"]]
            copy["work_queue_counts"] = {action: sum(d.get("action") == action for d in decisions)
                                         for action in ("copy_original", "assign_existing", "technical_role", "exclude")}
            copy["progress"] = self.role_status(role)
            roles.append(copy)
        counts = {name: sum(r["progress"]["status"] == name for r in roles) for name in ("ready", "incomplete", "blocked")}
        processing_counts = {name: sum(self.dialogue_processing(str(d.get("native_speech_id") or "")) == name
                                       for d in self.dialogues)
                             for name in ("render", "copy_original", "exclude", "pending")}
        return {"format": FORMAT, "source_manifest_sha256": self.source_sha256,
                "revision": self.state["revision"], "updated_at": self.state["updated_at"],
                "total_dialog_count": len(self.dialogues), "processing_counts": processing_counts,
                "render_dialog_count": processing_counts["render"], "roles": roles, "counts": counts,
                "join_silence_ms": JOIN_MS, "composite_target_ms": [TARGET_MIN_MS, TARGET_MAX_MS],
                "prewarm_target_ms": [PREWARM_MIN_MS, PREWARM_MAX_MS],
                "render_capability": render_capability(self.dubbing_root),
                "assignment_targets": [{"role_id": r["role_id"], "display_name": r["display_name"]}
                                       for r in roles if not r["work_queue"]],
                "m1_command": "./work/python/cpython-3.12.14-macos-aarch64-none/bin/python3.12 voice_anchor_wizard/render_tests_m1.py"}

    def candidate_preview_fingerprint(self, role_id: str, speech_id: str, kind: str) -> str:
        self.require_render_role(role_id)
        role = self.role_by_id.get(role_id)
        if not role or kind not in {"anchor", "prewarm"}:
            raise ValueError("Unbekannte Rolle oder Kandidatenart")
        candidate = self._candidate_map(role).get(speech_id)
        if not candidate:
            raise ValueError("Unbekannter Kandidat")
        if kind == "anchor" and candidate.get("source_kind") in {"neutral_standard", "custom_text"}:
            raise ValueError("Text-Prewarms sind keine Stimmanker")
        value = {"format": "mi1-candidate-preview/2", "source_manifest_sha256": self.fingerprint_source_sha256(role_id),
                 "role_id": role_id, "kind": kind, "candidate": candidate, "seed": SEED}
        if kind == "prewarm":
            if self.role_rules(role_id)["without_anchor"]:
                raise ValueError("Ohne Stimmanker wird der Prewarm erst in der Einzelprobe mit dem jeweiligen Original erzeugt")
            record = self.state["roles"].get(role_id)
            if not record or not record.get("anchors"):
                raise ValueError("Prewarm-Probe braucht zuerst einen gewählten Stimmanker")
            value["anchor_fingerprint"] = self.anchor_fingerprint(role_id, record)
        return digest_bytes(canonical(value))

    def candidate_preview_paths(self, role_id: str, speech_id: str, kind: str) -> tuple[Path, Path]:
        fingerprint = self.candidate_preview_fingerprint(role_id, speech_id, kind)
        folder = self.data_root / "candidate-previews" / self.role_slug(role_id) / f"{kind}-{fingerprint}"
        return folder / "preview.wav", folder / "report.json"

    def candidate_preview_status(self, role_id: str, speech_id: str, kind: str) -> dict:
        try:
            expected = self.candidate_preview_fingerprint(role_id, speech_id, kind)
        except ValueError as exc:
            return {"ready": False, "fingerprint": None, "sha256": None,
                    "render_fingerprint": None, "duration_ms": None,
                    "audio_url": None, "error": str(exc)}
        audio, report_path = self.candidate_preview_paths(role_id, speech_id, kind)
        result = {"ready": False, "fingerprint": expected, "sha256": None,
                  "render_fingerprint": None, "duration_ms": None, "audio_url": None, "error": None}
        if not report_path.is_file():
            return result
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if (report.get("status") != "ok" or report.get("selection_fingerprint") != expected
                    or not report.get("fingerprint") or not audio.is_file()):
                result["error"] = report.get("error") or "Kandidatenprobe ist unvollständig oder veraltet"
                return result
            actual = digest_file(audio)
            if actual != report.get("output_sha256"):
                result["error"] = "WAV-Hash stimmt nicht mit dem Kandidatenreport überein"
                return result
            query = (f"role_id={quote(role_id)}&speech_id={quote(speech_id)}&kind={kind}"
                     f"&fingerprint={expected}&audio_sha256={actual}")
            result.update(ready=True, sha256=actual, render_fingerprint=report["fingerprint"],
                          duration_ms=report.get("duration_ms"),
                          audio_url="/audio/candidate-preview?" + query)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            result["error"] = str(exc)
        return result

    def candidate_preview_plan(self, role_id: str, speech_id: str, kind: str) -> dict:
        self.require_render_role(role_id)
        role = self.role_by_id.get(role_id)
        if not role or kind not in {"anchor", "prewarm"}:
            raise ValueError("Unbekannte Rolle oder Kandidatenart")
        visible_ids = {item["speech_id"] for item in self.visible_candidates(role, kind)}
        candidate = self._candidate_map(role).get(speech_id)
        if not candidate or speech_id not in visible_ids:
            raise ValueError("Nur ein aktuell sichtbarer Kandidat kann gerendert werden")
        audio, report = self.candidate_preview_paths(role_id, speech_id, kind)
        result = {"format": "mi1-candidate-preview-plan/1", "role_id": role_id,
                "display_name": role["display_name"], "kind": kind, "seed": SEED,
                "candidate": candidate, "fingerprint": self.candidate_preview_fingerprint(role_id, speech_id, kind),
                "output": str(audio.relative_to(self.dubbing_root)).replace(os.sep, "/"),
                "report": str(report.relative_to(self.dubbing_root)).replace(os.sep, "/")}
        if kind == "prewarm":
            record = self.state["roles"][role_id]
            main = record["anchors"][0]
            result.update(main_anchor=main,
                          components=[self._candidate_map(role)[sid] for sid in main["component_speech_ids"]],
                          join_silence_ms=main["join_silence_ms"])
        return result

    def _anchor_preview_target(self, role: dict, components: list[dict]) -> dict:
        if role["_eligible_candidates"]:
            return role["_eligible_candidates"][0]
        neutral = self._neutral_prewarm_candidates(role)
        return neutral[0] if neutral else components[0]

    def render_plan(self, role_id: str) -> dict:
        self.require_render_role(role_id)
        role = self.role_by_id[role_id]
        rules = self.role_rules(role_id)
        if rules["without_anchor"]:
            raise ValueError("Ohne festen Stimmanker erfolgt die Generierung pro Dialog-ID über die Einzelprobe")
        record = self.state["roles"].get(role_id)
        if not record or not record.get("anchors"):
            raise ValueError("Rolle hat keinen gespeicherten Stimmanker")
        if record.get("prewarm_id") is None and not rules["without_prewarm"]:
            raise ValueError("Zuerst einen Prewarm wählen und speichern")
        cmap = self._candidate_map(role)
        main = record["anchors"][0]
        components = [cmap[sid] for sid in main["component_speech_ids"]]
        preview_source = self._anchor_preview_target(role, components)
        prewarm = cmap[record["prewarm_id"]] if not rules["without_prewarm"] else None
        anchor_audio, anchor_report = self.artifact_paths(role_id, record, "anchor")
        prewarm_audio, prewarm_report = self.artifact_paths(role_id, record, "prewarm") if prewarm else (None, None)
        result = {
            "format": "mi1-voice-anchor-test-plan/1", "role_id": role_id, "display_name": role["display_name"],
            "seed": SEED, "join_silence_ms": JOIN_MS, "main_anchor": main,
            "prewarm_for_long_text": bool(record.get("prewarm_for_long_text", False)),
            "use_voice_anchor_for_long_text": bool(record.get("use_voice_anchor_for_long_text", False)),
            "components": components, "preview_target": preview_source, "prewarm_target": prewarm,
            "anchor": {"fingerprint": self.anchor_fingerprint(role_id, record),
                       "output": str(anchor_audio.relative_to(self.dubbing_root)).replace(os.sep, "/"),
                       "report": str(anchor_report.relative_to(self.dubbing_root)).replace(os.sep, "/")},
            "prewarm": {"fingerprint": self.prewarm_fingerprint(role_id, record),
                        "output": str(prewarm_audio.relative_to(self.dubbing_root)).replace(os.sep, "/"),
                        "report": str(prewarm_report.relative_to(self.dubbing_root)).replace(os.sep, "/")} if prewarm else None,
        }
        if any(rules.values()):
            result.update(**rules, role_rules=rules, use_prewarm=not rules["without_prewarm"])
        return result

    def dialogue_cases(self, role_id: str, query: str = "", limit: int | None = None) -> list[dict]:
        role = self.role_by_id.get(role_id)
        if not role:
            raise ValueError("Unbekannte Rolle")
        needle = query.strip().casefold()
        result = []
        for dialogue in self.dialogues:
            if role_id not in {role_id_for(dialogue), self.effective_role_id(dialogue)}:
                continue
            speech_id = str(dialogue.get("native_speech_id") or "")
            english, english_status = english_text(dialogue)
            german = self.effective_text(speech_id)["tts_text"]
            text_decision = (dialogue.get("unclear_resolution") or {}).get("text_decision") or {}
            proposed_german = text_decision.get("display_text_from_review") or text_decision.get("audition_tts_text") or ""
            haystack = " ".join((speech_id, dialogue.get("dialog_id", ""), english, german, proposed_german)).casefold()
            if needle and needle not in haystack:
                continue
            duration_ms = int((dialogue.get("original_audio") or {}).get("duration_ms_from_extraction") or 0)
            short = duration_ms < 3000 or len(german.split()) < 4
            record = self.state.get("roles", {}).get(role_id)
            use_voice_anchor, use_prewarm = self.effective_dialogue_render_flags(dialogue, role_id)
            processing = self.dialogue_processing(speech_id)
            decision = self.state.get("dialogue_actions", {}).get(speech_id)
            status = (self.dialogue_test_status(role_id, speech_id)
                      if processing == "render" and self.effective_role_id(dialogue) == role_id and (record or self.role_rules(role_id)["without_anchor"]) and german
                      else {"ready": False})
            result.append({"speech_id": speech_id, "dialog_id": dialogue["dialog_id"],
                           "english_text": english, "english_text_status": english_status,
                           "german_text": german, "proposed_german_text": proposed_german, "duration_ms": duration_ms, "short": short,
                           "original_texts": (dialogue.get("text_versions") or {}).get("original") or {
                               "english_text": english, "german_text": german},
                           "text_versions": (dialogue.get("text_versions") or {}).get("versions", []),
                           "use_prewarm": use_prewarm, "use_voice_anchor": use_voice_anchor,
                           "processing": processing,
                           "decision": decision, "content_type": (decision or {}).get("content_type"), "test": status,
                           "audio_url": "/audio/original/" + speech_id})
            if limit is not None and len(result) >= limit:
                break
        return result

    def dialogue_test_plan(self, role_id: str, speech_id: str, *, text_override: str | None = None,
                           allow_copy_original: bool = False) -> dict:
        self.require_render_role(role_id)
        role = self.role_by_id.get(role_id)
        rules = self.role_rules(role_id)
        record = self.state.get("roles", {}).get(role_id) or {}
        dialogue = self.dialogue_by_speech.get(speech_id)
        if not role or (not record and not rules["without_anchor"]) or not dialogue or self.effective_role_id(dialogue) != role_id:
            raise ValueError("Dialog-ID gehört nicht zu einer vollständig gewählten Rolle")
        processing = self.dialogue_processing(speech_id)
        if processing != "render" and not (allow_copy_original and processing == "copy_original"):
            if processing == "copy_original":
                raise ValueError("Original übernommene Dialog-ID ist nicht für eine deutsche Probe freigegeben")
            raise ValueError("Dialog-ID ist ausgeschlossen oder nicht für eine deutsche Probe freigegeben")
        german = self.effective_text(speech_id)["tts_text"] if text_override is None else text_override
        if not isinstance(german, str) or len(german) > 4000:
            raise ValueError("Deutsche Testfassung muss Text mit höchstens 4000 Zeichen enthalten")
        if not german:
            raise ValueError("Dialog-ID hat keinen deutschen TTS-Text")
        audio = dialogue.get("original_audio") or {}
        path = safe_relative(audio.get("path") or "")
        sha256 = audio.get("sha256_from_extraction")
        if not sha256:
            raise ValueError("Dialog-ID hat kein hashgebundenes Originalaudio")
        english, english_status = english_text(dialogue)
        duration_ms = int(audio.get("duration_ms_from_extraction") or 0)
        short = duration_ms < 3000 or len(german.split()) < 4
        use_voice_anchor, use_prewarm = self.effective_dialogue_render_flags(dialogue, role_id)
        if use_prewarm and record.get("prewarm_id") is None:
            raise ValueError("Zuerst einen Prewarm wählen und speichern")
        method = "standard" if use_voice_anchor else "without_fixed_anchor"
        if use_voice_anchor and not record.get("anchors"):
            raise ValueError("Zuerst einen gültigen Stimmanker speichern")
        reference = self.reference_status(role_id, record) if use_voice_anchor else None
        per_line = use_prewarm and not use_voice_anchor
        prewarm = self.artifact_status(role_id, record, "prewarm") if use_prewarm and not per_line else None
        if use_voice_anchor and not reference["ready"]:
            raise ValueError("Materialisierter Stimmanker fehlt")
        if use_prewarm and not per_line and not prewarm["ready"]:
            raise ValueError("Ausgewählter Prewarm fehlt")
        cmap = self._candidate_map(role)
        components = ([cmap[sid] for sid in record["anchors"][0]["component_speech_ids"]]
                      if use_voice_anchor else [])
        target = {"speech_id": speech_id, "dialog_id": dialogue["dialog_id"],
                  "english_text": english, "english_text_status": english_status,
                  "german_text": german, "duration_ms": duration_ms,
                  "audio": {"root_id": audio.get("root_id", "dubbing_project"), "path": path,
                            "sha256": sha256, "media_type": audio.get("media_type", "audio/ogg")}}
        prewarm_candidate = cmap.get(record.get("prewarm_id"))
        per_line_prewarm = None
        if per_line:
            if not prewarm_candidate or not prewarm_candidate.get("german_text"):
                raise ValueError("Zuerst einen Prewarm-Text wählen und speichern")
            prewarm_text = self.prewarm_text(prewarm_candidate)
            definition = {"format": "mi1-line-original-prewarm/1", "role_id": role_id,
                          "source_speech_id": prewarm_candidate["speech_id"], "target_speech_id": speech_id,
                          "text": prewarm_text,
                          "text_sha256": digest_bytes(prewarm_text.encode("utf-8")),
                          "reference_mode": "line_original", "reference_audio": target["audio"],
                          "reference_text": english, "seed": SEED,
                          "target_duration_ms": [PREWARM_MIN_MS, PREWARM_MAX_MS]}
            line_fingerprint = digest_bytes(canonical(definition))
            line_folder = self.data_root / "generated" / self.role_slug(role_id) / ("line-prewarm-" + line_fingerprint)
            per_line_prewarm = {**definition, "fingerprint": line_fingerprint,
                                "output": str((line_folder / "prewarm.wav").relative_to(self.dubbing_root)).replace(os.sep, "/"),
                                "report": str((line_folder / "report.json").relative_to(self.dubbing_root)).replace(os.sep, "/")}
        identity = {"format": "mi1-dialogue-selection-test/1", "source_manifest_sha256": self.fingerprint_source_sha256(role_id),
                    "role_id": role_id, "target": target, "method": method,
                    "anchor_fingerprint": self.anchor_fingerprint(role_id, record) if use_voice_anchor else None,
                    "anchor_audio_sha256": reference["sha256"] if reference else None,
                    "short": short, "use_voice_anchor": use_voice_anchor, "use_prewarm": use_prewarm,
                    "prewarm_fingerprint": (per_line_prewarm["fingerprint"] if per_line else self.prewarm_fingerprint(role_id, record)) if use_prewarm else None,
                    "prewarm_audio_sha256": prewarm["sha256"] if prewarm else None, "seed": SEED}
        if any(rules.values()):
            identity.update(role_rules=rules, **rules)
        if per_line_prewarm:
            identity["per_line_prewarm"] = per_line_prewarm
        fingerprint = digest_bytes(canonical(identity))
        folder = self.data_root / "selection-tests" / self.role_slug(role_id) / fingerprint
        return {**identity, "fingerprint": fingerprint, "seed": SEED, "target": target,
                "performance_prompt_missing": not english or english_status not in {"unique", "inferred_reviewed"},
                "components": components, "anchor_prompt_text": " ".join(item["english_text"] for item in components) if components else None,
                "reference_path": reference["relative_path"] if reference else None,
                "prewarm_path": per_line_prewarm["output"] if per_line else prewarm["relative_path"] if prewarm else None,
                "prewarm_text": prewarm_candidate["german_text"] if use_prewarm else None,
                "output": str((folder / "preview.wav").relative_to(self.dubbing_root)).replace(os.sep, "/"),
                "report": str((folder / "report.json").relative_to(self.dubbing_root)).replace(os.sep, "/")}

    def dialogue_prewarm_status(self, plan: dict) -> dict:
        descriptor = plan.get("per_line_prewarm")
        if not descriptor:
            return {"ready": False, "playable": False, "error": None, "duration_ms": None, "audio_url": None}
        result = {"ready": False, "playable": False, "fingerprint": descriptor["fingerprint"],
                  "error": None, "duration_ms": None, "audio_url": None}
        audio = self.dubbing_root / descriptor["output"]
        report_path = self.dubbing_root / descriptor["report"]
        if not report_path.is_file():
            return result
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if (report.get("selection_fingerprint") != descriptor["fingerprint"] or not audio.is_file()
                    or digest_file(audio) != report.get("output_sha256")):
                result["error"] = "Dialogbezogener Prewarm ist unvollständig, verändert oder veraltet"
                return result
            duration = report.get("duration_ms")
            ready = report.get("status") == "ok" and type(duration) is int and PREWARM_MIN_MS <= duration <= PREWARM_MAX_MS
            query = urlencode({"role_id": plan["role_id"], "speech_id": plan["target"]["speech_id"],
                               "fingerprint": descriptor["fingerprint"], "audio_sha256": report["output_sha256"]})
            result.update(ready=ready, playable=True, duration_ms=duration, sha256=report["output_sha256"],
                          error=None if ready else report.get("error") or "Prewarm-Ausgabe liegt außerhalb 3–4 s",
                          audio_url="/audio/dialogue-prewarm?" + query)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            result["error"] = str(exc)
        return result

    def dialogue_test_status(self, role_id: str, speech_id: str) -> dict:
        try:
            plan = self.dialogue_test_plan(role_id, speech_id)
        except ValueError as exc:
            return {"ready": False, "fingerprint": None, "error": str(exc)}
        audio = self.dubbing_root / plan["output"]
        report_path = self.dubbing_root / plan["report"]
        result = {"ready": False, "fingerprint": plan["fingerprint"], "error": None}
        if plan.get("per_line_prewarm"):
            result["per_line_prewarm"] = self.dialogue_prewarm_status(plan)
        if not report_path.is_file():
            return result
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if (report.get("status") != "ok" or report.get("selection_fingerprint") != plan["fingerprint"]
                    or not audio.is_file() or report.get("output_sha256") != digest_file(audio)):
                result["error"] = report.get("error") or "Auswahltest ist unvollständig oder veraltet"
                return result
            query = urlencode({"role_id": role_id, "speech_id": speech_id,
                               "fingerprint": plan["fingerprint"], "audio_sha256": report["output_sha256"]})
            result.update(ready=True, sha256=report["output_sha256"], duration_ms=report.get("duration_ms"),
                          audio_url="/audio/dialogue-test?" + query)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            result["error"] = str(exc)
        return result

    def _export_anchor(self, role: dict, anchor: dict, selection_evidence: str) -> dict:
        cmap = self._candidate_map(role)
        components = [cmap[sid] for sid in anchor["component_speech_ids"]]
        return {"kind": anchor["kind"], "anchor_dialog_id": components[0]["dialog_id"],
                "anchor_component_dialog_ids": [c["dialog_id"] for c in components[1:]],
                "source_audio": [c["audio"] for c in components],
                "prompt_text": " ".join(c["english_text"] for c in components),
                "prompt_text_sha256": digest_bytes(" ".join(c["english_text"] for c in components).encode("utf-8")),
                "duration_ms": anchor["duration_ms"], "join_silence_ms": anchor["join_silence_ms"],
                "selection_evidence": selection_evidence}

    def _export_overridden_role(self, role: dict, status: dict) -> dict:
        role_id = role["role_id"]
        rules = {"without_anchor": status.get("without_anchor", False),
                 "without_prewarm": status.get("without_prewarm", False)}
        record = self.state["roles"].get(role_id) or {}
        main = selection = prewarm = None
        if not rules["without_anchor"]:
            reference = status["anchor_reference"]
            artifact = status["anchor_artifact"]
            main = self._export_anchor(role, record["anchors"][0], "local_wizard_direct_selection")
            main["materialized_audio"] = {"root_id": "dubbing_project", "path": reference["relative_path"],
                "sha256": reference["sha256"], "media_type": "audio/wav", "duration_ms": reference["duration_ms"],
                "sample_rate_hz": 48000, "channels": 1}
            selection = {"status": "selected", "selected_at": record.get("selected_at", record["updated_at"]),
                         "evidence_ref": "voice_anchor_wizard_data/state.json",
                         "selection_fingerprint": self.anchor_fingerprint(role_id, record)}
            if artifact.get("ready"):
                selection["preview_audio"] = {"root_id": "dubbing_project", "path": artifact["relative_path"],
                    "sha256": artifact["sha256"], "media_type": "audio/wav", "duration_ms": artifact["duration_ms"],
                    "sample_rate_hz": 48000, "channels": 1}
        if not rules["without_prewarm"]:
            candidate = self._candidate_map(role)[record["prewarm_id"]]
            prewarm_text = self.prewarm_text(candidate)
            text_only = candidate.get("source_kind") in {"neutral_standard", "custom_text"}
            source_in_window = not text_only and PREWARM_MIN_MS <= candidate["duration_ms"] <= PREWARM_MAX_MS
            prewarm = {"source": candidate.get("source_kind", "dialog"),
                       **({"prewarm_dialog_id": candidate["dialog_id"]} if candidate.get("dialog_id") else {}),
                       "source_duration_ms": None if text_only else candidate["duration_ms"],
                       "source_duration_window_match": source_in_window,
                       "source_selection": candidate["source_kind"] if text_only else "target_3_4s" if source_in_window else "nearest_duration_fallback",
                       "text": prewarm_text, "text_sha256": digest_bytes(prewarm_text.encode("utf-8")),
                       "selection": {"status": "selected", "selected_at": record.get("selected_at", record["updated_at"]),
                                     "evidence_ref": "voice_anchor_wizard_data/state.json"}}
            if rules["without_anchor"]:
                prewarm["reference_mode"] = "line_original"
            else:
                anchor_reference = status["anchor_reference"]
                artifact = status["prewarm_artifact"]
                if not artifact or not artifact.get("ready"):
                    raise ValueError("Ausgewählter Prewarm fehlt")
                prewarm.update({
                    "audio": {"root_id": "dubbing_project", "path": artifact["relative_path"],
                              "sha256": artifact["sha256"], "media_type": "audio/wav",
                              "duration_ms": artifact["duration_ms"], "sample_rate_hz": 48000, "channels": 1},
                    "technical_check": "pass",
                    "selection": {"status": "selected", "selected_at": record.get("selected_at", record["updated_at"]),
                                  "evidence_ref": "voice_anchor_wizard_data/state.json",
                                  "selection_fingerprint": self.prewarm_fingerprint(role_id, record)},
                    "generated_with_anchor_fingerprint": status["anchor_artifact"]["fingerprint"],
                    "generated_with_anchor_audio_sha256": anchor_reference["sha256"]})
        return {"role_id": role_id, "name": role["display_name"],
                "speaker_evidence": {"speaker_code": role["speaker_code"], "names": role["speaker_names"],
                    "statuses": role["speaker_statuses"], "evidence": role["speaker_evidence"], "actor_ids_observed": role["actor_ids_observed"]},
                **rules, "main_anchor": main, "main_anchor_selection": selection,
                "backup_anchors": [] if rules["without_anchor"] else [self._export_anchor(role, value, "local_wizard_original_audio_review") for value in record["anchors"][1:]],
                "prewarm_for_long_text": bool(record.get("prewarm_for_long_text", False)),
                "use_voice_anchor_for_long_text": bool(record.get("use_voice_anchor_for_long_text", False)), "prewarm": prewarm}

    def _text_prewizard_export(self) -> tuple[dict, list[dict]]:
        rows, overrides = [], []
        block = self.state["text_prewizard"]
        for speech_id in sorted(block["ids"], key=int):
            dialogue = self.dialogue_by_speech[speech_id]
            prepared = self.effective_text(speech_id)
            candidate = dialogue.get("text_variant_candidate") or {}
            manual = bool(block["ids"][speech_id].get("manual_reading"))
            text_actions = block.get("text_actions", {}).get(speech_id, [])
            tts = prepared["tts_text"]
            digest = digest_bytes(tts.encode("utf-8"))
            variant_id = candidate.get("variant_id") if manual else None
            if not variant_id:
                variant_id = f"{dialogue['dialog_id']}:de:prewizard-{digest[:16]}"
            provenance = candidate.get("provenance")
            if text_actions and not manual:
                provenance = {"kind": "text_review_action", "source_ref": "text_prewizard.text_actions", "source_version": "1"}
            if not isinstance(provenance, dict) or not all(provenance.get(key) for key in ("kind", "source_ref", "source_version")):
                provenance = ({"kind": "manual", "source_ref": "text_prewizard.manual_reading", "source_version": "1"}
                              if manual else {"kind": "normalization_rule", "source_ref": "text_prewizard.preparation_rules", "source_version": "1"})
            status = candidate.get("review_status")
            if status not in {"unreviewed", "reviewed", "approved", "rejected"}:
                status = "reviewed" if manual else "unreviewed"
            edit_status = candidate.get("edit_status")
            if edit_status not in {"imported", "machine_draft", "user_edited"}:
                edit_status = "user_edited" if manual else "imported"
            rows.append({"speech_id": speech_id,
                         "source_text": prepared["source_text"],
                         "display_text": prepared["display_text"],
                         "tts_text": tts,
                         "qa_expected_text": prepared["qa_expected_text"] or tts,
                         "qa_allowed_variants": [] if speech_id in self.state.get("patch_review_tts", {}) else list(candidate.get("qa_allowed_variants", [])),
                         "variant_id": variant_id,
                         "variant_revision": max(1, int(candidate.get("variant_revision", 1))) + self.state.get("patch_review_tts", {}).get(speech_id, {}).get("revision", 0),
                         "provenance": provenance,
                         "creation_version": candidate.get("creation_version", "mi1-text-prewizard:1"),
                         "edit_version": candidate.get("edit_version", "1"),
                         "edit_status": edit_status, "review_status": status,
                         "normalization_steps": ([{"kind": "review_action", "finding_ids": [x["finding"]["id"] for x in text_actions]}]
                                               if text_actions and not manual else [])})
            policy = self.dialogue_text_policy(dialogue)
            overrides.append({"speech_id": speech_id,
                              "anchor": policy["anchor"]["value"] if policy["anchor"]["source"] != "role" else "inherit",
                              "prewarm": policy["prewarm"]["value"] if policy["prewarm"]["source"] != "role" else "inherit"})
        return {"format": "mi1-text-pipeline-export/1", "dialogues": rows}, overrides

    def build_export(self, require_complete: bool = True) -> dict:
        for sid, review in self.state.get("patch_review_processing", {}).items():
            if review.get("processing") != "copy_original":continue
            dialogue = self.dialogue_by_speech.get(sid)
            if not dialogue or self.effective_processing(dialogue) != "copy_original":
                raise ValueError("Gespeicherte Originaltonbindung stimmt nicht mit der aktuellen Wizardquelle überein")
            try:valid = digest_file(self.resolve_audio(sid)) == review["original_audio_sha256"]
            except (OSError, KeyError):valid = False
            if not valid:raise ValueError("Originalton wurde seit der gespeicherten Exportentscheidung verändert")
        statuses = {role["role_id"]: self.role_status(role) for role in self.roles}
        incomplete = [{"role_id": rid, **status} for rid, status in statuses.items() if status["status"] != "ready"]
        if require_complete and incomplete:
            raise ValueError(f"Finalexport gesperrt: {len(incomplete)} von {len(self.roles)} Rollen sind nicht vollständig")
        exported = []
        for role in self.roles:
            if statuses[role["role_id"]]["status"] != "ready":
                continue
            if not self.active_role_dialogues(role):
                continue
            status = statuses[role["role_id"]]
            if (any(self.role_rules(role["role_id"]).values()) or status.get("without_anchor")
                    or status.get("without_prewarm") or status.get("anchor_reference") is None
                    or status.get("prewarm_artifact") is None):
                exported.append(self._export_overridden_role(role, status))
                continue
            record = self.state["roles"][role["role_id"]]
            anchor_artifact = statuses[role["role_id"]]["anchor_artifact"]
            anchor_reference = statuses[role["role_id"]]["anchor_reference"]
            prewarm_artifact = statuses[role["role_id"]]["prewarm_artifact"]
            main = self._export_anchor(role, record["anchors"][0], "local_wizard_direct_selection")
            main["materialized_audio"] = {"root_id": "dubbing_project", "path": anchor_reference["relative_path"],
                                           "sha256": anchor_reference["sha256"], "media_type": "audio/wav",
                                           "duration_ms": anchor_reference["duration_ms"],
                                           "sample_rate_hz": 48000, "channels": 1}
            prewarm_candidate = self._candidate_map(role)[record["prewarm_id"]]
            text_only = prewarm_candidate.get("source_kind") in {"neutral_standard", "custom_text"}
            source_in_window = not text_only and PREWARM_MIN_MS <= prewarm_candidate["duration_ms"] <= PREWARM_MAX_MS
            exported.append({
                "role_id": role["role_id"], "name": role["display_name"],
                "speaker_evidence": {"speaker_code": role["speaker_code"], "names": role["speaker_names"],
                                     "statuses": role["speaker_statuses"], "evidence": role["speaker_evidence"],
                                     "actor_ids_observed": role["actor_ids_observed"]},
                "main_anchor": main,
                "main_anchor_selection": {"status": "selected", "selected_at": record.get("selected_at", record["updated_at"]),
                                          "evidence_ref": "voice_anchor_wizard_data/state.json",
                                          "selection_fingerprint": self.anchor_fingerprint(role["role_id"], record),
                                          **({"preview_audio": {"root_id": "dubbing_project", "path": anchor_artifact["relative_path"],
                                                                 "sha256": anchor_artifact["sha256"], "media_type": "audio/wav",
                                                                 "duration_ms": anchor_artifact["duration_ms"], "sample_rate_hz": 48000, "channels": 1}}
                                             if anchor_artifact.get("ready") else {})},
                "backup_anchors": [self._export_anchor(role, value, "local_wizard_original_audio_review") for value in record["anchors"][1:]],
                "prewarm_for_long_text": bool(record.get("prewarm_for_long_text", False)),
                "use_voice_anchor_for_long_text": bool(record.get("use_voice_anchor_for_long_text", False)),
                "prewarm": {"source": prewarm_candidate.get("source_kind", "dialog"),
                            **({"prewarm_dialog_id": prewarm_candidate["dialog_id"]}
                               if prewarm_candidate.get("dialog_id") else {}),
                            "source_duration_ms": None if text_only else prewarm_candidate["duration_ms"],
                            "source_duration_window_match": source_in_window,
                            "source_selection": (prewarm_candidate["source_kind"] if text_only
                                                 else "target_3_4s" if source_in_window else "nearest_duration_fallback"),
                            "text": prewarm_candidate["german_text"],
                            "text_sha256": digest_bytes(prewarm_candidate["german_text"].encode("utf-8")),
                            "audio": {"root_id": "dubbing_project", "path": prewarm_artifact["relative_path"],
                                      "sha256": prewarm_artifact["sha256"], "media_type": "audio/wav",
                                      "duration_ms": prewarm_artifact["duration_ms"], "sample_rate_hz": 48000, "channels": 1},
                            "technical_check": "pass",
                            "selection": {"status": "selected", "selected_at": record.get("selected_at", record["updated_at"]),
                                          "evidence_ref": "voice_anchor_wizard_data/state.json",
                                          "selection_fingerprint": self.prewarm_fingerprint(role["role_id"], record)},
                            "generated_with_anchor_fingerprint": anchor_artifact["fingerprint"],
                            "generated_with_anchor_audio_sha256": anchor_reference["sha256"]},
            })
        text_export, dialogue_overrides = self._text_prewizard_export()
        result = {"format": EXPORT_FORMAT, "status": "complete" if not incomplete else "blocked",
                  "source_manifest": {"root_id": "extractor_project", "path": "work/mi1_manifest_v1.draft.json",
                                      "sha256": self.source_sha256},
                  "policy": {"anchor_count": [1, 3], "composite_component_count": [2, MAX_COMPOSITE_COMPONENTS],
                             "composite_join_silence_ms": JOIN_MS,
                             "composite_target_duration_ms": [TARGET_MIN_MS, TARGET_MAX_MS],
                             "composite_max_duration_ms": TARGET_MAX_MS,
                             "prewarm_target_duration_ms": [PREWARM_MIN_MS, PREWARM_MAX_MS],
                             "audition_only_audio_allowed": False,
                             "reviewed_render_ids_enter_normal_production_scope": True},
                  "scope": {"processing": "render",
                            "source_dialog_ids": [d["dialog_id"] for d in self.dialogues],
                            "dialog_count": sum(self.dialogue_processing(str(d["native_speech_id"])) == "render" for d in self.dialogues),
                            "dialog_ids": [d["dialog_id"] for d in self.dialogues
                                           if self.dialogue_processing(str(d["native_speech_id"])) == "render"]},
                  "dialogue_decisions": self.export_dialogue_decisions(),
                  "roles": exported, "incomplete_roles": incomplete,
                  "text_prewizard": text_export, "dialogue_overrides": dialogue_overrides,
                  "generated_at": utc_now()}
        validate_export(result, require_complete=require_complete)
        return result

    def export_dialogue_decisions(self) -> list[dict]:
        decisions = {speech_id: {"speech_id": speech_id, **decision}
                     for speech_id, decision in self.state.get("dialogue_actions", {}).items()}
        for dialogue in self.dialogues:
            role_id = self.effective_role_id(dialogue)
            speech_id = str(dialogue["native_speech_id"])
            review = self.state.get("patch_review_processing", {}).get(speech_id)
            if review and self.effective_processing(dialogue) == "render":
                if decisions.get(speech_id, {}).get("action") == "copy_original":decisions.pop(speech_id)
                continue
            if self.role_processing(role_id) != "copy_original":
                if self.effective_processing(dialogue) == "render":
                    continue
                if self.effective_processing(dialogue) == "pending":
                    continue
                # Confirmed source-level original processing is already a
                # settled decision and belongs in the export receipt.
                if self.effective_processing(dialogue) != "copy_original":
                    continue
            speech_id = str(dialogue["native_speech_id"])
            source_role_id = role_id_for(dialogue)
            resolution = dialogue.get("unclear_resolution") or {}
            override = self.state.get("role_processing", {}).get(role_id, {})
            decided_at = override.get("decided_at") or (review or {}).get("decided_at") or decisions.get(speech_id, {}).get("decided_at") or resolution.get("confirmed_at")
            decision = {"speech_id": speech_id, "action": "copy_original",
                "source_role_id": source_role_id, "dialog_id": dialogue["dialog_id"],
                **({"target_role_id": role_id} if role_id != source_role_id else {}),
                **({"decided_at": decided_at} if decided_at else {}),
                **({"content_type": "nonverbal"} if decisions.get(speech_id, {}).get("content_type") == "nonverbal" else {})}
            decisions[speech_id] = decision
        return [decisions[speech_id] for speech_id in sorted(decisions, key=int)]

    def write_export(self) -> Path:
        result = self.build_export(require_complete=True)
        atomic_json(self.export_path, result)
        return self.export_path

    def write_render_export(self) -> Path:
        # app.py is also launched directly from the USB project.
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from manifest_renderer.mi1_prepare import compose_mi1_manifest
        component = self.build_export(require_complete=True)
        manifest = compose_mi1_manifest(self.draft, component, self.dubbing_root)
        output = self.data_root / "mi1-render.manifest.json"
        atomic_json(output, manifest)
        atomic_json(self.export_path, component)
        return output

    def resolve_audio(self, speech_id: str) -> Path:
        dialogue = self.dialogue_by_speech.get(speech_id)
        audio = dialogue.get("original_audio") if dialogue else None
        if not audio or not audio.get("path"):
            raise FileNotFoundError(speech_id)
        path = (self.dubbing_root / safe_relative(audio["path"])).resolve()
        if not path.is_relative_to(self.dubbing_root) or path.is_symlink() or not path.is_file():
            raise FileNotFoundError(speech_id)
        return path

    def resolve_custom_audio(self, role_id: str, import_id: str, requested_sha256: str) -> Path:
        record = self.state.get("custom_anchors", {}).get(role_id, {}).get(import_id)
        if not record or record["audio"]["sha256"] != requested_sha256:
            raise FileNotFoundError(import_id)
        path = (self.dubbing_root / record["audio"]["path"]).resolve()
        if (not path.is_relative_to((self.data_root / "custom-anchors").resolve())
                or path.is_symlink() or not path.is_file() or digest_file(path) != requested_sha256):
            raise FileNotFoundError(import_id)
        return path


PROGRESS_PHASES = {"preparing", "loading_model", "model_ready", "rendering", "saving", "reusing"}
PROGRESS_KINDS = {"anchor", "prewarm", "candidate-anchor", "candidate-prewarm", "dialogue"}


def render_progress(wizard: Wizard, job: dict) -> dict:
    """Read only this job's sidecar and current CLI invocation, with bounded IO."""
    fallback = {"phase": "preparing", "kind": "anchor" if job.get("role_id") else None, "speech_id": job.get("speech_id"), "model_loaded": False}
    if job.get("kind"):
        fallback["kind"] = "dialogue" if job["kind"] == "dialogue" else "candidate-" + job["kind"]
    job_id = job.get("job_id")
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
        return fallback
    expected = wizard.data_root / "jobs" / (job_id + ".progress.json")
    try:
        path = (wizard.dubbing_root / job["progress_path"]).resolve()
        if path != expected.resolve() or not path.is_relative_to(wizard.data_root.resolve()):
            return fallback
        if path.stat().st_size > 16384:
            return fallback
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("job_id") != job_id
                or value.get("phase") not in PROGRESS_PHASES or value.get("kind") not in PROGRESS_KINDS
                or not isinstance(value.get("updated_at"), str)
                or value["updated_at"] < job["started_at"]
                or value["updated_at"] > utc_now()
                or (value.get("speech_id") is not None and not isinstance(value["speech_id"], str))
                or (job.get("kind") and (value["kind"] != fallback["kind"]
                    or value.get("speech_id") != job.get("speech_id")))
                or (not job.get("kind") and value["kind"] not in {"anchor", "prewarm"})):
            return fallback
        updated = datetime.fromisoformat(value["updated_at"])
        if not datetime.fromisoformat(job["started_at"]) <= updated <= datetime.now(timezone.utc):
            return fallback
        result = {"phase": value["phase"], "kind": value["kind"],
                  "speech_id": value.get("speech_id"), "model_loaded": False}
        if job.get("status") != "running" or result["phase"] != "loading_model":
            return result
        log = (wizard.dubbing_root / value["synthesis_log"]).resolve()
        if log.name != "synthesis.log" or not log.is_relative_to(wizard.data_root.resolve()):
            return fallback
        stat = log.stat()
        if stat.st_ino != value["log_inode"] or stat.st_mtime_ns < value["log_mtime_ns"]:
            return fallback
        # Initialization is near line 534 in real logs; preserve the prefix even
        # when the last render messages move beyond the bounded tail.
        with log.open("rb") as stream:
            prefix = stream.read(256 * 1024)
            if stat.st_size > len(prefix):
                stream.seek(max(len(prefix), stat.st_size - 64 * 1024))
                prefix += stream.read(64 * 1024)
        text = prefix.decode("utf-8", errors="replace")
        initialized = text.find("VoxCPM2Runtime: initialized (")
        if initialized >= 0:
            result.update(phase="model_ready", model_loaded=True)
            subsequent = text[initialized:]
            if any(marker in subsequent for marker in ("  Reference:", "Generating (combined)", "Generating...")):
                result["phase"] = "rendering"
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return fallback


class RenderJobManager:
    """One controlled render subprocess for this local Wizard server."""
    def __init__(self, wizard: Wizard, *, capability: dict | None = None, popen_factory=None):
        self.wizard = wizard
        self.capability = capability or render_capability(wizard.dubbing_root)
        self.popen_factory = popen_factory or subprocess.Popen
        self.job_path = wizard.data_root / "render-job.json"
        self._process = None
        self._lock = threading.Lock()
        self._job = self._load_job()

    def _load_job(self) -> dict:
        if not self.job_path.is_file():
            return {"status": "idle", "capability": self.capability}
        try:
            job = json.loads(self.job_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {"status": "idle", "capability": self.capability}
        if job.get("status") == "running":
            job.update(status="interrupted", error="Server wurde während des Renderjobs beendet", finished_at=utc_now())
            atomic_json(self.job_path, job)
        job["capability"] = self.capability
        return job

    def _persist(self) -> None:
        stored = {key: value for key, value in self._job.items() if key not in {"capability", "log"}}
        atomic_json(self.job_path, stored)

    def status(self) -> dict:
        with self._lock:
            result = dict(self._job)
            result["capability"] = self.capability
            result["progress"] = render_progress(self.wizard, result)
            log_path = result.get("log_path")
            if log_path:
                path = (self.wizard.dubbing_root / log_path).resolve()
                if path.is_relative_to(self.wizard.data_root) and path.is_file():
                    result["log"] = path.read_text(encoding="utf-8", errors="replace")[-20000:]
            return result

    def start(self, role_id: str, revision: int) -> dict:
        with self._lock:
            if not self.capability["available"]:
                raise ValueError("Rendering auf M1 erforderlich")
            self.wizard.reload_state()
            if type(revision) is not int or revision != self.wizard.state["revision"]:
                raise RuntimeError("Zustand wurde zwischenzeitlich geändert; Seite neu laden")
            role = self.wizard.role_by_id.get(role_id)
            if not role or role["blocked_reasons"]:
                raise ValueError("Unbekannte oder gesperrte Rolle")
            self.wizard.require_render_role(role_id)
            record = self.wizard.state["roles"].get(role_id)
            if not record:
                raise ValueError("Zuerst Anker und Prewarm speichern")
            if record.get("prewarm_id") is None:
                raise ValueError("Zuerst einen Prewarm wählen und speichern")
            if self._job.get("status") == "running" and self._process and self._process.poll() is None:
                raise RuntimeError("Es läuft bereits ein Rollen-Hörtest")
            job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + self.wizard.role_slug(role_id)
            log = self.wizard.data_root / "jobs" / (job_id + ".log")
            log.parent.mkdir(parents=True, exist_ok=True)
            progress = log.with_suffix(".progress.json")
            started_at = utc_now()
            command = [sys.executable, str(Path(__file__).resolve().parent / "render_tests_m1.py"),
                       "--role", role_id, "--draft", str(self.wizard.draft_path),
                       "--dubbing-root", str(self.wizard.dubbing_root), "--data-root", str(self.wizard.data_root),
                       "--expected-anchor-fingerprint", self.wizard.anchor_fingerprint(role_id, record),
                       "--expected-prewarm-fingerprint", self.wizard.prewarm_fingerprint(role_id, record)]
            command.extend(["--progress-file", str(progress)])
            stream = log.open("x", encoding="utf-8")
            try:
                self._process = self.popen_factory(command, cwd=self.wizard.dubbing_root, stdout=stream,
                                                   stderr=subprocess.STDOUT, shell=False)
            except Exception:
                stream.close()
                raise
            self._job = {"job_id": job_id, "status": "running", "role_id": role_id,
                         "anchor_selection_fingerprint": self.wizard.anchor_fingerprint(role_id, record),
                         "prewarm_selection_fingerprint": self.wizard.prewarm_fingerprint(role_id, record),
                         "log_path": str(log.relative_to(self.wizard.dubbing_root)).replace(os.sep, "/"),
                         "progress_path": str(progress.relative_to(self.wizard.dubbing_root)).replace(os.sep, "/"),
                         "started_at": started_at, "error": None}
            self._persist()
            threading.Thread(target=self._wait, args=(self._process, stream), daemon=True).start()
            return self.status_unlocked()

    def status_unlocked(self) -> dict:
        result = dict(self._job); result["capability"] = self.capability
        result["progress"] = render_progress(self.wizard, result)
        return result

    def _wait(self, process, stream) -> None:
        return_code = process.wait()
        stream.close()
        with self._lock:
            self._job["status"] = "complete" if return_code == 0 else "error"
            self._job["return_code"] = return_code
            self._job["finished_at"] = utc_now()
            if return_code != 0:
                self._job["error"] = "M1-Hörtest fehlgeschlagen; Protokoll prüfen"
            self._persist()


class CandidateRenderJobManager:
    """One controlled, selection-independent candidate preview subprocess."""
    def __init__(self, wizard: Wizard, *, capability: dict | None = None, popen_factory=None):
        self.wizard = wizard
        self.capability = capability or render_capability(wizard.dubbing_root)
        self.popen_factory = popen_factory or subprocess.Popen
        self.job_path = wizard.data_root / "candidate-render-job.json"
        self._process = None
        self._lock = threading.Lock()
        self._job = self._load_job()

    def _load_job(self) -> dict:
        if not self.job_path.is_file():
            return {"status": "idle", "capability": self.capability}
        try:
            job = json.loads(self.job_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {"status": "idle", "capability": self.capability}
        if job.get("status") == "running":
            job.update(status="interrupted", error="Server wurde während der Kandidatenprobe beendet",
                       finished_at=utc_now())
            atomic_json(self.job_path, job)
        job["capability"] = self.capability
        return job

    def _persist(self) -> None:
        atomic_json(self.job_path, {key: value for key, value in self._job.items()
                                    if key not in {"capability", "log"}})

    def status(self) -> dict:
        with self._lock:
            result = dict(self._job); result["capability"] = self.capability
            result["progress"] = render_progress(self.wizard, result)
            log_path = result.get("log_path")
            if log_path:
                path = (self.wizard.dubbing_root / log_path).resolve()
                if path.is_relative_to(self.wizard.data_root) and path.is_file():
                    result["log"] = path.read_text(encoding="utf-8", errors="replace")[-20000:]
            return result

    def start(self, role_id: str, speech_id: str, kind: str, revision: int) -> dict:
        with self._lock:
            if not self.capability["available"]:
                raise ValueError("Rendering auf M1 erforderlich")
            self.wizard.reload_state()
            if type(revision) is not int or revision != self.wizard.state["revision"]:
                raise RuntimeError("Zustand wurde zwischenzeitlich geändert; Seite neu laden")
            if kind == "dialogue":
                plan = self.wizard.dialogue_test_plan(role_id, speech_id)
            else:
                plan = self.wizard.candidate_preview_plan(role_id, speech_id, kind)
            if self._job.get("status") == "running" and self._process and self._process.poll() is None:
                raise RuntimeError("Es läuft bereits eine Kandidatenprobe")
            job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-candidate"
            log = self.wizard.data_root / "jobs" / (job_id + ".log")
            log.parent.mkdir(parents=True, exist_ok=True)
            progress = log.with_suffix(".progress.json")
            started_at = utc_now()
            command = [sys.executable, str(Path(__file__).resolve().parent / "render_tests_m1.py")]
            if kind == "dialogue":
                command.extend(["--dialogue-role", role_id, "--dialogue-speech-id", speech_id,
                                "--expected-dialogue-fingerprint", plan["fingerprint"]])
            else:
                command.extend(["--candidate-role", role_id, "--candidate-speech-id", speech_id,
                                "--candidate-kind", kind,
                                "--expected-candidate-fingerprint", plan["fingerprint"]])
            command.extend(["--draft", str(self.wizard.draft_path),
                            "--dubbing-root", str(self.wizard.dubbing_root), "--data-root", str(self.wizard.data_root)])
            command.extend(["--progress-file", str(progress)])
            stream = log.open("x", encoding="utf-8")
            try:
                self._process = self.popen_factory(command, cwd=self.wizard.dubbing_root, stdout=stream,
                                                   stderr=subprocess.STDOUT, shell=False)
            except Exception:
                stream.close(); raise
            self._job = {"job_id": job_id, "status": "running", "role_id": role_id,
                         "speech_id": speech_id, "kind": kind,
                         "candidate_fingerprint": plan["fingerprint"],
                         "log_path": str(log.relative_to(self.wizard.dubbing_root)).replace(os.sep, "/"),
                         "progress_path": str(progress.relative_to(self.wizard.dubbing_root)).replace(os.sep, "/"),
                         "started_at": started_at, "error": None}
            self._persist()
            threading.Thread(target=self._wait, args=(self._process, stream), daemon=True).start()
            return {**self._job, "capability": self.capability,
                    "progress": render_progress(self.wizard, self._job)}

    def _wait(self, process, stream) -> None:
        return_code = process.wait(); stream.close()
        with self._lock:
            self._job["status"] = "complete" if return_code == 0 else "error"
            self._job["return_code"] = return_code
            self._job["finished_at"] = utc_now()
            if return_code != 0:
                self._job["error"] = "Auswahlprobe fehlgeschlagen; Protokoll prüfen"
            self._persist()


def validate_export(value: object, require_complete: bool = True) -> None:
    if not isinstance(value, dict) or value.get("format") != EXPORT_FORMAT:
        raise ValueError("Manifestbaustein hat ein unbekanntes Format")
    if value.get("status") not in {"complete", "blocked"}:
        raise ValueError("Manifestbaustein hat ungültigen Status")
    if require_complete and (value["status"] != "complete" or value.get("incomplete_roles")):
        raise ValueError("Manifestbaustein ist nicht vollständig")
    if value.get("policy", {}).get("audition_only_audio_allowed") is not False:
        raise ValueError("Audition-only-Audio darf nicht in den Produktionsbaustein")
    for decision in value.get("dialogue_decisions", []):
        if "content_type" in decision and (decision["content_type"] != "nonverbal" or decision.get("action") != "copy_original"):
            raise ValueError("Nichtsprachliche IDs müssen als Original übernommen werden")
    text_block = value.get("text_prewizard")
    if text_block is not None:
        if not isinstance(text_block, dict) or text_block.get("format") != "mi1-text-pipeline-export/1" or not isinstance(text_block.get("dialogues"), list):
            raise ValueError("Vorwizard-Export hat ein unbekanntes Format")
        seen_text_ids = set()
        for item in text_block["dialogues"]:
            if not isinstance(item, dict) or not isinstance(item.get("speech_id"), str) or not ID.fullmatch(item["speech_id"]):
                raise ValueError("Vorwizard-Export braucht eine native Dialog-ID")
            if item["speech_id"] in seen_text_ids:
                raise ValueError("Doppelte native Dialog-ID im Vorwizard-Export")
            seen_text_ids.add(item["speech_id"])
            for key in ("source_text", "display_text", "tts_text", "qa_expected_text"):
                if not isinstance(item.get(key), str):
                    raise ValueError(f"Vorwizard-Feld {key} muss Text sein")
            provenance = item.get("provenance")
            if not isinstance(provenance, dict) or not all(isinstance(provenance.get(k), str) and provenance[k] for k in ("kind", "source_ref", "source_version")):
                raise ValueError("Vorwizard-Variante braucht Provenienz")
            if item.get("edit_status") not in {"imported", "machine_draft", "user_edited"} or item.get("review_status") not in {"unreviewed", "reviewed", "approved", "rejected"}:
                raise ValueError("Vorwizard-Variante hat einen unbekannten Reviewstatus")
        seen_override_ids = set()
        for item in value.get("dialogue_overrides", []):
            if not isinstance(item, dict) or not isinstance(item.get("speech_id"), str) or not ID.fullmatch(item["speech_id"]):
                raise ValueError("Dialog-Override braucht eine native Dialog-ID")
            if item["speech_id"] in seen_override_ids:
                raise ValueError("Doppelter Dialog-Override")
            seen_override_ids.add(item["speech_id"])
            if any(item.get(feature) not in {"inherit", "use", "skip"} for feature in ("anchor", "prewarm")):
                raise ValueError("Dialog-Override hat einen unbekannten Modus")
    role_ids = set()
    for role in value.get("roles", []):
        if role.get("role_id") in role_ids:
            raise ValueError("Doppelte Rollen-ID")
        role_ids.add(role.get("role_id"))
        rules = {key: role.get(key, False) for key in ("without_anchor", "without_prewarm")}
        if any(type(rule) is not bool for rule in rules.values()):
            raise ValueError("Rollenregeln müssen boolesch sein")
        if rules["without_anchor"]:
            if role.get("main_anchor") is not None or role.get("main_anchor_selection") is not None:
                raise ValueError("Ohne Stimmanker darf kein Hauptanker exportiert werden")
        elif not role.get("main_anchor") or (role.get("main_anchor_selection") or {}).get("status") != "selected":
            raise ValueError("Hauptanker ist nicht ausgewählt")
        prewarm = role.get("prewarm")
        if rules["without_prewarm"]:
            if prewarm is not None:
                raise ValueError("Ohne Prewarm darf kein Prewarm exportiert werden")
        elif not prewarm or prewarm.get("selection", {}).get("status") != "selected":
            raise ValueError("Prewarm ist nicht ausgewählt")
        elif rules["without_anchor"]:
            if (prewarm.get("reference_mode") != "line_original" or "audio" in prewarm
                    or not prewarm.get("text") or digest_bytes(prewarm["text"].encode("utf-8")) != prewarm.get("text_sha256")):
                raise ValueError("Dialogbezogener Prewarm braucht hashgebundenen Text und Originalreferenz je ID")
        elif not PREWARM_MIN_MS <= prewarm["audio"]["duration_ms"] <= PREWARM_MAX_MS:
            raise ValueError("Prewarm liegt außerhalb 3–4 s")
        if type(role.get("prewarm_for_long_text")) is not bool:
            raise ValueError("Rollenregel für lange Texte fehlt")
        if type(role.get("use_voice_anchor_for_long_text")) is not bool:
            raise ValueError("Stimmankerregel für lange Texte fehlt")
        for path in paths_in(role):
            safe_relative(path)


def paths_in(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"path", "evidence_ref"} and isinstance(child, str):
                yield child
            else:
                yield from paths_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from paths_in(child)


def default_dubbing_root(script_root: Path) -> Path:
    return script_root.parent


def handler_for(wizard: Wizard, ui_root: Path, job_manager: RenderJobManager | None = None,
                candidate_job_manager: CandidateRenderJobManager | None = None,
                text_audition_job_manager=None, review_service=None):
    job_manager = job_manager or RenderJobManager(wizard)
    candidate_job_manager = candidate_job_manager or CandidateRenderJobManager(wizard)
    if text_audition_job_manager is None:
        from text_auditions import TextAuditionJobManager
        text_audition_job_manager = TextAuditionJobManager(wizard)
    def review_renderer_availability():
        busy=[name for name,manager in (("role_render",job_manager),("candidate_render",candidate_job_manager),("text_audition",text_audition_job_manager))
              if manager.status().get("status")=="running"]
        return ({"busy":True,"busy_source":busy[0],"reason":"Wizardrenderer belegt ("+", ".join(busy)+"); keine Warteschlange."}
                if busy else {"busy":False,"busy_source":None,"reason":None})
    wizard.review_busy_callback=review_renderer_availability
    review_holder = [review_service]
    def dispatch_review(method, path, payload=None):
        from patch_review.http import dispatch
        if path.startswith("/api/patch-review/"):
            if review_holder[0] is None:
                from patch_review.wizard import for_wizard
                review_holder[0] = for_wizard(wizard,
                    getattr(wizard, "review_manifest_path", None), getattr(wizard, "review_run_root", None))
            return dispatch(review_holder[0], method, path, payload)
        return dispatch(None, method, path, payload)
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8", filename: str | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'self'; object-src 'none'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, value: object, status: int = 200):
            self.respond(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

        def payload(self, max_bytes: int = 65536) -> object:
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError("Content-Type muss application/json sein")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= max_bytes:
                raise ValueError("Ungültige Anfragegröße")
            return json.loads(self.rfile.read(length))

        def do_GET(self):
            parsed = urlparse(self.path)
            try:
                if parsed.path.startswith(("/api/patch-review/", "/patch-review/")):
                    with LOCK:
                        wizard.reload_state()
                        response = dispatch_review("GET", self.path)
                    if response is not None:
                        return self.respond(*response)
                # Original sources and UI files do not depend on mutable choices.
                # Serve them without waiting for role/status API work under LOCK.
                match = re.fullmatch(r"/audio/original/([0-9]+)", parsed.path)
                if match:
                    audio = wizard.resolve_audio(match.group(1))
                    return self.respond(200, audio.read_bytes(), mimetypes.guess_type(audio.name)[0] or "application/octet-stream")
                if parsed.path in {"/", "/index.html"}:
                    return self.respond(200, (ui_root / "index.html").read_bytes(), "text/html; charset=utf-8")
                if parsed.path == "/app.js":
                    return self.respond(200, (ui_root / "app.js").read_bytes(), "application/javascript; charset=utf-8")
                if parsed.path == "/text-prewizard.js":
                    return self.respond(200, (ui_root / "text-prewizard.js").read_bytes(), "application/javascript; charset=utf-8")
                with LOCK:
                    wizard.reload_state()
                    if parsed.path == "/api/state":
                        return self.send_json(wizard.public_state())
                    if parsed.path == "/api/text-prewizard":
                        query = parse_qs(parsed.query)
                        requested = query.get("revision", [None])[0]
                        detail_id = query.get("detail", [None])[0]
                        revision = int(requested) if requested is not None and requested.isdigit() else None
                        if detail_id is not None and revision is not None and revision != wizard.state["revision"]:
                            raise RuntimeError("Der Stand wurde geändert. Neu laden und Hörbereitschaft erneut prüfen.")
                        audition_job = text_audition_job_manager.status() if detail_id is not None else None
                        busy = detail_id is not None and any(manager.status().get("status") == "running" for manager in
                            (job_manager, candidate_job_manager, text_audition_job_manager))
                        return self.send_json(wizard.text_prewizard_state(revision, detail_id=detail_id,
                            audition_busy=busy, audition_job=audition_job))
                    if parsed.path == "/api/export":
                        result = wizard.build_export(require_complete=True)
                        wizard.write_export()
                        return self.respond(200, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
                                            filename=wizard.export_path.name)
                    if parsed.path == "/api/render-manifest":
                        output = wizard.write_render_export()
                        return self.respond(200, output.read_bytes(), filename=output.name)
                    if parsed.path == "/api/export-preview":
                        return self.send_json(wizard.build_export(require_complete=False))
                    if parsed.path == "/api/render-plan":
                        role_id = parse_qs(parsed.query).get("role_id", [""])[0]
                        if role_id not in wizard.role_by_id:
                            raise ValueError("Unbekannte Rolle")
                        return self.send_json(wizard.render_plan(role_id))
                    if parsed.path == "/api/dialogues":
                        query = parse_qs(parsed.query)
                        items = wizard.dialogue_cases(query.get("role_id", [""])[0], query.get("q", [""])[0])
                        if query.get("filter", ["all"])[0] == "open":
                            items = [item for item in items if item["processing"] == "pending" or
                                     (item["processing"] == "render" and not item["german_text"])]
                        offset = max(0, int(query.get("offset", ["0"])[0]))
                        limit = max(1, min(200, int(query.get("limit", ["100"])[0])))
                        # A saved decision can shrink the open list on its last page.
                        if offset >= len(items):
                            offset = max(0, ((len(items) - 1) // limit) * limit)
                        return self.send_json({"items": items[offset:offset + limit], "total": len(items),
                                               "offset": offset, "limit": limit})
                    if parsed.path == "/api/dialogue-test-plan":
                        query = parse_qs(parsed.query)
                        return self.send_json(wizard.dialogue_test_plan(query.get("role_id", [""])[0],
                                                                        query.get("speech_id", [""])[0]))
                    if parsed.path == "/api/render-job":
                        return self.send_json(job_manager.status())
                    if parsed.path == "/api/candidate-render-job":
                        return self.send_json(candidate_job_manager.status())
                    if parsed.path == "/api/text-audition-job":
                        return self.send_json(text_audition_job_manager.status())
                audition_match = re.fullmatch(r"/audio/text-audition/([0-9]+)/([a-f0-9]{64})/([1-4])", parsed.path)
                if audition_match:
                    speech_id, fingerprint, index = audition_match.groups()
                    folder = wizard.data_root / "text-auditions" / speech_id / fingerprint
                    try:
                        plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
                        from text_auditions import cached_results
                        rows = cached_results(folder, plan)
                        artifact = rows[int(index) - 1]
                        audio = folder / f"variant-{index}.wav"
                        if plan.get("speech_id") != speech_id or plan.get("fingerprint") != fingerprint or not artifact.get("audio_url"):
                            raise FileNotFoundError(speech_id)
                    except (OSError, ValueError, IndexError, KeyError):
                        raise FileNotFoundError(speech_id)
                    return self.respond(200, audio.read_bytes(), "audio/wav")
                if parsed.path == "/audio/custom-anchor":
                    query = parse_qs(parsed.query)
                    role_id = query.get("role_id", [""])[0]
                    import_id = query.get("import_id", [""])[0]
                    requested_sha256 = query.get("audio_sha256", [""])[0]
                    with LOCK:
                        wizard.reload_state()
                        audio = wizard.resolve_custom_audio(role_id, import_id, requested_sha256)
                    return self.respond(200, audio.read_bytes(), "audio/wav")
                match = re.fullmatch(r"/audio/generated/(anchor|prewarm|reference)", parsed.path)
                if match:
                    query = parse_qs(parsed.query)
                    role_id = query.get("role_id", [""])[0]
                    requested_selection = query.get("selection_fingerprint", [""])[0]
                    requested_sha256 = query.get("audio_sha256", [""])[0]
                    with LOCK:
                        wizard.reload_state(); record = wizard.state["roles"].get(role_id)
                        kind = match.group(1)
                        artifact = (wizard.reference_status(role_id, record) if kind == "reference"
                                    else wizard.artifact_status(role_id, record, kind)) if record and record.get("anchors") else {"ready": False}
                        if (not artifact.get("playable", artifact["ready"]) or requested_selection != artifact.get("selection_fingerprint")
                                or requested_sha256 != artifact.get("sha256")):
                            raise FileNotFoundError(role_id)
                        if kind == "reference":
                            anchor_audio, _ = wizard.artifact_paths(role_id, record, "anchor")
                            audio = anchor_audio.parent / "reference.wav"
                        else:
                            audio, _ = wizard.artifact_paths(role_id, record, kind)
                    return self.respond(200, audio.read_bytes(), "audio/wav")
                if parsed.path == "/audio/candidate-preview":
                    query = parse_qs(parsed.query)
                    role_id = query.get("role_id", [""])[0]
                    speech_id = query.get("speech_id", [""])[0]
                    kind = query.get("kind", [""])[0]
                    requested_fingerprint = query.get("fingerprint", [""])[0]
                    requested_sha256 = query.get("audio_sha256", [""])[0]
                    with LOCK:
                        wizard.reload_state()
                        artifact = wizard.candidate_preview_status(role_id, speech_id, kind)
                        if (not artifact["ready"] or artifact["fingerprint"] != requested_fingerprint
                                or artifact["sha256"] != requested_sha256):
                            raise FileNotFoundError(speech_id)
                        audio, _ = wizard.candidate_preview_paths(role_id, speech_id, kind)
                    return self.respond(200, audio.read_bytes(), "audio/wav")
                if parsed.path == "/audio/dialogue-prewarm":
                    query = parse_qs(parsed.query)
                    role_id = query.get("role_id", [""])[0]
                    speech_id = query.get("speech_id", [""])[0]
                    with LOCK:
                        wizard.reload_state()
                        plan = wizard.dialogue_test_plan(role_id, speech_id)
                        artifact = wizard.dialogue_prewarm_status(plan)
                        if (not artifact["playable"] or artifact.get("fingerprint") != query.get("fingerprint", [""])[0]
                                or artifact.get("sha256") != query.get("audio_sha256", [""])[0]):
                            raise FileNotFoundError(speech_id)
                        audio = wizard.dubbing_root / plan["per_line_prewarm"]["output"]
                    return self.respond(200, audio.read_bytes(), "audio/wav")
                if parsed.path == "/audio/dialogue-test":
                    query = parse_qs(parsed.query)
                    role_id = query.get("role_id", [""])[0]
                    speech_id = query.get("speech_id", [""])[0]
                    requested_fingerprint = query.get("fingerprint", [""])[0]
                    requested_sha256 = query.get("audio_sha256", [""])[0]
                    with LOCK:
                        wizard.reload_state()
                        artifact = wizard.dialogue_test_status(role_id, speech_id)
                        if (not artifact["ready"] or artifact["fingerprint"] != requested_fingerprint
                                or artifact["sha256"] != requested_sha256):
                            raise FileNotFoundError(speech_id)
                        plan = wizard.dialogue_test_plan(role_id, speech_id)
                        audio = wizard.dubbing_root / plan["output"]
                    return self.respond(200, audio.read_bytes(), "audio/wav")
                self.respond(404, b"", "text/plain")
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 409 if "gesperrt" in str(exc) else 400)
            except FileNotFoundError:
                self.respond(404, b"", "text/plain")
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)

        def do_POST(self):
            origin = self.headers.get("Origin")
            expected = f"http://127.0.0.1:{self.server.server_port}"
            if origin and origin != expected:
                return self.send_json({"error": "Fremder Ursprung"}, 403)
            try:
                with LOCK:
                    wizard.reload_state()
                    if self.path.startswith("/api/patch-review/"):
                        if self.path == "/api/patch-review/start" and any(manager.status().get("status") == "running" for manager in
                                (job_manager, candidate_job_manager, text_audition_job_manager)):
                            raise RuntimeError("Modell belegt; keine Warteschlange")
                        response = dispatch_review("POST", self.path, self.payload(32 * 1024 * 1024))
                        if response is not None:
                            return self.respond(*response)
                    if self.path in {"/api/render-job", "/api/candidate-render-job", "/api/text-audition-job"} and review_holder[0] is not None:
                        review_status = review_holder[0].status()
                        if (review_status["job"]["status"] == "running" or
                                review_status.get("patch_build", {}).get("job", {}).get("status") == "running"):
                            raise RuntimeError("Reviewauftrag oder Spielerstellung läuft; keine Warteschlange")
                    if self.path == "/api/selection":
                        return self.send_json(wizard.save_selection(self.payload()))
                    routes = {"/api/text-prewizard/rules": "rules", "/api/text-prewizard/vocabulary": "vocabulary",
                              "/api/text-prewizard/sounds": "sounds", "/api/text-prewizard/patterns": "patterns",
                              "/api/text-prewizard/id": "id", "/api/text-prewizard/lists": "lists",
                              "/api/text-prewizard/review": "review", "/api/text-prewizard/actions": "actions",
                              "/api/text-prewizard/workflow": "workflow"}
                    if self.path in routes:
                        operation = routes[self.path]
                        return self.send_json(wizard.save_text_prewizard_api(operation, self.payload()))
                    if self.path == "/api/prewarm-text":
                        return self.send_json(wizard.save_prewarm_text(self.payload()))
                    if self.path == "/api/original-transcript":
                        if any(manager.status().get("status") == "running" for manager in (job_manager, candidate_job_manager)):
                            raise RuntimeError("Während eines Renderlaufs können Transkripte nicht geändert werden")
                        return self.send_json(wizard.save_original_transcript(self.payload()))
                    if self.path == "/api/dialogue-text":
                        if any(manager.status().get("status") == "running" for manager in (job_manager, candidate_job_manager)):
                            raise RuntimeError("Während eines Renderlaufs können Dialogtexte nicht geändert werden")
                        return self.send_json(wizard.save_dialogue_text(self.payload()))
                    if self.path == "/api/role-rules":
                        return self.send_json(wizard.save_role_rules(self.payload()))
                    if self.path == "/api/role-processing":
                        return self.send_json(wizard.save_role_processing(self.payload()))
                    if self.path == "/api/candidates":
                        return self.send_json(wizard.expand_candidates(self.payload()))
                    if self.path == "/api/custom-anchor/import":
                        return self.send_json(wizard.import_custom_anchor(self.payload(24 * 1024 * 1024)))
                    if self.path == "/api/custom-anchor/delete":
                        return self.send_json(wizard.delete_custom_anchor(self.payload()))
                    if self.path == "/api/dialogue-action":
                        return self.send_json(wizard.save_dialogue_action(self.payload()))
                    if self.path == "/api/render-job":
                        payload = self.payload()
                        if not isinstance(payload, dict) or set(payload) != {"role_id", "revision"}:
                            raise ValueError("Renderstart enthält unbekannte oder fehlende Felder")
                        if any(manager.status().get("status") == "running" for manager in
                               (candidate_job_manager, text_audition_job_manager)):
                            raise RuntimeError("Modell belegt; keine Warteschlange")
                        return self.send_json(job_manager.start(payload["role_id"], payload["revision"]), 202)
                    if self.path == "/api/candidate-render-job":
                        payload = self.payload()
                        if (not isinstance(payload, dict)
                                or set(payload) != {"role_id", "speech_id", "kind", "revision"}):
                            raise ValueError("Kandidatenrenderstart enthält unbekannte oder fehlende Felder")
                        if any(manager.status().get("status") == "running" for manager in
                               (job_manager, text_audition_job_manager)):
                            raise RuntimeError("Modell belegt; keine Warteschlange")
                        return self.send_json(candidate_job_manager.start(payload["role_id"], payload["speech_id"],
                                                                          payload["kind"], payload["revision"]), 202)
                    if self.path == "/api/text-audition-job":
                        payload = self.payload()
                        if (not isinstance(payload, dict) or
                                set(payload) not in ({"speech_id", "variants", "revision", "seed"},
                                                     {"speech_id", "variants", "revision", "seed", "mode"})):
                            raise ValueError("Ausspracheauftrag braucht speech_id, variants, revision, seed und optional mode")
                        mode = payload.get("mode", "original")
                        if mode not in {"original", "role"}:
                            raise ValueError("Unbekannter Hörprobenmodus")
                        if any(manager.status().get("status") == "running" for manager in
                               (job_manager, candidate_job_manager, text_audition_job_manager)):
                            raise RuntimeError("Modell belegt; keine Warteschlange")
                        return self.send_json(text_audition_job_manager.start(payload["speech_id"], payload["variants"],
                                                                              payload["revision"], payload["seed"], mode=mode), 202)
                self.respond(404, b"", "text/plain")
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, 409)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)

        def log_message(self, format, *args):
            pass
    return Handler


def parse_args():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, default=root.parent / "work/mi1_manifest_v1.draft.json")
    parser.add_argument("--dubbing-root", type=Path, default=default_dubbing_root(root))
    parser.add_argument("--data-root", type=Path)
    review_render = parser.add_mutually_exclusive_group()
    review_render.add_argument("--review-enable-render", action="store_true", help="Legacy-Kompatibilitätsflag; echtes Runreview rendert standardmäßig")
    review_render.add_argument("--review-read-only", action="store_true", help="Review-Renderer ausdrücklich deaktivieren")
    parser.add_argument("--review-ane-worker-config", type=Path)
    parser.add_argument("--review-manifest", type=Path, help="Vorhandenes Rendermanifest für Wizardreview")
    parser.add_argument("--review-run-root", type=Path, help="Vorhandener Run mit Belegen; startet keine Synthese")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--validate-export", type=Path)
    parser.add_argument("--export", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.validate_export:
        validate_export(json.loads(args.validate_export.read_text(encoding="utf-8")), require_complete=True)
        print("Manifestbaustein ist vollständig und gültig.")
        return 0
    data_root = args.data_root or args.dubbing_root / "voice_anchor_wizard_data"
    wizard = Wizard(args.draft, args.dubbing_root, data_root)
    wizard.review_manifest_path = args.review_manifest
    wizard.review_run_root = args.review_run_root
    if args.review_enable_render and not (args.review_manifest and args.review_run_root):
        raise ValueError("Review-Rendering benötigt --review-manifest und --review-run-root")
    wizard.review_render_enabled = bool(args.review_manifest and args.review_run_root and not args.review_read_only)
    wizard.review_ane_config = json.loads(args.review_ane_worker_config.read_text()) if args.review_ane_worker_config else None
    if args.summary:
        state = wizard.public_state()
        print(json.dumps({k: state[k] for k in ("source_manifest_sha256", "render_dialog_count", "counts")}, ensure_ascii=False, indent=2))
        return 0
    if args.export:
        print(wizard.write_export())
        return 0
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(wizard, Path(__file__).resolve().parent, RenderJobManager(wizard)))
    url = f"http://127.0.0.1:{server.server_port}/"
    print("MI1 Rollenanker-Wizard:", url, flush=True)
    print("Zustand:", wizard.state_path, flush=True)
    if not args.no_browser:
        import webbrowser
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
