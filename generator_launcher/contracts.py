"""Versioned, model-free launcher contracts. No legacy evidence is reinterpreted.

Only this module's explicit v1 projections are eligible for local audio reuse.
Callers must resolve effective render settings and verify component bytes first.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys
from dataclasses import dataclass, field
from typing import Any
import uuid

SCHEMA_VERSION = 1
APP_NAME = "MI1PatchLauncher"


class ContractError(ValueError):
    """Incomplete, unsupported, unsafe or incorrectly bound launcher evidence."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ContractError("contract must contain finite JSON values") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _fields(value: Any, required: set[str], optional: set[str] = frozenset()) -> None:
    _require(isinstance(value, dict) and required <= value.keys()
             and value.keys() <= required | optional, "missing or unknown contract fields")


def _text(value: Any) -> None:
    _require(isinstance(value, str) and bool(value.strip()), "nonempty string required")


def _hash(value: Any) -> None:
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
             "lowercase SHA-256 required")


def _uuid(value: Any) -> None:
    try:
        _require(isinstance(value, str) and str(uuid.UUID(value)) == value,
                 "canonical UUID required")
    except (ValueError, AttributeError) as exc:
        raise ContractError("canonical UUID required") from exc


def relative_path(value: Any) -> str:
    """Portable relative POSIX file/directory path, safe on Windows too."""
    _text(value)
    _require(not value.startswith("/") and "\\" not in value and ":" not in value,
             "absolute, drive and backslash paths forbidden")
    for part in value.split("/"):
        _require(part not in {"", ".", ".."} and not part.endswith((" ", "."))
                 and not any(ord(c) < 32 or c in '<>"|?*' for c in part), "unsafe path segment")
        name = part.split(".", 1)[0].upper()
        _require(name not in {"CON", "PRN", "AUX", "NUL"}
                 and re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", name) is None,
                 "Windows device path forbidden")
    return value


def resolve_relative(root: str | Path, relative: str, *, must_exist: bool = False) -> Path:
    """Confine to root and reject symlink components (including internal links)."""
    relative_path(relative)
    base = Path(root).resolve(strict=True)
    _require(base.is_dir(), "work root must be a directory")
    path = base
    for part in relative.split("/"):
        path = path / part
        _require(not path.is_symlink(), "symlink artifacts forbidden")
    try:
        resolved = path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise ContractError("artifact path cannot be resolved") from exc
    _require(resolved != base and resolved.is_relative_to(base), "artifact escapes work root")
    return resolved


def version_provenance(manifest_id: str, manifest_revision: int, manifest_sha256: str,
                       *, version_id: str) -> dict:
    result = {"manifest_id": manifest_id, "manifest_revision": manifest_revision,
              "manifest_sha256": manifest_sha256, "version_id": version_id}
    _version(result)
    return result


def _version(value: Any) -> None:
    _fields(value, {"manifest_id", "manifest_revision", "manifest_sha256", "version_id"})
    _text(value["manifest_id"])
    _require(type(value["manifest_revision"]) is int and value["manifest_revision"] > 0,
             "positive manifest revision required")
    _hash(value["manifest_sha256"])
    _uuid(value["version_id"])


def _artifact(value: Any) -> None:
    _fields(value, {"artifact_id", "path", "sha256"})
    _text(value["artifact_id"])
    relative_path(value["path"])
    _hash(value["sha256"])


def validate_state(value: dict) -> dict:
    """Validate exact project/setup/run/review v1 shape; return a detached copy."""
    _require(isinstance(value, dict) and type(value.get("schema_version")) is int
             and value["schema_version"] == SCHEMA_VERSION, "unsupported state schema")
    kind = value.get("kind")
    common = {"schema_version", "kind", "project_id"}
    shapes = {
        "project": {"paths", "version"},
        "setup": {"setup_id", "recipe_sha256", "status", "foreign_artifacts"},
        "run": {"run_id", "setup_sha256", "version", "status", "receipts"},
        "review": {"review_id", "run_id", "version", "status", "bindings"},
    }
    _require(isinstance(kind, str) and kind in shapes, "unknown state kind")
    _fields(value, common | shapes[kind])
    _uuid(value["project_id"])
    if "version" in value:
        _version(value["version"])
    if kind == "project":
        paths = value["paths"]
        _fields(paths, {"input", "foreign", "generated", "runs", "reviews"})
        for path in paths.values():
            relative_path(path)
        entries = [p.casefold() for p in paths.values()]
        _require(all(a != b and not a.startswith(b + "/") and not b.startswith(a + "/")
                     for i, a in enumerate(entries) for b in entries[i + 1:]),
                 "input, foreign and own artifact namespaces must be separate")
    else:
        _require(value["status"] in {"pending", "running", "completed", "failed"}, "invalid status")
        _uuid(value[{"setup": "setup_id", "run": "run_id", "review": "review_id"}[kind]])
        if kind == "setup":
            _hash(value["recipe_sha256"])
            _require(isinstance(value["foreign_artifacts"], list), "artifact list required")
            for artifact in value["foreign_artifacts"]:
                _artifact(artifact)
            ids = [a["artifact_id"] for a in value["foreign_artifacts"]]
            _require(len(ids) == len(set(ids)), "duplicate foreign artifact ID")
        elif kind == "run":
            _hash(value["setup_sha256"])
            _require(isinstance(value["receipts"], list), "receipt paths required")
            for path in value["receipts"]:
                relative_path(path)
            _require(len(value["receipts"]) == len(set(value["receipts"])), "duplicate receipt path")
        else:
            _uuid(value["run_id"])
            _require(isinstance(value["bindings"], list), "review bindings required")
            for binding in value["bindings"]:
                _fields(binding, {"dialog_id", "synthesis_key", "qa_key", "audio_sha256", "receipt_path"})
                _text(binding["dialog_id"])
                for key in ("synthesis_key", "qa_key", "audio_sha256"):
                    _hash(binding[key])
                relative_path(binding["receipt_path"])
            ids = [b["dialog_id"] for b in value["bindings"]]
            _require(len(ids) == len(set(ids)), "duplicate review dialogue")
    return json.loads(canonical_json(value))


def _model(value: dict) -> dict:
    """Locations and QA models are not synthesis inputs; file order is retained."""
    for key in ("model_id", "version", "files"):
        _require(key in value, "model identity incomplete")
    _text(value["model_id"])
    _text(value["version"])
    _require(isinstance(value["files"], list) and bool(value["files"]), "model hashes required")
    hashes = []
    for item in value["files"]:
        _hash(item.get("sha256"))
        hashes.append(item["sha256"])
    return {"model_id": value["model_id"], "version": value["version"], "file_sha256": hashes}


def _runtime(value: dict) -> dict:
    keys = {"runtime_id", "runtime_version", "backend", "sample_rate_hz", "channels",
            "sample_format", "tts_parameters"}
    _fields(value, keys, {"artifacts"})
    for key in ("runtime_id", "runtime_version", "backend", "sample_format"):
        _text(value[key])
    for key in ("sample_rate_hz", "channels"):
        _require(type(value[key]) is int and value[key] > 0, "positive runtime format required")
    _require(isinstance(value["tts_parameters"], dict), "TTS parameters required")
    # Actual executable bytes are bound by the separate backend identity.
    # Their portable locations are not effective synthesis inputs.
    return json.loads(canonical_json({k:value[k] for k in keys}))


def _backend(value: dict) -> dict:
    _fields(value, {"profile_id", "runtime_contract", "effective_parameters", "assets"})
    _text(value["profile_id"])
    _text(value["runtime_contract"])
    _require(isinstance(value["effective_parameters"], dict) and bool(value["effective_parameters"]),
             "effective backend parameters required")
    _require(isinstance(value["assets"], list) and bool(value["assets"]),
             "actual runtime/decoder/backend asset hashes required")
    assets = []
    for asset in value["assets"]:
        _fields(asset, {"artifact_id", "sha256"})
        _text(asset["artifact_id"])
        _hash(asset["sha256"])
        assets.append(asset)
    _require(len({a["artifact_id"] for a in assets}) == len(assets), "duplicate backend asset")
    return {**value, "assets": sorted(assets, key=lambda a: a["artifact_id"])}


def _reference(value: dict | None, *, text_key: str) -> dict | None:
    if value is None:
        return None
    _hash(value.get("sha256"))
    _text(value.get(text_key))
    return {"audio_sha256": value["sha256"], "text": value[text_key]}


def synthesis_projection(dialogue: dict, attempt: dict, *, backend: dict) -> dict:
    """Project an already validated/resolved render attempt, never its fingerprint.

    backend is the *actual bound worker*, with all synthesis-affecting executable,
    decoder and backend asset hashes, not the dispatch/parallel profile selection.
    """
    try:
        _require(dialogue["processing"] == "render" and dialogue["dialog_id"] == attempt["dialog_id"],
                 "render dialogue binding required")
        _text(dialogue["dialog_id"])
        _text(dialogue["role_id"])
        _text(attempt["target_text"])
        _require(type(attempt["seed"]) is int and attempt["seed"] >= 0, "resolved seed required")
        _hash(dialogue["original_audio"]["sha256"])
        _hash(dialogue["original_text"]["sha256"])
        inputs = []
        for part in attempt["input_parts"]:
            if part["kind"] != "german_prewarm" or "generation_fingerprint" not in part:
                _hash(part["sha256"])
                inputs.append({"kind": part["kind"], "sha256": part["sha256"]})
            else:
                inputs.append({"kind": "german_prewarm", "source": "generated_recipe"})
        prewarm = attempt["prewarm"]
        if prewarm is not None and "generation_fingerprint" not in prewarm and "fingerprint" not in prewarm:
            prewarm = _reference(prewarm, text_key="text")
        elif prewarm is not None:
            _hash(prewarm["reference_audio"]["sha256"])
            _hash(prewarm["text_sha256"])
            prewarm = {"source": "generated_recipe", "reference_mode": prewarm["reference_mode"],
                       "reference_audio_sha256": prewarm["reference_audio"]["sha256"],
                       "text": prewarm["text"], "text_sha256": prewarm["text_sha256"],
                       "seed": prewarm["seed"], "runtime": _runtime(prewarm["runtime"]),
                       "tts_model": _model(prewarm["models"]["tts"]),
                       "target_duration_ms": prewarm["target_duration_ms"]}
        # Thresholds/strategy choose attempts; only the resulting recipe is bound.
        policy_keys = {"composite_join_silence_ms", "composite_target_duration_ms",
                       "composite_max_duration_ms", "prewarm_target_duration_ms", "prompt_order"}
        policy = {k: attempt["project_policy"][k] for k in sorted(policy_keys)}
        projection = {
            "dialog_id": dialogue["dialog_id"], "role_id": dialogue["role_id"],
            "target_text": attempt["target_text"], "seed": attempt["seed"],
            "original_audio_sha256": dialogue["original_audio"]["sha256"],
            "original_text_sha256": dialogue["original_text"]["sha256"],
            "method": attempt["method"], "reference_mode": attempt["reference_mode"],
            "runtime_mode": attempt["runtime_mode"], "input_parts": inputs,
            "prompt_parts": attempt["prompt_parts"],
            "fixed_anchor": _reference(attempt["fixed_anchor"], text_key="prompt_text"),
            "prewarm": prewarm, "performance_direction": attempt["performance_direction"],
            "performance_prompt_missing": attempt["performance_prompt_missing"],
            "runtime": _runtime(attempt["runtime"]), "tts_model": _model(attempt["models"]["tts"]),
            "backend": _backend(backend), "effective_policy": policy,
        }
        return _identity("synthesis", projection)
    except (KeyError, TypeError) as exc:
        raise ContractError("incomplete effective synthesis projection") from exc


def qa_projection(*, audio_sha256: str, expected_text: str, allowed_variants: list,
                  content_type: str, original_duration_ms: int, model: dict,
                  decoder: dict, rules: dict) -> dict:
    _hash(audio_sha256)
    _require(isinstance(expected_text, str) and isinstance(allowed_variants, list)
             and all(isinstance(t, str) for t in allowed_variants), "QA text required")
    _require(content_type in {"speech", "nonverbal", "mixed", "unknown"}, "invalid content type")
    _require(type(original_duration_ms) is int and original_duration_ms >= 0, "QA duration required")
    _require(isinstance(rules, dict) and bool(rules), "versioned QA rules required")
    for key in ("policy_id", "policy_version", "normalization_version"):
        _text(rules.get(key))
    return _identity("qa", {"audio_sha256": audio_sha256, "expected_text": expected_text,
                            "allowed_variants": allowed_variants, "content_type": content_type,
                            "original_duration_ms": original_duration_ms, "model": _model(model),
                            "decoder": _backend(decoder), "rules": rules})


def _identity(kind: str, inputs: dict) -> dict:
    body = {"schema_version": SCHEMA_VERSION, "kind": kind, "inputs": inputs}
    result = json.loads(canonical_json({**body, "key": digest(body)}))
    _validate_identity(result, kind)
    return result


def _model_identity(value: dict) -> None:
    _fields(value, {"model_id", "version", "file_sha256"})
    _text(value["model_id"])
    _text(value["version"])
    _require(isinstance(value["file_sha256"], list) and bool(value["file_sha256"]), "model hashes required")
    for item in value["file_sha256"]:
        _hash(item)


def _reference_identity(value: dict | None) -> None:
    if value is not None:
        _fields(value, {"audio_sha256", "text"})
        _hash(value["audio_sha256"])
        _text(value["text"])


def _synthesis_inputs(value: dict) -> None:
    _fields(value, {"dialog_id", "role_id", "target_text", "seed", "original_audio_sha256",
                    "original_text_sha256", "method", "reference_mode", "runtime_mode", "input_parts",
                    "prompt_parts", "fixed_anchor", "prewarm", "performance_direction",
                    "performance_prompt_missing", "runtime", "tts_model", "backend", "effective_policy"})
    for key in ("dialog_id", "role_id", "target_text"):
        _text(value[key])
    for key in ("original_audio_sha256", "original_text_sha256"):
        _hash(value[key])
    _require(type(value["seed"]) is int and value["seed"] >= 0, "resolved seed required")
    _require(value["method"] in {"standard", "without_emotion", "without_fixed_anchor", "main_anchor_only"},
             "unsupported method")
    _require(value["reference_mode"] in {"fixed_anchor", "line_original"}
             and value["runtime_mode"] in {"combined", "legacy_isolated"}, "unsupported reference/runtime mode")
    _require(type(value["performance_prompt_missing"]) is bool, "prompt state required")
    _require(value["performance_direction"] is None or isinstance(value["performance_direction"], str),
             "invalid performance direction")
    _runtime(value["runtime"])
    _model_identity(value["tts_model"])
    _backend(value["backend"])
    _reference_identity(value["fixed_anchor"])
    prewarm = value["prewarm"]
    if prewarm is not None and prewarm.get("source") == "generated_recipe":
        _fields(prewarm, {"source", "reference_mode", "reference_audio_sha256", "text", "text_sha256",
                          "seed", "runtime", "tts_model", "target_duration_ms"})
        _require(prewarm["reference_mode"] == "line_original", "invalid generated prewarm reference")
        _hash(prewarm["reference_audio_sha256"])
        _text(prewarm["text"])
        _hash(prewarm["text_sha256"])
        _require(hashlib.sha256(prewarm["text"].encode("utf-8")).hexdigest() == prewarm["text_sha256"],
                 "prewarm text hash mismatch")
        _require(type(prewarm["seed"]) is int and prewarm["seed"] >= 0, "prewarm seed required")
        _runtime(prewarm["runtime"])
        _model_identity(prewarm["tts_model"])
        _require(isinstance(prewarm["target_duration_ms"], list) and len(prewarm["target_duration_ms"]) == 2
                 and all(type(n) is int and n > 0 for n in prewarm["target_duration_ms"]),
                 "prewarm duration range required")
    else:
        _reference_identity(prewarm)
    _require(isinstance(value["input_parts"], list) and bool(value["input_parts"]), "audio inputs required")
    for part in value["input_parts"]:
        _require(part.get("kind") in {"main_anchor", "line_original", "german_prewarm"}, "unknown input kind")
        if part.get("source") == "generated_recipe":
            _fields(part, {"kind", "source"})
            _require(part["kind"] == "german_prewarm" and prewarm is not None
                     and prewarm.get("source") == "generated_recipe", "generated prewarm recipe missing")
        else:
            _fields(part, {"kind", "sha256"})
            _hash(part["sha256"])
    _require(isinstance(value["prompt_parts"], list), "prompt parts required")
    for part in value["prompt_parts"]:
        _fields(part, {"input_kind", "text"})
        _require(part["input_kind"] in {"main_anchor", "line_original", "german_prewarm"}, "unknown prompt kind")
        _text(part["text"])
    policy = value["effective_policy"]
    _fields(policy, {"composite_join_silence_ms", "composite_target_duration_ms",
                     "composite_max_duration_ms", "prewarm_target_duration_ms", "prompt_order"})
    canonical_json(policy)


def _validate_identity(value: dict, kind: str) -> None:
    _fields(value, {"schema_version", "kind", "inputs", "key"})
    _require(type(value["schema_version"]) is int and value["schema_version"] == SCHEMA_VERSION
             and value["kind"] == kind and isinstance(value["inputs"], dict), "identity schema mismatch")
    _hash(value["key"])
    _require(value["key"] == digest({k: v for k, v in value.items() if k != "key"}), "identity hash mismatch")
    if kind == "synthesis":
        _synthesis_inputs(value["inputs"])
    elif kind == "qa":
        inputs = value["inputs"]
        _fields(inputs, {"audio_sha256", "expected_text", "allowed_variants", "content_type",
                         "original_duration_ms", "model", "decoder", "rules"})
        _hash(inputs["audio_sha256"])
        _model_identity(inputs["model"])
        _backend(inputs["decoder"])


@dataclass(frozen=True)
class LocalInstallation:
    installation_id: str
    receipt_key: bytes = field(repr=False)

    def __post_init__(self):
        _uuid(self.installation_id)
        _require(isinstance(self.receipt_key, bytes) and len(self.receipt_key) == 32,
                 "local receipt key must be 32 random bytes")


def installation_config_path(*, platform: str | None = None,
                             home: Path | None = None, environ: dict | None = None) -> Path:
    platform = platform or sys.platform
    home = Path.home() if home is None else Path(home)
    environ = os.environ if environ is None else environ
    if platform == "darwin":
        base = home / "Library" / "Application Support"
    elif platform == "win32":
        _require(bool(environ.get("LOCALAPPDATA")), "Windows LOCALAPPDATA unavailable")
        base = Path(environ["LOCALAPPDATA"])
    else:
        base = Path(environ.get("XDG_CONFIG_HOME", home / ".config"))
    _require(base.is_absolute(), "OS app configuration path must be absolute")
    return base / APP_NAME / "installation.json"


def load_existing_installation(work_root: str | Path) -> LocalInstallation:
    """Read the existing host identity without creating files or directories."""
    root = Path(work_root).resolve(strict=True)
    path = installation_config_path()
    _require(not path.resolve().is_relative_to(root), "installation identity must be outside work root")
    for component in (path, *path.parents):
        _require(not component.is_symlink(), "symlink local configuration forbidden")
    value = _read_json(path)
    _fields(value, {"schema_version", "kind", "installation_id", "receipt_key_hex"})
    _require(type(value["schema_version"]) is int and value["schema_version"] == SCHEMA_VERSION
             and value["kind"] == "local_installation", "unsupported local identity")
    _hash(value["receipt_key_hex"])
    return LocalInstallation(value["installation_id"], bytes.fromhex(value["receipt_key_hex"]))


def load_or_create_installation(work_root: str | Path, *, config_path: Path | None = None) -> LocalInstallation:
    """Load an OS-local identity. Tests may inject a separate temporary config path.

    Never reset malformed existing evidence or infer identity from portable data.
    """
    root = Path(work_root).resolve(strict=True)
    path = Path(config_path) if config_path is not None else installation_config_path()
    _require(path.is_absolute(), "local configuration must be absolute")
    _require(not path.resolve().is_relative_to(root), "installation identity must be outside work root")
    for component in (path, *path.parents):
        _require(not component.is_symlink(), "symlink local configuration forbidden")
    path.parent.mkdir(parents=True, exist_ok=True)
    new = {"schema_version": SCHEMA_VERSION, "kind": "local_installation",
           "installation_id": str(uuid.uuid4()), "receipt_key_hex": secrets.token_hex(32)}
    # Publish complete bytes atomically; a concurrent creator keeps its identity.
    temporary = path.with_name(f".installation-{uuid.uuid4()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_json(new) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    value = _read_json(path)
    _fields(value, {"schema_version", "kind", "installation_id", "receipt_key_hex"})
    _require(type(value["schema_version"]) is int and value["schema_version"] == SCHEMA_VERSION
             and value["kind"] == "local_installation", "unsupported local identity")
    _hash(value["receipt_key_hex"])
    return LocalInstallation(value["installation_id"], bytes.fromhex(value["receipt_key_hex"]))


def _read_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    try:
        _require(path.is_file() and not path.is_symlink(), "regular JSON file required")
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ContractError("invalid local evidence JSON") from exc


def generation_receipt(installation: LocalInstallation, synthesis: dict, version: dict,
                       *, project_id: str, run_id: str, audio_path: str, audio_sha256: str) -> dict:
    _validate_identity(synthesis, "synthesis")
    _version(version)
    _uuid(project_id)
    _uuid(run_id)
    relative_path(audio_path)
    _hash(audio_sha256)
    body = {"schema_version": SCHEMA_VERSION, "kind": "generation_receipt",
            "installation_id": installation.installation_id, "project_id": project_id,
            "run_id": run_id, "version": version, "synthesis": synthesis,
            "audio": {"path": audio_path, "sha256": audio_sha256}}
    signature = hmac.new(installation.receipt_key, canonical_json(body), hashlib.sha256).hexdigest()
    return json.loads(canonical_json({**body, "local_signature": signature}))


def authorize_audio(receipt: dict, installation: LocalInstallation, synthesis: dict,
                    *, project_id: str, work_root: str | Path, verify_audio: bool = True) -> Path:
    """Fail closed for legacy/foreign/incomplete/modified inputs or WAVs.

    Version may differ: callers must create a new run/review version binding.
    The source receipt is immutable and retains its own original version.
    """
    _require(type(verify_audio) is bool, "verify_audio must be boolean")
    _fields(receipt, {"schema_version", "kind", "installation_id", "project_id", "run_id",
                      "version", "synthesis", "audio", "local_signature"})
    _require(type(receipt["schema_version"]) is int and receipt["schema_version"] == SCHEMA_VERSION
             and receipt["kind"] == "generation_receipt", "unsupported generation receipt")
    _uuid(project_id)
    _uuid(receipt["run_id"])
    _version(receipt["version"])
    _validate_identity(synthesis, "synthesis")
    _validate_identity(receipt["synthesis"], "synthesis")
    _require(receipt["installation_id"] == installation.installation_id, "foreign host")
    _require(receipt["project_id"] == project_id, "foreign project")
    _require(receipt["synthesis"] == synthesis, "synthesis inputs changed")
    _hash(receipt["local_signature"])
    body = {k: v for k, v in receipt.items() if k != "local_signature"}
    signature = hmac.new(installation.receipt_key, canonical_json(body), hashlib.sha256).hexdigest()
    _require(hmac.compare_digest(signature, receipt["local_signature"]), "local receipt signature mismatch")
    _fields(receipt["audio"], {"path", "sha256"})
    _hash(receipt["audio"]["sha256"])
    path = resolve_relative(work_root, receipt["audio"]["path"], must_exist=True)
    _require(path.is_file(), "audio file missing")
    if verify_audio:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        _require(hasher.hexdigest() == receipt["audio"]["sha256"], "audio hash mismatch")
    return path
