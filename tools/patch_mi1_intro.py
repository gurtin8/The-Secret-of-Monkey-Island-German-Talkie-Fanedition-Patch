#!/usr/bin/env python3
"""Add Anniki's dedication to an existing generated German UTE 1.02 build."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
INTRO = Path("DISK_0001/LECF/LFLF_0010/ROOM/ENCD")
FONT = Path("DISK_0001/LECF/LFLF_0010/CHAR_0002")
NEW_LINES = (
    "F\u00fcr Anniki",
    "Monkey Island German Ultimate Talkie Edition",
    "mit KI generierten Stimmen basierend auf",
)
OLD_LINES = (
    "Ultimate Talkie v1.02 - US zu DE - Patch v1.01",
    "http://la-patches.gandimann.de",
)
NEWLINE = b"\xff\x01"
OLD_PREFIX = bytes.fromhex("0c036f0ac8ff80080200e001030018f5ff14ff01080400a000b2000f")
OLD_BODY = OLD_PREFIX + NEWLINE.join(s.encode("cp850") for s in OLD_LINES) + b"\x00\xa0"
BASIS_HASHES = {
    "monkey.000": "99bfc02317b68d538d48b227054a0f172aaf29ab55b8650d4ea1189cb048fd62",
    "monkey.001": "b749252e01005643a3bd7184a54c9a455fe4dc07f8dd36b28fd369ce0ea3ade6",
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_info(path):
    return {"name": path.name, "sha256": sha256(path), "size_bytes": path.stat().st_size}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_scummrp(tool, game, dump, mode, tag=None):
    args = [str(tool), "-g", "monkeycd", "-p", str(game), "-d", str(dump), mode]
    if tag:
        args.extend(["-t", tag])
    result = subprocess.run(args, capture_output=True, text=True, errors="replace", check=False)
    output = (result.stdout + result.stderr).lower()
    if result.returncode or "warning" in output or "error:" in output:
        raise RuntimeError("ScummRP: " + (result.stdout + result.stderr).strip())


def resource_hashes(root):
    return {p.relative_to(root).as_posix(): sha256(p) for p in root.rglob("*") if p.is_file()}


def layout(font):
    data = font.read_bytes()
    base = 29
    height = data[base + 1]
    if data[:4] != b"CHAR" or height != 9:
        raise ValueError("Unerwartete Introschrift")
    widths = []
    for line in NEW_LINES + OLD_LINES:
        width = 1
        for char in line.encode("cp850"):
            offset = struct.unpack_from("<I", data, base + 4 + char * 4)[0]
            if not offset:
                raise ValueError("Zeichen fehlt in der Introschrift: " + repr(chr(char)))
            width += data[base + offset] + struct.unpack_from("b", data, base + offset + 2)[0]
        if width > 300:
            raise ValueError("Introzeile ist zu breit: " + line)
        widths.append(width)
    # Move only the three added lines upward; the old credits retain y=178/187.
    y = 178 - len(NEW_LINES) * height
    if y + len(widths) * height > 200:
        raise ValueError("Introtext passt nicht auf den Bildschirm")
    return {"charset": 2, "line_height_px": height, "line_widths_px": widths,
            "x_center_px": 160, "y_px": y, "bottom_exclusive_px": y + len(widths) * height}


def patch_resources(game, tool, work):
    """Operate exclusively on a disposable copy of monkey.000/001."""
    # ScummRP expects uppercase filenames, including on case-sensitive USB disks.
    for name in BASIS_HASHES:
        (game / name).rename(game / name.upper())
    try:
        return _patch_resources(game, tool, work)
    finally:
        for name in BASIS_HASHES:
            upper = game / name.upper()
            if upper.exists():
                upper.rename(game / name)


def _patch_resources(game, tool, work):
    before, after, edits = work / "before", work / "after", work / "edits"
    run_scummrp(tool, game, before, "-o")
    old = (before / INTRO).read_bytes()
    if old != b"ENCD" + struct.pack(">I", 8 + len(OLD_BODY)) + OLD_BODY:
        raise ValueError("Intro entspricht nicht dem unveraenderten deutschen UTE-1.02-Patch")
    metrics = layout(before / FONT)
    prefix = OLD_PREFIX.replace(b"\x00\xa0\x00\xb2\x00\x0f",
                               b"\x00\xa0\x00" + struct.pack("<H", metrics["y_px"]) + b"\x0f")
    # The only jump precedes the print opcode and points backward; it is unchanged.
    body = prefix + NEWLINE.join(s.encode("cp850") for s in NEW_LINES + OLD_LINES) + b"\x00\xa0"
    new = b"ENCD" + struct.pack(">I", 8 + len(body)) + body
    target = edits / INTRO
    target.parent.mkdir(parents=True)
    target.write_bytes(new)
    run_scummrp(tool, game, edits, "-i", "ENCD")
    run_scummrp(tool, game, after, "-o")
    old_hashes, new_hashes = resource_hashes(before), resource_hashes(after)
    if old_hashes.keys() != new_hashes.keys():
        raise ValueError("Ressourcenbestand hat sich veraendert")
    changed = [name for name in old_hashes if old_hashes[name] != new_hashes[name]]
    if changed != [INTRO.as_posix()] or (after / INTRO).read_bytes() != new:
        raise ValueError("Ausser dem Intro wurde eine Spielressource veraendert")
    return {"resource": INTRO.as_posix(), "resource_count": len(old_hashes),
            "changed_resources": changed, "lines": list(NEW_LINES + OLD_LINES),
            "encoding": "CP850; FF 01 line breaks", "layout": metrics,
            "game_runtime_tested": False}


def resolve_scummrp(root=ROOT):
    """Prefer the installed runtime; use the local vendor build for development."""
    root = Path(root)
    for candidate in (root / "bin/scummrp", root / "work/vendor/scummtr/build/bin/scummrp",
                      ROOT / "work/vendor/scummtr/build/bin/scummrp"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError("ScummRP fehlt: Runtime einrichten (bin/scummrp)")


def patch_build(game, tool, *, published_root=None, announce=True):
    game = game.resolve(strict=True)
    tool = tool.resolve(strict=True)
    published_root = Path(published_root).resolve() if published_root is not None else game
    names = ("monkey.000", "monkey.001", "build-report.json", "checksums.json")
    for name in names + ("monkey.sof",):
        if not (game / name).is_file() or (game / name).is_symlink():
            raise ValueError("Benoetigt einen generierten KI-Spielbuild mit regulaerer Datei " + name)
    marker = game / "intro-patch-report.json"
    if marker.exists():
        report = json.loads(marker.read_text(encoding="utf-8"))
        if report.get("lines") == list(NEW_LINES + OLD_LINES) and all(
                sha256(game / name) == report.get("patched_files", {}).get(name, {}).get("sha256")
                for name in BASIS_HASHES):
            if announce:
                print("Widmung ist bereits vorhanden; keine Aenderung.")
            return report
        raise ValueError("Vorhandener Intro-Patch ist abweichend oder beschaedigt")
    original = {name: file_info(game / name) for name in names}
    for name, expected in BASIS_HASHES.items():
        if original[name]["sha256"] != expected:
            raise ValueError("Unbekannte Spielbasis: " + name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = game / ("intro-backup-" + stamp)
    untouched = {name: (game / name).stat() for name in ("monkey.sof", "selected-snapshot.json")
                 if (game / name).is_file()}
    with tempfile.TemporaryDirectory(prefix=".intro-patch-", dir=game.parent) as temp:
        work = Path(temp)
        staging = work / "game"
        staging.mkdir()
        for name in names:
            shutil.copy2(game / name, staging / name)
        report = patch_resources(staging, tool, work)
        report.update(format="mi1-intro-patch/1", patched_at_utc=stamp, game_root=str(published_root),
                      backup_root=str(published_root / backup.name), original_files=original,
                      patched_files={name: file_info(staging / name) for name in BASIS_HASHES})
        build = json.loads((staging / "build-report.json").read_text(encoding="utf-8"))
        build.setdefault("post_build_patches", []).append({
            "kind": "intro-dedication", "report": marker.name, "patched_at_utc": stamp,
            "game_runtime_tested": False})
        for entry in build["files"]:
            if entry["name"] in report["patched_files"]:
                entry.update(report["patched_files"][entry["name"]])
        write_json(staging / "build-report.json", build)
        write_json(staging / marker.name, report)
        checksums = json.loads((staging / "checksums.json").read_text(encoding="utf-8"))
        for entry in checksums["files"]:
            if entry["name"] in names and entry["name"] != "checksums.json":
                entry.update(file_info(staging / entry["name"]))
        checksums["files"].append(file_info(staging / marker.name))
        write_json(staging / "checksums.json", checksums)
        if any(sha256(game / name) != original[name]["sha256"] for name in names):
            raise ValueError("Spielbuild wurde waehrend der Vorbereitung geaendert")
        backup.mkdir()
        for name in names:
            shutil.copy2(game / name, backup / name)
            if sha256(backup / name) != original[name]["sha256"]:
                raise ValueError("Sicherung konnte nicht bestaetigt werden")
        installed = []
        try:
            for name in names + (marker.name,):
                os.replace(staging / name, game / name)
                installed.append(name)
            if any(sha256(game / name) != report["patched_files"][name]["sha256"]
                   for name in BASIS_HASHES):
                raise ValueError("Installierte Spielbytes stimmen nicht mit der Pruefung ueberein")
            for name, stat in untouched.items():
                current = (game / name).stat()
                if (current.st_size, current.st_mtime_ns, current.st_ino) != (
                        stat.st_size, stat.st_mtime_ns, stat.st_ino):
                    raise ValueError("Spracharchiv oder Auswahlsnapshot wurde gleichzeitig geaendert")
        except BaseException:
            for name in installed:
                if name == marker.name:
                    (game / name).unlink(missing_ok=True)
                else:
                    shutil.copy2(backup / name, game / name)
            raise
    if announce:
        print("Intro geaendert: " + str(published_root))
        print("Sicherung: " + report["backup_root"])
        print("Pruefung: nur Raum 10 / ENCD geaendert; Spracharchiv unveraendert.")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, required=True,
                        help="Fertiger KI-Spielbuild; Original-Spielbasis wird abgelehnt")
    parser.add_argument("--scummrp", type=Path, help="Sonst bin/scummrp oder lokaler Vendor-Build")
    args = parser.parse_args()
    try:
        patch_build(args.game_dir, args.scummrp or resolve_scummrp())
    except (OSError, ValueError, RuntimeError, KeyError, struct.error) as error:
        parser.exit(1, "Intro-Patch fehlgeschlagen: " + str(error) + "\n")


if __name__ == "__main__":
    main()
