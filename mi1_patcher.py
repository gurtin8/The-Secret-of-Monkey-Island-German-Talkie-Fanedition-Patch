"""Asset-free UTE 1.02 patch writer. Encoding happens only on explicit build.

The index retains original script speech offsets; scripts and their VCTL lengths
are never rewritten. No original input, review selection or history is modified.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import uuid
import wave

TOTAL_DIALOGUES = 4393
UINT32_MAX = 0xffffffff
BASIS_HASHES = {
    'monkey.000': '99bfc02317b68d538d48b227054a0f172aaf29ab55b8650d4ea1189cb048fd62',
    'monkey.001': 'b749252e01005643a3bd7184a54c9a455fe4dc07f8dd36b28fd369ce0ea3ade6',
    'monster.sou': '7990dd22199c76aa75eda2952b7e6c9cec3048451e8babd7766f19eabeff2b47',
}
GERMAN_SOF_SHA256 = 'e9941aac5a5357c56846b983c266457127b399242dd793999ca57f74745bc52e'


def _unique_case_path(root, name):
    matches=[p for p in Path(root).iterdir() if p.name.lower()==name.lower()]
    if len(matches)!=1 or matches[0].is_symlink() or not matches[0].is_file():
        raise ValueError('Spielbasisdatei fehlt oder ist mehrdeutig: '+name)
    return matches[0]


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def json_write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def speech_id(dialogue):
    match = re.fullmatch(r'mi1-ute-1\.02:vctl:(\d+)', dialogue.get('dialog_id', ''))
    if not match:
        raise ValueError('Dialog-ID gehört nicht zur UTE 1.02: ' + str(dialogue.get('dialog_id')))
    value = dialogue.get('native_speech_id')
    if value is None:
        value = match.group(1)
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
        raise ValueError('Ungültige native Speech-ID')
    value = int(value)
    if value != int(match.group(1)):
        raise ValueError('Native Speech-ID widerspricht der stabilen Dialog-ID')
    if not 0 <= value <= UINT32_MAX:
        raise ValueError('Speech-ID liegt außerhalb uint32')
    return value


def verify_basis(game_root, *, signed_archive=None):
    """Verify exact UTE game assets and one supported, provenance-bound speech archive."""
    root = Path(game_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Spielbasis ist kein Verzeichnis')
    result = {}
    for name in ('monkey.000','monkey.001'):
        path=_unique_case_path(root,name).resolve(strict=True);expected=BASIS_HASHES[name]
        if sha256_file(path)!=expected:raise ValueError('Spielbasis stimmt nicht mit der deutschen UTE 1.02 überein: '+name)
        result[name]=dict(path=str(path),sha256=expected)
    archives=[]
    for name in ('monster.sou','monkey.sof'):
        matches=[p for p in root.iterdir() if p.name.lower()==name]
        if len(matches)>1:raise ValueError('Mehrdeutiges Spracharchiv: '+name)
        if matches:
            path=matches[0].resolve(strict=True)
            if path.is_symlink() or not path.is_file():raise ValueError('Spracharchiv ist keine reguläre Datei: '+name)
            archives.append((name,path))
    if len(archives)!=1:raise ValueError('Genau ein deutsches monster.sou oder monkey.sof wird benötigt')
    name,path=archives[0]
    expected=BASIS_HASHES.get(name)
    if name=='monkey.sof':
        if not signed_archive or Path(signed_archive.get('root','')).resolve()!=root or signed_archive.get('archive_name','').lower()!=name:
            raise ValueError('Deutsches FLAC-SOF benötigt den passenden lokalen Vorbereitungsbeleg')
        if Path(signed_archive.get('archive_path','')).resolve()!=path:
            raise ValueError('Signierter FLAC-SOF-Pfad stimmt nicht mit der Spielbasis überein')
        expected=signed_archive.get('archive_sha256')
        if not isinstance(expected,str) or not re.fullmatch(r'[0-9a-f]{64}',expected):
            raise ValueError('Signierter FLAC-SOF-Hash ist ungültig')
    actual=sha256_file(path)
    if actual!=expected:raise ValueError('Spracharchiv stimmt nicht mit dem belegten UTE-1.02-Original überein: '+name)
    if name=='monkey.sof':
        from types import SimpleNamespace
        from generator_launcher.original_audio import scan_sof
        scan_sof(path,lambda sid,*_args:SimpleNamespace(speech_id=sid))
    result[name]=dict(path=str(path),sha256=expected,format='raw_sou' if name=='monster.sou' else 'flac_sof')
    return result


def discover_basis(candidates):
    for root in dict.fromkeys(str(Path(p)) for p in candidates):
        if all((Path(root) / name).is_file() for name in BASIS_HASHES):
            try:
                verify_basis(root)
                return str(Path(root).resolve())
            except (OSError, ValueError):
                pass
    return None


def copy_verified(source, target, expected, *, allow_symlink=False):
    source = Path(source)
    if (source.is_symlink() and not allow_symlink) or not source.is_file():
        raise ValueError('Quelldatei fehlt oder ist kein erlaubter regulärer Beleg: ' + str(source))
    if not re.fullmatch(r'[0-9a-f]{64}', expected or ''):
        raise ValueError('SHA-256-Beleg fehlt')
    target.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    with source.open('rb') as inp, target.open('xb') as out:
        for block in iter(lambda: inp.read(1024 * 1024), b''):
            h.update(block)
            out.write(block)
    if h.hexdigest() != expected:
        raise ValueError('Quellbytes stimmen nicht mit dem eingefrorenen SHA-256 überein: ' + str(source))


def flac_info(path):
    """Read mandatory STREAMINFO; no decoder/model/ffprobe invocation."""
    with Path(path).open('rb') as f:
        if f.read(4) != b'fLaC':
            raise ValueError('FFmpeg lieferte keinen eigenständigen FLAC-Stream')
        header = f.read(4)
        if len(header) != 4 or header[0] & 0x7f != 0 or int.from_bytes(header[1:], 'big') != 34:
            raise ValueError('FLAC-STREAMINFO fehlt oder ist ungültig')
        info = f.read(34)
        if len(info) != 34:
            raise ValueError('FLAC-STREAMINFO ist abgeschnitten')
        packed = int.from_bytes(info[10:18], 'big')
        rate, channels, bits, samples = packed >> 44, ((packed >> 41) & 7) + 1, ((packed >> 36) & 31) + 1, packed & ((1 << 36) - 1)
        if not rate or not samples or channels != 1 or bits != 16:
            raise ValueError('FLAC muss nichtleeres Mono-PCM16 enthalten')
        return dict(sample_rate_hz=rate, channels=channels, sample_width_bits=bits,
                    samples=samples, duration_ms=round(samples * 1000 / rate, 3))


def _read_sou_sync(stream, speech_id_value, dialog_id):
    stream.seek(speech_id_value)
    if stream.read(8)!=b'VCTL\x00\x00\x00\x0a':
        raise ValueError('Original-VCTL-Header/-Länge stimmt nicht: '+dialog_id)
    sync=stream.read(2)
    if len(sync)!=2:raise ValueError('Original-VCTL-Syncdaten sind abgeschnitten: '+dialog_id)
    return sync


def export_root(path, basis_root=None):
    """Resolve a destination without accepting symlink aliases or source overlap."""
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise ValueError('Exportziel als Pfad erforderlich')
    candidate = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (candidate, *candidate.parents)):
        raise ValueError('Exportziel darf keine symbolischen Links enthalten')
    root = candidate.resolve()
    if root.exists() and not root.is_dir():
        raise ValueError('Exportziel ist kein Verzeichnis')
    if basis_root:
        basis = Path(basis_root).expanduser().resolve(strict=True)
        if root == basis or root.is_relative_to(basis) or basis.is_relative_to(root):
            raise ValueError('Buildausgabe und Spielbasis müssen getrennt liegen')
    return root


def next_export_name(root, created_at=None):
    stamp = created_at or datetime.now().astimezone().isoformat(timespec='seconds')
    name = 'Monkey Island Deutsch - ' + datetime.fromisoformat(stamp).strftime('%Y-%m-%d_%H-%M')
    candidate = name
    suffix = 2
    while (Path(root) / candidate).exists() or (Path(root) / candidate).is_symlink():
        candidate = f'{name} ({suffix})'
        suffix += 1
    return candidate


def verified_export(path, *, build_id, report_sha256):
    """Authenticate an app-catalogued completed output before destructive replacement."""
    path = Path(path)
    export_root(path)
    report_path = path / 'build-report.json'
    if not path.is_dir() or report_path.is_symlink() or not report_path.is_file():
        raise ValueError('Spielausgabe ist nicht mehr eindeutig belegt')
    if sha256_file(report_path) != report_sha256:
        raise ValueError('Spielausgabe wurde seit dem Export verändert')
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if (report.get('format') != 'mi1-patch-build-report/1' or report.get('build_id') != build_id or
            report.get('status') != 'completed' or report.get('playable') is not True or
            report.get('output_dir') != str(path.resolve())):
        raise ValueError('Spielausgabe besitzt keinen passenden abgeschlossenen Buildbeleg')
    if any(item.is_symlink() for item in path.rglob('*')):
        raise ValueError('Spielausgabe enthält symbolische Links')
    return report


def build_patch(snapshot, game_root, output_root, ffmpeg, progress=None, build_id=None, *, scummrp=None, signed_archive=None, output_name=None, replace_output=None, replace_identity=None, on_publish=None):
    """Build one isolated, atomically published playable directory or failure report.

    Internal writer: the caller must deeply verify evidence before calling.
    snapshot['dialogues'] is the complete accepted selection, including defaults.
    Each row has input_path/input_sha256, speech_id, processing and its evidence
    binding. QA findings are recorded and deliberately never used as build gates.
    """
    progress = progress or (lambda phase, completed, total: None)
    build_id = build_id or uuid.uuid4().hex
    if not re.fullmatch(r'[a-f0-9]{32}', build_id):
        raise ValueError('Ungültige Build-ID')
    basis_root = Path(game_root).expanduser().resolve(strict=True)
    root = export_root(output_root, basis_root)
    created_at = datetime.now().astimezone().isoformat(timespec='seconds')
    root.mkdir(parents=True, exist_ok=True)
    staging = root / ('.building-' + build_id)
    if replace_output is not None:
        final = Path(replace_output).expanduser().absolute()
        if final.parent != root or not replace_identity:
            raise ValueError('Ersetzen erfordert eine belegte Spielausgabe direkt im Exportziel')
        verified_export(final, **replace_identity)
    else:
        name = output_name or next_export_name(root, created_at)
        if not isinstance(name, str) or name in ('', '.', '..') or Path(name).name != name:
            raise ValueError('Ungültiger Spielordnername')
        final = root / name
        if final.exists() or final.is_symlink():
            raise ValueError('Buildziel existiert bereits')
    backup = root / ('.replaced-' + build_id)
    if backup.exists() or backup.is_symlink():
        raise ValueError('Ersetzungsbackup existiert bereits')
    staging.mkdir()
    report = dict(format='mi1-patch-build-report/1', build_id=build_id, created_at=created_at, status='running', playable=False, game_runtime_tested=False,
                  review_revision=snapshot['review_revision'], snapshot_sha256=hashlib.sha256(
                      json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                  development_only=snapshot.get('development_only') is True, game_preparation=snapshot.get('game_preparation', {}),
                  game_root=str(basis_root), target='ScummVM / German Ultimate Talkie Edition 1.02',
                  archive=dict(name='monkey.sof', format='BE uint32 index bytes; sorted BE uint32[4] entries; raw sync tags + standalone FLAC',
                               payload_offsets='relative to payload area after 4 + index bytes',
                               scripts='original speech references and VCTL lengths retained; standard intro credits patched'),
                  audio_policy='FLAC mono PCM16; syntheses retain 48000 Hz; originals retain decoded sample rate',
                  qa_policy='QA warn/fail/not_run are findings, not build gates; invalid/missing evidence blocks',
                  basis={}, dialogues=[], files=[], error=None)
    try:
        rows = snapshot['dialogues']
        ids = [r['dialog_id'] for r in rows]
        offsets = [r['speech_id'] for r in rows]
        if len(rows) != TOTAL_DIALOGUES or len(set(ids)) != len(rows) or len(set(offsets)) != len(rows):
            raise ValueError('Build benötigt genau 4.393 eindeutige Dialoge und Speech-IDs')
        if any(type(x) is not int or not 0 <= x <= UINT32_MAX for x in offsets):
            raise ValueError('Ungültige Speech-ID im Snapshot')
        ordered = sorted(rows, key=lambda r: r['speech_id'])
        progress('validating_basis', 0, len(rows))
        basis = verify_basis(basis_root,signed_archive=signed_archive)
        report['basis'] = basis
        work = staging / '.inputs'
        work.mkdir()
        archive_name=next(name for name in ('monster.sou','monkey.sof') if name in basis)
        archive_path=Path(basis[archive_name]['path'])
        for name in ('monkey.000','monkey.001'):
            evidence=basis[name]
            copy_verified(evidence['path'],staging/name,evidence['sha256'],allow_symlink=True)
        if archive_name=='monster.sou':
            copy_verified(basis[archive_name]['path'],work/'monster.sou',basis[archive_name]['sha256'],allow_symlink=True)
        # Staging every selected file before encoding guarantees an immutable
        # build even when the live review continues concurrently.
        prepared = []
        if archive_name=='monster.sou':
            source_archive=(work/'monster.sou').open('rb')
            sync_for=lambda row,sou: _read_sou_sync(sou,row['speech_id'],row['dialog_id'])
        else:
            from types import SimpleNamespace
            from generator_launcher.original_audio import scan_sof
            sof_samples=scan_sof(archive_path,lambda sid,start,size,audio,flac_size,vctl_size:
                SimpleNamespace(speech_id=sid,archive_offset=start,voc_offset=audio))
            sof_index={sample.speech_id:sample for sample in sof_samples}
            source_archive=archive_path.open('rb')
            def sync_for(row,_unused):
                sample=sof_index.get(row['speech_id'])
                if sample is None:raise ValueError('Speech-ID fehlt im deutschen FLAC-SOF: '+row['dialog_id'])
                size=sample.voc_offset-sample.archive_offset
                if size!=2:raise ValueError('FLAC-SOF-Syncdaten müssen genau zwei Bytes enthalten: '+row['dialog_id'])
                source_archive.seek(sample.archive_offset);sync=source_archive.read(size)
                if len(sync)!=2:raise ValueError('FLAC-SOF-Syncdaten sind abgeschnitten: '+row['dialog_id'])
                return sync
        with source_archive as sou:
            for n, row in enumerate(ordered):
                progress('staging_audio', n, len(rows))
                source = Path(row['input_path'])
                if root == source or root in source.parents:
                    raise ValueError('Audioeingabe darf nicht im Buildausgabeverzeichnis liegen')
                suffix = '.wav' if row['processing'] == 'render' else source.suffix.lower()
                if suffix not in ('.wav', '.ogg', '.flac', '.voc', '.mp3'):
                    raise ValueError('Nicht unterstütztes Audioformat im Snapshot')
                audio = work / (str(row['speech_id']) + suffix)
                copy_verified(source, audio, row['input_sha256'])
                if row['processing'] == 'render':
                    with wave.open(str(audio), 'rb') as wav:
                        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 48000, 'NONE') or wav.getnframes() < 1:
                            raise ValueError('Synthese muss nichtleeres 48-kHz-Mono-PCM16-WAV sein')
                elif row['processing'] != 'copy_original':
                    raise ValueError('Unbekannte Ausgabeverarbeitung')
                if archive_name=='monster.sou':sync=sync_for(row,sou)
                else:sync=sync_for(row,None)
                prepared.append((row, audio, sync))
        if archive_name=='monster.sou':(work / 'monster.sou').unlink()
        index_bytes = len(rows) * 16
        mapping = []
        archive = staging / 'monkey.sof'
        with archive.open('xb+') as sou:
            sou.write(struct.pack('>I', index_bytes))
            sou.write(b'\0' * index_bytes)
            payload_start = sou.tell()
            for n, (row, audio, sync) in enumerate(prepared):
                progress('encoding', n, len(rows))
                encoded = work / (str(row['speech_id']) + '.encoded.flac')
                result = subprocess.run([str(ffmpeg), '-nostdin', '-hide_banner', '-loglevel', 'error',
                    '-i', str(audio), '-map', '0:a:0', '-vn', '-ac', '1', '-sample_fmt', 's16',
                    '-c:a', 'flac', '-f', 'flac', str(encoded)], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
                if result.returncode:
                    raise RuntimeError('FLAC-Kodierung fehlgeschlagen für ' + row['dialog_id'] + ': ' + result.stderr.decode('utf-8', 'replace')[-2000:])
                info = flac_info(encoded)
                if row['processing'] == 'render' and info['sample_rate_hz'] != 48000:
                    raise ValueError('Synthese-Samplerate wurde beim Kodieren verändert')
                length = encoded.stat().st_size
                offset = sou.tell() - payload_start
                if max(length, offset, payload_start + offset + len(sync) + length) > UINT32_MAX:
                    raise ValueError('SOF-Payload überschreitet uint32')
                mapping.append((row['speech_id'], offset, len(sync), length))
                sou.write(sync)
                with encoded.open('rb') as stream:
                    shutil.copyfileobj(stream, sou, 1024 * 1024)
                report['dialogues'].append({**row, 'archive_entry':dict(original_offset=row['speech_id'],
                    payload_relative_offset=offset, sync_tag_byte_length=len(sync), flac_bytes=length),
                    'flac_sha256':sha256_file(encoded), 'encoded_audio':info})
                encoded.unlink()
                audio.unlink()
            sou.seek(4)
            for values in mapping:
                sou.write(struct.pack('>IIII', *values))
            sou.flush()
            os.fsync(sou.fileno())
        # Only own CD music tracks are optional. Flatten safe numbered filenames;
        # no executables, old speech archives, arbitrary directories or symlinks.
        seen_tracks = set()
        for directory in (basis_root, basis_root / 'audio', basis_root / 'music'):
            if not directory.is_dir() or directory.is_symlink():
                continue
            for music in sorted(directory.iterdir()):
                if not re.fullmatch(r'track\d{1,3}\.(?:flac|ogg|mp3|wav)', music.name, re.I) or music.is_symlink() or not music.is_file():
                    continue
                name = music.name.lower()
                if name in seen_tracks:
                    raise ValueError('Mehrdeutiger Musiktrack: ' + name)
                seen_tracks.add(name)
                h = sha256_file(music)
                copy_verified(music, staging / name, h)
                report.setdefault('music', []).append(dict(source=str(music), name=name, sha256=h))
        # Preserve the finished UTE soundtracks separately: their track names
        # overlap and must never be flattened into one directory.
        for codec in ('flac', 'ogg'):
            for prefix, numbers in (('cd', set(range(1, 25)) - {10, 20}),
                                    ('se', set(range(1, 30)))):
                dirname = prefix + '_music_' + codec
                directory = basis_root / dirname
                if not directory.exists() and not directory.is_symlink():
                    continue
                if directory.is_symlink() or not directory.is_dir():
                    raise ValueError('Ungültiger Musikordner: ' + dirname)
                expected = {f'track{number}.{codec}' for number in numbers}
                if prefix == 'se':
                    expected.add('track8_no_sfx.' + codec)
                candidates = [path for path in directory.iterdir() if path.suffix.lower() == '.' + codec]
                tracks = {path.name.lower(): path for path in candidates}
                if set(tracks) != expected or len(candidates) != len(expected):
                    raise ValueError('Unvollständiger oder mehrdeutiger Musikbestand: ' + dirname)
                destination = staging / dirname
                destination.mkdir()
                for name in sorted(expected):
                    music = tracks[name]
                    if music.is_symlink() or not music.is_file() or not music.stat().st_size:
                        raise ValueError('Ungültiger Musiktrack: ' + dirname + '/' + name)
                    h = sha256_file(music)
                    copy_verified(music, destination / name, h)
                    report.setdefault('music', []).append(dict(source=str(music),
                        name=dirname + '/' + name, sha256=h))
        if any((staging / (prefix + '_music_' + codec)).is_dir()
               for prefix in ('cd', 'se') for codec in ('flac', 'ogg')):
            (staging / 'MUSIK.txt').write_text(
                'Die fertigen Original-Musikdateien liegen in cd_music_flac bzw. se_music_flac '
                '(bei OGG entsprechend cd_music_ogg bzw. se_music_ogg).\n'
                'In ScummVM: Spiel bearbeiten > Pfade > Extra-Pfad auf den gewünschten '
                'vorhandenen Musikordner setzen. CD und SE sind alternative Musikfassungen.\n'
                'Keine erneute Konvertierung erforderlich.\n', encoding='utf-8')
        shutil.rmtree(work)
        json_write(staging / 'selected-snapshot.json', snapshot)
        for path in sorted(staging.rglob('*')):
            if path.is_file():
                report['files'].append(dict(name=path.relative_to(staging).as_posix(),
                    size_bytes=path.stat().st_size, sha256=sha256_file(path)))
        report.update(output_dir=str(final), report_path=str(final / 'build-report.json'))
        json_write(staging / 'build-report.json', report)
        checksums = [{**item} for item in report['files']]
        checksums.append(dict(name='build-report.json', size_bytes=(staging / 'build-report.json').stat().st_size,
                              sha256=sha256_file(staging / 'build-report.json')))
        json_write(staging / 'checksums.json', dict(format='mi1-patch-checksums/1', files=checksums))
        # The standard intro is part of every build. Any patch/resource/hash
        # failure reaches the full-build failure handler before publication.
        from tools.patch_mi1_intro import patch_build, resolve_scummrp
        progress('patching_intro', len(rows), len(rows))
        patch_build(staging, Path(scummrp) if scummrp else resolve_scummrp(),
                    published_root=final, announce=False)
        report = json.loads((staging / 'build-report.json').read_text(encoding='utf-8'))
        intro_report = staging / 'intro-patch-report.json'
        report['files'].append(dict(name=intro_report.name, size_bytes=intro_report.stat().st_size,
                                    sha256=sha256_file(intro_report)))
        report.update(status='completed', playable=True)
        json_write(staging / 'build-report.json', report)
        checksums = json.loads((staging / 'checksums.json').read_text(encoding='utf-8'))
        for entry in checksums['files']:
            if entry['name'] == 'build-report.json':
                entry.update(size_bytes=(staging / entry['name']).stat().st_size,
                             sha256=sha256_file(staging / entry['name']))
        json_write(staging / 'checksums.json', checksums)
        progress('publishing', len(rows), len(rows))
        if replace_output is not None:
            verified_export(final, **replace_identity)
            os.rename(final, backup)
            published = False
            try:
                # Reserving the now-free directory makes accidental collisions
                # fail before rename can overwrite an unrelated empty directory.
                final.mkdir()
                try:
                    os.rename(staging, final)
                except Exception:
                    final.rmdir()
                    raise
                published = True
                if on_publish:
                    on_publish(report)
            except Exception:
                if published:
                    shutil.rmtree(final)
                if not final.exists() and not final.is_symlink():
                    os.rename(backup, final)
                raise
            # Publication succeeded; old output is never removed before this point.
            try:
                shutil.rmtree(backup)
            except OSError:
                report['replacement_backup'] = str(backup)
        else:
            final.mkdir()
            try:
                os.rename(staging, final)
                if on_publish:
                    on_publish(report)
            except Exception:
                if staging.exists():final.rmdir()
                else:shutil.rmtree(final)
                raise
        return report
    except Exception as exc:
        report.update(status='failed', playable=False, error=str(exc), output_dir=None,
                      report_path=str(root / (build_id + '.failed-report.json')))
        json_write(report['report_path'], report)
        if staging.exists():shutil.rmtree(staging)
        raise


def main(argv=None):
    # The CLI always resolves review selections and verifies evidence, rather
    # than accepting a caller-authored snapshot as a substitute for receipts.
    from patch_review.build import main as review_main
    return review_main(argv)


if __name__ == '__main__':
    main()
