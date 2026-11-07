"""Static loading and resolution for executable dubbing manifests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from validate_dubbing_manifest import validate


class ManifestError(ValueError):
    """The input is not a complete, valid executable manifest."""


def canonical_json(value: Any) -> bytes:
    """Return stable UTF-8 JSON bytes used for all plan and input hashes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Load JSON from disk and run static schema and semantic validation."""
    def no_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ManifestError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        with Path(path).open("r", encoding="utf-8-sig") as stream:
            data = json.load(stream, object_pairs_hook=no_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest: {exc}") from exc
    return resolve_manifest(data)


def resolve_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate and resolve the included dialogue set to selected variants/roles."""
    if not isinstance(manifest, dict):
        raise ManifestError("manifest root must be an object")
    status = manifest.get("status")
    if isinstance(status, str) and status in {"blocked", "blocked_pending_required_evidence", "incomplete"}:
        raise ManifestError(f"manifest is not executable (status={manifest['status']})")
    issues = validate(manifest)
    if issues:
        details = "; ".join(f"{issue.code} at {issue.location}: {issue.message}" for issue in issues[:8])
        raise ManifestError(f"manifest validation failed: {details}")
    scope = manifest["scope"]
    if scope.get("selection_complete") is not True:
        raise ManifestError("scope selection is incomplete")
    dialogues = {row["dialog_id"]: row for row in manifest["dialogues"]}
    roles = {row["role_id"]: row for row in manifest["roles"]}
    resolved = []
    for dialog_id in scope["included_dialog_ids"]:
        row = dialogues.get(dialog_id)
        if row is None:
            raise ManifestError(f"included dialogue is missing: {dialog_id}")
        selected = next((v for v in row["text_variants"] if v["variant_id"] == row["selected_variant_id"]), None)
        if selected is None and not (manifest.get("manifest_profile") == "local_render" and row["processing"] == "copy_original" and row["selected_variant_id"] is None and not row["text_variants"]):
            raise ManifestError(f"selected text variant is missing: {dialog_id}")
        role = roles.get(row.get("role_id")) if row["processing"] == "render" else None
        if row["processing"] == "render" and role is None:
            raise ManifestError(f"render dialogue has no resolvable role: {dialog_id}")
        resolved.append({"dialogue": row, "variant": selected, "role": role})
    return {"manifest": manifest, "manifest_sha256": sha256(canonical_json(manifest)), "dialogues": resolved}
