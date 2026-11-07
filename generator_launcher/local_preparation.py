"""Small, resumable local game and fixed-recipe preparation adapter.

This module deliberately has no import-time downloads, audio work, or game work.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import uuid

from .contracts import (ContractError, canonical_json, digest, relative_path, resolve_relative,
                        LocalInstallation, load_or_create_installation)

FORMAT = "mi1-local-preparation/1"
NATIVE_AUDIO_MODE = "native_experimental"
PREPARATION_RECIPE = Path(__file__).with_name("recipes") / "preparation.json"


def _sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _fetch_original(artifact: dict, target: Path, runner=subprocess.run) -> Path:
    """Resume an approved original download and activate only matching bytes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_symlink() or (artifact.get("bytes") is not None and target.stat().st_size != artifact["bytes"]) or _sha(target) != artifact["sha256"]:
            raise ContractError(f"Vorhandener Originaldownload stimmt nicht mit Rezept überein: {target.name}")
        return target
    part = target.with_name(target.name + ".part")
    if part.is_symlink():
        raise ContractError(f"Temporäre Originaldatei darf kein symbolischer Link sein: {part.name}")
    expected_bytes = artifact.get("bytes")
    # A byte-complete file with the wrong digest cannot be repaired by curl's
    # resume mode. Preserve it under a diagnostic name so the next attempt
    # starts from byte zero without destroying evidence.
    if part.is_file() and expected_bytes is not None and part.stat().st_size >= expected_bytes:
        actual_sha = _sha(part)
        if part.stat().st_size == expected_bytes and actual_sha == artifact["sha256"]:
            os.replace(part, target)
            return target
        else:
            quarantine = part.with_name(f"{part.name}.corrupt-{actual_sha[:12]}-{uuid.uuid4().hex[:12]}")
            os.replace(part, quarantine)
            raise ContractError(
                f"Vollständige .part-Datei hat falschen SHA-256; erhalten unter {quarantine.name}. "
                f"Erneuter Versuch startet ohne Resume bei 0 Byte (erwartet {expected_bytes} Byte, "
                f"SHA-256 {artifact['sha256']}; erhalten SHA-256 {actual_sha})."
            )
    args = ["/usr/bin/curl", "--http1.1", "--location", "--fail", "--show-error", "--silent",
            "--connect-timeout", "30", "--speed-limit", "1024", "--speed-time", "120",
            "--continue-at", "-", "--output", str(part), artifact["url"]]
    runner(args, check=True)
    actual_bytes = part.stat().st_size if part.exists() else 0
    actual_sha = _sha(part) if part.is_file() else "nicht verfügbar"
    if ((expected_bytes is not None and actual_bytes != expected_bytes)
            or actual_sha != artifact["sha256"]):
        if expected_bytes is not None and actual_bytes >= expected_bytes and part.is_file():
            quarantine = part.with_name(f"{part.name}.corrupt-{actual_sha[:12]}-{uuid.uuid4().hex[:12]}")
            os.replace(part, quarantine)
            raise ContractError(
                f"Originaldownload hat falsche Größe oder falschen Hash; erhalten unter {quarantine.name}. "
                f"Erneuter Versuch startet bei 0 Byte (erhalten {actual_bytes} Byte, erwartet {expected_bytes}; "
                f"SHA-256 {actual_sha}, erwartet {artifact['sha256']})."
            )
        raise ContractError(
            f"Originaldownload unvollständig oder Hash falsch; .part bleibt für Resume erhalten: {target.name} "
            f"(erhalten {actual_bytes} Byte, erwartet {expected_bytes if expected_bytes is not None else 'unbekannt'}; "
            f"SHA-256 {actual_sha}, erwartet {artifact['sha256']})."
        )
    os.replace(part, target)
    return target


def preparation_downloads(recipe=None):
    """Pure pinned package catalogue, shared by preview and acquisition."""
    recipe = recipe or json.loads(PREPARATION_RECIPE.read_text(encoding="utf-8"))
    return {
        "ute": {"url": recipe["builder"]["url"], "sha256": recipe["builder"]["sha256"], "bytes": recipe["builder"]["bytes"], "name": "ute-1.02.zip"},
        "german_patch": {"url": recipe["german_patch"]["url"], "sha256": recipe["german_patch"]["sha256"], "bytes": recipe["german_patch"]["bytes"], "name": "german-v1.01.zip"},
        "wine": {"url": recipe["wine"]["url"], "sha256": recipe["wine"]["sha256"], "name": "wine-11.18.tar.xz"},
    }


def acquire_preparation_sources(work_root: Path, *, terms_confirmed: bool,
                                runner=subprocess.run) -> dict:
    """Fetch exact P01 original packages and extract into foreign-only storage.

    Caller must obtain explicit terms consent first. This is never invoked by
    project open, manifest load/save, or recipe resolution.
    """
    if not terms_confirmed:
        raise ContractError("Originalbedingungen müssen vor dem Paketdownload bestätigt werden")
    root = work_root.resolve(strict=True)
    recipe = json.loads(PREPARATION_RECIPE.read_text(encoding="utf-8"))
    foreign = root / "MI1 Generator" / "foreign" / "preparation"
    foreign.mkdir(parents=True, exist_ok=True)
    sources = preparation_downloads(recipe)
    archives = {name: _fetch_original(value, foreign / value["name"], runner) for name, value in sources.items()}
    extracted = foreign / "extracted"
    extracted.mkdir(exist_ok=True)
    ute_dir, patch_dir, wine_dir = extracted / "ute", extracted / "german-patch", extracted / "wine"
    extractions = (
        ("ute", ute_dir, ["/usr/bin/ditto", "-x", "-k"]),
        ("german_patch", patch_dir, ["/usr/bin/ditto", "-x", "-k"]),
        ("wine", wine_dir, ["/usr/bin/tar", "-xf"]),
    )
    for source_key, destination, command in extractions:
        marker = destination / ".source-sha256"
        if marker.is_file() and marker.read_text().strip() == sources[source_key]["sha256"]:
            continue
        archive = archives[source_key]
        stage = destination.with_name(destination.name + ".part")
        shutil.rmtree(stage, ignore_errors=True); stage.mkdir(parents=True)
        args = (command + [str(archive), str(stage)] if command[-1] == "-k"
                else command + [str(archive), "-C", str(stage)])
        runner(args, check=True)
        (stage / ".source-sha256").write_text(sources[source_key]["sha256"] + "\n")
        shutil.rmtree(destination, ignore_errors=True); stage.replace(destination)
    flac_candidates = list(ute_dir.rglob("install_flac.bat"))
    if len(flac_candidates) != 1:
        raise ContractError("Im UTE-Paket muss genau eine install_flac.bat als Quelle der Rezeptur vorliegen")
    patch_candidates = [item for item in patch_dir.rglob("*") if item.is_file() and _sha(item) == recipe["german_patch"]["exe_sha256"]]
    if len(patch_candidates) != 1:
        raise ContractError("Deutsches Patchpaket enthält nicht genau die gebundene Patch-EXE")
    return {"recipe": recipe, "archives": {key: value.relative_to(root).as_posix() for key, value in archives.items()},
            "stages": {"builder": flac_candidates[0].relative_to(root).as_posix(),
                       "patch": patch_candidates[0].relative_to(root).as_posix()},
            "wine_root": wine_dir.relative_to(root).as_posix(),
            "installer_action_required": False,
            "rosetta_action": "macOS prompts for Rosetta when the original x86_64 Wine app is first opened"}


def open_original_wine(work_root: Path, *, runner=subprocess.run) -> None:
    """Open the exact extracted x86_64 Wine app so macOS can show Rosetta consent."""
    root = work_root.resolve(strict=True)
    archive = root / "MI1 Generator/foreign/preparation/wine-11.18.tar.xz"
    if _sha(archive) != json.loads(PREPARATION_RECIPE.read_text())["wine"]["sha256"]:
        raise ContractError("Wine-Originaldownload fehlt oder Hash stimmt nicht")
    wine_root = root / "MI1 Generator/foreign/preparation/extracted/wine"
    candidates = list(wine_root.rglob("Wine Devel.app"))
    if len(candidates) != 1:
        raise ContractError("Im gebundenen Originalbundle muss genau ein Wine Devel.app enthalten sein")
    runner(["/usr/bin/open", str(candidates[0])], check=True)


def resolve_wine_binary(wine_root: Path) -> Path:
    """Choose the original CLI, excluding the app's separate GUI wrapper."""
    candidates = [path for path in wine_root.glob("*.app/Contents/Resources/wine/bin/wine")
                  if path.is_file() and os.access(path, os.X_OK)]
    if len(candidates) != 1:
        raise ContractError("Originale Wine-CLI im Resources/wine/bin-Ordner fehlt oder ist mehrdeutig")
    return candidates[0]


def _effective_manifest(manifest: dict, correction: dict | None) -> dict:
    # Corrections are applied by the existing strict patch-review contract. For
    # preparation, only their explicit manifest snapshot override is accepted.
    result = json.loads(json.dumps(manifest))
    if correction:
        edits = correction.get("edits", correction.get("changes", []))
        if not isinstance(edits, list):
            raise ContractError("Korrektur enthält keine gültige edit-Liste")
        for edit in edits:
            if not isinstance(edit, dict):
                raise ContractError("Korrektureintrag ist ungültig")
            dialog_id = edit.get("dialog_id")
            if not dialog_id:
                continue
            matches = [d for d in result.get("dialogues", []) if d.get("dialog_id") == dialog_id]
            if len(matches) != 1:
                raise ContractError(f"Korrektur-Dialog unbekannt oder doppelt: {dialog_id}")
            for field in ("text", "role_id", "source_dialog_id"):
                if field in edit:
                    matches[0][field] = edit[field]
    return result


def resolve_preparation(manifest: dict, correction: dict | None = None,
                        extraction: dict | None = None) -> dict:
    """Resolve fixed recipes from the selected effective manifest, without I/O/TTS."""
    effective = _effective_manifest(manifest, correction)
    evidence = effective.get("source_evidence", {}).get("launcher_preparation")
    if not isinstance(evidence, dict) or evidence.get("format") != FORMAT:
        raise ContractError("Ausgewähltes Manifest enthält keine launcher_preparation/1-Rezepte")
    required = {"sources", "roles", "stages", "runtime"}
    fields = set(evidence)
    if not required.issubset(fields) or fields - (required | {"format", "script_exports"}):
        raise ContractError("launcher_preparation enthält fehlende oder unbekannte Felder")
    if "script_exports" in evidence:
        exports = evidence["script_exports"]
        if (not isinstance(exports, dict)
                or set(exports) != {"format", "english_sha256", "german_sha256", "english_rows", "german_rows"}
                or exports.get("format") != "mi1-scummtr-local-index/1"
                or any(not isinstance(exports.get(key), str) or len(exports[key]) != 64
                       or any(c not in "0123456789abcdef" for c in exports[key])
                       for key in ("english_sha256", "german_sha256"))
                or any(type(exports.get(key)) is not int or exports[key] <= 0
                       for key in ("english_rows", "german_rows"))):
            raise ContractError("launcher_preparation script_exports ist ungültig")
    sources, roles = evidence["sources"], evidence["roles"]
    if not isinstance(sources, list) or not isinstance(roles, list):
        raise ContractError("launcher_preparation sources/roles müssen Listen sein")
    source_ids = set()
    source_required = {"dialog_id", "native_speech_id", "source_kind", "archive_stage", "archive_member"}
    for item in sources:
        if (not isinstance(item, dict) or not source_required.issubset(item)
                or set(item) - (source_required | {"script_export_locations", "publication_note"})):
            raise ContractError("Quellrecipe enthält fehlende oder unbekannte Felder")
        if ("script_export_locations" in item and not isinstance(item["script_export_locations"], dict)
                or "publication_note" in item and not isinstance(item["publication_note"], str)):
            raise ContractError("Quellrecipe enthält ungültige Exportmetadaten")
        dialog_id = item["dialog_id"]
        if not isinstance(dialog_id, str) or not dialog_id.startswith("mi1-ute-1.02:vctl:"):
            raise ContractError(f"Ungültige VCTL-Dialog-ID: {dialog_id}")
        offset = dialog_id.rsplit(":", 1)[-1]
        if not offset.isdigit() or str(item["native_speech_id"]) != offset:
            raise ContractError(f"Widersprüchliche VCTL-Quell-ID: {dialog_id}")
        if item["source_kind"] != "ute_sou_vctl" or item["archive_stage"] != "ute_english" or item["archive_member"] != "monster.sou":
            raise ContractError(f"Nicht unterstützte Originalquelle: {dialog_id}")
        if dialog_id in source_ids:
            raise ContractError(f"Doppelte Quell-ID: {dialog_id}")
        source_ids.add(dialog_id)
    roles_seen = set()
    resolved_roles = []
    for role in roles:
        if not isinstance(role, dict) or not isinstance(role.get("role_id"), str):
            raise ContractError("Rollenrecipe ohne role_id")
        if role["role_id"] in roles_seen:
            raise ContractError(f"Doppeltes Rollenrecipe: {role['role_id']}")
        roles_seen.add(role["role_id"])
        if role.get("without_anchor") is True:
            resolved_roles.append(role); continue
        anchor = role.get("reference")
        prewarm = role.get("prewarm")
        if not isinstance(anchor, dict) or not isinstance(prewarm, dict):
            raise ContractError(f"Rollenrecipe fehlt Referenz oder Prewarm: {role['role_id']}")
        ids = anchor.get("source_dialog_ids")
        if not isinstance(ids, list) or not ids or any(value not in source_ids for value in ids):
            unknown = next((value for value in ids or [] if value not in source_ids), "(leer)")
            raise ContractError(f"Unbekannte Referenz-ID für {role['role_id']}: {unknown}")
        for key in ("prompt_text", "join_silence_ms", "minimum_duration_ms", "maximum_duration_ms"):
            if key not in anchor:
                raise ContractError(f"Referenzrecipe {role['role_id']} fehlt Feld {key}")
        for key in ("source", "target_id", "text", "reference_mode", "seed"):
            if key not in prewarm:
                raise ContractError(f"Prewarmrecipe {role['role_id']} fehlt Feld {key}")
        if not isinstance(prewarm["seed"], int) or prewarm["seed"] < 0:
            raise ContractError(f"Ungültiger Prewarm-Seed für {role['role_id']}")
        resolved_roles.append(role)
    return {"format": FORMAT, "manifest_id": effective.get("manifest_id"),
            "revision": effective.get("revision", effective.get("manifest_revision", 1)),
            "roles": resolved_roles, "sources": sources, "stages": evidence["stages"],
            "runtime": evidence["runtime"], "correction_sha256": digest(correction) if correction else None}


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(canonical_json(value) + b"\n")
    os.replace(temporary, path)


def _inventory(path: Path) -> dict:
    if path.is_file():
        return {path.name: _sha(path)}
    return {item.relative_to(path).as_posix(): _sha(item)
            for item in sorted(path.rglob("*")) if item.is_file()}


def _native_preparation_identity(identity) -> bool:
    # The oldest native receipts predate the explicit audio-mode field.
    return (isinstance(identity, dict) and bool(identity.get("native_audio"))
            and identity.get("audio_mode", NATIVE_AUDIO_MODE) == NATIVE_AUDIO_MODE)


def preserve_previous_preparation(output: Path) -> bool:
    """Save earlier or unidentifiable preparation when starting over."""
    if not output.is_dir():
        return False
    if not any(output.iterdir()):
        return False
    try:
        prior = json.loads((output / "english.receipt.json").read_text(encoding="utf-8"))
        return not isinstance(prior, dict) or not _native_preparation_identity(prior.get("identity"))
    except (OSError, ValueError):
        return True


PREPARATION_RESTART_REQUIRED = (
    "Die bisherige Spielvorbereitung erfordert einen Neubeginn. Verwende "
    "„Vorbereitung sichern und neu beginnen“ und starte danach die Spielvorbereitung."
)


def restart_game_preparation(work_root: Path) -> dict:
    """Reset generated preparation on explicit restart."""
    root = work_root.resolve(strict=True)
    relative = "MI1 Generator/generated/game-preparation"
    current = resolve_relative(root, relative)
    if preserve_previous_preparation(current):
        archive = current.with_name("game-preparation.previous-" + uuid.uuid4().hex)
        current.rename(archive)
        return {"removed": False, "removed_directories_count": 0, "archive_path": str(archive),
                "explanation": f"Bisherige Vorbereitung samt Protokollen gesichert unter {archive}. "
                               "Starte jetzt die Spielvorbereitung erneut."}
    candidates = [current] if current.exists() else []
    if current.parent.is_dir():
        for path in sorted(current.parent.iterdir()):
            if re.fullmatch(r"game-preparation\.saved-[0-9a-f]{32}", path.name):
                candidates.append(resolve_relative(root, "MI1 Generator/generated/" + path.name))
    for path in candidates:
        if not path.is_dir():
            raise ContractError("Vorbereitung ist kein Ordner; Neubeginn wurde nicht ausgeführt")
    for path in candidates:
        shutil.rmtree(path)
    return {"removed": bool(candidates), "removed_directories_count": len(candidates),
            "explanation": "Alte Vorbereitungskopien samt Protokollen und Stufenbelegen gelöscht. "
                           "Starte die Spielvorbereitung erneut."}


def _stage_receipt_error(receipt: Path, identity: dict, status: str, reasons: list[str]) -> ContractError:
    stage = ("Deutscher Patch" if identity.get("stage") == "ute_german"
             else "Native Spielvorbereitung (Englisch)")
    return ContractError(
        f"{stage}: Vorbereitungsstufe hat abweichenden oder unvollständigen Beleg. "
        f"Beleg: {receipt.absolute()}; bisheriger Status: {status}. "
        + "; ".join(reasons)
        + ". Aktion „Vorbereitung löschen und neu beginnen“ verwenden; dabei werden die alten "
          "Vorbereitungskopien samt Belegen und Protokollen gelöscht."
    )


def _run_stage(command: list[str], cwd: Path, receipt: Path, identity: dict,
               output_directory: Path, *, root: Path, installation: LocalInstallation,
               project_id: str, environment: dict | None = None, validate_output=None,
               progress=None, executor=None, retry_failed=True) -> dict:
    def report(record, *, status=None):
        if progress is not None:
            progress({"phase": "german_patch" if identity.get("stage") == "ute_german" else "original_builder",
                      "stage": identity.get("stage"), "stage_id": record.get("stage_id"),
                      "status": status or record["status"], "cwd": str(cwd),
                      "log_path": record.get("log_path"), "error": record.get("error"),
                      "file_count": None, "expected_file_count": None})
    if receipt.exists():
        try:
            prior = json.loads(receipt.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise _stage_receipt_error(receipt, identity, "unbekannt",
                                       ["Beleg ist nicht als JSON lesbar"]) from exc
        if not isinstance(prior, dict):
            raise _stage_receipt_error(receipt, identity, "unbekannt",
                                       ["Beleg ist kein JSON-Objekt"])
        artifacts = prior.get("outputs")
        inventory = _inventory(output_directory)
        signed = prior.get("signed_receipt")
        try:
            verified = isinstance(signed, dict) and verify_preparation_receipt(
                signed, installation, project_id=project_id, inputs=identity, work_root=root)
        except (TypeError, ValueError, AttributeError, OSError):
            verified = False
        if (prior.get("identity") == identity and prior.get("status") == "completed"
                and artifacts == inventory and verified):
            report(prior, status="reused")
            return prior
        # A known zero-output failed stage may be retried only by this explicit
        # caller, preserving the failure receipt. Interrupted running stages
        # and changed inputs remain blocked rather than replayed blindly.
        if (retry_failed and prior.get("identity") == identity and prior.get("status") == "failed"
                and prior.get("outputs_before") == inventory):
            receipt.replace(receipt.with_name(receipt.name + ".failed-" + uuid.uuid4().hex))
        else:
            reasons = []
            previous_identity = prior.get("identity")
            if isinstance(previous_identity, dict):
                changed = sorted(key for key in identity.keys() | previous_identity.keys()
                                 if key not in identity or key not in previous_identity
                                 or identity[key] != previous_identity[key])
                if changed:
                    reasons.append("Abweichende Eingabekennungen: " + ", ".join(changed))
            else:
                reasons.append("Eingabekennungen fehlen oder sind ungültig")
            status = prior.get("status")
            if status not in ("completed", "failed"):
                reasons.append("Stufe wurde nicht abgeschlossen")
            elif status == "failed" and prior.get("outputs_before") != inventory:
                reasons.append("Fehlgeschlagene Stufe hat Ausgaben verändert")
            elif status == "failed" and not retry_failed:
                reasons.append("Fehlgeschlagene Stufe erfordert einen ausdrücklichen Neustart")
            if artifacts != inventory and status == "completed":
                reasons.append("Ausgabeinventar wurde verändert oder ist unvollständig")
            if not verified and status == "completed":
                if not isinstance(signed, dict):
                    reasons.append("Signierter lokaler Beleg fehlt oder ist ungültig")
                elif signed.get("installation_id") != installation.installation_id:
                    reasons.append("Beleg gehört zu einer anderen lokalen Installation")
                elif signed.get("project_id") != project_id:
                    reasons.append("Beleg gehört zu einem anderen Projekt")
                elif signed.get("inputs") != identity:
                    inputs = signed.get("inputs")
                    changed = (sorted(key for key in identity.keys() | inputs.keys()
                                      if key not in identity or key not in inputs
                                      or identity[key] != inputs[key]) if isinstance(inputs, dict) else [])
                    reasons.append("Signierter Beleg hat abweichende Eingabekennungen"
                                   + (": " + ", ".join(changed) if changed else " (ungültige Struktur)"))
                else:
                    reasons.append("Lokale Signatur oder signierte Ausgabedateien sind ungültig")
            safe_status = status if status in ("running", "completed", "failed") else "unbekannt"
            raise _stage_receipt_error(receipt, identity, safe_status, reasons)
    record = {"identity": identity, "status": "running", "stage_id": str(uuid.uuid4()),
              "outputs_before": _inventory(output_directory), "command": command,
              "cwd": str(cwd)}
    log = receipt.with_name(receipt.stem + "." + record["stage_id"] + ".log")
    record["log_path"] = str(log)
    _atomic_json(receipt, record)
    report(record)
    try:
        # Keep process diagnostics in the stage log.
        with log.open("wb") as stream:
            if executor is None:
                subprocess.run(command, cwd=cwd, check=True, env=environment,
                               stdout=stream, stderr=subprocess.STDOUT)
            else:
                details = executor(stream)
                if details:
                    record.update(details)
        record["exit_code"] = 0
        if validate_output is not None:
            validate_output(output_directory)
        record["outputs"] = _inventory(output_directory)
        record["status"] = "completed"
        signed_outputs = {f"{output_directory.name}:{name}": {
            "path": (output_directory / name).relative_to(root).as_posix(), "sha256": sha}
            for name, sha in record["outputs"].items()}
        record["signed_receipt"] = preparation_receipt(installation, project_id=project_id,
            inputs=identity, outputs=signed_outputs)
    except subprocess.CalledProcessError as exc:
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 4000))
            detail = stream.read().decode("utf-8", "replace").strip()
        message = f"Originalwerkzeug fehlgeschlagen (Exit {exc.returncode}). Protokoll: {log}"
        if detail: message += "\n" + detail
        record.update(status="failed", error=message, exit_code=exc.returncode)
        raise ContractError(message) from exc
    except Exception as exc:
        record.update(status="failed", error=str(exc))
        raise
    finally:
        _atomic_json(receipt, record)
        report(record)
    return record


def prepare_game(*, work_root: Path, game_directory: Path, recipe: dict,
                 wine: Path, user_consented: bool = False, progress=None,
                 ffmpeg: Path | None = None, native_sox: Path | None = None,
                 native_jobs: int | None = None, patch_process_runner=None) -> dict:
    """Prepare the game with native audio, then run the German patch in Wine."""
    if not user_consented:
        raise ContractError("Werkzeugbedingungen vor der Spielvorbereitung bestätigen")
    root = work_root.resolve(strict=True)
    game = game_directory.resolve(strict=True)
    if not game.is_dir():
        raise ContractError("PC-Special-Edition-Ordner muss ein vorhandener Ordner sein")
    output = root / "MI1 Generator" / "generated" / "game-preparation"
    if preserve_previous_preparation(output):
        raise ContractError(PREPARATION_RESTART_REQUIRED)
    stages = recipe["stages"]
    builder = resolve_relative(root, stages["builder"], must_exist=True)
    patch = resolve_relative(root, stages["patch"], must_exist=True)
    if builder.name.lower() != "install_flac.bat":
        raise ContractError("Die UTE-Rezeptur benötigt die gebundene install_flac.bat als Quelle")
    expected_patch_sha256 = json.loads(PREPARATION_RECIPE.read_text(encoding="utf-8"))["german_patch"]["exe_sha256"]
    if not patch.is_file() or _sha(patch) != expected_patch_sha256:
        raise ContractError("Deutsche Patch-EXE fehlt oder stimmt nicht mit dem gepinnten Paket überein")
    english_receipt_path = output / "english.receipt.json"
    prior_identity = None
    if english_receipt_path.is_file():
        prior_identity = json.loads(english_receipt_path.read_text(encoding="utf-8"))["identity"]
    recipe_sha256 = digest({"builder": stages["builder"], "patch": stages["patch"],
                            "patch_sha256": expected_patch_sha256})
    if prior_identity is not None:
        # Earlier native receipts bind a differently shaped recipe object.
        # Preserve their hash only for reuse. _run_stage verifies the full
        # signed native identity and output inventory before returning it.
        recipe_sha256 = prior_identity.get("recipe_sha256", recipe_sha256)
    wineprefix = root / "MI1 Generator" / "foreign" / "preparation" / "wineprefix"
    environment = {**os.environ, "WINEPREFIX": str(wineprefix)}
    environment["WINEDEBUG"] = ",".join(filter(None, [environment.get("WINEDEBUG"), "warn+console,err+console"]))
    identity = {"recipe_sha256": recipe_sha256, "game_directory": str(game),
                "wine_sha256": _sha(wine), "stage": "ute_english", "layout_version": 2}
    if prior_identity is None or "audio_mode" in prior_identity:
        identity["audio_mode"] = NATIVE_AUDIO_MODE
    from . import ute_native_flac as native
    from . import ute_native_sbl
    from . import ute_xwma
    if ffmpeg is None:
        raise ContractError("Die native Spielvorbereitung benötigt FFmpeg")
    ffmpeg = Path(ffmpeg)
    if not ffmpeg.is_file() or ffmpeg.is_symlink() or not os.access(ffmpeg, os.X_OK):
        raise ContractError("Native FFmpeg-Laufzeit fehlt oder ist nicht ausführbar")
    plan, source_hashes = native.source_plan(builder.parent)
    if native_sox is None:
        from .native_audio_tools import ensure_native_sox
        def sox_progress(message):
            if progress:
                progress({"phase": "native_sox", "status": "running", "message": str(message),
                          "file_count": None, "expected_file_count": None})
        native_sox = ensure_native_sox(root / "MI1 Generator", progress=sox_progress)
    native_sox = Path(native_sox)
    if not native_sox.is_file() or native_sox.is_symlink() or not os.access(native_sox, os.X_OK):
        raise ContractError("Native SoX-Laufzeit fehlt oder ist nicht ausführbar")
    identity = {**identity, "native_audio": native.REVISION,
                "native_converter_sha256": _sha(Path(native.__file__)),
                "native_sbl_sha256": _sha(Path(ute_native_sbl.__file__)),
                "native_xwma_sha256": _sha(Path(ute_xwma.__file__)),
                "ffmpeg_sha256": _sha(ffmpeg), "sox_sha256": _sha(native_sox),
                "original_sources": source_hashes, "native_plan_sha256": digest(plan)}
    output.mkdir(parents=True, exist_ok=True)
    english = output / "english"
    german = output / "german"
    english.mkdir(exist_ok=True); german.mkdir(exist_ok=True)
    project_identity_path = root / "MI1 Generator" / ".launcher" / "project-id"
    project_id = json.loads(project_identity_path.read_text(encoding="utf-8"))["project_id"]
    installation = load_or_create_installation(root)
    # The unchanged builder reads ../Monkey1.pak and ../../audio from tools/.
    # This is layout, not a version/hash/content preflight on the supplied PC-SE.
    builder_work = english / builder.parent.name
    english_game = builder_work / "MI1_Ultimate_Talkie_Edition"
    german_game = german / builder.parent.name / "MI1_Ultimate_Talkie_Edition"
    if not builder_work.exists():
        shutil.copytree(game, english, dirs_exist_ok=True)
        shutil.copytree(builder.parent, builder_work, dirs_exist_ok=True)
    # Never silently accept changed original scripts in the working copy.
    working_plan, working_hashes = native.source_plan(builder_work)
    if plan != working_plan or source_hashes != working_hashes:
        raise ContractError("UTE-Arbeitskopie wurde verändert; Spielvorbereitung neu beginnen")
    builder_cmd = ["native-ute-audio", native.REVISION]
    def executor(stream):
        return native.execute_native_builder(builder=builder_work, plan=plan, wine=wine,
            ffmpeg=ffmpeg, sox=native_sox, log=stream, environment=environment,
            jobs=native_jobs, progress=progress)
    def finished_game(directory):
        files = {p.name.lower(): p for p in directory.iterdir()} if directory.is_dir() else {}
        if any(name not in files or not files[name].is_file() or not files[name].stat().st_size
               for name in ("monkey.000", "monkey.001")):
            raise ContractError("Die Spielvorbereitung hat keine vollständige spielbare Ausgabe erzeugt; prüfe das Protokoll")
        if not any(name in files and files[name].is_file() and files[name].stat().st_size
                   for name in ("monster.sou", "monkey.sof", "monkey.sog", "monkey.so3")):
            raise ContractError("Spracharchiv fehlt; es wird kein Abschlussbeleg erstellt")
    builder_receipt = _run_stage(builder_cmd, builder_work, english_receipt_path, identity, english_game,
                                 root=root, installation=installation, project_id=project_id,
                                 environment=environment, validate_output=finished_game, progress=progress,
                                 executor=executor, retry_failed=False)
    music = sorted(path for path in english_game.rglob("*") if path.is_file() and
                   ("music" in path.as_posix().lower() or path.suffix.lower() in {".xwm", ".ogg", ".wav", ".flac"}))
    music_hashes = {path.relative_to(english_game).as_posix(): _sha(path) for path in music}
    if not german_game.exists():
        shutil.copytree(english_game, german_game)
    from .ute_patch_compat import prepare_patch_copy
    compatibility = prepare_patch_copy(english_game, german_game,
        output / "german.receipt.json", builder_receipt["stage_id"],
        output / "german-resource-compatibility.json")
    if compatibility and progress:
        progress({"message": "US→DE-Patchbasis geprüft: 40 Soundbytes kompatibel; Spracharchiv und Musik bleiben erhalten."})
    patch_cmd = [str(wine), str(patch)]
    patch_identity = {"english_receipt": builder_receipt["stage_id"], "recipe_sha256": recipe_sha256,
                      "wine_sha256": _sha(wine), "stage": "ute_german", "layout_version": 2}
    if compatibility:
        patch_identity["resource_compatibility"] = compatibility
    def patched_game(directory):
        finished_game(directory)
        before = {p.name.lower(): _sha(p) for p in english_game.iterdir() if p.name.lower() in {"monkey.000", "monkey.001"}}
        if compatibility:
            before["monkey.001"] = compatibility["patch_input_sha256"]
        after = {p.name.lower(): _sha(p) for p in directory.iterdir() if p.name.lower() in before}
        if any(before[name] == after.get(name) for name in before):
            raise ContractError("Deutscher Patch hat nicht beide Spieldateien geändert; richtigen Zielordner wählen und Installation abschließen")
    patch_receipt = _run_stage(patch_cmd, german_game, output / "german.receipt.json", patch_identity, german_game,
                               root=root, installation=installation, project_id=project_id,
                               environment=environment, validate_output=patched_game, progress=progress,
                               retry_failed=False,
                               executor=(lambda stream: patch_process_runner(command=patch_cmd, cwd=german_game,
                                   environment=environment, log=stream)) if patch_process_runner else None)
    after = {path.relative_to(german_game).as_posix(): _sha(path) for path in german_game.rglob("*") if path.is_file() and
             ("music" in path.as_posix().lower() or path.suffix.lower() in {".xwm", ".ogg", ".wav", ".flac"})}
    if music_hashes != after:
        raise ContractError("Musikdateien änderten sich nach dem deutschen Patch")
    artifacts = {}
    for name, directory in (("english", english_game), ("german", german_game)):
        for relative, sha in _inventory(directory).items():
            artifact_path = (directory / relative).relative_to(root).as_posix()
            artifacts[f"{name}:{relative}"] = {"path": artifact_path, "sha256": sha}
    inputs = {"recipe_sha256": recipe_sha256, "selected_manifest_sha256":
              json.loads((root / "MI1 Generator" / ".launcher" / "project.json").read_text())["project_state"]["version"]["manifest_sha256"]}
    signed = preparation_receipt(installation, project_id=project_id, inputs=inputs, outputs=artifacts)
    result = {"english": builder_receipt, "german": patch_receipt, "music_sha256": music_hashes,
              "audio_mode": NATIVE_AUDIO_MODE,
              "english_game_directory": str(english_game), "german_game_directory": str(german_game),
              "preparation_receipt": signed}
    _atomic_json(output / "preparation.json", result)
    return result


def materialize_preparation(*, resolved: dict, role_id: str, materializer) -> dict:
    """Explicit P20-only entry point; resolution and file loading never call this."""
    role = next((item for item in resolved["roles"] if item["role_id"] == role_id), None)
    if role is None:
        raise ContractError(f"Kein festes Rollenrecipe für {role_id}")
    if role.get("without_anchor"):
        return {"role_id": role_id, "status": "not_required"}
    return materializer(role=role, resolved=resolved)


def materialize_role_audio(*, root: Path, role_plan: dict, verified_inputs: dict,
                           ffmpeg: Path, cli: Path, models: list[Path], cache,
                           reference: Path, reference_report: Path, target_case: dict,
                           output: Path, report: Path, seed: int,
                           runner=None, progress=None) -> dict:
    """Run the shared real reference and render_one cores for a resolved role.

    `runner` is the external process boundary passed through to render_one's
    session hook in fixture contexts; no materializer is mocked here.
    """
    from manifest_renderer import preparation_core as core

    core.materialize_role_reference(root, role_plan, verified_inputs, ffmpeg,
                                    reference, reference_report, cache)
    # Keep render_one's identity, resume, reports and WAV validation intact.
    class RunnerSession:
        def run(self, command, stream):
            return runner(command, stream)
    sessions = RunnerSession() if runner is not None else None
    core.render_one(root, cli, ffmpeg, models, reference, {**target_case, "audio": None},
                    output, report, role_plan["anchor"]["fingerprint"], "prewarm", seed,
                    verified_inputs, sessions=sessions, progress=progress)
    return {"reference": str(reference), "output": str(output),
            "reference_sha256": _sha(reference), "output_sha256": _sha(output)}


def preparation_receipt(installation: LocalInstallation, *, project_id: str,
                        inputs: dict, outputs: dict) -> dict:
    """P03 host-local HMAC receipt; explicitly not a dialog generation receipt."""
    import hmac
    body = {"schema_version": 1, "kind": "preparation_receipt",
            "installation_id": installation.installation_id, "project_id": project_id,
            "inputs": inputs, "outputs": outputs}
    body["local_signature"] = hmac.new(installation.receipt_key, canonical_json(body),
                                       hashlib.sha256).hexdigest()
    return body


def verify_preparation_receipt(receipt: dict, installation: LocalInstallation,
                               *, project_id: str, inputs: dict, work_root: Path) -> bool:
    import hmac
    if (receipt.get("schema_version") != 1 or receipt.get("kind") != "preparation_receipt"
            or receipt.get("installation_id") != installation.installation_id
            or receipt.get("project_id") != project_id or receipt.get("inputs") != inputs):
        return False
    body = {key: value for key, value in receipt.items() if key != "local_signature"}
    signature = receipt.get("local_signature", "")
    if not hmac.compare_digest(signature, hmac.new(installation.receipt_key,
            canonical_json(body), hashlib.sha256).hexdigest()):
        return False
    for artifact in receipt.get("outputs", {}).values():
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            return False
        try:
            path = resolve_relative(work_root, artifact["path"], must_exist=True)
        except (ContractError, OSError):
            return False
        if not path.is_file() or _sha(path) != artifact["sha256"]:
            return False
    return True


def prepared_german_ute_directory(work_root: Path, project: dict,
                                 installation: LocalInstallation,
                                 diagnostic: dict | None = None,
                                 basis_info: dict | None = None) -> str | None:
    """Resolve only the signed, current-version German UTE output as patch basis."""
    import hmac
    from mi1_patcher import BASIS_HASHES

    try:
        root = Path(work_root).resolve(strict=True)
    except OSError:
        if diagnostic is not None: diagnostic.update(receipt_path=None, reason="Projektordner fehlt")
        return None
    path = root / "MI1 Generator/generated/game-preparation/preparation.json"
    def reject(reason):
        if diagnostic is not None: diagnostic.update(receipt_path=str(path), reason=reason)
        return None
    if path.is_symlink(): return reject("Vorbereitungsbeleg ist ein symbolischer Link")
    if not path.is_file(): return reject("Aktueller Vorbereitungsbeleg fehlt")
    try:
        prepared = json.loads(path.read_text(encoding="utf-8"))
        receipt = prepared["preparation_receipt"]
        inputs = receipt["inputs"]
        signature = receipt.get("local_signature", "")
        body = {key: value for key, value in receipt.items() if key != "local_signature"}
        expected = hmac.new(installation.receipt_key, canonical_json(body), hashlib.sha256).hexdigest()
        if receipt.get("schema_version") != 1 or receipt.get("kind") != "preparation_receipt":
            return reject("Vorbereitungsbeleg hat ein unbekanntes Format")
        if receipt.get("installation_id") != installation.installation_id:
            return reject("Vorbereitungsbeleg gehört zu einer anderen Launcher-Installation")
        if receipt.get("project_id") != project["project_id"]:
            return reject("Vorbereitungsbeleg gehört zu einem anderen Projekt")
        if not hmac.compare_digest(signature, expected):
            return reject("Signatur des Vorbereitungsbelegs ist ungültig")
        outputs = receipt["outputs"]
        def artifact_for(name):
            matches=[(key,value) for key,value in outputs.items()
                     if isinstance(key,str) and key.lower()==("german:"+name).lower()]
            if len(matches)!=1: return None
            return matches[0][1]
        paths = {}
        for name in ("monkey.000", "monkey.001"):
            artifact = artifact_for(name)
            if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
                return reject(f"Signierter deutscher Output fehlt: {name}")
            resolved = resolve_relative(root, artifact["path"], must_exist=False)
            if resolved.name.lower() != name:
                return reject(f"Signierter deutscher Output ist ungültig: {name}")
            if not resolved.is_file():
                return reject(f"Signierter deutscher Output fehlt: {resolved}")
            if _sha(resolved)!=artifact["sha256"]:
                return reject(f"Hash des signierten deutschen Outputs stimmt nicht: {resolved}")
            if artifact["sha256"]!=BASIS_HASHES[name]:
                return reject(f"Deutsche UTE-Basisdatei hat unerwarteten Inhalt: {resolved}")
            paths[name] = resolved
        archives=[]
        for name in ("monster.sou","monkey.sof"):
            artifact=artifact_for(name)
            if artifact is None: continue
            if not isinstance(artifact,dict) or set(artifact)!={"path","sha256"}:
                return reject(f"Signierter deutscher Spracharchivbeleg ist ungültig: {name}")
            resolved=resolve_relative(root,artifact["path"],must_exist=False)
            if resolved.name.lower()!=name or not resolved.is_file():
                return reject(f"Signiertes deutsches Spracharchiv fehlt: {resolved}")
            # SOF is encoded locally: its bytes depend on the selected native
            # audio path. The authenticated receipt above binds this exact file.
            wanted=BASIS_HASHES[name] if name=="monster.sou" else artifact["sha256"]
            if artifact["sha256"]!=wanted or _sha(resolved)!=wanted:
                return reject(f"Deutsches Spracharchiv hat unerwarteten Inhalt: {resolved}")
            archives.append((name,resolved,wanted))
        if len(archives)!=1:return reject("Der signierte deutsche Beleg muss genau ein SOU- oder FLAC-SOF-Archiv enthalten")
        archive_name,archive_path,archive_sha=archives[0]
        paths[archive_name]=archive_path
        parents = {path.parent for path in paths.values()}
        if len(parents) != 1: return reject("Signierte UTE-Dateien liegen nicht im selben Ordner")
        result=str(next(iter(parents)).resolve())
        if basis_info is not None:
            basis_info.clear();basis_info.update(root=result,archive_name=archive_name,
                archive_path=str(archive_path),archive_sha256=archive_sha,
                preparation_receipt_sha256=hashlib.sha256(canonical_json(receipt)).hexdigest(),
                preparation_manifest_sha256=inputs.get("selected_manifest_sha256"),
                project_id=receipt["project_id"],installation_id=receipt["installation_id"])
        if diagnostic is not None: diagnostic.update(receipt_path=str(path), reason=None)
        return result
    except OSError as exc:
        return reject(f"Vorbereitungsdatei nicht lesbar: {exc}")
    except (ValueError, KeyError, TypeError, ContractError):
        return reject("Vorbereitungsbeleg oder signierter UTE-Ordner ist unvollständig")
