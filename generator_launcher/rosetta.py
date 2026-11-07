"""Explicit Rosetta probe and visible, interactive macOS installation handoff."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
import time


class RosettaPreparation:
    def __init__(self, root: Path, write_json):
        self.directory = root / 'MI1 Generator/.launcher'
        self.state_path = self.directory / 'preparation-state.json'
        self.write_json = write_json
        self.installer = None
        self.installer_error = None
        self.launched_at = None
        self.lock = threading.RLock()

    def _state(self):
        return json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {}

    def _installer_status(self):
        if self.installer_error:
            return {'status': 'failed', 'detail': self.installer_error}
        if self.installer is None:
            return {'status': 'idle'}
        run = self.installer
        detail = ''
        log = run / 'output.log'
        if log.is_file():
            with log.open('rb') as stream:
                stream.seek(max(0, log.stat().st_size - 6000))
                detail = stream.read(6000).decode('utf-8', 'replace').strip()
        done = run / 'exit-code'
        if done.is_file():
            try:
                code = int(done.read_text().strip())
            except ValueError:
                return {'status': 'failed', 'detail': 'Ungültiger Rosetta-Installationsabschluss. ' + detail}
            return {'status': 'completed' if code == 0 else 'failed', 'exit_code': code, 'detail': detail}
        pid_path = run / 'pid'
        if pid_path.is_file():
            try:
                os.kill(int(pid_path.read_text().strip()), 0)
            except (ValueError, ProcessLookupError):
                return {'status': 'failed', 'detail': 'Rosetta-Installation wurde unterbrochen. ' + detail}
            except PermissionError:
                pass
            return {'status': 'running', 'detail': detail}
        if time.monotonic() - self.launched_at < 15:
            return {'status': 'starting', 'detail': detail}
        return {'status': 'failed', 'detail': 'Terminal hat die Rosetta-Installation nicht gestartet. ' + detail}

    def snapshot(self):
        with self.lock:
            state = self._state().get('rosetta', {})
            verified = state.get('status') == 'verified'
            installer = self._installer_status()
            detail = state.get('detail') or ('Rosetta ist verfügbar. Du kannst mit der Vorbereitung fortfahren.'
                     if verified else 'Klicke auf „Rosetta prüfen“.')
            if not verified and installer['status'] == 'failed':
                detail += ('\nDie Rosetta-Installation ist fehlgeschlagen oder wurde nicht gestartet. '
                           'Du musst Rosetta installieren. Öffne Terminal und führe '
                           '„/usr/sbin/softwareupdate --install-rosetta“ aus; klicke nach dem Abschluss '
                           'erneut auf „Rosetta prüfen“.\n' + installer.get('detail', ''))
            elif not verified and installer['status'] == 'completed':
                detail = ('Die Rosetta-Installation wurde abgeschlossen. Klicke jetzt erneut auf '
                          '„Rosetta prüfen“, damit die Verfügbarkeit bestätigt wird.')
            return {'verified': verified, 'rosetta_verified': verified,
                    'status': 'verified' if verified else state.get('status', 'unchecked'),
                    'detail': detail,
                    'installer': installer, 'installer_started': False,
                    'wine_opened': False, 'game_build_started': False}

    def _start_installer(self):
        if self._installer_status()['status'] in {'starting', 'running'}:
            return False, ''
        self.installer_error = None
        self.directory.mkdir(parents=True, exist_ok=True)
        run = Path(tempfile.mkdtemp(prefix='rosetta-install-', dir=self.directory))
        command = run / 'Rosetta installieren.command'
        quote = lambda name: shlex.quote(str(run / name))
        command.write_text('#!/bin/bash\n'
            f'echo $$ > {quote("pid")}\n'
            'echo "Rosetta installieren: Bitte die Lizenz im Terminal lesen und bestätigen."\n'
            f'/usr/sbin/softwareupdate --install-rosetta 2>&1 | /usr/bin/tee {quote("output.log")}\n'
            'rosetta_install_result=${PIPESTATUS[0]}\n'
            f'printf "%s\\n" "$rosetta_install_result" > {quote("exit-code.tmp")}\n'
            f'/bin/mv {quote("exit-code.tmp")} {quote("exit-code")}\n'
            'echo "Zurück zum Launcher wechseln und erneut auf Rosetta prüfen klicken."\n'
            'exit "$rosetta_install_result"\n', encoding='utf-8')
        command.chmod(0o700)
        try:
            result = subprocess.run(['/usr/bin/open', '-a', 'Terminal', str(command)],
                                    capture_output=True, text=True, timeout=15)
            if result.returncode:
                self.installer_error = (result.stderr or result.stdout or 'Terminal konnte nicht geöffnet werden.').strip()
                return False, self.installer_error
        except (OSError, subprocess.SubprocessError) as exc:
            self.installer_error = str(exc)
            return False, self.installer_error
        self.installer, self.launched_at = run, time.monotonic()
        return True, ''

    def check(self):
        with self.lock:
            state = self._state()
            try:
                result = subprocess.run(['/usr/bin/arch', '-x86_64', '/usr/bin/true'],
                                        capture_output=True, text=True, timeout=15)
                verified = result.returncode == 0
                diagnostic = (result.stderr or result.stdout or '').strip()
            except (OSError, subprocess.SubprocessError) as exc:
                verified, diagnostic = False, str(exc)
            if verified:
                state['rosetta'] = {'status': 'verified', 'architecture': 'x86_64',
                                    'detail': 'Rosetta ist verfügbar. Du kannst mit der Vorbereitung fortfahren.'}
                self.write_json(self.state_path, state)
                return self.snapshot()
            # Revoke any earlier success before attempting the installation handoff.
            detail = ('Du musst Rosetta installieren. Lies und bestätige die Installation im sichtbaren Terminal. '
                      'Warte auf deren Abschluss und klicke danach erneut auf „Rosetta prüfen“. '
                      'Falls kein Terminal erscheint oder die Installation fehlschlägt: Öffne Terminal und führe '
                      '„/usr/sbin/softwareupdate --install-rosetta“ aus; prüfe danach erneut.')
            if diagnostic:
                detail += '\nRosetta-Prüfung: ' + diagnostic[-6000:]
            state['rosetta'] = {'status': 'installation_required', 'detail': detail}
            self.write_json(self.state_path, state)
            started, _error = self._start_installer()
            value = self.snapshot()
            value['installer_started'] = started
            return value
