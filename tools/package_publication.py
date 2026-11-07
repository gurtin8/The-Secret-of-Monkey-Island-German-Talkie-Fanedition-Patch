#!/usr/bin/env python3
"""Export the deliberately bounded, source-available project publication tree."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEXTLESS_FORMAT = "mi1-textless-recipe/1"


def _regular_source(relative: str) -> Path:
    source = ROOT / relative
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"Publication source is missing or not a regular file: {relative}")
    resolved = source.resolve(strict=True)
    if not resolved.is_relative_to(ROOT):
        raise ValueError(f"Publication source escapes the repository: {relative}")
    return resolved


def publication_sources() -> tuple[str, ...]:
    """Return the explicit source tree; own_files() is the production boundary."""
    from generator_launcher.package_native import own_files

    sources = set(own_files())
    sources.update({
        ".gitignore",
        "generator_launcher/package_native.py",
        "generator_launcher/native/Launcher.swift",
        "generator_launcher/native/ExistingServer.swift",
        "generator_launcher/native/PatchWindowDock.swift",
        "generator_launcher/native/assets/LauncherIcon.icns",
        "generator_launcher/native/assets/LauncherIcon.svg",
        "tools/wiki/build.py",
        "tools/package_publication.py",
        "docs/wiki/control-inventory.json",
        "docs/wiki/site/.nojekyll",
        "docs/wiki/README.md",
        "docs/wiki/coverage.json",
        "docs/wiki/github-pages.yml.example",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "README.md",
        "FANEDITION.md",
        "docs/PUBLICATION.md",
        "docs/screenshots/01-speicherorte.png",
        "docs/screenshots/02-downloads.png",
        "docs/screenshots/04-dialoge-pruefen.png",
        "docs/screenshots/05-rezept-speichern.png",
        "docs/screenshots/06-spielordner-erstellen.png",
        "tools/export_textless_manifest.py",
        "tests/test_native_publication.py",
        "tests/test_text_reconstruction.py",
        "tests/test_reconstruction_runtime.py",
        "tests/test_wiki_integration.py",
        "licenses/third-party/ScummTR-MIT.txt",
        "licenses/third-party/llama-cpp-omni-MIT.txt",
        "licenses/third-party/VoxCPM-Apache-2.0.txt",
    })
    sources.update(p.relative_to(ROOT).as_posix()
                   for p in (ROOT / "docs/wiki/content").glob("*.json"))
    sources.update(p.relative_to(ROOT).as_posix()
                   for p in (ROOT / "docs/wiki/pages").glob("*.md"))
    return tuple(sorted(sources))


def _check_manifest() -> str:
    relative = "generator_launcher/defaults/manifest.json"
    source = _regular_source(relative)
    manifest = json.loads(source.read_text(encoding="utf-8"))
    reconstruction = manifest.get("text_reconstruction")
    if not isinstance(reconstruction, dict) or reconstruction.get("format") != TEXTLESS_FORMAT:
        raise ValueError(
            "Refusing source publication: standard manifest must declare "
            f"text_reconstruction.format={TEXTLESS_FORMAT!r}; no full dialogue text is exported."
        )
    from generator_launcher.text_reconstruction import FORMAT, validate_recipe_structure
    if FORMAT != TEXTLESS_FORMAT:
        raise ValueError(f"Exporter/recipe format mismatch: {FORMAT!r}")
    validate_recipe_structure(manifest)
    return relative


def export(output: Path) -> Path:
    manifest_path = _check_manifest()
    manifest = set(publication_sources())
    manifest.add(manifest_path)
    # Resolve and validate the complete allowlist before touching the destination.
    files = [(relative, _regular_source(relative)) for relative in sorted(manifest)]
    output = output.expanduser().absolute()
    if output == ROOT or output.is_relative_to(ROOT):
        raise ValueError("Choose a publication output directory outside the source repository")
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise ValueError(f"Publication output must be a new or empty directory: {output}")
    else:
        output.mkdir(parents=True)

    hashes: dict[str, str] = {}
    for relative, source in files:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        shutil.copymode(source, target)
        hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()

    record = {
        "format": "mi1-publication-export/1",
        "manifest": manifest_path,
        "text_reconstruction_format": TEXTLESS_FORMAT,
        "file_count": len(files),
        "files_sha256": hashes,
        "scope": "Explicit package_native.own_files() inventory plus reviewed documentation, native launcher sources, wiki sources, project notices and selected license notices.",
        "excluded": ["full-copy tree export", "input/", "work/", "tests/ except explicitly listed publication/reconstruction/wiki tests", ".codex/", ".agents/", "audit and session logs", "third-party source trees", "model weights", "game data and audio"],
    }
    (output / "PUBLICATION_EXPORT.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="New or empty destination outside the source repository")
    args = parser.parse_args(argv)
    print(export(args.output))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"publication export failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
