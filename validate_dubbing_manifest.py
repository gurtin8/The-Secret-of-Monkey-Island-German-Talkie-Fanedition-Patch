#!/usr/bin/env python3
"""Validate the v1 executable dubbing manifest without third-party packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import unicodedata
import wave

SCHEMA = Path(__file__).parent / "docs/schema/dubbing-manifest-v1.schema.json"
STAGES = ("prepare", "render", "qa", "build")
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10)),
            *(f"{prefix}{digit}" for prefix in ("com", "lpt") for digit in "¹²³")}


class Issue:
    def __init__(self, code: str, location: str, message: str):
        self.code, self.location, self.message = code, location, message

    def as_dict(self):
        return {"code": self.code, "location": self.location, "message": self.message}


def pointer(base, key):
    return base + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _unique_key(value):
    """Hashable structural key with JSON types kept distinct and linear cost."""
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, (int, float)):
        # JSON numbers compare by value, so 1 and 1.0 share a key.
        return ("number", value)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_unique_key(item) for item in value))
    if isinstance(value, dict):
        return ("object", tuple(sorted((key, _unique_key(item)) for key, item in value.items())))
    return (type(value).__name__, repr(value))


def schema_errors(value, schema, root, at="", limit=100):
    """Evaluate the JSON Schema vocabulary used by this project's v1 schema."""
    errors = []

    def check(obj, rule, loc):
        if len(errors) >= limit:
            return
        if "$ref" in rule:
            ref = root
            for part in rule["$ref"].split("/")[1:]:
                ref = ref[part.replace("~1", "/").replace("~0", "~")]
            check(obj, ref, loc)
        types = rule.get("type")
        if types:
            types = types if isinstance(types, list) else [types]
            valid = {"object": lambda x: isinstance(x, dict), "array": lambda x: isinstance(x, list),
                     "string": lambda x: isinstance(x, str), "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
                     "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
                     "boolean": lambda x: isinstance(x, bool), "null": lambda x: x is None}
            if not any(valid[t](obj) for t in types):
                errors.append(Issue("E_SCHEMA", loc or "/", "wrong JSON type"))
                return
        if "const" in rule and obj != rule["const"] or "enum" in rule and obj not in rule["enum"]:
            errors.append(Issue("E_SCHEMA", loc or "/", "value is not permitted"))
        if isinstance(obj, dict):
            if len(obj) > rule.get("maxProperties", float("inf")):
                errors.append(Issue("E_SCHEMA", loc or "/", "too many object properties"))
            for key in rule.get("required", []):
                if key not in obj:
                    errors.append(Issue("E_SCHEMA", pointer(loc, key), "required property is missing"))
            props = rule.get("properties", {})
            for key, item in obj.items():
                if key in props:
                    check(item, props[key], pointer(loc, key))
                elif rule.get("additionalProperties") is False:
                    errors.append(Issue("E_SCHEMA", pointer(loc, key), "unknown property"))
                elif isinstance(rule.get("additionalProperties"), dict):
                    check(item, rule["additionalProperties"], pointer(loc, key))
        if isinstance(obj, list):
            if len(obj) < rule.get("minItems", 0) or len(obj) > rule.get("maxItems", float("inf")):
                errors.append(Issue("E_SCHEMA", loc or "/", "array length is invalid"))
            if rule.get("uniqueItems"):
                seen = set()
                for item in obj:
                    key = _unique_key(item)
                    if key in seen:
                        errors.append(Issue("E_SCHEMA", loc or "/", "array items must be unique"))
                        break
                    seen.add(key)
            prefix = rule.get("prefixItems", [])
            for i, item in enumerate(obj):
                sub = prefix[i] if i < len(prefix) else rule.get("items")
                if sub is False:
                    errors.append(Issue("E_SCHEMA", pointer(loc, i), "extra array item"))
                elif isinstance(sub, dict):
                    check(item, sub, pointer(loc, i))
        if isinstance(obj, str):
            if len(obj) < rule.get("minLength", 0) or len(obj) > rule.get("maxLength", float("inf")):
                errors.append(Issue("E_SCHEMA", loc or "/", "string length is invalid"))
            if "pattern" in rule and re.search(rule["pattern"], obj) is None:
                errors.append(Issue("E_SCHEMA", loc or "/", "string does not match pattern"))
            if rule.get("format") == "uri" and not re.match(r"^https://[^/\s]+(?:/[^\s]*)?$", obj):
                errors.append(Issue("E_SCHEMA", loc or "/", "HTTPS URI is invalid"))
        if isinstance(obj, (int, float)) and not isinstance(obj, bool):
            if obj < rule.get("minimum", -float("inf")) or obj > rule.get("maximum", float("inf")):
                errors.append(Issue("E_SCHEMA", loc or "/", "number outside allowed range"))
        for sub in rule.get("allOf", []):
            check(obj, sub, loc)
        if "if" in rule:
            branch = "then" if not schema_errors(obj, rule["if"], root, loc, 1) else "else"
            check(obj, rule.get(branch, {}), loc)
        if "not" in rule and not schema_errors(obj, rule["not"], root, loc, 1):
            errors.append(Issue("E_SCHEMA", loc or "/", "forbidden combination"))
        if "anyOf" in rule and all(schema_errors(obj, sub, root, loc, 1) for sub in rule["anyOf"]):
            errors.append(Issue("E_SCHEMA", loc or "/", "none of the alternatives matches"))

    check(value, schema, at)
    return errors[:limit]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def version_parts(value: str):
    return tuple(int(x) for x in value.split(".")) if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value) else ()


def host_facts():
    system = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(platform.system())
    arch = {"AMD64": "x86_64", "x86_64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if system == "macos":
        version = platform.mac_ver()[0]
    elif system == "windows":
        version = platform.win32_ver()[1]
    else:
        version = platform.release().split("-", 1)[0]
    features = set()
    if system == "linux":
        try:
            data = Path("/proc/cpuinfo").read_text(encoding="utf-8")
            per_cpu = [set(line.split()) for line in re.findall(r"^(?:flags|Features)\s*:\s*(.*)$", data, re.M)]
            features = set.intersection(*per_cpu) if per_cpu else set()
        except (OSError, AttributeError):
            pass
    elif system == "macos":
        for name in ("machdep.cpu.features", "machdep.cpu.leaf7_features", "hw.optional.arm.FEAT_AES"):
            try:
                result = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=2, check=True)
                features.update(result.stdout.casefold().split())
                if result.stdout.strip() == "1":
                    features.add(name.casefold())
            except (OSError, subprocess.SubprocessError):
                pass
    ram = None
    if system == "linux":
        try:
            ram = int(re.search(r"^MemTotal:\s*(\d+) kB", Path("/proc/meminfo").read_text(), re.M).group(1)) * 1024
        except (OSError, AttributeError):
            pass
    elif system == "macos":
        try:
            ram = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2, check=True).stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    elif system == "windows":
        try:
            import ctypes
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            info = MemoryStatus()
            info.dwLength = ctypes.sizeof(info)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(info)):
                ram = info.ullTotalPhys
        except (AttributeError, OSError):
            pass
    return {"os_family": system, "os_version": version, "cpu_architecture": arch,
            "logical_cores": os.cpu_count(), "cpu_features": sorted(features), "system_ram_bytes": ram,
            "accelerator_api": "cpu", "accelerator_memory_bytes": None}


def canonical_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path or ":" in path or unicodedata.normalize("NFC", path) != path:
        return False
    for part in path.split("/"):
        if not part or part in (".", "..") or part[-1] in (".", " ") or part.casefold().split(".")[0] in RESERVED:
            return False
        if any(ord(char) < 32 or char in '<>"|?*' for char in part):
            return False
    return True


def path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def validate(manifest: dict, *, asset_root: Path | None = None, check_assets=False, check_probes=False, check_host=False, facts=None,
             max_hash_bytes: int | None = None, skip_content_hashes: bool = False):
    issues = []
    add = lambda code, loc, msg: issues.append(Issue(code, loc, msg))
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    issues.extend(schema_errors(manifest, schema, schema))
    if issues:
        return issues

    def ids(entries, field, loc):
        found = {}
        for i, entry in enumerate(entries):
            ident = entry[field]
            if ident in found:
                add("E_DUP_ID", f"{loc}/{i}/{field}", f"{ident} already defined at {found[ident]}")
            found[ident] = f"{loc}/{i}/{field}"
        return found

    local_render = manifest.get("manifest_profile") == "local_render"
    roles = manifest["roles"]
    dialogues = manifest["dialogues"]
    reqs = manifest["requirements"]
    profiles = manifest["hardware"]["profiles"]
    role_ids = ids(roles, "role_id", "/roles")
    dialog_ids = ids(dialogues, "dialog_id", "/dialogues")
    req_ids = ids(reqs, "component_id", "/requirements")
    ids(profiles, "profile_id", "/hardware/profiles")
    ids(manifest["game"]["required_inputs"]["patches"], "patch_id", "/game/required_inputs/patches")
    variants = [v for d in dialogues for v in d["text_variants"]]
    ids(variants, "variant_id", "/dialogues/*/text_variants")
    global_ids = {}
    for ident, loc in ((manifest["manifest_id"], "/manifest_id"),
                       (manifest["game"]["game_id"], "/game/game_id"),
                       (manifest["game"]["adapter_id"], "/game/adapter_id"),
                       (manifest["qa"]["policy_id"], "/qa/policy_id")):
        if ident in global_ids:
            add("E_DUP_ID", loc, f"{ident} already defined at {global_ids[ident]}")
        global_ids.setdefault(ident, loc)
    for group, field, base in ((roles, "role_id", "/roles"), (dialogues, "dialog_id", "/dialogues"),
                               (reqs, "component_id", "/requirements"), (profiles, "profile_id", "/hardware/profiles"),
                               (manifest["game"]["required_inputs"]["patches"], "patch_id", "/game/required_inputs/patches"),
                               (variants, "variant_id", "/dialogues/*/text_variants")):
        for index, item in enumerate(group):
            ident, loc = item[field], f"{base}/{index}/{field}"
            if ident in global_ids and global_ids[ident] != loc:
                add("E_DUP_ID", loc, f"{ident} already defined at {global_ids[ident]}")
            global_ids.setdefault(ident, loc)
    dmap = {d["dialog_id"]: d for d in dialogues}
    rmap = {r["role_id"]: r for r in roles}
    qmap = {r["component_id"]: r for r in reqs}

    # Treat every path as a POSIX package path, including nested output paths.
    paths = []
    files = []
    def register(path, loc, fingerprint=None, size=None, *, is_output=False, audio=None):
        if not canonical_path(path):
            add("E_PATH", loc, f"non-canonical portable relative path: {path}")
        paths.append((path, loc, is_output))
        if fingerprint:
            files.append((path, fingerprint, size, loc, audio))

    def asset(obj, loc):
        register(obj["path"], loc + "/path", obj["sha256"], None, audio=obj)
        if obj["media_type"] == "audio/wav" and any(k not in obj for k in ("duration_ms", "sample_rate_hz", "channels")):
            add("E_AUDIO", loc, "WAV asset needs duration, sample rate and channels")

    inputs = manifest["game"]["required_inputs"]
    for section in ("base_files", "final_files"):
        for i, item in enumerate(inputs[section]):
            register(item["path"], f"/game/required_inputs/{section}/{i}/path", item["sha256"], item["size_bytes"])
    for i, patch in enumerate(inputs["patches"]):
        for section in ("files", "result_files"):
            for j, item in enumerate(patch[section]):
                register(item["path"], f"/game/required_inputs/patches/{i}/{section}/{j}/path", item["sha256"], item["size_bytes"])
    if inputs["patches"]:
        last = {(x["path"], x["sha256"], x["size_bytes"]) for x in inputs["patches"][-1]["result_files"]}
        final = {(x["path"], x["sha256"], x["size_bytes"]) for x in inputs["final_files"]}
        if not final.issubset(last):
            add("E_PATCH_CHAIN", "/game/required_inputs/final_files", "final files must be present in last patch result with identical fingerprints")
    else:
        base = {(x["path"], x["sha256"], x["size_bytes"]) for x in inputs["base_files"]}
        final = {(x["path"], x["sha256"], x["size_bytes"]) for x in inputs["final_files"]}
        if not final.issubset(base):
            add("E_PATCH_CHAIN", "/game/required_inputs/final_files", "unpatched final files must match base files")

    for i, req in enumerate(reqs):
        loc = f"/requirements/{i}"
        if req["acquisition"] == "system":
            name = req["system_binary"]["executable_name"]
            if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name.casefold().split(".")[0] in RESERVED:
                add("E_PATH", loc + "/system_binary/executable_name", "unsafe executable name")
        for j, art in enumerate(req["artifacts"]):
            register(art["path"], f"{loc}/artifacts/{j}/path", art["sha256"], art["size_bytes"])
        if "executable_path" in req["probe"]:
            register(req["probe"]["executable_path"], loc + "/probe/executable_path")
        try:
            re.compile(req["probe"]["version_pattern"])
        except re.error:
            add("E_PROBE", loc + "/probe/version_pattern", "invalid regular expression")
        for dep in req["depends_on"]:
            if dep not in qmap:
                add("E_REF", loc + "/depends_on", f"unknown component {dep}")
            elif dep == req["component_id"]:
                add("E_DEPENDENCY", loc + "/depends_on", "component depends on itself")
            else:
                other = qmap[dep]
                if req["required"] and not other["required"]:
                    add("E_DEPENDENCY_REQUIRED", loc + "/depends_on", f"required component depends on optional component {dep}")
                for field in ("stages", "platforms", "architectures"):
                    if not set(req[field]).issubset(other[field]):
                        add("E_DEPENDENCY", loc + "/depends_on", f"{dep} does not cover all {field}")
        if req["acquisition"] == "download_on_first_run" and req["probe"].get("executable_path") and req["probe"]["executable_path"] not in {a["path"] for a in req["artifacts"]}:
            add("E_PROBE", loc + "/probe/executable_path", "probe executable must be a declared artifact")
    visiting, visited = set(), set()
    def visit(cid):
        if cid in visiting:
            add("E_DEPENDENCY_CYCLE", req_ids[cid], f"dependency cycle through {cid}")
            return
        if cid in visited:
            return
        visiting.add(cid)
        for dep in qmap[cid]["depends_on"]:
            if dep in qmap:
                visit(dep)
        visiting.remove(cid)
        visited.add(cid)
    for cid in qmap:
        visit(cid)

    for kind, model in manifest["models"].items():
        loc = f"/models/{kind}"
        comp = qmap.get(model["component_id"])
        if not local_render and (not comp or comp["kind"] != "model" or not comp["required"] or ("render" if kind == "tts" else "qa") not in comp["stages"]):
            add("E_MODEL", loc + "/component_id", "required model component for stage is missing")
        elif comp and comp["version"] != model["version"]:
            add("E_VERSION", loc + "/version", "model and component versions differ")
        for j, item in enumerate(model["files"]):
            asset(item, f"{loc}/files/{j}")
            if comp and (item["path"], item["sha256"]) not in {(a["path"], a["sha256"]) for a in comp["artifacts"]}:
                add("E_MODEL", f"{loc}/files/{j}", "model file is not bound to a matching requirement artifact")
    runtime = manifest["runtime"]
    execution = manifest.get("render_execution")
    if execution:
        expected_backend = {"gpu": "metal", "cpu": "cpu", "ane": "ane"}[execution["profile_id"]]
        if execution["operating_mode"] in {"single", "adaptive_ane"}:
            if execution["dispatch_profiles"] != [execution["profile_id"]]:
                add("E_BACKEND", "/render_execution/dispatch_profiles", "single mode must select exactly its named profile")
            if execution['operating_mode']=='adaptive_ane' and execution['profile_id']!='ane':
                add('E_BACKEND','/render_execution/profile_id','adaptive ANE requires the ANE profile')
        elif execution["dispatch_profiles"] != ["gpu", "ane"]:
            add("E_BACKEND", "/render_execution/dispatch_profiles", "parallel mode requires the ordered GPU and ANE profiles")
        if execution['operating_mode']=='adaptive_gpu_ane' and execution['profile_id']!='gpu':
            add('E_BACKEND','/render_execution/profile_id','adaptive GPU+ANE requires the GPU profile')
        if (execution["profile_id"] != "ane" and execution["operating_mode"] == "single"
                and execution["assets"]):
            add("E_BACKEND", "/render_execution/assets", "GPU/CPU selection cannot bind ANE-specific assets")
        if runtime["backend"] != expected_backend:
            add("E_BACKEND", "/runtime/backend", "runtime backend differs from selected profile")
        for i, item in enumerate(execution["assets"]):
            register(item["path"], f"/render_execution/assets/{i}/path", item["sha256"])
    runtime_comp = qmap.get(runtime["runtime_id"])
    if not local_render and (not runtime_comp or runtime_comp["kind"] != "runtime" or not runtime_comp["required"] or "render" not in runtime_comp["stages"]):
        add("E_RUNTIME", "/runtime/runtime_id", "required render runtime is missing")
    elif runtime_comp and runtime_comp["version"] != runtime["runtime_version"]:
        add("E_VERSION", "/runtime/runtime_version", "runtime and component versions differ")

    for name, item in runtime.get("artifacts", {}).items():
        asset(item, f"/runtime/artifacts/{name}")

    inc = manifest["scope"]["included_dialog_ids"]
    exc = manifest["scope"]["excluded"]
    exc_ids = ids(exc, "dialog_id", "/scope/excluded")
    if local_render:
        inventory = set(manifest["source_evidence"]["source_dialog_ids"])
        if inventory != set(inc) | set(exc_ids):
            add("E_SCOPE", "/scope", "all source IDs must have an explicit render, original-copy or exclusion decision")
        if any(item["reason"] != "deferred_by_user" or not item.get("note", "").strip() for item in exc):
            add("E_SCOPE", "/scope/excluded", "local source inventory permits only explicitly justified exclusions")
    if set(inc) != set(dialog_ids):
        add("E_SCOPE", "/scope/included_dialog_ids", "included IDs must equal dialogue IDs")
    if set(inc) & set(exc_ids):
        add("E_SCOPE", "/scope/excluded", "included and excluded IDs overlap")
    for i, item in enumerate(exc):
        if item.get("role_id") and item["role_id"] not in role_ids:
            add("E_REF", f"/scope/excluded/{i}/role_id", "unknown role")

    def text_hash(text, expected, loc):
        if digest(text.encode("utf-8")) != expected:
            add("E_TEXT_HASH", loc, "SHA-256 does not match exact UTF-8 text")

    role_by_id = {role["role_id"]: role for role in roles}
    short_by_role = {r: False for r in role_ids}
    fixed_anchor_by_role = {r: False for r in role_ids}
    prewarm_by_role = {r: False for r in role_ids}
    for i, d in enumerate(dialogues):
        loc = f"/dialogues/{i}"
        asset(d["original_audio"], loc + "/original_audio")
        text_hash(d["original_text"]["text"], d["original_text"]["sha256"], loc + "/original_text/sha256")
        choices = {v["variant_id"]: v for v in d["text_variants"]}
        selected = choices.get(d["selected_variant_id"])
        if not selected and not (local_render and d["processing"] == "copy_original" and d["selected_variant_id"] is None and not d["text_variants"]):
            add("E_REF", loc + "/selected_variant_id", "selected variant is absent")
        for j, v in enumerate(d["text_variants"]):
            fields = [v[k] for k in ("display_text", "tts_text", "qa_expected_text", "qa_allowed_variants")]
            serial = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if digest(serial) != v["sha256"]:
                add("E_TEXT_HASH", f"{loc}/text_variants/{j}/sha256", "variant SHA-256 does not match canonical JSON array")
            provenance = v["provenance"]
            if ("module_id" in provenance) != ("module_version" in provenance):
                add("E_VERSION", f"{loc}/text_variants/{j}/provenance", "module ID and version must occur together")
            if provenance["kind"] == "machine_translation" and "module_id" not in provenance:
                add("E_VERSION", f"{loc}/text_variants/{j}/provenance", "machine translation needs module ID and version")
        if d["processing"] == "render" and d["role_id"] not in role_ids:
            add("E_REF", loc + "/role_id", "rendered dialogue has no role")
        if d["processing"] == "render" and d["content_type"] == "speech" and selected and d["role_id"] in short_by_role:
            rid = d["role_id"]
            short = d["original_audio"].get("duration_ms", 0) < 3000 or len(selected["tts_text"].split()) < 4
            short_by_role[rid] |= short
            role = role_by_id[rid]
            no_anchor, no_prewarm = role.get("without_anchor", False), role.get("without_prewarm", False)
            legacy_prewarm = not no_prewarm and (short or role.get("prewarm_for_long_text", False))
            legacy_anchor = not no_anchor and (short or legacy_prewarm or role.get("use_voice_anchor_for_long_text", False))
            overrides = d.get("render_overrides", {})
            anchor_mode, prewarm_mode = overrides.get("anchor", "inherit"), overrides.get("prewarm", "inherit")
            anchor_reference = overrides.get("anchor_reference")
            prewarm_reference = overrides.get("prewarm_reference")
            use_anchor = legacy_anchor if anchor_mode == "inherit" else anchor_mode == "use"
            use_prewarm = legacy_prewarm if prewarm_mode == "inherit" else prewarm_mode == "use"
            if anchor_reference is not None:
                use_anchor = anchor_mode == "use"
            if prewarm_reference is not None:
                use_prewarm = prewarm_mode == "use"
            if anchor_reference is not None and use_anchor and prewarm_mode == "use" and prewarm_reference is None:
                add("E_PREWARM", loc + "/render_overrides/prewarm_reference", "alternate anchor use requires its paired prewarm reference when prewarm is enabled")
            fixed_anchor_by_role[rid] |= use_anchor
            # A per-dialogue prewarm reference is validated against its paired
            # anchor below; it does not consume or require role.prewarm.
            prewarm_by_role[rid] |= use_prewarm and prewarm_reference is None
            if use_anchor and anchor_reference is None and "main_anchor" not in role:
                add("E_ANCHOR", loc + "/render_overrides/anchor", "anchor use requires the selected role anchor")
        if d["performance_prompt"] == "original_audio_and_text" and d["original_text"]["verification"] not in ("exact", "inferred_reviewed"):
            add("E_PROMPT", loc + "/performance_prompt", "source text is not verified for audio pairing")
        if d["performance_prompt"] == "none" and d["processing"] == "render" and d["original_text"]["verification"] not in ("exact", "inferred_reviewed") and d["performance_prompt_missing_reason"] != "performance_prompt_missing":
            add("E_PROMPT", loc + "/performance_prompt_missing_reason", "unverified prompt needs missing reason")
        register(d["output_stem"], loc + "/output_stem", is_output=True)

    def portable_generated_anchor(anchor_obj, role_id):
        """Recognize a locally reconstructed, complete generated reference recipe."""
        key=hashlib.sha256(json.dumps(anchor_obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        source=manifest.get('source_evidence',{}).get('portable_reference_sources',{}).get(key)
        if not isinstance(source,dict) or source.get('kind')!='generated':return False
        from manifest_renderer.recipes import _source
        try:_source(source,set(dmap))
        except (ValueError,KeyError,TypeError):return False
        dialogue=dmap.get(source.get('dialog_id'))
        return bool(dialogue and dialogue.get('role_id')==role_id and dialogue.get('content_type')=='speech'
            and anchor_obj.get('prompt_text')==source['recipe']['tts_text']
            and anchor_obj.get('audio',{}).get('path','').startswith('portable-references/')
            and anchor_obj.get('selection_evidence')=='local_portable_recipe_reconstruction')

    def anchor(anchor_obj, role_id, loc, main=False):
        generated=portable_generated_anchor(anchor_obj,role_id)
        source_ids = [anchor_obj["anchor_dialog_id"], *anchor_obj["anchor_component_dialog_ids"]]
        if len(set(source_ids)) != len(source_ids):
            add("E_ANCHOR", loc, "anchor components must be distinct")
        if anchor_obj["kind"] == "single" and len(source_ids) != 1 or anchor_obj["kind"] == "composite" and not 2 <= len(source_ids) <= 8:
            add("E_ANCHOR", loc, "anchor component count does not match kind")
        source = []
        originals = anchor_obj.get("source_audio", [])
        if local_render and main and len(originals) != len(source_ids):
            add("E_ANCHOR", loc + "/source_audio", "materialized anchor needs all source audio receipts")
        for j, original_asset in enumerate(originals):
            asset(original_asset, f"{loc}/source_audio/{j}")
        for j, ident in enumerate(source_ids):
            d = dmap.get(ident)
            if not d:
                if not generated and not (local_render and main and ident.startswith("custom-anchor:") and j < len(originals)):
                    add("E_REF", loc + "/anchor_dialog_id", f"unknown anchor dialogue {ident}")
            elif d["role_id"] != role_id or d["content_type"] != "speech":
                add("E_ANCHOR", loc + "/anchor_dialog_id", f"anchor dialogue {ident} has wrong speaker/type")
            else:
                source.append(d)
                if not generated and local_render and main and j < len(originals) and (originals[j]["path"], originals[j]["sha256"]) != (d["original_audio"]["path"], d["original_audio"]["sha256"]):
                    add("E_ANCHOR", f"{loc}/source_audio/{j}", "anchor source receipt differs from original dialogue")
        asset(anchor_obj["audio"], loc + "/audio")
        text_hash(anchor_obj["prompt_text"], anchor_obj["prompt_text_sha256"], loc + "/prompt_text_sha256")
        if not generated and len(source) == len(source_ids):
            expected = " ".join(d["original_text"]["text"] for d in source)
            if anchor_obj["prompt_text"] != expected:
                add("E_PROMPT", loc + "/prompt_text", "anchor prompt text differs from ordered source texts")
            if any(d["original_text"]["verification"] not in ("exact", "inferred_reviewed") for d in source):
                add("E_PROMPT", loc + "/prompt_text", "anchor source text must be verified")
            if len(source) == 1:
                original = source[0]["original_audio"]
                if not (local_render and main) and (anchor_obj["audio"]["path"], anchor_obj["audio"]["sha256"]) != (original["path"], original["sha256"]):
                    add("E_ANCHOR", loc + "/audio", "single anchor audio differs from source dialogue")
            elif anchor_obj["audio"].get("duration_ms", 0) > 15000:
                add("E_AUDIO", loc + "/audio/duration_ms", "composite anchor exceeds 15 seconds")
        if main:
            for field in ("sample_rate_hz", "channels"):
                if anchor_obj["audio"].get(field) != runtime[field]:
                    add("E_AUDIO", loc + "/audio/" + field, "anchor format differs from runtime")
        if generated and not 4000<=anchor_obj['audio'].get('duration_ms',0)<=15000:
            add('E_AUDIO',loc+'/audio/duration_ms','generated anchor must be 4–15 seconds')

    for i, d in enumerate(dialogues):
        overrides = d.get("render_overrides", {})
        anchor_reference = overrides.get("anchor_reference")
        prewarm_reference = overrides.get("prewarm_reference")
        if anchor_reference is None and prewarm_reference is None:
            continue
        loc = f"/dialogues/{i}/render_overrides"
        rid = d.get("role_id")
        role = rmap.get(rid)
        if d.get("processing") != "render" or d.get("content_type") != "speech" or role is None:
            add("E_ANCHOR", loc, "per-dialogue anchor references require a rendered speech dialogue with a role")
            continue
        if anchor_reference is not None:
            generated_reference=portable_generated_anchor(anchor_reference,rid)
            anchor_loc = loc + "/anchor_reference"
            if overrides.get("anchor") != "use":
                add("E_ANCHOR", anchor_loc, "anchor_reference requires anchor=use")
            selected_anchor = role.get("main_anchor") or {}
            selected_source_ids = [selected_anchor.get("anchor_dialog_id"), *selected_anchor.get("anchor_component_dialog_ids", [])]
            source_ids = [anchor_reference.get("anchor_dialog_id"), *anchor_reference.get("anchor_component_dialog_ids", [])]
            if (set(source_ids) == set(selected_source_ids)
                    and anchor_reference.get("audio", {}).get("sha256") == selected_anchor.get("audio", {}).get("sha256")
                    and anchor_reference.get("prompt_text") == selected_anchor.get("prompt_text")):
                add("E_ANCHOR", anchor_loc, "anchor_reference must differ in source components, materialized audio, or prompt from the role's selected main anchor")
            custom_choice=(manifest.get('source_evidence',{}).get('launcher_voice_choices',{}).get('roles',{}).get(rid) or {})
            custom_alternative=custom_choice.get('alternative') or {}
            custom_reference=bool(local_render and custom_choice.get('source')=='custom'
                and source_ids==['custom-anchor:'+str(custom_alternative.get('semantic_sha256',''))]
                and anchor_reference.get('kind')=='single'
                and anchor_reference.get('prompt_text')==custom_alternative.get('transcript')
                and anchor_reference.get('audio',{}).get('sha256')==custom_alternative.get('sha256')
                and len(anchor_reference.get('source_audio',[]))==1
                and anchor_reference['source_audio'][0].get('path')==custom_alternative.get('path')
                and anchor_reference['source_audio'][0].get('sha256')==custom_alternative.get('sha256'))
            if not generated_reference and not custom_reference and any(source_id not in dmap for source_id in source_ids):
                add("E_ANCHOR", anchor_loc + "/anchor_dialog_id", "anchor_reference must name existing source dialogues")
            if len(anchor_reference.get("source_audio", [])) != len(source_ids):
                add("E_ANCHOR", anchor_loc + "/source_audio", "anchor_reference needs one source-audio receipt per source dialogue")
            for source_index, source_id in enumerate(source_ids):
                source_dialogue = dmap.get(source_id)
                if not generated_reference and source_dialogue and source_index < len(anchor_reference.get("source_audio", [])):
                    source_audio = source_dialogue["original_audio"]
                    cited_audio = anchor_reference["source_audio"][source_index]
                    if (cited_audio["path"], cited_audio["sha256"]) != (source_audio["path"], source_audio["sha256"]):
                        add("E_ANCHOR", f"{anchor_loc}/source_audio/{source_index}", "anchor reference source hash differs from the original dialogue audio")
            anchor(anchor_reference, rid, anchor_loc, True)
        if prewarm_reference is not None:
            prewarm_loc = loc + "/prewarm_reference"
            if overrides.get("prewarm") != "use":
                add("E_PREWARM", prewarm_loc, "prewarm_reference requires prewarm=use")
            if anchor_reference is None:
                key=hashlib.sha256(json.dumps(prewarm_reference,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
                source=manifest.get('source_evidence',{}).get('portable_reference_sources',{}).get(key)
                generated=False
                if isinstance(source,dict) and source.get('kind')=='generated':
                    from manifest_renderer.recipes import _source
                    try:
                        _source(source,set(dmap))
                        recipe=source['recipe']
                        generated=bool(source['dialog_id']==d['dialog_id'] and overrides.get('anchor')=='skip'
                            and not recipe['anchor'] and not recipe['prewarm'] and recipe['conditioning']['line_original']
                            and prewarm_reference.get('text')==recipe['tts_text']
                            and prewarm_reference.get('audio',{}).get('path','').startswith('portable-references/'))
                    except (ValueError,KeyError,TypeError):pass
                elif isinstance(source,dict) and source.get('kind')=='shared_prewarm':
                    from manifest_renderer.recipes import _source,source_from_reference
                    try:
                        _source(source,set(dmap))
                        selected_anchor=role.get('main_anchor') or {}
                        generated=bool(overrides.get('anchor')=='use' and selected_anchor
                            and source['anchor_source']==source_from_reference(manifest,selected_anchor)
                            and prewarm_reference.get('text')==source['tts_text']
                            and prewarm_reference.get('generated_with_anchor_dialog_id')==selected_anchor.get('anchor_dialog_id')
                            and prewarm_reference.get('generated_with_anchor_audio_sha256')==selected_anchor.get('audio',{}).get('sha256')
                            and prewarm_reference.get('audio',{}).get('path','').startswith('portable-references/'))
                    except (ValueError,KeyError,TypeError):pass
                if not generated:add("E_PREWARM", prewarm_loc, "prewarm_reference requires an anchor_reference or a local per-line reconstruction recipe")
            else:
                if (prewarm_reference.get("generated_with_anchor_dialog_id") != anchor_reference.get("anchor_dialog_id")
                        or prewarm_reference.get("generated_with_anchor_audio_sha256") != anchor_reference.get("audio", {}).get("sha256")):
                    add("E_PREWARM", prewarm_loc, "prewarm_reference is not bound to the per-dialogue anchor_reference")
            text_hash(prewarm_reference["text"], prewarm_reference["text_sha256"], prewarm_loc + "/text_sha256")
            asset(prewarm_reference["audio"], prewarm_loc + "/audio")
            register(prewarm_reference["selection"]["evidence_ref"], prewarm_loc + "/selection/evidence_ref")
            source_id = prewarm_reference.get("prewarm_dialog_id")
            if prewarm_reference["source"] == "dialog":
                if source_id not in dmap or dmap[source_id]["role_id"] != rid:
                    add("E_REF", prewarm_loc + "/prewarm_dialog_id", "prewarm dialogue is missing or belongs to another speaker")
            elif source_id is not None:
                add("E_PREWARM", prewarm_loc + "/prewarm_dialog_id", "neutral prewarm must not name a dialogue")
            for field in ("sample_rate_hz", "channels"):
                if prewarm_reference["audio"].get(field) != runtime[field]:
                    add("E_AUDIO", prewarm_loc + "/audio/" + field, "prewarm format differs from runtime")
        elif anchor_reference is not None:
            # Inherit would otherwise silently pair the alternate anchor with
            # the role's prewarm, which is bound to the selected main anchor.
            selected = next((v for v in d.get("text_variants", []) if v.get("variant_id") == d.get("selected_variant_id")), None)
            short = bool(selected) and (d.get("original_audio", {}).get("duration_ms", 0) < 3000 or len(selected.get("tts_text", "").split()) < 4)
            role_prewarm_needed = not role.get("without_prewarm", False) and (short or role.get("prewarm_for_long_text", False))
            if overrides.get("prewarm", "inherit") == "inherit" and role_prewarm_needed and role.get("prewarm"):
                add("E_PREWARM", loc + "/prewarm_reference", "alternate anchor cannot inherit the role prewarm; use prewarm=skip or provide its paired prewarm_reference")

    for i, role in enumerate(roles):
        loc = f"/roles/{i}"
        rid = role["role_id"]
        if not any(d["role_id"] == rid and d["processing"] == "render" and d["content_type"] == "speech" for d in dialogues):
            add("E_ROLE", loc, "role has no rendered speech dialogue")
        without_anchor = role.get("without_anchor", False)
        without_prewarm = role.get("without_prewarm", False)
        if "main_anchor" in role:
            anchor(role["main_anchor"], rid, loc + "/main_anchor", True)
        for j, backup in enumerate(role.get("backup_anchors", [])):
            anchor(backup, rid, f"{loc}/backup_anchors/{j}")
        selection = role.get("main_anchor_selection", {})
        if selection:
            register(selection["evidence_ref"], loc + "/main_anchor_selection/evidence_ref")
        if "preview_audio" in selection:
            asset(selection["preview_audio"], loc + "/main_anchor_selection/preview_audio")
        anchor_dialog = dmap.get(role.get("main_anchor", {}).get("anchor_dialog_id"))
        preview_variant = selection.get("preview_text_variant_id")
        if anchor_dialog and preview_variant is not None and preview_variant not in {v["variant_id"] for v in anchor_dialog["text_variants"]}:
            add("E_REF", loc + "/main_anchor_selection/preview_text_variant_id", "preview variant is not attached to main anchor dialogue")
        prewarm = role.get("prewarm")
        needs_prewarm = prewarm_by_role[rid]
        if needs_prewarm and not prewarm:
            add("E_PREWARM", loc + "/prewarm", "short rendered speech or the long-text role rule requires prewarm")
        if not needs_prewarm and prewarm:
            add("E_PREWARM", loc + "/prewarm", "prewarm exists without short rendered speech or the long-text role rule")
        if prewarm:
            text_hash(prewarm["text"], prewarm["text_sha256"], loc + "/prewarm/text_sha256")
            if "selection" in prewarm:
                register(prewarm["selection"]["evidence_ref"], loc + "/prewarm/selection/evidence_ref")
            per_line = prewarm.get("reference_mode") == "line_original"
            if per_line != without_anchor and not (not without_anchor and not fixed_anchor_by_role[rid]):
                add("E_PREWARM", loc + "/prewarm", "line-original prewarm descriptor is required when the role has no fixed-anchor use")
            if not per_line:
                asset(prewarm["audio"], loc + "/prewarm/audio")
                main = role.get("main_anchor")
                if not main or prewarm["generated_with_anchor_dialog_id"] != main["anchor_dialog_id"] or prewarm["generated_with_anchor_audio_sha256"] != main["audio"]["sha256"]:
                    add("E_PREWARM", loc + "/prewarm", "prewarm is bound to a different main anchor")
            source_id = prewarm.get("prewarm_dialog_id")
            if prewarm["source"] == "dialog":
                if source_id not in dmap or dmap[source_id]["role_id"] != rid:
                    add("E_REF", loc + "/prewarm/prewarm_dialog_id", "prewarm dialogue is missing or belongs to another speaker")
            elif source_id is not None:
                add("E_PREWARM", loc + "/prewarm/prewarm_dialog_id", "neutral prewarm must not name a dialogue")
            for field in ("sample_rate_hz", "channels"):
                if not per_line and prewarm["audio"].get(field) != runtime[field]:
                    add("E_AUDIO", loc + "/prewarm/audio/" + field, "prewarm format differs from runtime")

    output = manifest["output"]
    for field, path in output.items():
        register(path, "/output/" + field, is_output=True)
    output_files = [(str(PurePosixPath(output["root"]) / output["wav_directory"] / (d["output_stem"] + ".wav")), f"/dialogues/{i}/output_stem") for i, d in enumerate(dialogues)]
    output_files += [(str(PurePosixPath(output["root"]) / output[field]), "/output/" + field) for field in ("render_report", "qa_summary")]
    output_dirs = [str(PurePosixPath(output["root"]) / output[field]) for field in ("wav_directory", "qa_directory")]
    used = {}
    for path, loc in output_files:
        key = path_key(path)
        if key in used:
            add("E_OUTPUT", loc, f"output path collides with {used[key]}")
        used[key] = loc
        if not canonical_path(path):
            add("E_PATH", loc, "resolved output path is not canonical")
    input_keys = {path_key(p) for p, _, is_output in paths if not is_output}
    if any(path_key(p) == path_key(output["root"]) or path_key(p).startswith(path_key(output["root"]) + "/") for p, _, is_output in paths if not is_output):
        add("E_OUTPUT", "/output/root", "output root contains an input asset")
    for path, loc in output_files:
        if path_key(path) in input_keys:
            add("E_OUTPUT", loc, "output path overwrites an input asset")
        if any(path_key(path) == path_key(directory) or path_key(directory).startswith(path_key(path) + "/") for directory in output_dirs):
            add("E_OUTPUT", loc, "output file collides with an output directory")
    fingerprints = {}
    for path, sha, size, loc, _ in files:
        key = path_key(path)
        if key in fingerprints and (fingerprints[key][0] != sha or (size is not None and fingerprints[key][1] is not None and fingerprints[key][1] != size)):
            add("E_FILE", loc, f"conflicting fingerprint for {path}")
        fingerprints.setdefault(key, (sha, size, loc))

    for i, profile in enumerate(profiles):
        loc = f"/hardware/profiles/{i}"
        selector = profile["selector"]
        if not version_parts(selector["os_min_version"]):
            add("E_HARDWARE", loc + "/selector/os_min_version", "OS minimum version must be dotted decimal without suffix")
        if "render" in profile["stages"] and profile["execution_path"] != runtime["backend"]:
            add("E_HARDWARE", loc + "/execution_path", "render execution path differs from runtime backend")
        for section in ("minimum", "recommended"):
            if section not in profile:
                continue
            resource = profile[section]
            accel = resource["accelerator"]
            if accel["api"] != profile["execution_path"]:
                add("E_HARDWARE", loc + f"/{section}/accelerator/api", "accelerator API differs from execution path")
            if accel.get("memory_model") == "unified" and accel.get("minimum_memory_bytes", 0) > resource["system_ram_bytes"]:
                add("E_MEMORY", loc + f"/{section}/accelerator/minimum_memory_bytes", "unified memory exceeds system RAM")
            disk = resource["disk_free_bytes"]
            if disk["peak_total"] < sum(disk[k] for k in ("download_cache", "installation_models", "temporary_workspace", "output")):
                add("E_MEMORY", loc + f"/{section}/disk_free_bytes/peak_total", "peak total is below simultaneous category sum")
        if "recommended" in profile:
            minimum, recommended = profile["minimum"], profile["recommended"]
            if (recommended["system_ram_bytes"] < minimum["system_ram_bytes"]
                    or recommended["cpu"]["minimum_logical_cores"] < minimum["cpu"]["minimum_logical_cores"]
                    or not set(minimum["cpu"]["required_features"]).issubset(recommended["cpu"]["required_features"])
                    or any(recommended["disk_free_bytes"][key] < minimum["disk_free_bytes"][key] for key in ("download_cache", "installation_models", "temporary_workspace", "output", "peak_total"))
                    or recommended["accelerator"]["api"] != minimum["accelerator"]["api"]
                    or recommended["accelerator"].get("minimum_memory_bytes", 0) < minimum["accelerator"].get("minimum_memory_bytes", 0)):
                add("E_MEMORY", loc + "/recommended", "recommended resources are below minimum")
        for stage in profile["stages"]:
            applicable = [r for r in reqs if r["required"] and stage in r["stages"] and selector["os_family"] in r["platforms"] and selector["cpu_architecture"] in r["architectures"]]
            for needed in ([runtime["runtime_id"], manifest["models"]["tts"]["component_id"]] if stage == "render" else [manifest["models"]["stt"]["component_id"]] if stage == "qa" else []):
                if needed not in {r["component_id"] for r in applicable}:
                    add("E_REQUIREMENT", loc + "/stages", f"required {stage} component {needed} does not cover selector")

    if check_host:
        facts = facts if facts is not None else host_facts()
        expected_facts = {"os_family": str, "os_version": str, "cpu_architecture": str,
                          "logical_cores": int, "cpu_features": list, "system_ram_bytes": int,
                          "accelerator_api": str}
        if not isinstance(facts, dict) or any(not isinstance(facts.get(key), typ) for key, typ in expected_facts.items()) or any(not isinstance(x, str) for x in facts.get("cpu_features", []) if isinstance(facts, dict)):
            add("E_HOST_FACTS", "/", "host facts are incomplete or have invalid types")
            return issues
        for stage in STAGES:
            candidates = [p for p in profiles if stage in p["stages"] and p["selector"]["os_family"] == facts.get("os_family") and p["selector"]["cpu_architecture"] == facts.get("cpu_architecture")]
            selected = None
            for candidate in candidates:
                sel, minimum = candidate["selector"], candidate["minimum"]
                host_version, required_version = version_parts(facts.get("os_version", "")), version_parts(sel["os_min_version"])
                if not host_version or not required_version or host_version < required_version:
                    continue
                if not facts.get("logical_cores") or facts["logical_cores"] < minimum["cpu"]["minimum_logical_cores"]:
                    continue
                if not set(x.casefold() for x in minimum["cpu"]["required_features"]).issubset(x.casefold() for x in facts.get("cpu_features", [])):
                    continue
                if not facts.get("system_ram_bytes") or facts["system_ram_bytes"] < minimum["system_ram_bytes"]:
                    continue
                accel = minimum["accelerator"]
                if facts.get("accelerator_api") != accel["api"]:
                    continue
                if accel["api"] != "cpu" and (facts.get("accelerator_memory_bytes") or 0) < accel["minimum_memory_bytes"]:
                    continue
                selected = candidate
                break
            if not selected:
                add("E_HOST_PROFILE", "/hardware/profiles", f"no minimum profile satisfied for {stage}")
            elif stage == "render" and selected["execution_path"] != runtime["backend"]:
                add("E_HOST_PROFILE", "/hardware/profiles", "selected render profile does not match runtime")

    if check_assets:
        if asset_root is None:
            add("E_CLI", "/", "--check-assets requires --asset-root")
        else:
            root = asset_root.resolve()
            seen = set()
            verified = set()
            for path, sha, size, loc, audio_obj in files:
                key = path_key(path)
                if key in seen or not canonical_path(path):
                    continue
                seen.add(key)
                target = root / path
                if any(p.is_symlink() for p in [target, *target.parents] if p != root and root in p.parents):
                    add("E_ASSET_ESCAPE", loc, "asset path contains a symlink")
                    continue
                try:
                    if not target.is_file():
                        raise FileNotFoundError(path)
                    actual_size = target.stat().st_size
                except OSError:
                    add("E_ASSET_MISSING", loc, f"asset missing or unreadable: {path}")
                    continue
                if size is not None and actual_size != size:
                    add("E_ASSET_SIZE", loc, f"asset size mismatch: {path}")
                if not skip_content_hashes and (max_hash_bytes is None or actual_size <= max_hash_bytes):
                    try:
                        actual_hash = file_digest(target)
                    except OSError:
                        add("E_ASSET_MISSING", loc, f"asset unreadable: {path}")
                        continue
                    if actual_hash != sha:
                        add("E_ASSET_HASH", loc, f"asset SHA-256 mismatch: {path}")
                    elif size is None or actual_size == size:
                        verified.add(path_key(path))
                if audio_obj and audio_obj["media_type"] == "audio/wav":
                    try:
                        with wave.open(str(target), "rb") as wav:
                            actual_duration = round(wav.getnframes() * 1000 / wav.getframerate())
                            if wav.getframerate() != audio_obj["sample_rate_hz"] or wav.getnchannels() != audio_obj["channels"] or abs(actual_duration - audio_obj["duration_ms"]) > 1:
                                add("E_AUDIO", loc, "WAV metadata differs from manifest")
                    except (OSError, EOFError, wave.Error):
                        add("E_AUDIO", loc, "WAV cannot be decoded")
            for i, req in enumerate(reqs):
                if req["acquisition"] == "system":
                    current = host_facts()
                    if current["os_family"] not in req["platforms"] or current["cpu_architecture"] not in req["architectures"]:
                        continue
                    binary = shutil.which(req["system_binary"]["executable_name"])
                    loc = f"/requirements/{i}/system_binary"
                    if not binary:
                        add("E_ASSET_MISSING", loc, "system executable not found on PATH")
                        continue
                    target = Path(binary).resolve()
                    if target.stat().st_size != req["system_binary"]["size_bytes"]:
                        add("E_ASSET_SIZE", loc, "system executable size mismatch")
                        continue
                    if not skip_content_hashes and (max_hash_bytes is None or target.stat().st_size <= max_hash_bytes):
                        try:
                            system_hash = file_digest(target)
                        except OSError:
                            add("E_ASSET_MISSING", loc, "system executable unreadable")
                            continue
                        if system_hash != req["system_binary"]["sha256"]:
                            add("E_ASSET_HASH", loc, "system executable SHA-256 mismatch")
                            continue
                elif req["kind"] != "model":
                    name = req["probe"].get("executable_path") or req["artifacts"][0]["path"]
                    binary = str(root / name)
                    loc = f"/requirements/{i}/probe"
                    if path_key(name) not in verified:
                        continue
                else:
                    continue
                if check_probes:
                    try:
                        proc = subprocess.run([binary, *req["probe"]["argv"][1:]], shell=False, capture_output=True, text=True, timeout=5, check=False)
                        probe_text = proc.stdout + proc.stderr
                        if proc.returncode != 0 or re.search(req["probe"]["version_pattern"], probe_text) is None:
                            add("E_PROBE", loc, "probe exited nonzero or output did not match version pattern")
                    except (OSError, subprocess.TimeoutExpired):
                        add("E_PROBE", loc, "probe could not be executed")
    return issues


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="v1 JSON manifest")
    parser.add_argument("--asset-root", type=Path, help="local package root for optional asset checks")
    parser.add_argument("--check-assets", action="store_true", help="check local file bytes, sizes and WAV metadata")
    parser.add_argument("--check-probes", action="store_true", help="execute version probes after asset checks")
    parser.add_argument("--check-host", action="store_true", help="select a minimum hardware profile for this host")
    parser.add_argument("--host-facts", type=Path, help="JSON host facts for deterministic preflight instead of auto-detection")
    parser.add_argument("--json", action="store_true", help="emit machine-readable diagnostics")
    args = parser.parse_args(argv)
    if args.check_probes and not args.check_assets:
        parser.error("--check-probes requires --check-assets")
    if args.host_facts and not args.check_host:
        parser.error("--host-facts requires --check-host")
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        issues = [Issue("E_INPUT", "/", str(exc))]
    else:
        try:
            facts = json.loads(args.host_facts.read_text(encoding="utf-8")) if args.host_facts else None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            issues = [Issue("E_INPUT", "/", str(exc))]
        else:
            issues = validate(manifest, asset_root=args.asset_root, check_assets=args.check_assets, check_probes=args.check_probes, check_host=args.check_host, facts=facts)
    if args.json:
        print(json.dumps({"valid": not issues, "issues": [i.as_dict() for i in issues]}, ensure_ascii=False, indent=2))
    else:
        for issue in issues:
            print(f"{issue.code} {issue.location}: {issue.message}", file=sys.stderr)
        if not issues:
            print("OK: manifest v1 is valid" + ("; local assets checked" if args.check_assets else "; static checks only"))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
