"""Read-only admission for returning directly to an existing render job."""
from pathlib import Path
import hashlib
import hmac
import json
import subprocess
from .contracts import resolve_relative, validate_state, load_existing_installation, canonical_json, digest
from .runtime_setup import verify_installed_runtime


def _bundled_default(path):
    """Recognize the launcher resource default, independent of app version."""
    path = Path(path).resolve(strict=True)
    return (path.name == 'manifest.json' and path.parent.name == 'defaults' and
            path.parent.parent.name == 'Resources' and
            path.parent.parent.parent.name == 'Contents' and
            path.parent.parent.parent.parent.suffix == '.app')


def _authenticated_review(root, home, *, version=None, project_id=None, active=None, installation=None):
    """Validate signed activation, completed review, and materialized source selection."""
    try:
        if active is None:
            active_path = home / '.launcher/active-review.json'
            if active_path.is_symlink():
                return None
            active = json.loads(active_path.read_text(encoding='utf-8'))
        if version is not None and active.get('version') != version:
            return None
        installation = installation or load_existing_installation(home)
        body = {key: value for key, value in active.items() if key != 'signature'}
        expected = hmac.new(installation.receipt_key, canonical_json(body), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(active.get('signature', ''), expected):
            return None
        review_root = resolve_relative(home, active['review'], must_exist=True)
        version_path = review_root / 'launcher-version.json'
        if version_path.is_symlink():
            return None
        review_state = validate_state(json.loads(version_path.read_text()))
        active_version = active['version']
        if (review_state.get('kind') != 'review' or review_state.get('status') != 'completed' or
                review_state.get('version') != active_version or
                review_state.get('project_id') != (project_id or review_state.get('project_id')) or
                active.get('state_sha256') != digest(review_state) or not review_state.get('bindings')):
            return None
        source_path = resolve_relative(home, active['source'], must_exist=True)
        run_root = resolve_relative(home, active['run'], must_exist=True)
        run_paths = [run_root / name for name in ('manifest.json', 'run-plan.json', 'render-report.json')]
        if any(path.is_symlink() for path in run_paths):
            return None
        source = json.loads(source_path.read_text(encoding='utf-8'))
        run_manifest = json.loads((run_root / 'manifest.json').read_text(encoding='utf-8'))
        production = run_manifest.get('source_evidence', {}).get('production', {})
        selection = source.get('source_evidence', {}).get('launcher_selection', {})
        run_selection = run_manifest.get('source_evidence', {}).get('launcher_selection', {})
        source_hash = active_version.get('manifest_sha256')
        correction_hash = selection.get('correction_sha256')
        from patch_review.evidence import Evidence
        evidence = Evidence(source_path, home, run_root)
        report = evidence.report
        if (not evidence.valid or report.get('status') != 'complete' or
                report.get('all_ids_accounted') is not True or
                not isinstance(source_hash, str) or not source_hash or
                production.get('source_manifest_sha256') != digest(source) or
                selection.get('manifest_sha256') != source_hash or
                run_selection.get('manifest_sha256') != source_hash or
                run_selection.get('correction_sha256') != correction_hash):
            return None
        return dict(active=active, version=active_version, review_state=review_state,
                    source=source, run_manifest=run_manifest, report=report, manifest_sha256=source_hash,
                    correction_sha256=correction_hash)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def completed_review_status(home, config):
    """Return a synthetic completed status for an authenticated review only."""
    try:
        home = Path(home).resolve(strict=True)
        root = home.parent
        version = config['project_state']['version']
        # Preserve an actual incomplete job for this same version.
        try:
            saved = json.loads((home / '.launcher/render-status.json').read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            saved = {}
        if saved.get('version') == version and saved.get('status', {}).get('status') in {
                'running', 'pausing', 'paused', 'failed', 'completed_with_errors'}:
            return None
        selected = _authenticated_review(root, home, version=version,
                                         project_id=config['project_state']['project_id'])
        if selected is None:
            return None
        if selected['manifest_sha256'] != version['manifest_sha256']:
            return None
        manifest_path = resolve_relative(home, config['manifest'], must_exist=True)
        manifest_raw = manifest_path.read_bytes()
        if hashlib.sha256(manifest_raw).hexdigest() != version['manifest_sha256']:
            return None
        manifest_value = json.loads(manifest_raw)
        if (manifest_value.get('manifest_id') != version['manifest_id'] or
                manifest_value.get('revision', manifest_value.get('manifest_revision', 1)) != version['manifest_revision']):
            return None
        correction_path = (resolve_relative(home, config['correction'], must_exist=True)
                           if config.get('correction') else None)
        correction_raw = correction_path.read_bytes() if correction_path else None
        if correction_path and hashlib.sha256(correction_raw).hexdigest() != correction_path.stem:
            return None
        correction_hash = digest(json.loads(correction_raw)) if correction_path else None
        if correction_hash != selected['correction_sha256']:
            return None
        count = len(selected['report']['dialogues'])
        return {'status': 'completed', 'phase': 'review', 'total': count,
                'completed': count, 'remaining': 0, 'qa_backlog': 0,
                'run': selected['active']['run']}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _recover_signed_review_selection(root, home, project, default_path):
    """Find exact retained sources named by a completed signed review."""
    # A matching version needs no recovery. In particular, opening the app
    # must not parse the entire materialized run just to keep its current
    # inputs. Explicit review activation still authenticates all its evidence.
    active_path=resolve_relative(home,'.launcher/active-review.json',must_exist=True)
    active=json.loads(active_path.read_text(encoding='utf-8'))
    if active.get('version')==project['project_state']['version']:
        return None
    selected = _authenticated_review(root, home, project_id=project['project_state']['project_id'])
    if selected is None:
        return None
    version = selected['version']
    try:
        saved = json.loads((home / '.launcher/render-status.json').read_text())
    except (OSError, ValueError, TypeError):
        saved = {}
    if (saved.get('version') in (version, project['project_state']['version']) and
            saved.get('status', {}).get('status') in
            {'running', 'pausing', 'paused', 'failed', 'completed_with_errors'}):
        return None
    current_path = resolve_relative(home, project['manifest'], must_exist=True)
    current_hash = hashlib.sha256(current_path.read_bytes()).hexdigest()
    default_hash = hashlib.sha256(Path(default_path).read_bytes()).hexdigest()
    if current_hash not in {version['manifest_sha256'], default_hash}:
        return None
    if current_hash == version['manifest_sha256']:
        current_correction = project.get('correction')
        current_correction_hash = None
        if current_correction:
            correction_path = resolve_relative(home, current_correction, must_exist=True)
            correction_raw = correction_path.read_bytes()
            if hashlib.sha256(correction_raw).hexdigest() != correction_path.stem:
                return None
            current_correction_hash = digest(json.loads(correction_raw))
        if current_correction_hash != selected['correction_sha256']:
            return None
    manifest_dir = home / 'input/manifests'
    if manifest_dir.is_symlink() or not manifest_dir.is_dir():
        return None
    matches = []
    for candidate in manifest_dir.glob('manifest-*.json'):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        raw = candidate.read_bytes()
        if hashlib.sha256(raw).hexdigest() != version['manifest_sha256']:
            continue
        value = json.loads(raw)
        if (value.get('manifest_id') == version['manifest_id'] and
                value.get('revision', value.get('manifest_revision', 1)) == version['manifest_revision']):
            matches.append(candidate)
    if len(matches) != 1:
        return None
    correction_sha = selected['correction_sha256']
    correction_relative = None
    if correction_sha is not None:
        correction_dir = home / 'input/corrections'
        if correction_dir.is_symlink() or not correction_dir.is_dir():
            return None
        corrections = []
        for candidate in correction_dir.glob('*.json'):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            raw = candidate.read_bytes()
            value = json.loads(raw)
            if (hashlib.sha256(raw).hexdigest() == candidate.stem and
                    digest(value) == correction_sha):
                corrections.append(candidate)
        if len(corrections) != 1:
            return None
        correction_relative = corrections[0].relative_to(home).as_posix()
    state = dict(project['project_state'])
    state['version'] = version
    recovered = dict(project)
    recovered['manifest'] = matches[0].relative_to(home).as_posix()
    recovered['correction'] = correction_relative
    recovered['project_state'] = validate_state(state)
    return recovered


def retained_default_selection(root, manifest, correction=None):
    """Return integrity-checked prior inputs when the caller supplied app defaults.

    A user-selected file keeps the ordinary exact-match requirement. The app's
    bundled defaults can change between launcher versions, so use the prior
    project inputs only when their retained paths and version hash still agree.
    """
    try:
        if not _bundled_default(manifest):
            return None
        root = Path(root).resolve(strict=True)
        home = root / 'MI1 Generator'
        if home.is_symlink():
            return None
        project = json.loads((home / '.launcher/project.json').read_text())
        state = validate_state(project['project_state'])
        if state['kind'] != 'project' or Path(project['root']).resolve(strict=True) != root:
            return None
        retained_manifest = resolve_relative(home, project['manifest'], must_exist=True)
        raw = retained_manifest.read_bytes()
        version = state['version']
        if hashlib.sha256(raw).hexdigest() != version['manifest_sha256']:
            return None
        manifest_value = json.loads(raw)
        if (manifest_value.get('manifest_id') != version['manifest_id'] or
                manifest_value.get('revision', manifest_value.get('manifest_revision', 1)) != version['manifest_revision']):
            return None
        previous_correction = project.get('correction')
        if correction is not None:
            selected = Path(correction).resolve(strict=True)
            # A different correction is an explicit user choice and must be honored.
            if not (selected.name == 'correction.json' and selected.parent.name == 'defaults' and
                    selected.parent.parent.name == 'Resources' and
                    selected.parent.parent.parent.name == 'Contents' and
                    selected.parent.parent.parent.parent.suffix == '.app'):
                return None
            if previous_correction is None:
                return None
        if previous_correction is not None:
            retained_correction = resolve_relative(home, previous_correction, must_exist=True)
            corr_hash = hashlib.sha256(retained_correction.read_bytes()).hexdigest()
            if retained_correction.stem != corr_hash:
                return None
        # Recovery for a workdir that was already opened with a newer app:
        # project.json now describes the new bundled manifest, while the
        # interrupted render-status still carries the prior immutable version.
        default_bytes = Path(manifest).read_bytes()
        current_hash = hashlib.sha256(default_bytes).hexdigest()
        if hashlib.sha256(raw).hexdigest() == current_hash:
            try:
                try:
                    saved = json.loads((home / '.launcher/render-status.json').read_text())
                except (OSError, ValueError, TypeError):
                    saved = {}
                current_incomplete = (saved.get('version') == version and
                    saved.get('status', {}).get('status') in {
                        'running', 'pausing', 'paused', 'failed', 'completed_with_errors'})
                old_version = saved.get('version')
                if (not current_incomplete and isinstance(old_version, dict) and
                        old_version != version and old_version.get('manifest_sha256') != current_hash):
                    recovered_state = dict(state)
                    recovered_state['version'] = old_version
                    recovered_state = validate_state(recovered_state)
                    matches = []
                    for candidate in (home / 'input/manifests').glob('manifest-*.json'):
                        if candidate.is_symlink() or not candidate.is_file():
                            continue
                        candidate_raw = candidate.read_bytes()
                        if hashlib.sha256(candidate_raw).hexdigest() != old_version['manifest_sha256']:
                            continue
                        candidate_value = json.loads(candidate_raw)
                        if (candidate_value.get('manifest_id') == old_version['manifest_id'] and
                                candidate_value.get('revision', candidate_value.get('manifest_revision', 1)) == old_version['manifest_revision']):
                            matches.append(candidate)
                    if len(matches) == 1:
                        # Without a correction pointer, only an empty retained
                        # correction set makes the old selection unambiguous.
                        correction_files = list((home / 'input/corrections').glob('*.json'))
                        signed_old = _authenticated_review(root, home, version=old_version,
                                                           project_id=state['project_id'])
                        old_job_incomplete = (saved.get('status', {}).get('status') in
                            {'running', 'pausing', 'paused', 'failed', 'completed_with_errors'})
                        if not correction_files and not (signed_old is not None and old_job_incomplete):
                            recovered = dict(project)
                            recovered['manifest'] = matches[0].relative_to(home).as_posix()
                            recovered['correction'] = None
                            recovered['project_state'] = recovered_state
                            return recovered
            except (OSError, ValueError, KeyError, TypeError):
                pass
        # Also recover when the current manifest bytes still match the signed
        # review's source but project version_id drifted during app startup.
        try:
            recovered = _recover_signed_review_selection(root, home, project, manifest)
            if recovered is not None:
                return recovered
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return project
    except (OSError, ValueError, KeyError, TypeError):
        return None


def resume_check(root, manifest, correction=None):
    try:
        root=Path(root).resolve(strict=True);home=root/'MI1 Generator'
        if home.is_symlink():raise ValueError('Arbeitsordner ist ein symbolischer Link')
        project=json.loads((home/'.launcher/project.json').read_text())
        state=validate_state(project['project_state'])
        if Path(project['root']).resolve(strict=True)!=root:raise ValueError('Anderes Arbeitslaufwerk')
        retained_default = retained_default_selection(root, manifest, correction)
        if retained_default is not None:
            project = retained_default
            state = validate_state(project['project_state'])
            manifest = resolve_relative(home, retained_default['manifest'], must_exist=True)
            correction = (resolve_relative(home, retained_default['correction'], must_exist=True)
                          if retained_default.get('correction') else None)
        selected=Path(manifest).read_bytes()
        retained=resolve_relative(home,project['manifest'],must_exist=True).read_bytes()
        if selected!=retained:raise ValueError('Anderes Manifest')
        expected=project.get('correction')
        if bool(correction)!=bool(expected):raise ValueError('Anderes Rezept gewählt')
        if correction and Path(correction).read_bytes()!=resolve_relative(home,expected,must_exist=True).read_bytes():
            raise ValueError('Anderes Rezept')
        try:
            saved=json.loads((home/'.launcher/render-status.json').read_text())
        except (OSError,ValueError,TypeError):
            saved={}
        if saved.get('version')==state['version']:
            status=saved.get('status',{})
            if status.get('status') not in {'running','pausing','paused','failed','completed_with_errors','completed'}:
                status=None
        else:
            status=None
        if status is None:
            status=completed_review_status(home,project)
        if status is None:raise ValueError('Kein vorhandener Renderauftrag')
        installation=load_existing_installation(home)
        verify_installed_runtime(home,installation=installation)
        from .local_ane import resolve_local_ane
        resolve_local_ane(home,read_only=True)
        return dict(resume_available=True,project=project,render_status=status,
                    manifest_sha256=hashlib.sha256(selected).hexdigest())
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        return dict(resume_available=False,reason=str(exc))
