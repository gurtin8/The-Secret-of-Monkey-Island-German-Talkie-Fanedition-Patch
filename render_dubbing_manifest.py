#!/usr/bin/env python3
"""Validate/plan executable manifests and run the local MI1 production pipeline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from manifest_renderer.manifest import ManifestError, load_manifest
from manifest_renderer.plan import build_run_plan
from manifest_renderer.mi1_prepare import prepare_mi1_manifest
from validate_dubbing_manifest import main as validator_main
from manifest_renderer.hardware import report as hardware_report, recommend as recommend_hardware


def _write_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(payload)
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManifestError(f"existing run plan is unreadable: {path}") from exc
        if existing != value:
            raise ManifestError(f"existing run plan differs and will not be overwritten: {path}")


def _execution_settings(manifest, backend=None, operating_mode=None, ane_config=None):
    """Honor the manifest recipe unless the caller explicitly selects a mode."""
    execution = manifest.get("render_execution", {})
    if backend is None and operating_mode is None:
        backend = execution.get("profile_id", "gpu")
        operating_mode = execution.get("operating_mode", "single")
    else:
        backend = backend or "gpu"
        operating_mode = operating_mode or "single"
    if ane_config is None and (backend == "ane" or operating_mode == "parallel_gpu_ane"):
        ane_config = execution.get("ane_worker")
    return backend, operating_mode, ane_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    hardware = commands.add_parser("hardware", help="modellfreie Hardwarefakten und vorläufige Empfehlung")
    hardware.add_argument("--disk-path", type=Path)
    validate = commands.add_parser("validate", help="run manifest preflight")
    validate.add_argument("manifest", type=Path)
    validate.add_argument("--asset-root", type=Path)
    validate.add_argument("--check-assets", action="store_true")
    validate.add_argument("--check-probes", action="store_true")
    validate.add_argument("--check-host", action="store_true")
    validate.add_argument("--host-facts", type=Path)
    validate.add_argument("--json", action="store_true")
    plan = commands.add_parser("plan", help="write deterministic run-plan.json")
    plan.add_argument("manifest", type=Path)
    plan.add_argument("--output", type=Path, default=Path("run-plan.json"))
    prepare = commands.add_parser("prepare-mi1", help="combine MI1 draft and complete Wizard export without rendering")
    prepare.add_argument("component", type=Path)
    prepare.add_argument("--draft", type=Path, required=True)
    prepare.add_argument("--asset-root", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    full = commands.add_parser("run-mi1", help="complete local MI1 render with asynchronous QA v2/Base→Medium")
    full.add_argument("manifest", type=Path)
    full.add_argument("--asset-root", required=True, type=Path)
    full.add_argument("--prepare-only", action="store_true", help="derive/validate the production configuration without loading models")
    full.add_argument("--full-integrity-check", action="store_true", help="hash every bound asset; normal startup validates paths, sizes and WAV headers while using saved receipts")
    full.add_argument("--backend", choices=("gpu", "cpu", "ane"),
                      help="Explizites Backend; ohne Modusargumente gilt die Manifestvorgabe")
    full.add_argument("--operating-mode", choices=("single", "parallel_gpu_ane"),
                      help="Expliziter Betriebsmodus; sonst gilt die Manifestvorgabe")
    full.add_argument("--routing-policy", choices=("free-cue-v1", "fixed-character-v1"),
                      help="Run-gebundene Backendvergabe: frei je Cue oder dauerhaft je Figur")
    full.add_argument("--dialog-ids", nargs="+", help="bounded ID selection for a target-hardware sample")
    full.add_argument("--ane-worker-config", type=Path,
                      help="JSON mit externem model_root, hashgebundenen Komponenten, Tokenizer und Cache")
    full.add_argument("--run-label", help="optionaler Ausgabesuffix für getrennte diagnostische Läufe")
    full.add_argument("--resume-qa-compatible-from", type=Path,
                      help="eng begrenzter Resume eines unterbrochenen Laufs nach Whisper-Diagnose-UTF-8-Fix")
    full.add_argument("--serialize-ane-qa", action="store_true",
                      help="ANE-Worker nach TTS beenden und QA anschließend ohne gleichzeitige Modellladung ausführen")
    full.add_argument("--diagnostic-ane-single-low-ram", action="store_true",
                      help="nur für einen manifestgebundenen isolierten Einzelcue-Test: 16-GiB-ANE-Guard aussetzen")
    args = parser.parse_args(argv)
    if args.command == "hardware":
        print(json.dumps(hardware_report(args.disk_path), ensure_ascii=False, indent=2))
        return 0
    if args.command == "run-mi1":
        from manifest_renderer.production import (prepare_production, run_all, immutable_json,
                                                   verify_production, prepare_qa_compatible_resume)
        from manifest_renderer.audio import resolve_manifest_path
        from manifest_renderer.timing import RunTiming
        timing = None
        try:
            if args.resume_qa_compatible_from and (args.prepare_only or args.dialog_ids or not args.run_label):
                raise ValueError("QA-kompatibler Resume benötigt Vollmanifest, --run-label und keinen --prepare-only-Aufruf")
            timing = RunTiming(args.asset_root, label=args.run_label)
            print(f"Zeitlog: {timing.path}", flush=True)
            with timing.phase('manifest_and_receipt_preparation',announce=True):
                ane_config = None
                if args.ane_worker_config:
                    ane_config = json.loads(args.ane_worker_config.read_text(encoding="utf-8"))
                args.backend, args.operating_mode, ane_config = _execution_settings(
                    load_manifest(args.manifest)['manifest'], args.backend,
                    args.operating_mode, ane_config)
                manifest = prepare_production(args.manifest, args.asset_root, backend=args.backend,
                                              operating_mode=args.operating_mode,
                                              ane_worker_config=ane_config, full_integrity=args.full_integrity_check,
                                              routing_policy=args.routing_policy, timing=timing)
            timing.event('prepared_manifest',counts={'dialogues':len(manifest['scope']['included_dialog_ids']),
                         'integrity_mode':manifest['source_evidence']['production']['integrity_policy']['mode']})
            if args.serialize_ane_qa:
                if args.backend != "ane" or args.operating_mode != "single":
                    raise ValueError("--serialize-ane-qa gilt nur für den isolierten ANE-Einzelbetrieb")
            if args.diagnostic_ane_single_low_ram:
                if (args.backend != "ane" or args.operating_mode != "single" or
                        not args.serialize_ane_qa or not args.run_label or
                        not args.dialog_ids or len(args.dialog_ids) != 1):
                    raise ValueError("Low-RAM-Diagnose verlangt ANE single, genau eine ID, --serialize-ane-qa und --run-label")
            if args.run_label:
                import re
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.run_label):
                    raise ValueError("Ungültiger --run-label")
                manifest["output"]["root"] += "-" + args.run_label
            resume_compatibility = None
            if args.resume_qa_compatible_from:
                resume_root = resolve_manifest_path(args.asset_root, str(args.resume_qa_compatible_from))
                try:
                    saved_manifest = json.loads((resume_root / "manifest.json").read_text(encoding="utf-8"))
                    saved_progress = json.loads((resume_root / "progress.json").read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"Gespeicherter QA-Resume-Run ist unlesbar: {resume_root}") from exc
                resume_compatibility = prepare_qa_compatible_resume(
                    manifest, saved_manifest, args.asset_root, resume_root,
                    run_label=args.run_label, progress=saved_progress)
                # The original manifest and run plan remain the identity used by
                # every existing attempt and output. The sidecar records only
                # the narrowly verified diagnostic decoder compatibility.
                manifest = saved_manifest
            if args.prepare_only:
                with timing.phase('model_free_preflight',announce=True):
                    verify_production(manifest, args.asset_root)
                from manifest_renderer.audio import resolve_manifest_path
                target = resolve_manifest_path(args.asset_root, manifest["output"]["root"]) / "manifest.json"
                immutable_json(target, manifest)
                print(f"Modellfrei vorbereitet: {target}")
                timing.bind_run_root(manifest['output']['root'])
                timing.finish(status='prepared',counts={'dialogues':len(manifest['scope']['included_dialog_ids'])})
                return 0
            from manifest_renderer.production import validate_host_selection, validate_ane_runtime_installation
            with timing.phase('host_and_runtime_preflight',announce=True):
                host = hardware_report(args.asset_root)["facts"]
                validate_host_selection(args.backend, args.operating_mode, host,
                                        diagnostic_ane_single_low_ram=args.diagnostic_ane_single_low_ram)
                if args.backend == 'ane' or args.operating_mode == 'parallel_gpu_ane':
                    validate_ane_runtime_installation()
            selection = "ane-gpu" if args.operating_mode == "parallel_gpu_ane" else args.backend
            advice = recommend_hardware(host)
            print(f"Wirksame Auswahl: {selection}; vorläufige Empfehlung: {advice['candidate']} (keine automatische Umwahl).", flush=True)
            report = run_all(manifest, args.asset_root, dialog_ids=args.dialog_ids,
                             serialize_ane_qa=args.serialize_ane_qa,timing=timing,
                             resume_compatibility=resume_compatibility)
            print(json.dumps({"status": report["status"], "counts": report["counts"],
                              "output": manifest["output"]["root"]}, ensure_ascii=False))
            timing.finish(status=report['status'],counts=report['counts'])
            return 2 if report["counts"].get("fail") else 0
        except (ManifestError, ValueError, RuntimeError, OSError) as exc:
            if timing: timing.finish(status='error',counts={'error_type':type(exc).__name__})
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except BaseException as exc:
            if timing:
                timing.finish(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'error',
                              counts={'error_type':type(exc).__name__})
            raise
    if args.command == "prepare-mi1":
        try:
            manifest = prepare_mi1_manifest(args.draft, args.component, args.asset_root)
            _write_new(args.output, manifest)
        except ManifestError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"manifest": str(args.output), "profile": manifest["manifest_profile"],
                          "dialogues": len(manifest["dialogues"])}, ensure_ascii=False))
        return 0
    if args.command == "validate":
        forwarded = [str(args.manifest)]
        for flag in ("check_assets", "check_probes", "check_host", "json"):
            if getattr(args, flag):
                forwarded.append("--" + flag.replace("_", "-"))
        if args.asset_root:
            forwarded += ["--asset-root", str(args.asset_root)]
        if args.host_facts:
            forwarded += ["--host-facts", str(args.host_facts)]
        return validator_main(forwarded)
    try:
        run_plan = build_run_plan(load_manifest(args.manifest))
        _write_new(args.output, run_plan)
    except ManifestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"run_plan": str(args.output), "sha256": run_plan["sha256"],
                      "dialogues": len(run_plan["dialogues"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
