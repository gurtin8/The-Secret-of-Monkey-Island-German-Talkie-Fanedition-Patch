#!/usr/bin/env python3
"""Write four model-free, manifest-bound hardware run cards for Plan 07."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from manifest_renderer.manifest import load_manifest
from manifest_renderer.plan import build_run_plan
from manifest_renderer.production import select_run_plan

PROFILES = (
    ("ane-gpu-free", "gpu", "parallel_gpu_ane", "free-cue-v1"),
    ("ane-gpu", "gpu", "parallel_gpu_ane", "fixed-character-v1"),
    ("ane", "ane", "single", "fixed-character-v1"),
    ("gpu", "gpu", "single", "fixed-character-v1"),
)


def run_cards(manifest_path: Path, output: Path, asset_root: Path | None = None):
    manifest_path = manifest_path.resolve(strict=True)
    source = load_manifest(manifest_path)["manifest"]
    plan = build_run_plan(source)
    # Choose one real dialogue for every distinct reference/prompt/method signature.
    representatives = {}
    copy_dialogue = None
    for line in plan["dialogues"]:
        if not line["attempts"]:
            copy_dialogue = copy_dialogue or line["dialog_id"]
            continue
        for attempt in line["attempts"]:
            signature = (attempt["method"], attempt["reference_mode"],
                         tuple(part["kind"] for part in attempt["input_parts"]),
                         tuple(part["input_kind"] for part in attempt["prompt_parts"]),
                         attempt["runtime_mode"])
            representatives.setdefault(signature, line["dialog_id"])
    sample_ids = sorted(set(representatives.values()) | ({copy_dialogue} if copy_dialogue else set()))
    obsolete_sample = output / "plan07-sample.manifest.json"
    if obsolete_sample.exists():
        _backup(obsolete_sample, output)
        obsolete_sample.unlink()
    asset_root = (asset_root or Path.cwd()).resolve(strict=True)
    input_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    sample_plan = select_run_plan(plan, sample_ids)
    signatures = [{"method": method, "reference_mode": reference, "input_kinds": list(inputs),
                   "prompt_kinds": list(prompts), "runtime_mode": runtime, "dialog_id": dialog_id}
                  for (method, reference, inputs, prompts, runtime), dialog_id in sorted(representatives.items())]
    cards = []
    for name, backend, mode, routing_policy in PROFILES:
        uses_ane = backend == "ane" or mode == "parallel_gpu_ane"
        ane_config = asset_root / ("work/ane-worker-config-split.json" if uses_ane else "work/ane-worker-config.json")
        profile_flags = ["--ane-worker-config", str(ane_config)] if uses_ane else []
        run_label=f"plan07-20261004-routing-{name}"
        policy_flags=["--routing-policy",routing_policy]
        portable_python = asset_root / "work/python/cpython-3.12.14-macos-aarch64-none/bin/python3.12"
        ane_python = asset_root / "work/ane-runtime-venv/bin/python"
        if uses_ane:
            execution_python = ane_python
        else:
            execution_python = portable_python if portable_python.is_file() else Path(sys.executable)
        record = {"schema": "mi1-plan07-run-card-v1", "series_id":"plan07-m1-ane-gpu-parallel-v1",
                  "series_order":len(cards)+1, "selection": name,
                  "backend": backend, "operating_mode": mode,
                  "routing_policy":routing_policy,
                  "asset_root": str(asset_root),
                  "ane_worker_config": str(ane_config) if uses_ane else None,
                  "input_manifest": str(manifest_path), "input_manifest_sha256": input_hash,
                  "source_manifest": str(manifest_path),
                  "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                  "logical_dialogues": len(sample_plan["dialogues"]), "source_dialogues": len(plan["dialogues"]),
                  "render_attempts": sum(len(row["attempts"]) for row in sample_plan["dialogues"]),
                  "required_combinations": signatures,
                  "sample_dialog_ids": sample_ids,
                  "resume": "reuse only matching manifest, input, and backend binding hashes",
                  "timings": {"start_utc": None, "end_utc": None, "elapsed_seconds": None},
                  "qa_summary": None,
                  "prepare_command": ["python3", "render_dubbing_manifest.py", "run-mi1", str(manifest_path),
                                      "--asset-root", "<external-project-root>", "--backend", backend,
                                      "--operating-mode", mode, "--run-label", run_label, *policy_flags, *profile_flags,
                                      "--dialog-ids", *sample_ids, "--prepare-only"],
                  "execution_command": [str(execution_python),
                                      "render_dubbing_manifest.py", "run-mi1", str(manifest_path),
                                      "--asset-root", str(asset_root), "--backend", backend,
                                      "--operating-mode", mode, "--run-label", run_label, *policy_flags, *profile_flags,
                                      "--dialog-ids", *sample_ids]}
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        path = output / name / "run-card.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_preserving(path, payload, output)
        cards.append(str(path))
    role_lines={}
    for line in plan['dialogues']:
        if line['processing']=='render' and line.get('role_id'):
            role_lines.setdefault(line['role_id'],line)
    guybrush='mi1-ute-1.02:voice-code:GUY'
    probe_roles=([guybrush]+[role for role in role_lines if role!=guybrush][:2]
                 if guybrush in role_lines else [])
    probe_card=None
    if len(probe_roles)==3:
        probe_ids=[role_lines[role]['dialog_id'] for role in probe_roles]
        probe_plan=select_run_plan(plan,probe_ids)
        probe_signatures=[]
        for row in probe_plan['dialogues']:
            for attempt in row['attempts']:
                probe_signatures.append({
                    "method":attempt["method"],"reference_mode":attempt["reference_mode"],
                    "input_kinds":[part["kind"] for part in attempt["input_parts"]],
                    "prompt_kinds":[part["input_kind"] for part in attempt["prompt_parts"]],
                    "runtime_mode":attempt["runtime_mode"],"dialog_id":row["dialog_id"]})
        probe_label='plan07-20261004-routing-character-binding'
        probe_config=asset_root/'work/ane-worker-config-split.json'
        probe_python=asset_root/'work/ane-runtime-venv/bin/python'
        probe={"schema":"mi1-plan07-character-probe-v1","series_id":"plan07-character-routing-v1",
               "selection":"fixed-character-probe","backend":"gpu","operating_mode":"parallel_gpu_ane",
               "routing_policy":"fixed-character-v1","asset_root":str(asset_root),
               "input_manifest":str(manifest_path),"input_manifest_sha256":input_hash,
               "sample_dialog_ids":probe_ids,"sample_role_ids":probe_roles,"logical_dialogues":3,
               "required_combinations":probe_signatures,
               "resume":"reuse only matching manifest, input, policy, and persisted character bindings",
               "timings":{"start_utc":None,"end_utc":None,"elapsed_seconds":None},"qa_summary":None,
               "prepare_command":["python3","render_dubbing_manifest.py","run-mi1",str(manifest_path),
                   "--asset-root","<external-project-root>","--backend","gpu","--operating-mode","parallel_gpu_ane",
                   "--routing-policy","fixed-character-v1","--run-label",probe_label,
                   "--ane-worker-config",str(probe_config),"--dialog-ids",*probe_ids,"--prepare-only"],
               "execution_command":[str(probe_python),"render_dubbing_manifest.py","run-mi1",str(manifest_path),
                   "--asset-root",str(asset_root),"--backend","gpu","--operating-mode","parallel_gpu_ane",
                   "--routing-policy","fixed-character-v1","--run-label",probe_label,
                   "--ane-worker-config",str(probe_config),"--dialog-ids",*probe_ids]}
        probe_path=output/'character-probe'/'run-card.json'
        probe_path.parent.mkdir(parents=True,exist_ok=True)
        _write_preserving(probe_path,json.dumps(probe,ensure_ascii=False,sort_keys=True,indent=2)+'\n',output)
        probe_card=str(probe_path)
    return {"input_manifest_sha256": input_hash, "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "dialogues": len(sample_plan["dialogues"]), "source_dialogues": len(plan["dialogues"]),
            "sample_dialog_ids": sample_ids, "required_combinations": signatures,
            "cards": cards, "character_probe_card":probe_card,"character_probe_roles":probe_roles,
            "model_calls": 0, "full_generation": False}


def _write_preserving(path: Path, payload: str, output: Path):
    if path.is_symlink():
        raise ValueError(f"Symlink wird nicht überschrieben: {path}")
    if path.exists() and path.read_text(encoding="utf-8") != payload:
        _backup(path, output)
        temporary = path.with_name("." + path.name + ".plan07.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
    elif not path.exists():
        path.write_text(payload, encoding="utf-8")


def _backup(path: Path, output: Path):
    if path.is_symlink():
        raise ValueError(f"Symlink wird nicht gesichert oder entfernt: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    relative = path.relative_to(output)
    backup = output / ".backups" / (relative.as_posix() + "." + digest + ".bak")
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        if backup.is_symlink() or hashlib.sha256(backup.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Vorhandene Sicherung weicht ab: {backup}")
    else:
        backup.write_bytes(path.read_bytes())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path,
                        help="project root used by target-hardware runs; defaults to the current directory")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run_cards(args.manifest, args.output, args.asset_root), ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
