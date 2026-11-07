"""Own reader for original SOU and compressed FLAC SOF speech archives.

SOF contains a big-endian table length followed by 16-byte index records:
original VCTL offset, relative payload offset, sync-tag bytes, FLAC bytes.
The original offset remains the script's speech ID. Payload positions are
physical storage only. Decoding stays in the established extractor.
"""
from __future__ import annotations

from pathlib import Path
import struct

from .contracts import ContractError, resolve_relative


def english_archive(work_root, outputs):
    """Resolve exactly one hash-bound original-English archive inventory item."""
    from manifest_renderer.audio import sha256_file
    names={'english:monster.sou','english:monkey.sof','english:monster.sof'}
    matches=[(key,entry) for key,entry in outputs.items() if key.lower() in names]
    if len(matches)!=1:
        raise ContractError('Englischer SOU-/FLAC-SOF-Snapshot fehlt oder ist mehrdeutig')
    key,entry=matches[0]
    path=resolve_relative(work_root,entry['path'],must_exist=True)
    if path.name.lower()!=key.split(':',1)[1].lower() or sha256_file(path)!=entry['sha256']:
        raise ContractError('Englischer Archivname oder Snapshot-Hash stimmt nicht mit dem Vorbereitungsbeleg überein')
    return path


def scan_sof(source, sample_factory):
    """Map validated compressed entries to the extractor's existing Sample type."""
    source=Path(source);size=source.stat().st_size
    if source.is_symlink() or not source.is_file():raise ContractError('SOF muss eine lokale reguläre Datei sein')
    with source.open('rb') as stream:
        header=stream.read(4)
        if len(header)!=4:raise ContractError('SOF-Indexkopf ist abgeschnitten')
        table_size=struct.unpack('>I',header)[0];payload_start=4+table_size
        if table_size==0 or table_size%16 or payload_start>size:
            raise ContractError('SOF-Indexlänge ist ungültig oder abgeschnitten')
        rows=[];seen=set();spans=[]
        for index in range(table_size//16):
            stream.seek(4+index*16)
            speech_id,relative,tags,flac_size=struct.unpack('>IIII',stream.read(16))
            if speech_id<8 or speech_id in seen:raise ContractError('SOF enthält ungültige oder doppelte Original-Speech-IDs')
            seen.add(speech_id)
            start=payload_start+relative;audio=start+tags;end=audio+flac_size
            if flac_size<4 or start>=size or audio>size or end>size:
                raise ContractError('SOF-Payload liegt außerhalb des Archivs oder ist abgeschnitten')
            if tags%2:raise ContractError('SOF-Sync-Tags müssen vollständige 16-Bit-Werte enthalten')
            stream.seek(audio)
            if stream.read(4)!=b'fLaC':raise ContractError('SOF-Audiopayload hat keine FLAC-Signatur')
            spans.append((start,end));rows.append(sample_factory(speech_id,start,tags+flac_size,audio,flac_size,tags+8))
        ordered=sorted(spans)
        if any(end>next_start for (_,end),(next_start,_) in zip(ordered,ordered[1:])):
            raise ContractError('SOF-Payloadbereiche überlappen')
    return sorted(rows,key=lambda sample:sample.speech_id)


def scan_archive(source, extractor):
    """Keep raw SOU scanning untouched; adapt only the compressed SOF format."""
    with Path(source).open('rb') as stream:magic=stream.read(4)
    if magic==b'SOU ':return extractor.scan_sou(source)
    if Path(source).suffix.lower()=='.sof':return scan_sof(source,extractor.Sample)
    raise ContractError('Originalarchiv ist weder ein Roh-SOU noch ein FLAC-SOF')
