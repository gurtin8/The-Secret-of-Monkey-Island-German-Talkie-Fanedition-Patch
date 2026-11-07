"""Explicit GOG PC-SE data import. Never executes the supplied installer.

Opening a project only calls the cheap classifier. Extraction is an explicit
preparation action using a separately acquired, native innoextract executable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import uuid

from .contracts import (ContractError, LocalInstallation, canonical_json,
                        load_or_create_installation, relative_path, resolve_relative)

FORMAT = "mi1-gog-input/1"
GOG_GAME_ID = "1207666253"
_FOREIGN = "MI1 Generator/foreign/preparation/gog"
_INCLUDE = ["--include", "Monkey1.pak", "--include", "audio"]
_REQUIRED = {"Monkey1.pak", "audio/Speech.xwb"}
_MESSAGE = ("Bitte den GOG-Offline-Installer von The Secret of Monkey Island – "
            "Special Edition oder einen bereits entpackten PC-SE-Spielordner auswählen.")


def classify_game_input(path: str | Path) -> str:
    """Classify a directory unchanged, or a PE EXE candidate without authentication."""
    source = Path(path)
    if source.is_dir():
        return "directory"
    if source.is_file() and not source.is_symlink() and source.suffix.casefold() == ".exe":
        try:
            with source.open("rb") as stream:
                header = stream.read(64)
                if len(header) == 64 and header[:2] == b"MZ":
                    offset = struct.unpack_from("<I", header, 60)[0]
                    if 64 <= offset <= source.stat().st_size - 4:
                        stream.seek(offset)
                        if stream.read(4) == b"PE\0\0":
                            return "gog_installer_candidate"
        except OSError as exc:
            raise ContractError(f"Spieleingabe kann nicht gelesen werden. {_MESSAGE}") from exc
    raise ContractError(f"Nicht unterstützte Spieleingabe. {_MESSAGE}")


def _hash_file(path: Path, algorithm: str = "sha256") -> str:
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _run(extractor: Path, source: Path, options: list[str], log: Path, runner) -> str:
    """Use regular files for child output; large extraction logs cannot fill pipes."""
    with log.open("xb") as output:
        try:
            runner([str(extractor), "--color=0", "--progress=0", "--no-extract-unknown",
                    *options, str(source)], check=True, stdout=output, stderr=subprocess.STDOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ContractError(f"GOG-Installer konnte nicht mit innoextract gelesen werden. {_MESSAGE} "
                                f"Prüfprotokoll: {log}") from exc
    return log.read_text(encoding="utf-8", errors="replace")


def _metadata(info: str, game_id: str) -> dict:
    match = re.search(r'^Inspecting "([^"\n]+)" - setup data version ([^\n]+)$', info, re.M)
    ids = [re.findall(r"^GOG\.com game ID is (\d+)\s*$", text, re.M)
           for text in (info, game_id)]
    if match is None or ids != [[GOG_GAME_ID], [GOG_GAME_ID]]:
        raise ContractError(f"Installer ist kein unterstützter GOG-Inno-PC-SE-Installer. {_MESSAGE}")
    title = re.sub(r"[™®–—-]", " ", match[1]).casefold()
    if " ".join(title.split()) != "the secret of monkey island special edition":
        raise ContractError(f"GOG-Spielkennung und Installer-Titel passen nicht zu Monkey Island 1. {_MESSAGE}")
    version = match[2].strip()
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}(?: \(unicode\))?", version):
        raise ContractError(f"Unbekannte Inno-Setup-Metadaten. {_MESSAGE}")
    return {"gog_game_id": GOG_GAME_ID, "title": match[1], "inno_setup_data_version": version}


def _members(listing: str) -> dict:
    members = {}
    for line in listing.splitlines():
        if not line.startswith(' - "'):
            continue
        match = re.fullmatch(r' - "([^"\n]+)"(?: \[[^\]\n]+\])* \([^\n]+\) '
                             r'(MD5|SHA-1|SHA-256) ([0-9a-fA-F]+)', line)
        if match is None:
            raise ContractError("Inno-Dateiliste enthält eine nicht prüfbare Spieldatei")
        name, kind, checksum = match.groups()
        relative_path(name)
        if (name != "Monkey1.pak" and not name.startswith("audio/")) or name in members:
            raise ContractError("Inno-Dateiliste enthält unerwartete oder doppelte Spieldateien")
        if name != "Monkey1.pak" and Path(name).suffix.casefold() not in {".xwb", ".xsb", ".xgs", ".info"}:
            raise ContractError("GOG-Audioordner enthält keine unterstützte PC-SE-Datendatei")
        algorithm, length = {"MD5": ("md5", 32), "SHA-1": ("sha1", 40),
                             "SHA-256": ("sha256", 64)}[kind]
        if not 1 <= len(checksum) <= length:
            raise ContractError("Ungültige Inno-Dateiprüfsumme")
        # innoextract 1.9 prints hexadecimal checksums without leading zeroes.
        members[name] = {"algorithm": algorithm, "checksum": checksum.lower().zfill(length)}
    if not _REQUIRED <= members.keys():
        raise ContractError(f"GOG-Installer enthält nicht Monkey1.pak und audio/Speech.xwb. {_MESSAGE}")
    if len({name.casefold() for name in members}) != len(members):
        raise ContractError("GOG-Dateiliste enthält auf macOS mehrdeutige Pfade")
    return members


def _inventory(game: Path, members: dict | None = None) -> list[dict]:
    if not game.is_dir() or game.is_symlink():
        raise ContractError("GOG-Spielordner fehlt oder ist ein symbolischer Link")
    files = []
    for path in sorted(game.rglob("*")):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ContractError("GOG-Ausgabe enthält einen Link oder eine nicht reguläre Datei")
        if path.is_file():
            name = path.relative_to(game).as_posix()
            relative_path(name)
            if members is not None and name not in members:
                raise ContractError(f"Unerwartete GOG-Ausgabedatei: {name}")
            hashes = {"sha256": hashlib.sha256()}
            if members is not None:
                algorithm = members[name]["algorithm"]
                hashes.setdefault(algorithm, hashlib.new(algorithm))
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    for value in hashes.values():
                        value.update(block)
            item = {"path": name, "bytes": path.stat().st_size,
                    "sha256": hashes["sha256"].hexdigest()}
            if members is not None:
                expected = members[name]
                if hashes[expected["algorithm"]].hexdigest() != expected["checksum"]:
                    raise ContractError(f"Entpackte GOG-Datei stimmt nicht mit Installer-Prüfsumme überein: {name}")
            files.append(item)
    if members is not None and {item["path"] for item in files} != members.keys():
        raise ContractError("GOG-Entpackung ist unvollständig; erwartete Spieldateien fehlen")
    return files


def _signed(body: dict, installation: LocalInstallation) -> dict:
    return {**body, "local_signature": hmac.new(installation.receipt_key,
                canonical_json(body), hashlib.sha256).hexdigest()}


def verify_gog_receipt(receipt: dict, installation: LocalInstallation, *, source_sha256: str,
                       extractor_sha256: str, game_directory: Path) -> bool:
    """Validate source/tool/host binding and the complete current extracted inventory."""
    try:
        if (set(receipt) != {"format", "kind", "installation_id", "source_sha256", "source_bytes",
                            "extractor_sha256", "metadata", "members", "files", "local_signature"}
                or receipt["format"] != FORMAT or receipt["kind"] != "gog_import_receipt"
                or receipt["installation_id"] != installation.installation_id
                or receipt["source_sha256"] != source_sha256
                or receipt["extractor_sha256"] != extractor_sha256):
            return False
        body = {key: value for key, value in receipt.items() if key != "local_signature"}
        if not hmac.compare_digest(receipt["local_signature"], _signed(body, installation)["local_signature"]):
            return False
        return _inventory(game_directory) == receipt["files"]
    except (ContractError, OSError, TypeError, KeyError, ValueError):
        return False


def materialize_gog_input(work_root: Path, installer: Path, extractor: Path, *,
                          runner=subprocess.run, progress=None) -> dict:
    """Inspect then selectively extract the GOG PC-SE data into foreign storage.

    Completed imports are immutable and may be reused only with a matching
    OS-local signature and full output inventory. Failed staging is retained
    for diagnosis and never promoted or silently reused.
    """
    state = {"selected_installer": str(Path(installer).absolute()), "format": FORMAT,
             "status": "running", "phase": "hashing-source"}

    def update(**values):
        state.update(values)
        if progress is not None:
            progress(dict(state))

    update()
    try:
        result = _materialize(work_root, installer, extractor, runner, update)
    except Exception as exc:
        update(status="failed", phase="failed", error=str(exc))
        raise
    update(status="completed", phase="completed", game_directory=result["game_directory"],
           file_count=len(result["import_receipt"]["files"]), reused=result["reused"])
    return result


def _materialize(work_root, installer, extractor, runner, update):
    root = Path(work_root).resolve(strict=True)
    if classify_game_input(installer) != "gog_installer_candidate":
        raise ContractError(f"Für den GOG-Import ist eine Installer-EXE erforderlich. {_MESSAGE}")
    source = Path(installer).resolve(strict=True)
    tool = Path(extractor).resolve(strict=True)
    if not tool.is_file() or tool == source or tool.suffix.casefold() == ".exe":
        raise ContractError("GOG-Import benötigt das separat bezogene native innoextract-Werkzeug")
    source_sha = _hash_file(source)
    source_bytes = source.stat().st_size
    tool_sha = _hash_file(tool)
    parent = resolve_relative(root, _FOREIGN)
    destination = resolve_relative(root, f"{_FOREIGN}/{source_sha}")
    game = destination / "game"
    receipt_path = destination / "import-receipt.json"
    installation = load_or_create_installation(root)
    if destination.exists():
        update(phase="verifying-reuse", game_directory=str(game), log_path=str(destination / "extract.log"))
        try:
            if receipt_path.is_symlink() or not receipt_path.is_file():
                raise ValueError("missing receipt")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ContractError("GOG-Importziel enthält keine gültige Quittung; vorhandene Daten bleiben erhalten") from exc
        if not verify_gog_receipt(receipt, installation, source_sha256=source_sha,
                                  extractor_sha256=tool_sha, game_directory=game):
            raise ContractError("Vorhandener GOG-Import ist verändert, beschädigt oder stammt von einem anderen Host; Daten bleiben erhalten")
        update(supported=True, gog_game_id=receipt["metadata"]["gog_game_id"],
               expected_file_count=len(receipt["files"]))
        return {"format": FORMAT, "game_directory": str(game), "source_sha256": source_sha,
                "import_receipt": receipt, "receipt_path": str(receipt_path), "reused": True}

    # Authentication and the complete selected checksum catalogue precede any
    # writes into foreign/game storage. Metadata output uses regular temp files.
    with tempfile.TemporaryDirectory(prefix="mi1-gog-inspect-") as temporary:
        inspection = Path(temporary)
        update(phase="inspecting", log_path=str(inspection / "info.log"))
        info = _run(tool, source, ["--info"], inspection / "info.log", runner)
        game_id = _run(tool, source, ["--gog-game-id"], inspection / "gog-game-id.log", runner)
        metadata = _metadata(info, game_id)
        update(phase="listing", supported=True, gog_game_id=metadata["gog_game_id"],
               log_path=str(inspection / "members.log"))
        listing = _run(tool, source, ["--list", "--list-checksums", *_INCLUDE],
                       inspection / "members.log", runner)
        members = _members(listing)
        parent.mkdir(parents=True, exist_ok=True)
        lock = parent / f".{source_sha}.lock"
        try:
            lock.mkdir()
        except FileExistsError as exc:
            raise ContractError("Dieser GOG-Import läuft bereits oder sein Sperrordner muss nach einem Abbruch geprüft werden") from exc
        stage = parent / f".{source_sha}.{uuid.uuid4().hex}.part"
        try:
            if destination.exists():
                raise ContractError("GOG-Importziel wurde inzwischen angelegt; bitte erneut starten")
            stage.mkdir()
            for log in inspection.iterdir():
                shutil.copyfile(log, stage / log.name)
            staged_game = stage / "game"
            staged_game.mkdir()
            update(phase="extracting", game_directory=str(staged_game),
                   log_path=str(stage / "extract.log"), expected_file_count=len(members))
            _run(tool, source, ["--extract", "--exclude-temp", *_INCLUDE,
                               "--output-dir", str(staged_game)], stage / "extract.log", runner)
            update(phase="verifying-checksums")
            inventory = _inventory(staged_game, members)
            if source.stat().st_size != source_bytes or _hash_file(source) != source_sha:
                raise ContractError("GOG-Quelldatei wurde während des Imports verändert; Import wird nicht aktiviert")
            if _hash_file(tool) != tool_sha:
                raise ContractError("innoextract wurde während des Imports verändert; Import wird nicht aktiviert")
            receipt = _signed({"format": FORMAT, "kind": "gog_import_receipt",
                "installation_id": installation.installation_id, "source_sha256": source_sha,
                "source_bytes": source_bytes, "extractor_sha256": tool_sha,
                "metadata": metadata, "members": members, "files": inventory}, installation)
            with (stage / "import-receipt.json").open("xb") as output:
                output.write(canonical_json(receipt) + b"\n")
                output.flush()
                os.fsync(output.fileno())
            stage.rename(destination)
            update(log_path=str(destination / "extract.log"))
        finally:
            lock.rmdir()
    return {"format": FORMAT, "game_directory": str(game), "source_sha256": source_sha,
            "import_receipt": receipt, "receipt_path": str(receipt_path), "reused": False}
