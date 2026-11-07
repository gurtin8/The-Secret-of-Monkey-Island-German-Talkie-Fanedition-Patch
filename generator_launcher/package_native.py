"""Build the own-code macOS launcher bundle around explicitly supplied data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
from .version import LAUNCHER_VERSION, PATCH_NAME, PATCH_APP_NAME

ROOT = Path(__file__).resolve().parents[1]


def own_files():
    from manifest_renderer.production import CODE_FILES
    from manifest_renderer.backend import ANE_RUNTIME_ASSETS
    extras = ('mi1-dubbing-extractor', 'mi1_patcher.py',
        'manifest_renderer/__init__.py', 'manifest_renderer/mi1_adapter.py',
        'manifest_renderer/mi1_prepare.py', 'manifest_renderer/recipes.py', 'patch_review/build.py',
        'generator_launcher/__init__.py', 'generator_launcher/host.py', 'generator_launcher/version.py',
        'generator_launcher/patch_interaction.py', 'generator_launcher/rosetta.py',
        'generator_launcher/ute_patch_compat.py',
        'generator_launcher/patch_window.js',
        'generator_launcher/contracts.py', 'generator_launcher/local_preparation.py', 'generator_launcher/ute_native_flac.py', 'generator_launcher/ute_native_sbl.py', 'generator_launcher/ute_xwma.py', 'generator_launcher/native_audio_tools.py', 'generator_launcher/local_ane.py', 'generator_launcher/lifetime.py',
        'generator_launcher/render.py', 'generator_launcher/render_preparation.py', 'generator_launcher/text_reconstruction.py', 'generator_launcher/reconstruction_runtime.py', 'generator_launcher/benchmark.py', 'generator_launcher/resume.py',
        'generator_launcher/runtime_setup.py', 'generator_launcher/setup_recipes.py',
        'generator_launcher/setup_dashboard.py', 'generator_launcher/voice_choices.py',
        'generator_launcher/fassungen.js', 'generator_launcher/repair.py', 'generator_launcher/launcher_flow.js', 'generator_launcher/launcher_console.js',
        'generator_launcher/launcher_terminal.js', 'generator_launcher/launcher_wizard.js', 'generator_launcher/launcher_wizard.css',
        'generator_launcher/native/start.html', 'generator_launcher/native/start.js', 'generator_launcher/native/start-theme.css',
        'generator_launcher/preparation_help.py', 'generator_launcher/gog_input.py', 'generator_launcher/gog_tool.py', 'generator_launcher/original_audio.py',
        'voice_anchor_wizard/custom_anchors.py', 'voice_anchor_wizard/anchor_selection.py',
        'generator_launcher/metadata_projection.py', 'generator_launcher/project_defaults.py',
        'tools/patch_mi1_intro.py', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'FANEDITION.md', 'docs/PUBLICATION.md',
        'docs/THIRD_PARTY_COMPONENTS.md', 'docs/UPSTREAM_CHANGES.md',
        'licenses/third-party/llama-cpp-omni-MIT.txt', 'licenses/third-party/ScummTR-MIT.txt',
        'licenses/third-party/VoxCPM-Apache-2.0.txt',
        'docs/wiki/site/index.html', 'docs/wiki/site/data.js', 'docs/wiki/site/help-index.json',
        'docs/wiki/site/help.js', 'docs/wiki/site/help.css', 'docs/wiki/site/wiki.js',
        'docs/wiki/site/wiki.css', 'docs/wiki/site/app-help.js',
        *(f'docs/wiki/site/status-{kind}.svg' for kind in
          ('manual','clean','repaired','open','warning','invalid','active')))
    recipes = tuple('generator_launcher/recipes/'+name for name in
        ('runtime.json', 'preparation.json', 'vox-combined-speed.json', 'scummtr-actor.json', 'ane-constraints.txt', 'gog-extractor.json', 'native-audio.json'))
    # The launcher resolves ANE provenance from its own bundled Resources at
    # runtime. Bundle metadata only; the independently installed wheel and
    # model assets remain on the external project.
    ane_metadata = tuple(asset for asset in ANE_RUNTIME_ASSETS if not asset.endswith('.whl'))
    return tuple(dict.fromkeys((*CODE_FILES, *extras, *recipes, *ane_metadata)))


def package(manifest: Path | None, correction: Path | None, output: Path) -> Path:
    if manifest is None:
        manifest = ROOT / "generator_launcher/defaults/manifest.json"
    if manifest.is_symlink() or (correction is not None and correction.is_symlink()):
        raise ValueError("Manifest und Manifesterweiterung dürfen keine symbolischen Links sein")
    manifest = manifest.resolve(strict=True)
    correction = correction.resolve(strict=True) if correction is not None else None
    if not manifest.is_file() or (correction is not None and not correction.is_file()):
        raise ValueError("Manifest und Manifesterweiterung müssen reguläre Dateien sein")
    m = json.loads(manifest.read_text(encoding="utf-8"))
    c = json.loads(correction.read_text(encoding="utf-8")) if correction else None
    from manifest_renderer.manifest import canonical_json, sha256
    if not isinstance(m, dict) or not isinstance(m.get("manifest_id"), str):
        raise ValueError("Ungültige Manifestidentität")
    from generator_launcher.text_reconstruction import FORMAT, validate_recipe_structure
    if m.get('text_reconstruction', {}).get('format') != FORMAT:
        raise ValueError('Veröffentlichung benötigt ein textfreies Manifestrezept; Volltexte dürfen nur lokal verwendet werden')
    validate_recipe_structure(m)
    if c is not None:
        raise ValueError('Korrekturerweiterungen können Volltexte enthalten; nur lokal importieren, nicht im App-Paket ausliefern')
    if c is not None and (not isinstance(c, dict) or c.get("format") != "patch-review-extension/1" or
            c.get("manifest_id") != m["manifest_id"] or
            c.get("game_id") != m.get("game", {}).get("game_id") or
            c.get("source_manifest_sha256") != sha256(canonical_json(m)) or
            c.get("method_rules") != m.get("project_policy", {}).get("review_method_rules", ["seed", "anchor", "prewarm", "backend"]) or
            not isinstance(c.get("rows"), dict)):
        raise ValueError("Die Korrektur muss zur mitgelieferten Manifestidentität passen")

    output = output.absolute()
    app = output if output.suffix == ".app" else output / PATCH_APP_NAME
    contents = app / "Contents"
    resources = contents / "Resources"
    with tempfile.TemporaryDirectory(prefix="mi1-launcher-build-") as temporary:
        stage = Path(temporary)
        executable = stage / "Launcher"
        swift = ROOT / "generator_launcher/native/Launcher.swift"
        subprocess.run(["/usr/bin/xcrun", "swiftc", "-target", "arm64-apple-macos14.0",
                        "-framework", "AppKit", "-framework", "WebKit", "-framework", "CryptoKit",
                        "-module-cache-path", str(stage / "ModuleCache.noindex"),
                        "-o", str(executable), str(swift),
                        str(ROOT / "generator_launcher/native/ExistingServer.swift"),
                        str(ROOT / "generator_launcher/native/PatchWindowDock.swift")], check=True)
        shutil.rmtree(app, ignore_errors=True)
        (contents / "MacOS").mkdir(parents=True)
        resources.mkdir(parents=True)
        shutil.copy2(executable, contents / "MacOS/Launcher")
        shutil.copy2(ROOT / "generator_launcher/native/assets/LauncherIcon.icns", resources / "LauncherIcon.icns")
        info = {"CFBundleExecutable": "Launcher", "CFBundleIdentifier": "org.mi1.generator.launcher",
                "CFBundleName": PATCH_NAME, "CFBundleDisplayName": PATCH_NAME, "CFBundlePackageType": "APPL", "CFBundleVersion": LAUNCHER_VERSION,
                "CFBundleIconFile": "LauncherIcon.icns",
                "LSMinimumSystemVersion": "14.0", "LSArchitecturePriority": ["arm64"],
                "NSHighResolutionCapable": True, "NSSupportsAutomaticGraphicsSwitching": True}
        with (contents / "Info.plist").open("wb") as stream:
            plistlib.dump(info, stream, sort_keys=True)
        shutil.copy2(ROOT / "generator_launcher/recipes/runtime.json", resources / "runtime.json")
        defaults = resources / "defaults"; defaults.mkdir()
        shutil.copy2(manifest, defaults / "manifest.json")
        if correction is not None:
            shutil.copy2(correction, defaults / "correction.json")
        for relative in own_files():
            source = ROOT / relative
            if source.is_symlink() or not source.is_file(): raise ValueError('Eigenes Paketmodul fehlt: '+relative)
            target = resources / relative; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        # Seal the complete bundle after adding its resources. Swift's automatic
        # executable signature alone does not seal the surrounding app bundle.
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(app)], check=True)
    return app


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, help="Optionaler Ersatz für das versionierte Standardmanifest")
    parser.add_argument("--correction", type=Path, help="Optionale passende patch-review-extension/1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(package(args.manifest, args.correction, args.output))


if __name__ == "__main__":
    main()
