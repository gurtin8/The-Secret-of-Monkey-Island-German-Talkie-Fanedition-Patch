"""Native writer for the UTE wav2sbl helper's PCM8/22050 SBL format."""
from __future__ import annotations

import os
from pathlib import Path
import struct
import tempfile

from .contracts import ContractError


def wav_to_sbl(source: Path, target: Path) -> dict:
    """Write the original AUhd/AUdt/VOC framing without touching the WAV."""
    source, target = Path(source), Path(target)
    if source.is_symlink() or not source.is_file() or target.is_symlink():
        raise ContractError('SBL-Konvertierung verlangt reguläre Arbeitsdateien')
    fmt = data = None
    with source.open('rb') as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] != b'RIFF' or header[8:] != b'WAVE':
            raise ContractError('SBL-Eingabe muss RIFF/WAVE sein')
        end = struct.unpack('<I', header[4:8])[0] + 8
        if end > source.stat().st_size:
            raise ContractError('SBL-Eingabe ist abgeschnitten')
        while stream.tell() + 8 <= end:
            kind, size = struct.unpack('<4sI', stream.read(8))
            offset = stream.tell()
            if offset + size > end:
                raise ContractError('SBL-WAV-Chunk ist abgeschnitten')
            if kind == b'fmt ':
                if fmt is not None or size < 16:
                    raise ContractError('Ungültiger SBL-WAV-Formatblock')
                fmt = struct.unpack('<HHIIHH', stream.read(16))
            elif kind == b'data':
                if data is not None:
                    raise ContractError('Mehrere SBL-WAV-Datenblöcke')
                data = (offset, size)
            stream.seek(offset + size + (size & 1))
        if fmt != (1, 1, 22050, 22050, 1, 8) or data is None or not data[1]:
            raise ContractError('UTE-SBL benötigt Mono-PCM mit 8 Bit und 22050 Hz')
        offset, size = data
        if size + 2 > 0xFFFFFF:
            raise ContractError('SBL-Daten überschreiten die VOC-Blockgröße')
        # UTE helper: SBL(size+26), AUhd(3, 00 00 80), AUdt(size+7),
        # VOC type 1 + LE24(size+2) + time constant D2 + codec 0 + PCM + terminator.
        framing = (b'SBL ' + struct.pack('>I', size + 26)
                   + b'AUhd' + struct.pack('>I', 3) + b'\x00\x00\x80'
                   + b'AUdt' + struct.pack('>I', size + 7)
                   + b'\x01' + (size + 2).to_bytes(3, 'little') + b'\xd2\x00')
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() == target.resolve():
            raise ContractError('SBL-Ziel darf die Quell-WAV nicht überschreiben')
        name = None
        try:
            with tempfile.NamedTemporaryFile(prefix=target.name + '.', suffix='.part',
                                             dir=target.parent, delete=False) as output:
                name = Path(output.name)
                output.write(framing)
                stream.seek(offset)
                remaining = size
                while remaining:
                    block = stream.read(min(remaining, 1024 * 1024))
                    if not block:
                        raise ContractError('SBL-PCM-Daten sind abgeschnitten')
                    output.write(block)
                    remaining -= len(block)
                output.write(b'\x00')
            os.replace(name, target)
        finally:
            if name is not None:
                name.unlink(missing_ok=True)
    return {'frames': size, 'sample_rate': 22050, 'channels': 1, 'bits': 8,
            'output_bytes': size + 34}
