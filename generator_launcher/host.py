"""Loopback-only project setup host for the native launcher (no job engine)."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlparse, parse_qs
import uuid

from manifest_renderer.recipes import FORMAT as RECIPE_FORMAT, validate_recipe, recipe_mode, pipeline_label, recipe_warnings

from .contracts import resolve_relative, validate_state, version_provenance
from .version import LAUNCHER_VERSION
from .rosetta import RosettaPreparation
from .gog_input import classify_game_input, materialize_gog_input
from .local_preparation import (NATIVE_AUDIO_MODE, PREPARATION_RESTART_REQUIRED,
                                prepare_game, preserve_previous_preparation,
                                resolve_preparation, resolve_wine_binary)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".launcher-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def retain_input(home: Path, source: Path, relative: str, wanted: str) -> str:
    target = resolve_relative(home, relative)
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != wanted:
        raise ValueError("Eingabedatei hat sich während der Auswahl geändert")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_symlink() or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != wanted:
            raise ValueError("Vorhandene Projekteingabe stimmt nicht mit ihrem Hash überein")
        return relative
    fd, name = tempfile.mkstemp(prefix=".launcher-input-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(name, target)
        except FileExistsError:
            if hashlib.sha256(target.read_bytes()).hexdigest() != wanted:
                raise ValueError("Gleichzeitige Projekteingabe weicht ab")
        return relative
    finally:
        Path(name).unlink(missing_ok=True)


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def startup_consents(value: dict | None) -> dict:
    """Validate session-only confirmations from the native startup screen."""
    keys = ("packages", "wine", "game")
    if value is None:
        return {key: False for key in keys}
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        raise ValueError("Ungültige native Bestätigungsübergabe")
    result = {"schema_version": 1}
    for key in keys:
        if type(value.get(key)) is not bool:
            raise ValueError("Native Bestätigung fehlt: " + key)
        result[key] = value[key]
    return result


def startup_history(value: list | None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(line, str) for line in value):
        raise ValueError("Ungültiges natives Startprotokoll")
    return [line[:2000] for line in value[-400:]]


def project_config(root: Path, manifest_path: Path, correction_path: Path | None,
                   game_directory: Path | None = None) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Arbeitslaufwerk ist kein Ordner oder nicht verfügbar")
    # A newer app may ship a different textless default than the manifest that
    # created this workdir. Keep the existing project inputs for the bundled
    # default only; a separately selected manifest/correction remains explicit.
    from .resume import retained_default_selection
    retained = retained_default_selection(root, manifest_path, correction_path)
    retained_state = None
    if retained is not None:
        home = root / "MI1 Generator"
        manifest_path = resolve_relative(home, retained["manifest"], must_exist=True)
        retained_state = validate_state(retained["project_state"])
        if retained.get("correction"):
            correction_path = resolve_relative(home, retained["correction"], must_exist=True)
        else:
            correction_path = None
    manifest_path = manifest_path.resolve(strict=True)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("manifest_id"), str):
        raise ValueError("Manifest hat keine gültige Manifestidentität")
    revision = manifest.get("revision", manifest.get("manifest_revision", 1))
    if type(revision) is not int or revision < 1:
        raise ValueError("Manifestrevision ist ungültig")
    correction = None
    if correction_path is not None:
        correction_path = correction_path.resolve(strict=True)
        correction_bytes = correction_path.read_bytes()
        correction = json.loads(correction_bytes.decode("utf-8"))
        if isinstance(correction, dict) and correction.get("format") == RECIPE_FORMAT:
            correction = validate_recipe(correction, manifest)
        else:
            if not isinstance(correction, dict) or correction.get("format") != "patch-review-extension/1":
                raise ValueError("Wähle ein gültiges Rezept oder eine ältere Arbeitsstandsdatei aus.")
            if (correction.get("manifest_id") != manifest["manifest_id"] or
                    correction.get("game_id") != manifest.get("game", {}).get("game_id")):
                raise ValueError("Das Rezept gehört nicht zu den ausgewählten Dialogvorgaben")

    home = root / "MI1 Generator"
    if home.is_symlink():
        raise ValueError("Generatorarbeitsordner darf kein symbolischer Link sein")
    for path in (home / "input", home / "foreign", home / "generated", home / "runs", home / "reviews"):
        path.mkdir(parents=True, exist_ok=True)
    config_dir = home / ".launcher"
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_name = f"input/manifests/manifest-{revision}-{manifest_hash}.json"
    manifest_relative = retain_input(home, manifest_path, manifest_name, manifest_hash)
    correction_relative = None
    if correction_path is not None:
        correction_hash = hashlib.sha256(correction_bytes).hexdigest()
        correction_relative = retain_input(home, correction_path,
            f"input/corrections/{correction_hash}.json", correction_hash)
    identity_path = config_dir / "project-id"
    if identity_path.exists():
        project_id = json.loads(identity_path.read_text(encoding="utf-8"))["project_id"]
        uuid.UUID(project_id)
    else:
        project_id = str(uuid.uuid4())
        config_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(identity_path, {"project_id": project_id})
    state = validate_state({
        "schema_version": 1,
        "kind": "project",
        "project_id": project_id,
        "paths": {key: key for key in ("input", "foreign", "generated", "runs", "reviews")},
        "version": version_provenance(manifest["manifest_id"], revision,
            manifest_hash, version_id=str(uuid.uuid4())),
    })
    previous = {}
    previous_path = config_dir / "project.json"
    if previous_path.is_file():
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        if retained_state is not None:
            state = retained_state
        elif previous.get("manifest") == manifest_relative and previous.get("correction") == correction_relative:
            state = validate_state(previous["project_state"])
    result = {"schema_version": 1, "root": str(root), "project_state": state,
              "manifest": manifest_relative, "correction": correction_relative,
              "manifest_id": manifest["manifest_id"],
              "correction_loaded": correction is not None,
              "render_recipe": ({"pipeline": correction["pipeline"], "label": pipeline_label(correction),
                                 "mode": recipe_mode(correction), "warnings": recipe_warnings(correction)}
                                if correction and correction.get("format") == RECIPE_FORMAT else None),
              "game_input_mode": "pc_se",
              "audio_mode": NATIVE_AUDIO_MODE,
              "game_input_kind": classify_game_input(game_directory) if game_directory else "unselected",
              "pc_special_edition_directory": str(game_directory.resolve(strict=True)) if game_directory else None}
    atomic_json(config_dir / "project.json", result)
    return result


class HostServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address, token, root, manifest, correction, game_directory=None,
                 initial_consents=None, startup_log=None):
        self.token, self.root = token, root
        self.manifest, self.correction = manifest, correction
        self.game_directory = game_directory
        self.initial_consents = startup_consents(initial_consents)
        self.startup_log = startup_history(startup_log)
        self.lock_fd = None
        # Returning workdirs need newly shipped renderer modules even when the
        # package/setup page is skipped. Never touch models or render results.
        from .runtime_setup import install_own_code
        install_own_code(root / "MI1 Generator")
        from .render import LauncherRender
        self.renderer = LauncherRender(root)
        self.operation_lock = threading.RLock()
        self.rosetta = RosettaPreparation(root, atomic_json)
        self.renderer.operation_lock = self.operation_lock
        self.stopping = False
        self.game_preparation_active = False
        self.patch_window_state = None
        from .setup_dashboard import SetupDashboard
        saved = root / "MI1 Generator" / ".launcher" / "project.json"
        config = json.loads(saved.read_text()) if saved.is_file() else {}
        self.setup = SetupDashboard(root, operation_lock=self.operation_lock, render_busy=self.renderer.busy,
            gog_installer=config.get("game_input_kind") == "gog_installer_candidate")
        super().__init__(address, self.handler())

    def voice_choices(self):
        from .voice_choices import VoiceChoices
        _config, _source, manifest, _correction = self.renderer.selection()
        return VoiceChoices(self.root / "MI1 Generator", manifest)

    def server_close(self):
        self.renderer.stop()
        super().server_close()
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def restart_busy(self):
        """Read only; an active foreground operation also prevents shutdown."""
        if not self.operation_lock.acquire(blocking=False):
            return True
        try:
            if self.setup.busy() or self.renderer.busy():
                return True
            review = self.renderer.review
            builder = getattr(review, '_patch_builder', None)
            return bool(builder and builder.thread and builder.thread.is_alive())
        finally:
            self.operation_lock.release()

    def handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def respond(self, status, body, content="application/json; charset=utf-8"):
                data = body if isinstance(body, bytes) else body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'nonce-"+server.token+"'; style-src 'self' 'unsafe-inline'; media-src 'self'; object-src 'none'; base-uri 'none'")
                if urlparse(self.path).path == "/" and status == 200:
                    self.send_header("Set-Cookie", "mi1_launcher="+server.token+"; Path=/; HttpOnly; SameSite=Strict")
                self.end_headers()
                self.wfile.write(data)

            def authorized(self):
                host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
                if host not in {"127.0.0.1", "localhost"}: return False
                origin = self.headers.get("Origin")
                if origin and origin != f"http://127.0.0.1:{server.server_port}": return False
                cookie = SimpleCookie()
                try: cookie.load(self.headers.get("Cookie", ""))
                except ValueError: return False
                session = self.headers.get("X-Launcher-Session") or (cookie["mi1_launcher"].value if "mi1_launcher" in cookie else "")
                if self.command == "GET" and urlparse(self.path).path == "/":
                    session = session or parse_qs(urlparse(self.path).query).get("session", [""])[0]
                return secrets.compare_digest(session, server.token)

            def do_GET(self):
                if not self.authorized():
                    return self.respond(403, "Zugriff verweigert")
                route = urlparse(self.path).path
                if route == "/api/preparation/rosetta-status":
                    return self.respond(200, json.dumps(server.rosetta.snapshot(), ensure_ascii=False))
                if route == "/api/launcher/consents":
                    return self.respond(200, json.dumps({"consents": server.initial_consents,
                        "startup_log": server.startup_log}, ensure_ascii=False))
                if route == "/api/startup/resume":
                    from .resume import resume_check
                    return self.respond(200, json.dumps(resume_check(server.root,server.manifest,server.correction)))
                if route == "/api/server/status":
                    return self.respond(200, json.dumps({"pid":os.getpid(), "root":str(server.root.resolve()),
                        "profile":"mi1-patch-launcher/1", "launcher_version":LAUNCHER_VERSION,
                        "audio_mode":NATIVE_AUDIO_MODE,
                        "busy":server.restart_busy(), "stopping":server.stopping}))
                if route == "/api/review/activation-status":
                    return self.respond(200,json.dumps(server.renderer.activation_status(),ensure_ascii=False))
                if route.startswith(("/patch-review/", "/api/patch-review/", "/wiki/")):
                    if route.startswith("/api/patch-review/") and not self.review_context_current():
                        return self.respond(409, json.dumps({"error":"Die Fassungswahl hat sich geändert. Öffne die Review der ausgewählten Fassung erneut."}))
                    from patch_review.http import dispatch
                    result = dispatch(server.renderer.review, "GET", self.path)
                    if route == "/patch-review/index.html" and result and result[0] == 200:
                        result = (result[0], result[1].replace(b"</head>", b'<link rel="stylesheet" href="/launcher-wizard.css"><script src="/launcher-terminal.js" defer></script><script src="/launcher-console.js" defer></script></head>'), result[2])
                        result = (result[0], result[1].replace(b'<main id="patch-review"', b'<main id="patch-review" data-launcher-review'), result[2])
                    return self.respond(*(result or (404, b"Nicht gefunden", "text/plain")))
                if route == "/launcher-console.js":
                    return self.respond(200, (Path(__file__).parent / "launcher_console.js").read_bytes(), "text/javascript; charset=utf-8")
                if route == "/launcher-flow.js":
                    return self.respond(200, (Path(__file__).parent / "launcher_flow.js").read_bytes(), "text/javascript; charset=utf-8")
                if route == "/patch-window.js":
                    return self.respond(200, (Path(__file__).parent / "patch_window.js").read_bytes(), "text/javascript; charset=utf-8")
                if route in {"/launcher-wizard.js", "/launcher-wizard.css", "/launcher-terminal.js", "/fassungen.js"}:
                    asset = Path(__file__).parent / route[1:].replace("-", "_")
                    return self.respond(200, asset.read_bytes(), "text/css; charset=utf-8" if asset.suffix == ".css" else "text/javascript; charset=utf-8")
                if route == "/api/preparation/game-status":
                    home = server.root / "MI1 Generator"
                    path = home / ".launcher/game-input-progress.json"
                    value = json.loads(path.read_text()) if path.is_file() else {"status":"idle", "phase":"idle"}
                    value.pop("patch_interaction", None)
                    if value.get("status") == "running" and not server.game_preparation_active:
                        value.update(status="failed", phase="failed", prepared=False,
                            interrupted=True, interrupted_phase=value.get("phase"), jobs=0,
                            restart_available=True,
                            error="Die vorherige Vorbereitung wurde unterbrochen. Die Vorbereitung läuft derzeit nicht. Verwende „Vorbereitung löschen und neu beginnen“ und starte danach erneut.")
                        if value.get("interrupted_phase") == "german_patch" and value.get("audio_mode") == NATIVE_AUDIO_MODE:
                            value["error"] = "Der vorherige deutsche Patch wurde unterbrochen. Die Vorbereitung läuft derzeit nicht. Starte die Spielvorbereitung erneut; der gültige englische Stand kann ohne Löschen wiederverwendet werden."
                    live = getattr(server, "patch_window_state", None)
                    if (value.get("phase") == "german_patch" and value.get("status") == "running"
                            and live is not None and live[1].poll() is None):
                        value["patch_interaction"] = live[0]
                    output = home / "generated/game-preparation"
                    value["prepared"] = value.get("status") == "completed" and (output / "preparation.json").is_file()
                    config_path = home / ".launcher/project.json"
                    config = json.loads(config_path.read_text()) if config_path.is_file() else {}
                    if value.get("selected_input") and value["selected_input"] != config.get("pc_special_edition_directory"):
                        value = {"status":"idle", "phase":"idle"}
                    if not server.game_preparation_active and preserve_previous_preparation(output):
                        value.update(preserve_previous=True, restart_available=True)
                        if not value.get("prepared"):
                            value.update(status="failed", phase="failed", prepared=False,
                                         error=PREPARATION_RESTART_REQUIRED)
                    if value.get("phase") != "german_patch" or value.get("status") != "running":
                        value.pop("patch_interaction", None)
                    log = Path(value["log_path"]) if value.get("log_path") else None
                    if log and log.is_file() and log.resolve().is_relative_to(home.resolve()):
                        with log.open("rb") as stream:
                            size = log.stat().st_size
                            offset = max(0, size - 6000)
                            stream.seek(offset)
                            tail = stream.read(6000)
                            if offset and b"\n" in tail:
                                tail = tail.split(b"\n", 1)[1]
                            if value.get("status") == "running":
                                tail = tail.rsplit(b"\n", 1)[0] + b"\n" if b"\n" in tail else b""
                            value["log"] = tail.decode("utf-8", "replace")
                            value["log_bytes"] = size
                    return self.respond(200, json.dumps(value, ensure_ascii=False))
                if route == "/api/preparation/help":
                    from .preparation_help import preparation_help
                    try:
                        return self.respond(200, json.dumps(preparation_help(server.root), ensure_ascii=False))
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        return self.respond(422, json.dumps({"error": str(exc)}, ensure_ascii=False))
                if route == "/api/voices/status":
                    try:
                        return self.respond(200, json.dumps(server.voice_choices().snapshot(), ensure_ascii=False))
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        return self.respond(422, json.dumps({"error": str(exc)}, ensure_ascii=False))
                if route == "/api/repair/status":
                    try:
                        return self.respond(200, json.dumps(server.renderer.repair_status(), ensure_ascii=False))
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        return self.respond(422, json.dumps({"error": str(exc)}, ensure_ascii=False))
                if route == "/api/setup/status":
                    return self.respond(200, json.dumps(server.setup.snapshot(), ensure_ascii=False))
                if route == "/api/benchmark/status":
                    return self.respond(200, json.dumps(server.renderer.benchmark_status(), ensure_ascii=False))
                if route == "/api/render/status":
                    value=server.renderer.snapshot()
                    log=Path(value['preparation_log_path']) if value.get('preparation_log_path') else None
                    home=server.root/'MI1 Generator'
                    try:
                        if log and log.is_file() and log.resolve().is_relative_to(home.resolve()):
                            with log.open('rb') as stream:
                                offset=max(0,log.stat().st_size-6000)
                                stream.seek(offset)
                                tail=stream.read(6000)
                                if offset and b'\n' in tail: tail=tail.split(b'\n',1)[1]
                                if value.get('status')=='running':
                                    tail=tail.rsplit(b'\n',1)[0]+b'\n' if b'\n' in tail else b''
                                value['preparation_log']=tail.decode('utf-8','replace')
                    except OSError:
                        pass  # A stage can finish while its status is being polled.
                    return self.respond(200, json.dumps(value, ensure_ascii=False))
                if route == "/":
                    return self.respond(200, PAGE.replace("<script>", "<script nonce='"+server.token+"'>"), "text/html; charset=utf-8")
                if route == "/api/status":
                    try:
                        config = json.loads((server.root / "MI1 Generator" / ".launcher" / "project.json").read_text())
                        status = {"root_available": server.root.is_dir(), "project": config,
                                  "jobs_started": int(server.renderer.thread is not None), "review_available": server.renderer.review is not None,
                                  "platform": "Für Macs mit Apple Silicon"}
                    except FileNotFoundError:
                        status = {"root_available": server.root.is_dir(), "project": None,
                                  "jobs_started": 0, "review_available": False}
                    return self.respond(200, json.dumps(status, ensure_ascii=False))
                if route == "/api/preparation/resolve":
                    try:
                        config = json.loads((server.root / "MI1 Generator" / ".launcher" / "project.json").read_text())
                        manifest_path = resolve_relative(server.root / "MI1 Generator", config["manifest"], must_exist=True)
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                        correction = None
                        if config.get("correction"):
                            correction_path = resolve_relative(server.root / "MI1 Generator", config["correction"], must_exist=True)
                            correction = json.loads(correction_path.read_text(encoding="utf-8"))
                        resolved = resolve_preparation(manifest, correction)
                        return self.respond(200, json.dumps({"ready": True, "manifest_id": resolved["manifest_id"],
                            "roles": len(resolved["roles"]), "tts_started": False, "qa_started": False,
                            "game_build_started": False}, ensure_ascii=False))
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        return self.respond(422, json.dumps({"ready": False, "error": str(exc),
                            "tts_started": False, "qa_started": False, "game_build_started": False}, ensure_ascii=False))
                self.respond(404, "Nicht gefunden")

            def review_context_current(self):
                service = server.renderer.review
                if service is None or not hasattr(service, 'root'):
                    return True
                selected = server.renderer.selected_activation()
                return bool(selected and selected.get('review') and
                    service.root.resolve() == (server.renderer.root / selected['review']).resolve())

            def do_POST(self):
                if urlparse(self.path).path in {"/api/server/stop", "/api/preparation/restart"}:
                    if not self.authorized():
                        return self.respond(403, "Zugriff verweigert")
                    if not server.operation_lock.acquire(blocking=False):
                        return self.respond(409, json.dumps({"error":"Vorbereitung oder anderer Auftrag läuft; bitte dessen Abschluss abwarten."}))
                    try:
                        self.post_locked()
                    finally:
                        server.operation_lock.release()
                    return
                if urlparse(self.path).path == "/api/benchmark/cancel":
                    return self.post_locked()
                with server.operation_lock:
                    self.post_locked()

            def post_locked(self):
                if not self.authorized():
                    return self.respond(403, "Zugriff verweigert")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 32 * 1024 * 1024 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise ValueError("Ungültige Anfrage")
                    body = json.loads(self.rfile.read(length))
                    route = urlparse(self.path).path
                    if server.stopping:
                        return self.respond(409, json.dumps({"error":"Server wird beendet; bitte auf den Neustart warten."}))
                    if route == "/api/server/stop":
                        if not isinstance(body, dict) or body.get("confirmed") is not True:
                            raise ValueError("Serverneustart ausdrücklich bestätigen")
                        if server.restart_busy():
                            return self.respond(409, json.dumps({"error":"Setup-, Render-, Reparatur- oder Exportauftrag läuft; Server bleibt aktiv."}))
                        server.stopping = True
                        self.respond(202, json.dumps({"stopping":True}))
                        # shutdown must run outside the serving thread and only
                        # after the confirmation response has been written.
                        threading.Thread(target=server.shutdown, daemon=True).start()
                        return
                    if server.renderer.busy() and route.startswith(("/api/setup/", "/api/runtime/", "/api/preparation/")):
                        return self.respond(409, json.dumps({"error": "Renderauftrag läuft; Einrichtung wartet"}))
                    if server.setup.busy():
                        return self.respond(409, json.dumps({"error": "Gemeinsame Einrichtung läuft; Änderungen/Rendern warten bis zum Abschluss"}))
                    if server.renderer.repair_status().get("busy"):
                        return self.respond(409, json.dumps({"error": "Reparatur läuft; Änderungen und Review warten bis zum Abschluss"}))
                    if route == "/api/preparation/restart":
                        if not isinstance(body, dict) or body.get("confirmed") is not True:
                            raise ValueError("Neubeginn der Vorbereitung ausdrücklich bestätigen")
                        if server.restart_busy():
                            return self.respond(409, json.dumps({"error":"Ein Auftrag läuft; Vorbereitung bleibt erhalten."}))
                        from .local_preparation import restart_game_preparation
                        result = restart_game_preparation(server.root)
                        config_path = server.root / "MI1 Generator/.launcher/project.json"
                        config = json.loads(config_path.read_text()) if config_path.is_file() else {}
                        progress = {"selected_input":config.get("pc_special_edition_directory"), "status":"idle", "phase":"idle",
                                    "restart_available":False, "removed_directories_count":result["removed_directories_count"]}
                        if result.get("archive_path"):
                            progress["archive_path"] = result["archive_path"]
                        atomic_json(server.root / "MI1 Generator/.launcher/game-input-progress.json", progress)
                        return self.respond(200, json.dumps({**result, "game_build_started":False,
                            "tts_started":False, "qa_started":False}, ensure_ascii=False))
                    if route in {"/api/voices/select", "/api/voices/alternative", "/api/voices/original-demo"}:
                        if server.renderer.busy(): raise RuntimeError("Stimmwahl während eines Renderauftrags gesperrt")
                        choices = server.voice_choices()
                        if route == "/api/voices/original-demo":
                            if body.get("confirmed") is not True: raise ValueError("Tech-Demo-Auswahl ausdrücklich bestätigen")
                            result = choices.set_original_demo_all()
                        elif body.get("source") == "original_demo" and route == "/api/voices/select":
                            result = choices.set_choice(body["role_id"], source="original_demo")
                        else:
                            if body.get("speech_confirmed") is not True: raise ValueError("Mindestens drei Sekunden gesprochene Sprechprobe bestätigen")
                            suffix = Path(body.get("filename", "")).suffix.lower()
                            if suffix not in {".wav", ".flac", ".ogg"}: raise ValueError("WAV, FLAC oder OGG auswählen")
                            encoded = body.get("audio_base64")
                            if not isinstance(encoded, str) or len(encoded) > 28 * 1024 * 1024: raise ValueError("Sprechprobe überschreitet 20 MiB")
                            try: data = base64.b64decode(encoded, validate=True)
                            except (ValueError, TypeError) as exc: raise ValueError("Ungültige Audiodatei") from exc
                            if not 0 < len(data) <= 20 * 1024 * 1024: raise ValueError("Sprechprobe darf höchstens 20 MiB groß sein")
                            directory = server.root / "MI1 Generator" / ".launcher" / "uploads"
                            directory.mkdir(parents=True, exist_ok=True)
                            with tempfile.TemporaryDirectory(prefix="anchor-", dir=directory) as stage:
                                audio = Path(stage) / ("sample" + suffix); audio.write_bytes(data)
                                kwargs = dict(audio_path=audio, transcript=body.get("transcript"), language=body.get("language"), speech_confirmed=True)
                                if route == "/api/voices/alternative": result = choices.set_alternative(body["role_id"], **kwargs)
                                else: result = choices.set_choice(body["role_id"], source="custom", **kwargs)
                        return self.respond(200, json.dumps(result, ensure_ascii=False))
                    if route == "/api/repair/start":
                        if body.get("confirmed") is not True:
                            raise ValueError("Automatischen Reparaturversuch ausdrücklich bestätigen")
                        return self.respond(202, json.dumps(server.renderer.start_repair(confirmed=body.get('confirmed') is True), ensure_ascii=False))
                    if route.startswith("/api/patch-review/"):
                        if not self.review_context_current():
                            return self.respond(409, json.dumps({"error":"Die Fassungswahl hat sich geändert. Öffne die Review der ausgewählten Fassung erneut."}))
                        if route == "/api/patch-review/open-output":
                            result=server.renderer.review.patch_builder().open_output()
                            return self.respond(200,json.dumps(result,ensure_ascii=False))
                        from patch_review.http import dispatch
                        result = dispatch(server.renderer.review, "POST", self.path, body)
                        return self.respond(*(result or (404, b"Nicht gefunden", "text/plain")))
                    if route == "/api/benchmark/queue":
                        return self.respond(202, json.dumps(server.renderer.queue_benchmark(count=body.get("count", 12)), ensure_ascii=False))
                    if route == "/api/benchmark/cancel":
                        return self.respond(202, json.dumps(server.renderer.cancel_benchmark(), ensure_ascii=False))
                    if route == "/api/render/pause":
                        return self.respond(202,json.dumps(server.renderer.pause(),ensure_ascii=False))
                    if route == "/api/render/resume":
                        return self.respond(202,json.dumps(server.renderer.resume(),ensure_ascii=False))
                    if route == "/api/render/switch":
                        return self.respond(202, json.dumps(server.renderer.switch_mode(body["mode"], ultra_fast=body.get("ultra_fast", False), list_policy=body.get("list_policy", "mixed")), ensure_ascii=False))
                    if route == "/api/render/select":
                        return self.respond(200, json.dumps(server.renderer.select_list(body["list_id"]), ensure_ascii=False))
                    if route == "/api/render/start":
                        return self.respond(202, json.dumps(server.renderer.start(body.get("mode"), ultra_fast=body.get("ultra_fast", False), list_policy=body.get("list_policy", "mixed"), list_id=body.get("list_id"), replace_list_id=body.get("replace_list_id")), ensure_ascii=False))
                    if route in {"/api/setup/start", "/api/runtime/install", "/api/preparation/sources"}:
                        result = server.setup.start(terms_confirmed=body.get("terms_confirmed"))
                        return self.respond(202, json.dumps(result))
                    if route == "/api/review/activate":
                        review = server.renderer.activate_review()
                        return self.respond(200, json.dumps({"activated": True,
                            "dialogues":len(review.dialogues), "jobs_started":False}, ensure_ascii=False))
                    if route == "/api/save-selection":
                        if server.renderer.busy(): raise RuntimeError('Auswahl während eines Renderauftrags gesperrt')
                        result = project_config(server.root, Path(body["manifest"]),
                            Path(body["correction"]) if body.get("correction") else None,
                            Path(body["game_directory"]) if body.get("game_directory") else None)
                        server.setup.update_input(result["game_input_kind"] == "gog_installer_candidate")
                        server.renderer.review = None
                        return self.respond(200, json.dumps({"saved": True, "manifest_id": result["manifest_id"],
                            "correction_loaded": result["correction_loaded"]}, ensure_ascii=False))
                    if route in {"/api/preparation/rosetta-check", "/api/preparation/wine-consent"}:
                        if server.initial_consents.get("wine") is not True:
                            raise ValueError("Bestätige zuerst die Wine-Bedingungen im Startschritt „Bestätigungen“.")
                        return self.respond(200, json.dumps(server.rosetta.check(), ensure_ascii=False))
                    if route == "/api/preparation/game":
                        if body.get("terms_confirmed") is not True:
                            raise ValueError("Werkzeugbedingungen vor der Spielvorbereitung bestätigen")
                        if preserve_previous_preparation(server.root / "MI1 Generator/generated/game-preparation"):
                            raise ValueError(PREPARATION_RESTART_REQUIRED)
                        config = json.loads((server.root / "MI1 Generator" / ".launcher" / "project.json").read_text())
                        game_directory = config.get("pc_special_edition_directory")
                        if not game_directory:
                            raise ValueError("Wähle zuerst im Startschritt den Windows-Spielordner der Special Edition.")
                        sources = json.loads((server.root / "MI1 Generator" / ".launcher" / "preparation-sources.json").read_text())
                        if sources.get("development_only") is True:
                            raise ValueError("Pakete für die gewählte Spielbasis zuerst gemeinsam einrichten")
                        state = server.rosetta._state()
                        if state.get("rosetta", {}).get("status") != "verified":
                            raise ValueError("Du musst Rosetta installieren und danach auf „Rosetta prüfen“ klicken")
                        wine_root = resolve_relative(server.root, sources["wine_root"], must_exist=True)
                        wine_binary = resolve_wine_binary(wine_root)
                        recipe = {"stages": sources["stages"]}
                        gog_import = None
                        selected = Path(game_directory)
                        progress_path = server.root / "MI1 Generator/.launcher/game-input-progress.json"
                        current_progress = {"selected_input":str(selected), "audio_mode":NATIVE_AUDIO_MODE,
                            "status":"running", "phase":"preparing"}
                        def progress(value):
                            current_progress.update(value)
                            atomic_json(progress_path, current_progress)
                        def patch_process_runner(*, command, cwd, environment, log):
                            from .patch_interaction import run_patch_process
                            from .preparation_help import _wine_path
                            prefix = Path(environment["WINEPREFIX"])
                            target = Path(cwd).resolve(strict=True)
                            windows, mapping = _wine_path(target, prefix)
                            interaction = {"stage_id": current_progress["stage_id"],
                                "patch_target_macos": str(target), "patch_target_wine": windows,
                                "wine_path_mapping": mapping, "wine_binary": str(Path(command[0]).resolve()),
                                "patch_executable": str(Path(command[1]).resolve())}
                            def publish(descriptor, process):
                                server.patch_window_state = (descriptor, process) if descriptor else None
                            return run_patch_process(command=command, cwd=target, environment=environment,
                                log=log, interaction=interaction, publish=publish)
                        server.game_preparation_active = True
                        try:
                            progress({})
                            if classify_game_input(selected) == "gog_installer_candidate":
                                from .gog_tool import acquire_gog_extractor
                                progress({"phase":"extractor"})
                                extractor = acquire_gog_extractor(server.root)
                                gog_import = materialize_gog_input(server.root, selected, extractor, progress=progress)
                                selected = Path(gog_import["game_directory"])
                            progress({"status":"running", "phase":"original_builder", "game_directory":str(selected)})
                            result = prepare_game(work_root=server.root, game_directory=selected,
                                recipe=recipe, wine=wine_binary, ffmpeg=server.root / "MI1 Generator/bin/ffmpeg",
                                user_consented=True, progress=progress,
                                patch_process_runner=patch_process_runner)
                            if gog_import:
                                result["gog_import"] = {"source_sha256":gog_import["source_sha256"], "receipt_path":gog_import["receipt_path"]}
                                atomic_json(server.root / "MI1 Generator/generated/game-preparation/preparation.json", result)
                            progress({"status":"completed", "phase":"completed"})
                        except Exception as exc:
                            progress({"status":"failed", "phase":"failed", "error":str(exc),
                                "restart_available":current_progress.get("phase") in ("original_builder", "native_sox", "native_flac", "speech_archive", "builder_finish", "german_patch")})
                            raise
                        finally:
                            server.game_preparation_active = False
                            server.patch_window_state = None
                        return self.respond(200, json.dumps({"completed": True, "music_sha256": result["music_sha256"],
                            "audio_mode": NATIVE_AUDIO_MODE,
                            "tts_started": False, "qa_started": False}, ensure_ascii=False))
                    return self.respond(404, "Nicht gefunden")
                except (ValueError, RuntimeError, OSError, KeyError, TypeError, json.JSONDecodeError,
                        subprocess.SubprocessError) as exc:
                    self.respond(400, json.dumps({"error": str(exc)}, ensure_ascii=False))

            def log_message(self, *_args):
                pass
        return Handler


PAGE = r"""<!doctype html><html lang=de><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>MI German Talkie Patch</title><link rel=stylesheet href=/launcher-wizard.css><link rel="stylesheet" href="/wiki/help.css"></head><body class=launcher-wizard data-help-source="generator_launcher/host.py">
<div class=wizard-shell><header class=wizard-topbar><h1><svg class="island-mark" viewBox="0 0 40 32" aria-hidden="true" focusable="false"><path fill="#173b43" d="M3 24h5v-4h5v-5h5v-5h5v4h5v4h5v5h4v5H3z"/><path fill="#c99a45" d="M18 10h5V5h3v5h-3v4h-5zM9 26h4v2H9zm17-7h3v2h-3z"/><path fill="#d9ba78" d="M20 7h2v3h-2z"/></svg><span>MI German Talkie Patch</span></h1><p id=platform class=muted>Patchstatus wird geladen …</p></header>
<ol class=wizard-progress aria-label=Einrichtung><li class=wizard-step><span class=step-number>1</span>Pfade</li><li class=wizard-step><span class=step-number>2</span>Bestätigungen</li><li class=wizard-step data-step=downloads aria-current=step><span class=step-number>3</span>Downloads</li><li class=wizard-step data-step=preparation><span class=step-number>4</span>Patch-Vorbereitung</li><li class=wizard-step data-step=render><span class=step-number>5</span>Sprachausgabe rendern</li><li class=wizard-step data-step=repair><span class=step-number>6</span>Review</li></ol>
<main class=wizard-main>
<section class=phase id=downloads-page data-page=downloads aria-labelledby=downloads-title><p class=eyebrow>Schritt 3 von 6</p><h2 id=downloads-title>Pakete herunterladen und einrichten</h2><p class=muted>Hier richtet der Patch die Hilfsprogramme und Sprachmodelle ein, mit denen du deine Spielkopie vorbereitest und die neuen Stimmen erzeugst. Er lädt bis zu drei Pakete gleichzeitig und verwendet bereits geprüfte Dateien weiter. Sobald auch die Einrichtung abgeschlossen ist, kannst du zur Patch-Vorbereitung gehen.</p><p id=selection></p><p id=consents-status class=muted>Bestätigungen werden übernommen …</p><p id=runtime-status>Paketübersicht wird geladen …</p><p id=setup-summary></p><button id=acquire class=primary disabled>Pakete laden und Hilfsprogramme einrichten</button><p class=muted>Wurde die Einrichtung unterbrochen, kannst du sie mit dieser Taste erneut starten. Falls macOS nach den Command Line Tools fragt, bestätige die Installation dieses Apple-Werkzeugpakets. Die <a href="#launcher.setup-dashboard" data-help-open="launcher.setup-dashboard">Einrichtungshilfe</a> erklärt dir die angezeigten Phasen.</p><details><summary>Paketübersicht und Voraussetzungen</summary><ul id=setup-requirements></ul><div id=package-table><table><thead><tr><th>Paket / Originalquelle</th><th>Größe</th><th>Vorhanden</th><th>Wiederverwendet</th><th>Neu geladen</th><th>Verbleibend</th><th>Status</th></tr></thead><tbody id=setup-packages></tbody></table></div><p id=preparation class=muted></p></details></section>
<section class=phase id=preparation-page data-page=preparation aria-labelledby=preparation-title hidden><p class=eyebrow>Schritt 4 von 6</p><h2 id=preparation-title>Patch-Vorbereitung</h2><div class=wizard-card><h3>1 · Rosetta prüfen</h3><p>Damit die benötigten Windows-Werkzeuge auf deinem Mac laufen, prüfst du zunächst Apples Ausführungshilfe Rosetta. Falls sich dafür ein Terminalfenster öffnet, folge den Apple-Abfragen und klicke nach der Installation erneut auf „Rosetta prüfen“. Mehr darüber erfährst du unter <a href="#launcher.wine" data-help-open="launcher.wine">Wine und Rosetta</a>.</p><button id=wine>Rosetta prüfen</button><p id=rosetta-status role=status aria-live=polite>Rosetta wurde noch nicht geprüft.</p></div><div class=wizard-card><h3>2 · Spieldaten vorbereiten und deutschen Patch anwenden</h3><p id=builder-info>Klicke auf „Spieldaten vorbereiten und Patch starten“, um deine Spielkopie vorzubereiten. Anschließend öffnet sich das Fenster für die deutsche Übersetzung. Wenn er nach dem Zielordner fragt, verwendest du den hier hervorgehobenen Windows-Pfad. Fertig ist die Vorbereitung, sobald die Konsole „Vorbereitung abgeschlossen“ meldet. Die <a href="#launcher.preparation-workflow" data-help-open="launcher.preparation-workflow">Anleitung zur Spielvorbereitung</a> begleitet dich durch die geöffneten Fenster.</p><div class=patch-target-box><label for=patch-target-path>Windows-Zielordner für den deutschen Patch</label><textarea id=patch-target-path class=path-display rows=3 readonly spellcheck=false aria-label="Windows-Zielordner des deutschen Patchs">Der Zielordner wird geladen …</textarea><p>Wenn der deutsche Patch den Zielordner verlangt, kopiere diesen Pfad hier mit Command+C und füge ihn dort mit Control+V ein. So landet der Patch in der vorbereiteten deutschen Arbeitskopie.</p></div><section id=preparation-help aria-label="Anleitung zur Patch-Vorbereitung"><h4>Die geöffneten Fenster bedienen</h4><div id=preparation-help-steps></div></section><button id=game class=primary disabled>Spieldaten vorbereiten und Patch starten</button></div><section id=patch-assistant class=wizard-card role=status hidden><h3>Deutscher Patch · jetzt im Wine-Fenster weiter</h3><ol><li>Öffne den nächsten Dialog mit <strong>Weiter</strong>.</li><li>Wenn du nach einem Ordner gefragt wirst, füge den hervorgehobenen Windows-Zielordner ein. Der Pfad wird beim Start automatisch in die Zwischenablage kopiert.</li><li>Folge den weiteren Patchdialogen bis zur Abschlussmeldung. Danach kehrst du zu dieser Seite zurück.</li></ol><p>Falls du den Pfad noch einmal brauchst, verwende den Kopierknopf oder Command+C im hervorgehobenen Feld. Im Wine-Patch fügst du ihn mit Control+V ein; alternativ kannst du Rechtsklick → „Einfügen“ verwenden, wenn der Dialog dies anbietet.</p><div><button id=patch-focus type=button>Patchfenster in den Vordergrund holen</button><button id=patch-copy type=button>Zielordner in die Zwischenablage kopieren</button></div><p>Falls das Fenster nicht nach vorne kommt, klicke im macOS-Dock auf das gelbe Wine-Symbol.</p></section><div id=preparation-restart-panel hidden><p id=preparation-restart-description>Wenn du die Spielvorbereitung neu beginnen musst, werden ihre bisherigen Arbeitskopien samt Protokollen gelöscht. Sichere benötigte Protokolle deshalb vorher. Deine Downloads, Sprachmodelle, erzeugten Aufnahmen und Reviewentscheidungen bleiben erhalten; anschließend startest du die Vorbereitung erneut.</p><button id=preparation-restart type=button>Vorbereitung löschen und neu beginnen</button></div></section>
<section class=phase id=render-page data-page=render aria-labelledby=render-title hidden><p class=eyebrow>Schritt 5 von 6</p><h2 id=render-title>Sprachausgabe rendern</h2><p class=muted>Jetzt gibst du den Figuren ihre Stimmen. Wähle für jede Rolle eine Sprechprobe und entscheide anschließend, wie der Patch die deutschen Aufnahmen erzeugen soll. Mit „Rendering starten“ beginnt die Arbeit; nach dem vollständigen Durchlauf führt dich die Review zum Anhören und Verbessern.</p><div class=wizard-card><label>Rendermodus <select id=render-mode><option value=fast>Schnell · ANE + GPU gleichzeitig</option><option value=medium>Mittel · ANE · mittel</option><option value=slow selected>Langsam · GPU · höchst</option></select></label><p class=muted>Für die aktuellen Aussprachekorrekturen empfehlen wir GPU. ANE ist Apples zusätzlicher KI-Rechenteil; zusammen mit der GPU erzeugt er die Aufnahmen am schnellsten, spricht aber etwas anders. Eine eigene Abstimmung dafür fehlt noch. Die <a href="#launcher.render-mode" data-help-open="launcher.render-mode">Modusauswahl</a> erklärt dir die Unterschiede.</p><p id=render-recipe-hint class=muted role=status style="white-space:pre-line" hidden></p><label><input id=render-ultra-fast type=checkbox disabled> Ultra Fast · mehrere ANE-Aufgaben gleichzeitig</label><p class=muted>Mit Ultra Fast darf der Patch mehrere ANE-Aufgaben gleichzeitig ausführen, soweit Messung und freier Arbeitsspeicher das erlauben. Für den ersten Durchlauf kannst du die Option ausgeschaltet lassen. <a href="#launcher.ultra-fast" data-help-open="launcher.ultra-fast">Wann Ultra Fast hilft</a>.</p><label>Fassung <select id=render-list data-help-id="launcher.render-list" disabled><option value="__loading__">Fassungen werden geladen …</option></select></label><div id=fassung-details class=fassung-details></div><p id=fassung-progress role=status></p><p id=fassung-mode-note class=muted hidden></p><p id=fassung-message role=status hidden></p><p class=muted>Das Rendering kann mehrere Stunden dauern. Mit „Anhalten“ legst du eine Pause ein. Wähle später dieselbe Fassung und klicke „Rendering starten“, um weiterzuarbeiten. Auch nach einem Neustart bleiben gespeicherte Aufnahmen erhalten. Die <a href="#launcher.mode-switch" data-help-open="launcher.mode-switch">Hilfe zu Fassungen und Rendermodi</a> erklärt dir, wie du verschiedene Durchläufe vergleichst.</p><button id=repair-options type=button hidden>Automatische Reparaturmöglichkeiten</button><div class=render-benchmark-actions><button id=benchmark-cancel type=button hidden>Benchmark abbrechen</button></div><p class=muted>Fortschritt, Meldungen und Benchmark-Ergebnisse erscheinen in der integrierten Konsole unten.</p></div><div class=wizard-card id=benchmark-panel><h3>GPU / ANE vergleichen</h3><p class=muted>Beim ersten Durchlauf vergleicht der Patch an wenigen Dialogen, welche Verarbeitung auf deinem Mac innerhalb deiner Moduswahl am schnellsten arbeitet. Warte diese Messung ab; danach geht der Auftrag weiter. Später kann der Patch passende gespeicherte Ergebnisse verwenden. Du brauchst diesen <a href="#launcher.benchmark" data-help-open="launcher.benchmark">Geschwindigkeitsvergleich</a> nicht selbst zu starten.</p></div><div id=voice-selection><h3>Eine Stimme für jede Rolle</h3><p>Eine eigene Sprechprobe gibt der KI ein Klangvorbild für die Figur, den <a href="#launcher.anchor" data-help-open="launcher.anchor">Stimmanker</a>. Wähle WAV, FLAC oder OGG mit 3–15 Sekunden und höchstens 20 MiB und trage den genauen Wortlaut ein. Mindestens drei Sekunden müssen tatsächlich gesprochen sein. Eine zweite Aufnahme derselben Person kannst du später für automatische Reparaturversuche ergänzen.</p><button id=voices-demo>Originalstimmen für alle Rollen wählen · Tech-Demo</button><div id=voice-roles></div></div></section>
<section class=phase id=repair-section data-page=repair aria-labelledby=repair-title hidden><p class=eyebrow>Schritt 6 von 6</p><h2 id=repair-title>Review</h2><p class=muted>Die Aufnahmen sind erzeugt. Jetzt hörst du, ob die Figuren verständlich sprechen und die Betonung zur Szene passt. Du kannst zuerst einen automatischen Reparaturversuch für auffällige Dialoge starten oder direkt zur vollständigen Review gehen. Die <a href="#review.pronunciation" data-help-open="review.pronunciation">Aussprachehilfe</a> hilft dir, passende Einstellungen für weitere Versuche zu finden.</p><button id=repair-start>Automatischen Reparaturversuch starten</button><div id=repair-confirm-panel hidden><p>Soll der Patch für geeignete auffällige Dialoge weitere Varianten versuchen? Deine vorhandenen Aufnahmen und Entscheidungen bleiben erhalten. Höre dir anschließend auch die automatisch verbesserten Ergebnisse an.</p><button id=repair-confirm class=primary>Ja, automatischen Reparaturversuch starten</button><button id=repair-cancel>Abbrechen</button></div><p id=repair-status>Noch kein Reparaturversuch gestartet.</p><details><summary>Reparaturdetails</summary><pre id=repair-log></pre></details><p id=review-status></p></section>
</main><nav class=wizard-actions aria-label=Seitennavigation><button id=wizard-back type=button>Zurück</button><p id=wizard-next-hint>Richte zuerst die Pakete ein.</p><button id=render-pause type=button hidden disabled>Anhalten</button><button id=render-start class=primary type=button hidden>Rendering starten</button><button id=review-open class=primary type=button hidden>Zur Review</button><button id=wizard-next class=primary type=button disabled>Weiter zur Patch-Vorbereitung</button></nav><dialog id=fassung-restart-dialog aria-labelledby=fassung-restart-title><h2 id=fassung-restart-title>Diese Fassung ist bereits gerendert</h2><p id=fassung-restart-description></p><p>Beim Ersetzen wird auch die zugehörige Review ersetzt. Deine bisherige Fassung bleibt erhalten, bis die neue vollständig erstellt ist. Mit einer weiteren Fassung kannst du beide getrennt vergleichen.</p><div class=dialog-actions><button id=fassung-cancel type=button>Abbrechen</button><button id=fassung-replace type=button>Ausgewählte Fassung ersetzen</button><button id=fassung-new class=primary type=button>Weitere Fassung erstellen</button></div></dialog></div>
<section id=launcher-console class=launcher-console aria-label=Patch-Konsole><pre id=setup-log aria-label=Zentrales Patchprotokoll tabindex=0></pre></section>
<script src="/wiki/data.js"></script><script src="/wiki/help.js"></script><script src="/wiki/app-help.js"></script><script src=/launcher-terminal.js></script><script src=/launcher-console.js></script><script>const token=new URL(location.href).searchParams.get('session'),headers={'X-Launcher-Session':token};
async function post(path,body){
const actions={'/api/benchmark/queue':['benchmark','Benchmark vormerken'],'/api/benchmark/cancel':['benchmark','Benchmark abbrechen'],'/api/setup/start':['setup','Pakete und Hilfsprogramme einrichten'],'/api/preparation/rosetta-check':['rosetta','Rosetta prüfen'],'/api/preparation/game':['game','Vorbereitung'],'/api/preparation/restart':['game','Vorbereitung zurücksetzen'],'/api/voices/select':['voices','Stimme speichern'],'/api/voices/alternative':['voices','Zweite Sprechprobe speichern'],'/api/voices/original-demo':['voices','Originalstimmen wählen'],'/api/render/start':['render','Renderauftrag starten'],'/api/render/pause':['render','Renderauftrag anhalten'],'/api/render/resume':['render','Renderauftrag fortsetzen'],'/api/repair/start':['repair','Reparaturauftrag starten'],'/api/review/activate':['review','Review öffnen']};
const [source,action]=actions[path]||['game','Ausgewählten Schritt ausführen'];
window.LauncherConsole?.update(source,action+' · läuft …',[],{status:'running'});
try{const r=await fetch(path,{method:'POST',headers:{...headers,'Content-Type':'application/json'},body:JSON.stringify(body)});const v=await r.json();if(!r.ok)throw Error(v.error||'Schritt fehlgeschlagen');const completion={'/api/preparation/restart':v.explanation};if(!['/api/preparation/game','/api/preparation/rosetta-check'].includes(path))window.LauncherConsole?.update(source,completion[path]||action+(v.accepted?' · Auftrag angenommen':' · abgeschlossen'),[],{status:v.accepted?'running':v.status||'completed'});return v;}
catch(e){window.LauncherConsole?.update(source,action+' · Fehler: '+e.message,[],{status:'failed'});e.launcherReported=true;throw e;}}
function launcherActionError(source,error){if(!error.launcherReported){window.LauncherConsole?.update(source,'Fehler: '+error.message,[],{status:'failed'});error.launcherReported=true;}}

const byteLabel=n=>{if(n==null)return 'unbekannt';const units=['B','KB','MB','GB'];let i=0,v=n;while(v>=1000&&i<3){v/=1000;i++;}return v.toLocaleString('de-DE',{maximumFractionDigits:i?1:0})+' '+units[i];};
const phaseLabel={preview:'Bereit',download:'Pakete laden und prüfen',download_ready:'Downloads geprüft · Einrichtung folgt',verify_runtime:'Vorhandene Hilfsprogramme prüfen',runtime_reused:'Geprüfte Hilfsprogramme weiterverwenden',build:'Hilfsprogramme einrichten',extract:'Geladene Pakete entpacken',ane_install:'ANE-Verarbeitung einrichten',awaiting_user:'Programme und Sprachmodelle sind bereit',failed:'Unterbrochen / fehlgeschlagen'};
const stateLabel={pending:'Ausstehend',partial:'Teilweise vorhanden',existing_unverified:'Vorhanden · noch nicht geprüft',downloading:'Lädt / prüft',verified:'Geprüft',complete:'Geprüft',reused:'Wiederverwendet',failed:'Fehlgeschlagen',installing:'Wird installiert'};
let setupBusy=false, gameBusy=false, reviewBusy=false;
window.LauncherRosettaVerified=false;
window.LauncherRosettaBusy=false;
async function refreshSetup(){const startupCheck=!refreshSetup.startupChecked;refreshSetup.startupChecked=true;let checkError=null;try{const r=await fetch('/api/setup/status',{headers});if(!r.ok)throw Error('Paketstatus nicht verfügbar');const s=await r.json();setupBusy=s.busy;window.LauncherWizard?.update({setupReady:s.installed===true||window.LauncherResumeVerified===true,setupBusy:s.busy});
const table=document.querySelector('#setup-packages');table.replaceChildren();for(const p of s.packages){const row=document.createElement('tr');const name=document.createElement('td');const link=document.createElement('a');link.textContent=p.name;link.href=p.url;link.target='_blank';link.rel='noopener';name.append(link);if(p.terms.url){const terms=document.createElement('a');terms.textContent=' · Bedingungen';terms.href=p.terms.url;terms.target='_blank';terms.rel='noopener';name.append(terms);}row.append(name);for(const v of [byteLabel(p.bytes),byteLabel(p.actual_bytes)+(p.bytes?' / '+byteLabel(p.bytes)+' ('+Math.min(100,Math.floor(100*p.actual_bytes/p.bytes))+' %)':''),byteLabel(p.reused_bytes),byteLabel(p.downloaded_bytes),byteLabel(p.remaining_bytes),stateLabel[p.state]||p.state]){const cell=document.createElement('td');cell.textContent=v;row.append(cell);}table.append(row);}
document.querySelector('#setup-requirements').replaceChildren(...s.requirements.map(v=>{const li=document.createElement('li');li.textContent=v;return li;}));
document.querySelector('#runtime-status').textContent='Phase: '+(phaseLabel[s.phase]||s.phase)+(s.current?' · '+s.current:'')+(s.error?' · '+s.error:'');
document.querySelector('#setup-summary').textContent='Pakete: '+s.completed+' erledigt / '+s.package_count+' gesamt / '+s.remaining+' verbleibend · bekannte Gesamtgröße '+byteLabel(s.known_total_bytes)+' + '+s.unknown_size_count+' Größen unbekannt · tatsächlich vorhanden '+byteLabel(s.actual_bytes)+' · wiederverwendet '+byteLabel(s.reused_bytes)+' · neu geladen '+byteLabel(s.downloaded_bytes)+' · bekannte Restgröße '+byteLabel(s.known_remaining_bytes)+' · '+s.ane_status+(s.download_ready?' · Downloadbatch vollständig geprüft':'');
window.LauncherConsole?.update('setup',document.querySelector('#runtime-status').textContent+' · Pakete '+s.completed+' / '+s.package_count+' · neu geladen '+byteLabel(s.downloaded_bytes)+' · wiederverwendet '+byteLabel(s.reused_bytes)+' · bekannte Restgröße '+byteLabel(s.known_remaining_bytes),s.log,{status:s.busy?'running':s.phase==='failed'?'failed':'ready'});
for(const id of ['acquire','wine','game','render-start','review-open'])document.getElementById(id).disabled=s.busy||gameBusy||(id==='review-open'&&reviewBusy);
if(!window.LauncherConsents?.packages)document.getElementById('acquire').disabled=true;
if(window.updateLauncherLocks)window.updateLauncherLocks();
}catch(e){checkError=e;document.querySelector('#runtime-status').textContent=e.message;window.LauncherConsole?.update('setup','Paketstatus · Fehler: '+e.message);}finally{if(startupCheck)window.LauncherConsole?.startupCheckDone?.('packages',checkError);}}
document.querySelector('#acquire').onclick=async()=>{try{if(!window.LauncherConsents?.packages)throw Error('Lies die Originalbedingungen und bestätige sie zuerst.');document.getElementById('acquire').disabled=true;await post('/api/setup/start',{terms_confirmed:true});await refreshSetup();}catch(e){launcherActionError('setup',e);}};
refreshSetup();setInterval(refreshSetup,1000);
let rosettaStatusRequest=0;
function paintRosetta(value){
  window.LauncherRosettaInstallerActive=['starting','running'].includes(value.installer?.status);
  window.LauncherRosettaVerified=value.rosetta_verified===true;
  const detail=value.detail||(window.LauncherRosettaVerified?'Rosetta ist verfügbar.':'Du musst Rosetta installieren. Klicke auf „Rosetta prüfen“, um die Installation zu starten.');
  document.querySelector('#rosetta-status').textContent=detail;
  window.LauncherConsole?.update('rosetta',detail,[],{status:window.LauncherRosettaVerified?'completed':value.status==='unchecked'?'ready':'failed'});
  window.updateLauncherLocks?.();
}
async function refreshRosetta(){if(window.LauncherRosettaBusy)return;const request=++rosettaStatusRequest;try{const r=await fetch('/api/preparation/rosetta-status',{headers});const v=await r.json();if(!r.ok)throw Error(v.error||'Rosetta-Status nicht verfügbar');if(request!==rosettaStatusRequest||window.LauncherRosettaBusy)return;paintRosetta(v);}catch(e){if(request!==rosettaStatusRequest||window.LauncherRosettaBusy)return;window.LauncherRosettaVerified=false;document.querySelector('#rosetta-status').textContent='Rosetta konnte nicht geprüft werden. Klicke auf „Rosetta prüfen“.';launcherActionError('rosetta',e);window.updateLauncherLocks?.();}}
document.querySelector('#wine').onclick=async()=>{if(window.LauncherRosettaBusy)return;try{if(!window.LauncherConsents?.wine)throw Error('Bestätige zuerst die Wine-Bedingungen unter „Bestätigungen“.');window.LauncherRosettaBusy=true;rosettaStatusRequest++;window.LauncherRosettaVerified=false;window.updateLauncherLocks?.();paintRosetta(await post('/api/preparation/rosetta-check',{}));}catch(e){document.querySelector('#rosetta-status').textContent='Rosetta-Prüfung fehlgeschlagen. Du musst Rosetta installieren und danach erneut prüfen. Falls kein Terminal erscheint: /usr/sbin/softwareupdate --install-rosetta';launcherActionError('rosetta',e);}finally{window.LauncherRosettaBusy=false;window.updateLauncherLocks?.();}};
refreshRosetta();setInterval(()=>{if(window.LauncherRosettaInstallerActive)refreshRosetta();},2000);
document.querySelector('#game').onclick=async()=>{try{if(!window.LauncherConsents?.game)throw Error('Bestätige die Originalbedingungen vor dem Patchlauf.');if(!window.LauncherRosettaVerified)throw Error('Du musst Rosetta installieren und danach „Rosetta prüfen“ anklicken, bevor du die Spieldaten vorbereitest.');gameBusy=true;window.updateLauncherLocks?.();await post('/api/preparation/game',{terms_confirmed:true});}catch(e){launcherActionError('game',e);}finally{gameBusy=false;window.updateLauncherLocks?.();}};
document.querySelector('#render-start').onclick=()=>window.FassungControls?.start();
document.querySelector('#review-open').onclick=async()=>{
  const button=document.querySelector('#review-open'),out=document.querySelector('#review-status');if(reviewBusy)return;reviewBusy=true;window.LauncherReviewActivating=true;button.disabled=true;
  window.updateLauncherLocks?.();
  out.textContent='Vollständige Review wird geprüft und geladen …';
  window.LauncherConsole?.update('review','Review wird geladen · Aktivierung läuft …',[],{status:'running'});
  let polling=true,activationPending=false,lastActivationMessage=null;
  const refreshActivation=async()=>{if(!polling||activationPending)return;activationPending=true;try{const response=await fetch('/api/review/activation-status'),value=await response.json();
    if(!polling)return;
    const counts=value.total?` · ${value.completed||0} / ${value.total}`:'';
    const elapsed=value.elapsed_seconds?` · ${Math.round(value.elapsed_seconds)} s`:'';
    const detail=value.error||value.detail||'Review wird vorbereitet';
    out.textContent=detail+counts+elapsed;
    const message=detail+counts,key=JSON.stringify([value.phase,value.status,message]);
    if(key!==lastActivationMessage){lastActivationMessage=key;window.LauncherConsole?.update('review',message,[],{status:value.status==='failed'?'failed':value.status==='completed'?'completed':'running'});}
  }catch{}finally{activationPending=false}};
  const timer=setInterval(()=>{if(polling)refreshActivation();},750);
  try{await post('/api/review/activate',{});polling=false;clearInterval(timer);window.LauncherNavigating=true;
    window.LauncherConsole?.update('review','Review geöffnet · bereit',[],{status:'completed'});out.textContent='Review ist bereit.';
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));location.href='/patch-review/index.html?session='+encodeURIComponent(token);
  }catch(e){window.LauncherNavigating=false;reviewBusy=false;window.LauncherReviewActivating=false;polling=false;clearInterval(timer);window.updateLauncherLocks?.();window.LauncherConsole?.update('review','Review öffnen · Fehler: '+e.message,[],{status:'failed'});out.textContent=e.message;
  }
};
fetch('/api/status',{headers}).then(r=>r.json()).then(async s=>{document.querySelector('#platform').textContent=s.platform||'Arbeitslaufwerk ist nicht verfügbar';if(s.project){window.LauncherRecipeDefaults?.apply(s.project.render_recipe);document.querySelector('#selection').textContent=(s.project.render_recipe?'Dein Rezept ist geladen · '+s.project.render_recipe.label:s.project.correction_loaded?'Dein Rezept ist geladen.':'Die mitgelieferten Aussprachevorgaben sind geladen.')+' Spielquelle: '+(s.project.game_input_kind==='gog_installer_candidate'?'GOG-Installationsdatei gewählt':s.project.pc_special_edition_directory?'Windows-Spielordner gewählt':'noch nicht gewählt')+'.';const r=await fetch('/api/preparation/resolve',{headers}),p=await r.json();document.querySelector('#preparation').textContent=p.ready?'Die Vorgaben für '+p.roles+' Rollen sind bereit. Die Spracherzeugung beginnt später mit „Rendering starten“.':'Die Rollenvorgaben konnten nicht vorbereitet werden: '+p.error;}else document.querySelector('#selection').textContent='Wähle deine Spielquelle und kehre bei Bedarf zur Einrichtung im Patch zurück.';}).catch(()=>document.querySelector('#platform').textContent='Arbeitslaufwerk ist nicht verfügbar.');</script><script src="/launcher-wizard.js"></script><script src="/patch-window.js"></script><script src="/fassungen.js"></script><script src="/launcher-flow.js"></script></body></html>"""


def serve(root: Path, manifest: Path, correction: Path | None, *, game_directory: Path | None = None,
          stop_event=None, initial_consents=None, startup_log=None) -> tuple[HostServer, str]:
    root = root.resolve(strict=True)
    lock = root / "MI1 Generator" / ".launcher" / "host.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    server = HostServer(("127.0.0.1", 0), token, root, manifest, correction, game_directory,
                        initial_consents=initial_consents, startup_log=startup_log)
    payload = {"pid": os.getpid(), "port": server.server_port,
               "url": f"http://127.0.0.1:{server.server_port}/?session={token}",
               "token": token, "profile": "mi1-patch-launcher/1", "launcher_version": LAUNCHER_VERSION}
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        try:
            existing = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        os.close(fd)
        server.server_close()
        raise RuntimeError(f"Eigener Launcher läuft bereits auf Port {existing.get('port')}") from exc
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(payload).encode("utf-8"))
    os.fsync(fd)
    server.lock_fd = fd
    return server, token


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--correction", type=Path)
    parser.add_argument("--game-directory", type=Path)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--native-owned", action="store_true")
    parser.add_argument("--initial-consents", type=json.loads)
    parser.add_argument("--startup-log", type=json.loads)
    parser.add_argument("--start-setup", action="store_true")
    parser.add_argument("--resume-check", action="store_true")
    args = parser.parse_args(argv)
    if args.resume_check:
        from .resume import resume_check
        print(json.dumps(resume_check(args.root,args.manifest,args.correction)),flush=True)
        return
    # Reject malformed native input before creating project state or starting jobs.
    consents = startup_consents(args.initial_consents)
    log = startup_history(args.startup_log)
    if args.start_setup and not all(consents.get(key) is True for key in ("packages", "wine", "game")):
        raise ValueError("Downloadauftrag benötigt die drei nativen Bestätigungen")
    from .lifetime import NativeHostLifetime
    with NativeHostLifetime(args.parent_pid, args.native_owned):
        config = project_config(args.root, args.manifest, args.correction, args.game_directory)
        server, token = serve(args.root, args.manifest, args.correction, game_directory=args.game_directory,
                              initial_consents=consents if args.initial_consents is not None else None,
                              startup_log=log)
        try:
            if args.start_setup:
                server.setup.start(terms_confirmed=True)
            print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/?session={token}",
                              "session": token, "port": server.server_port, "jobs_started": int(args.start_setup),
                              "manifest_id": config["manifest_id"]}), flush=True)
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
