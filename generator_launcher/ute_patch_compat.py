"""Exact, measured native UTE resource correction for the original German patch.

Only 40 sound-sample bytes; no game resources, dialogue or decoder included.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
from .contracts import ContractError

POLICY = "ute-us-de-resource-compat/1"
INDEX_SHA = "34827cd5664361650c3081dc143e75167d31d80c462d22f2b8c35d45f5c8f74f"
DE_INDEX_SHA = "99bfc02317b68d538d48b227054a0f172aaf29ab55b8650d4ea1189cb048fd62"
NATIVE_SHA = "303d8d1ac1afef7a8b4bdfb6380e2da092d309335e6dcfb1ed0254145eb456f0"
ORIGINAL_SHA = "2954d067d56c1190456a911cd7bcc507ad6560365c1a24936115e3d9faa9c62f"
CHANGES = ((990688, 233, 22), (1379985, 253, 250), (1416101, 233, 22), (1416103, 233, 22), (1416127, 233, 22), (1416133, 233, 22), (1416229, 233, 232), (1416246, 233, 232), (1416260, 233, 232), (1416264, 233, 22), (1416275, 233, 232), (1416290, 233, 232), (1416295, 233, 232), (1416300, 233, 22), (1416308, 233, 232), (1416318, 233, 232), (1416322, 232, 233), (1416338, 233, 232), (1416344, 233, 22), (1416353, 233, 232), (1416359, 233, 22), (1416361, 233, 22), (2825144, 0, 3), (2839630, 207, 204), (3478831, 170, 173), (3479735, 47, 46), (3502482, 23, 20), (3529638, 23, 20), (3529642, 20, 21), (3529643, 21, 20), (4214944, 247, 244), (4218542, 217, 216), (4220581, 187, 186), (4243805, 232, 233), (4261289, 29, 28), (4278207, 12, 13), (4285285, 239, 236), (5637340, 21, 20), (6109077, 217, 198), (6111742, 248, 249))

def sha(data):
    return hashlib.sha256(data).hexdigest()


def _file(directory, name):
    matches = [p for p in directory.iterdir() if p.name.lower() == name]
    if len(matches) != 1 or matches[0].is_symlink() or not matches[0].is_file():
        raise ContractError("Patchbasis fehlt/ist mehrdeutig oder verlinkt: " + name)
    return matches[0]


def _write(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name+'.compat-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


def prepare_patch_copy(english, german, receipt, english_stage, evidence):
    """Normalize only the generated German input; preserve the signed English source.

    Recognized partial old patch attempts may be retried explicitly without
    rebuilding the English audio. Other resources/receipts remain guarded.
    """
    english, german = Path(english), Path(german)
    receipt, evidence = Path(receipt), Path(evidence)
    if english.is_symlink() or german.is_symlink() or receipt.is_symlink() or evidence.is_symlink():
        raise ContractError("Patchkompatibilität verlangt unverlinkte Arbeitsdateien")
    index = _file(english, 'monkey.000').read_bytes()
    source = _file(english, 'monkey.001').read_bytes()
    source_sha = sha(source)
    if sha(index) != INDEX_SHA or source_sha not in (NATIVE_SHA, ORIGINAL_SHA):
        raise ContractError("Native US→DE-Patchbasis ist noch nicht freigegeben: " + source_sha +
                            ". Keine Ressourcendatei verändert; MONKEY.001 und englischen Beleg zur Prüfung sichern.")
    target = _file(german, 'monkey.001')
    target_index = _file(german, 'monkey.000')
    target_sha = sha(target.read_bytes())
    # A successfully patched copy is handled by its existing signed stage.
    if target_sha not in (NATIVE_SHA, ORIGINAL_SHA):
        prior = json.loads(receipt.read_text()) if receipt.is_file() else {}
        if (prior.get('status') != 'completed'
                or prior.get('identity', {}).get('english_receipt') != english_stage
                or prior.get('identity', {}).get('stage') != 'ute_german'):
            raise ContractError("Unbekannte deutsche Arbeitskopie; keine automatische Korrektur")
        if evidence.is_file():
            report = json.loads(evidence.read_text())
            if report.get('policy') != POLICY or report.get('english_stage') != english_stage:
                raise ContractError("Patchkompatibilitätsbeleg passt nicht zur englischen Vorbereitung")
            return report
        return None
    if sha(target_index.read_bytes()) not in (INDEX_SHA, DE_INDEX_SHA):
        raise ContractError("Unbekannte MONKEY.000 in deutscher Arbeitskopie; keine automatische Korrektur")
    previous = None
    if receipt.exists():
        previous = json.loads(receipt.read_text())
        identity = previous.get('identity', {})
        if identity.get('stage') != 'ute_german' or identity.get('english_receipt') != english_stage:
            raise ContractError("Vorheriger deutscher Patchversuch gehört nicht zur englischen Vorbereitung")
    corrected = bytearray(source)
    if source_sha == NATIVE_SHA:
        for offset, expected, replacement in CHANGES:
            if corrected[offset] != expected:
                raise ContractError("Native Soundkorrektur passt nicht zur Patchbasis")
            corrected[offset] = replacement
    if sha(corrected) != ORIGINAL_SHA:
        raise ContractError("US→DE-Patchbasis verfehlt den freigegebenen Originalhash")
    report = dict(policy=POLICY, english_stage=english_stage, source_sha256=source_sha,
                  patch_input_sha256=ORIGINAL_SHA, corrected_sample_bytes=40 if source_sha==NATIVE_SHA else 0)
    # All guards and target bytes are checked before the first mutation.
    if previous is not None:
        receipt.replace(receipt.with_name(receipt.name+'.before-compat-'+uuid.uuid4().hex))
    _write(target, corrected)
    _write(target_index, index)
    _write(evidence, (json.dumps(report, sort_keys=True, indent=2)+'\n').encode())
    return report
