#!/usr/bin/env python3
"""Install the pinned, optional ANE runtime files onto the external project."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from manifest_renderer.backend import ANE_RUNTIME_ASSETS

WHEEL = ROOT / "work/ane_runtime_contract/voxcpmane2-0.1.3b1-py3-none-any.whl"
WHEEL_SHA256 = "88998a020f67c6b4f6391da5112fb7dd56ceca42886ebea9e1ebca4274f42a59"
CODE = (
    "render_dubbing_manifest.py", "validate_dubbing_manifest.py", "start_manifest_dubbing.command",
    "prepare_plan07_recipe.py", "run_plan07_hardware.py", "tools/setup_ane_runtime.py", "manifest_renderer/hardware.py",
    *(asset for asset in ANE_RUNTIME_ASSETS if asset != "work/ane_runtime_contract/voxcpmane2-0.1.3b1-py3-none-any.whl"),
    "docs/schema/dubbing-manifest-v1.schema.json",
    "LICENSE", "THIRD_PARTY_NOTICES.md", "FANEDITION.md", "docs/PUBLICATION.md",
    "manifest_renderer/__init__.py", "manifest_renderer/manifest.py", "manifest_renderer/production.py",
    "manifest_renderer/runner.py", "manifest_renderer/backend.py", "manifest_renderer/pipeline.py",
    "manifest_renderer/voxcpm2.py", "manifest_renderer/plan.py", "manifest_renderer/ane_worker.py",
    "manifest_renderer/ane_worker_process.py", "manifest_renderer/audio.py", "manifest_renderer/audio_cache.py",
    "manifest_renderer/session.py", "audio_qa_v1/audit.py", "audio_qa_v2/rescore.py",
)


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def install(target: Path, *, dry_run=False, require_external=True):
    target = target.expanduser().resolve(strict=True)
    if require_external and not str(target).startswith("/Volumes/"):
        raise ValueError("Ziel muss auf einem eingehängten externen Volume unter /Volumes liegen")
    if sha(WHEEL) != WHEEL_SHA256:
        raise ValueError("Hash des gepinnten VoxCPMANE2-Wheels stimmt nicht")
    sources = [ROOT / item for item in CODE] + [WHEEL, ROOT / "work/ane_runtime_contract/pyproject.toml"]
    targets = [(source, target / ("work/ane_runtime_contract/" + source.name) if source == WHEEL else
                target / source.relative_to(ROOT)) for source in sources if source.name != "pyproject.toml"]
    plan = []
    for source, destination in targets:
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Installationsquelle fehlt oder ist Symlink: {source}")
        if destination.parent.is_symlink() or destination.is_symlink():
            raise ValueError(f"Symlink im Installationsziel wird abgelehnt: {destination}")
        digest = sha(source)
        action = "same" if destination.is_file() and sha(destination) == digest else "install"
        plan.append({"path": str(destination), "sha256": digest, "action": action})
    if dry_run:
        return plan
    backups = target / "work/plan06-backups"
    for (source, destination), item in zip(targets, plan):
        if item["action"] == "same":
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            backup = backups / (destination.relative_to(target).as_posix() + "." + sha(destination) + ".bak")
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(destination, backup)
            if sha(backup) != sha(destination):
                raise ValueError(f"Sicherung vorhandener Datei fehlgeschlagen: {destination}")
        temporary = destination.with_name("." + destination.name + ".plan06.tmp")
        shutil.copy2(source, temporary)
        if sha(temporary) != item["sha256"]:
            temporary.unlink(missing_ok=True)
            raise ValueError(f"Installationshash stimmt nicht: {destination}")
        temporary.replace(destination)
    return plan


def install_venv(target: Path, *, python: Path | None = None, wheel: Path | None = None,
                 venv: Path | None = None, cache: Path | None = None,
                 constraints: Path | None = None, runner=subprocess.run):
    """Create a fresh user-local environment from independently downloaded originals.

    P17 callers pass verified Python/wheel paths; legacy project defaults remain.
    No developer environment is copied. Model acquisition is separate.
    """
    python = python or target / "work/python/cpython-3.12.14-macos-aarch64-none/bin/python3.12"
    if not python.is_file():
        raise ValueError(f"Portable Python 3.12 fehlt: {python}")
    venv = venv or target / "work/ane-runtime-venv"
    wheel = wheel or target / "work/ane_runtime_contract" / WHEEL.name
    cache = cache or target / "cache/ane-pip"
    if sha(wheel) != WHEEL_SHA256:
        raise ValueError("Hash des separat geladenen ANE-Wheels stimmt nicht")
    if not (venv / "bin/python").is_file():
        runner([str(python), "-m", "venv", str(venv)], check=True)
    pip = venv / "bin/python"
    cache.mkdir(parents=True, exist_ok=True)
    command = [str(pip), "-m", "pip", "install", "--index-url", "https://pypi.org/simple",
               "--only-binary", ":all:", "--cache-dir", str(cache)]
    if constraints is not None:
        command += ["--constraint", str(constraints)]
    runner([*command, str(wheel)], check=True)
    installed = runner([str(pip), "-c", "from importlib.metadata import version; print(version('voxcpmane2')); print(version('coremltools'))"],
                               check=True, capture_output=True, text=True).stdout.splitlines()
    if installed != ["0.1.3b1", "9.0"]:
        raise ValueError(f"Installierte ANE-Versionen weichen ab: {installed}")
    return {"python": str(pip), "voxcpmane2": installed[0], "coremltools": installed[1],
            "pip_cache": str(cache)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--install-python-runtime", action="store_true",
                        help="create isolated external venv and install pinned runtime/dependencies")
    args = parser.parse_args(argv)
    try:
        plan = install(args.target, dry_run=args.dry_run)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    runtime = None
    if args.install_python_runtime and not args.dry_run:
        try:
            runtime = install_venv(args.target.expanduser().resolve(strict=True))
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            print(f"ERROR: ANE-Pythoninstallation fehlgeschlagen: {exc}", file=sys.stderr)
            return 1
    print(json.dumps({"version": "mi1-ane-setup-v1", "dry_run": args.dry_run,
                      "wheel_sha256": WHEEL_SHA256, "files": plan,
                      "python_runtime": runtime,
                      "runtime_install_pending": bool(args.install_python_runtime and args.dry_run)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
