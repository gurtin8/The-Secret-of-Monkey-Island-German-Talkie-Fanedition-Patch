"""Restore unxwb's incomplete xWMA wrapper from authoritative XWB metadata.

No decoder workaround, ignored errors, audio padding or compressed-data edits.
Microsoft's original bank format stores WMA block alignment as a table index:
https://github.com/microsoft/DirectXTK/blob/main/Audio/WaveBankReader.cpp
(MINIWAVEFORMAT::BlockAlign/AvgBytesPerSec and FindSeekTable).
FFmpeg's xwma demuxer defaults to 2230-byte packets for block_align <= 1,
while its WMA decoder truncates each packet to the declared block_align:
https://github.com/FFmpeg/FFmpeg/blob/n7.1/libavformat/xwma.c
https://github.com/FFmpeg/FFmpeg/blob/n7.1/libavcodec/wmadec.c
unxwb's block_align=1 and dpds=0xffffffff wrapper thus needs bank metadata.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct

from .contracts import ContractError

# File-format constants independently implemented from Microsoft's documented
# miniformat indices, not inferred from bitrate or successful decoder guesses.
WMA_BLOCK_ALIGN = (929, 1487, 1280, 2230, 8917, 8192, 4459, 5945,
                   2304, 1536, 1485, 1008, 2731, 4096, 6827, 5462, 1280)
WMA_AVG_BYTES = (12000, 24000, 4000, 6000, 8000, 20000, 2500)
MAX_METADATA_BYTES = 32 * 1024 * 1024
MAX_XWMA_BYTES = 128 * 1024 * 1024


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _signature(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def _read_region(stream, offset, length, size):
    _require(0 <= offset <= size and 0 <= length <= size - offset, 'XWB region outside file')
    stream.seek(offset)
    data = stream.read(length)
    _require(len(data) == length, 'Truncated XWB region')
    return data


def _entry_name(names, name_size, index):
    if not names:
        return None
    raw = names[index * name_size:(index + 1) * name_size].split(b'\0', 1)[0]
    _require(bool(raw) and not any(c in raw for c in (b'/', b'\\')), 'Invalid XWB entry name')
    try:
        return raw.decode('ascii')
    except UnicodeDecodeError as exc:
        raise ContractError('Non-ASCII XWB entry name unsupported') from exc


def read_xwb_xwma_metadata(bank: Path, *, include_pcm=False) -> list[dict]:
    """Read bounded metadata, never load a whole audio bank into memory.

    Native UTE supports the actual PC-SE 43/44 header layout with normal
    (non-compact) 24-byte entries and WMAv2 mono/stereo. Unsupported layouts
    and codecs fail explicitly. Names may be absent; compressed bytes then
    identify the matching entry when prepare_xwma is called. include_pcm also
    reads PCM8/16 entries to verify original-recipe PCM-over-xWMA collisions.
    """
    bank = Path(bank).resolve(strict=True)
    _require(bank.is_file(), 'XWB source is not a regular file')
    signature = _signature(bank)
    size = signature[0]
    with bank.open('rb') as stream:
        header = _read_region(stream, 0, 52, size)
        _require(header[:4] == b'WBND', 'Unsupported XWB signature/endianness')
        tool_version, header_version = struct.unpack_from('<II', header, 4)
        _require(header_version in (43, 44), 'Unsupported XWB header version')
        regions = [struct.unpack_from('<II', header, 12 + 8 * i) for i in range(5)]
        for offset, length in regions:
            _require(offset <= size and length <= size - offset, 'XWB region outside file')
        _require(regions[0][1] >= 96, 'XWB bank metadata missing')
        bankdata = _read_region(stream, regions[0][0], 96, size)
        flags, count = struct.unpack_from('<II', bankdata)
        element_size, name_size = struct.unpack_from('<II', bankdata, 72)
        _require(not flags & 0x20000 and element_size == 24, 'Compact/nonstandard XWB entries unsupported')
        _require(0 < count <= 100000 and regions[1][1] == count * 24, 'Invalid XWB entry count/size')
        _require(sum(length for _, length in regions[1:4]) <= MAX_METADATA_BYTES, 'XWB metadata exceeds budget')
        entries = _read_region(stream, *regions[1], size)
        seeks = _read_region(stream, *regions[2], size)
        names = _read_region(stream, *regions[3], size)
        if names:
            _require(flags & 0x10000 and 0 < name_size <= 1024 and len(names) == count * name_size,
                     'Invalid XWB name table')
        records = []
        for index in range(count):
            duration_flags, mini, data_offset, data_bytes, _, _ = struct.unpack_from('<6I', entries, index * 24)
            if include_pcm and mini & 3 == 0:
                channels, rate = (mini >> 2) & 7, (mini >> 5) & 0x3ffff
                bits, align, frames = 8 + (mini >> 31) * 8, (mini >> 23) & 0xff, duration_flags >> 4
                _require(channels in (1, 2) and rate > 0 and frames > 0 and align == channels * (bits // 8),
                         'Unsupported XWB PCM format')
                _require(data_bytes == frames * align and data_offset <= regions[4][1]
                         and data_bytes <= regions[4][1] - data_offset, 'Invalid XWB PCM audio region/duration')
                records.append(dict(bank=str(bank), bank_signature=signature, index=index,
                                    name=_entry_name(names, name_size, index), codec_tag=1,
                                    channels=channels, sample_rate=rate, bits=bits, frames=frames,
                                    data_offset=regions[4][0] + data_offset, data_bytes=data_bytes))
                continue
            if mini & 3 != 3:
                continue
            channels = (mini >> 2) & 7
            rate = (mini >> 5) & 0x3ffff
            encoded_alignment = (mini >> 23) & 0xff
            align_index, bytes_index = encoded_alignment & 0x1f, encoded_alignment >> 5
            _require(mini >> 31 == 0 and channels in (1, 2) and rate > 0,
                     'Only WMAv2 mono/stereo XWB entries supported')
            _require(align_index < len(WMA_BLOCK_ALIGN) and bytes_index < len(WMA_AVG_BYTES),
                     'Invalid XWB WMA miniformat index')
            align = WMA_BLOCK_ALIGN[align_index]
            _require(data_bytes > 0 and data_offset <= regions[4][1] and data_bytes <= regions[4][1] - data_offset,
                     'XWB audio entry outside data region')
            _require(len(seeks) >= count * 4, 'XWB WMA seek table missing')
            seek_offset = struct.unpack_from('<I', seeks, index * 4)[0]
            seek_start = count * 4 + seek_offset
            _require(seek_offset != 0xffffffff and seek_start + 4 <= len(seeks), 'Invalid XWB seek offset')
            packets = struct.unpack_from('<I', seeks, seek_start)[0]
            _require(0 < packets and packets * 4 <= len(seeks) - seek_start - 4, 'Invalid XWB seek count')
            dpds = list(struct.unpack_from('<' + str(packets) + 'I', seeks, seek_start + 4))
            frames = duration_flags >> 4
            _require(frames > 0 and dpds[-1] == frames * channels * 2,
                     'XWB decoded duration disagrees with seek table')
            _require(all(a <= b for a, b in zip([0] + dpds, dpds)) and all(n % (2 * channels) == 0 for n in dpds),
                     'Invalid XWB cumulative PCM seek table')
            _require(data_bytes == packets * align, 'XWB WMA packet count/alignment disagrees with audio size')
            name = _entry_name(names, name_size, index)
            records.append(dict(bank=str(bank), bank_signature=signature, index=index, name=name,
                                tool_version=tool_version, header_version=header_version,
                                codec_tag=0x161, channels=channels, sample_rate=rate, bits=16,
                                block_align=align, avg_bytes_per_second=WMA_AVG_BYTES[bytes_index],
                                frames=frames, seek_table=dpds, data_offset=regions[4][0] + data_offset,
                                data_bytes=data_bytes))
    _require(_signature(bank) == signature, 'XWB changed while reading metadata')
    return records


def _xwma_info(source):
    size = source.stat().st_size
    _require(0 < size <= MAX_XWMA_BYTES, 'XWMA file exceeds input budget or is empty')
    with source.open('rb') as stream:
        header = stream.read(12)
        _require(len(header) == 12 and header[:4] == b'RIFF' and header[8:] == b'XWMA', 'Invalid XWMA RIFF header')
        _require(struct.unpack_from('<I', header, 4)[0] + 8 == size, 'XWMA RIFF size differs from file')
        chunks = {}
        while stream.tell() + 8 <= size:
            kind, length = struct.unpack('<4sI', stream.read(8))
            offset = stream.tell()
            _require(kind not in chunks and kind in (b'fmt ', b'dpds', b'data') and length <= size - offset,
                     'Unexpected/duplicate/truncated XWMA chunk')
            chunks[kind] = (offset, length)
            # Original unxwb may omit the RIFF pad after its final odd data
            # chunk. Accept only that terminal case; reconstructed output pads.
            end = offset + length
            stream.seek(end + (length & 1) if end < size else end)
        _require(stream.tell() == size and set(chunks) == {b'fmt ', b'dpds', b'data'}, 'XWMA chunks incomplete')
        _require(chunks[b'fmt '][1] == 18 and chunks[b'dpds'][1] > 0 and chunks[b'dpds'][1] % 4 == 0,
                 'Unexpected XWMA format/seek table')
        stream.seek(chunks[b'fmt '][0])
        tag, channels, rate, avg, alignment, bits, extra = struct.unpack('<HHIIHHH', stream.read(18))
        _require(tag == 0x161 and channels in (1, 2) and bits == 16 and extra == 0, 'Unsupported XWMA codec/format')
    return dict(codec_tag=tag, channels=channels, sample_rate=rate, avg_bytes_per_second=avg,
                block_align=alignment, bits=bits, data_offset=chunks[b'data'][0], data_bytes=chunks[b'data'][1])


def _region_sha(path, offset, length):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        stream.seek(offset)
        while length:
            block = stream.read(min(length, 1024 * 1024))
            _require(bool(block), 'Audio payload truncated')
            h.update(block)
            length -= len(block)
    return h.hexdigest()


def match_bank_pcm(source, info, metadata):
    """Bind direct PCM to the extracted bank; WAV headers do not identify it."""
    possible = [e for e in metadata if e['codec_tag'] == 1 and e['name'] == source.stem
                and e['channels'] == info['channels'] and e['sample_rate'] == info['rate']
                and e['bits'] == info['bits'] and e['frames'] == info['frames']
                and e['data_bytes'] == info['data_bytes']]
    pcm_sha = _region_sha(source, info['data_offset'], info['data_bytes'])
    matches = []
    for entry in possible:
        bank = Path(entry['bank'])
        _require(_signature(bank) == entry['bank_signature'], 'XWB changed before PCM verification')
        matched = _region_sha(bank, entry['data_offset'], entry['data_bytes']) == pcm_sha
        _require(_signature(bank) == entry['bank_signature'], 'XWB changed during PCM verification')
        if matched:
            matches.append(entry)
    _require(matches, 'Direct PCM bytes do not match an extracted bank entry')
    selected = matches[-1]  # Last extraction wins, as in the pinned BAT recipe.
    return dict(bank=selected['bank'], bank_index=selected['index'], bank_entry_name=selected['name'],
                pcm_sha256=pcm_sha, pcm_bytes_unchanged=True)


def prepare_xwma(source: Path, destination: Path, metadata: list[dict]) -> dict:
    """Write a new correct wrapper; refuse existing destination or unknown data.

    metadata may combine multiple banks. Named entries first match the exact
    source stem; unnamed entries use format, length and an exact payload hash.
    Ambiguous payloads with differing duration/header data are rejected.
    """
    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()
    _require(source != destination.resolve(), 'XWMA preparation must not overwrite source')
    signature = _signature(source)
    info = _xwma_info(source)
    keys = ('codec_tag', 'channels', 'sample_rate', 'bits', 'avg_bytes_per_second', 'data_bytes')
    possible = [entry for entry in metadata if entry['codec_tag'] == 0x161
                and all(info[key] == entry[key] for key in keys)
                and (entry['name'] is None or entry['name'] == source.stem)]
    _require(possible, 'XWMA has no matching bank name/format/size metadata')
    payload_sha = _region_sha(source, info['data_offset'], info['data_bytes'])
    matches = []
    for entry in possible:
        bank = Path(entry['bank'])
        _require(_signature(bank) == entry['bank_signature'], 'XWB changed after metadata extraction')
        if _region_sha(bank, entry['data_offset'], entry['data_bytes']) == payload_sha:
            matches.append(entry)
    _require(matches, 'XWMA compressed bytes do not match original bank entry')
    selected = matches[0]
    format_keys = ('block_align', 'avg_bytes_per_second', 'frames', 'seek_table')
    _require(all(all(entry[k] == selected[k] for k in format_keys) for entry in matches),
             'Ambiguous XWMA bank entries have differing metadata')
    _require(info['block_align'] in (1, selected['block_align']), 'Unexpected XWMA block alignment')
    fmt = struct.pack('<HHIIHHH', selected['codec_tag'], selected['channels'], selected['sample_rate'],
                      selected['avg_bytes_per_second'], selected['block_align'], 16, 0)
    dpds = struct.pack('<' + str(len(selected['seek_table'])) + 'I', *selected['seek_table'])
    prefix = b'XWMAfmt ' + struct.pack('<I', len(fmt)) + fmt + b'dpds' + struct.pack('<I', len(dpds)) + dpds
    prefix += b'data' + struct.pack('<I', info['data_bytes'])
    riff_size = len(prefix) + info['data_bytes'] + (info['data_bytes'] & 1)
    _require(_signature(source) == signature, 'XWMA changed while preparing wrapper')
    created = False
    try:
        with destination.open('xb') as output, source.open('rb') as original:
            created = True
            output.write(b'RIFF' + struct.pack('<I', riff_size) + prefix)
            original.seek(info['data_offset'])
            remaining = info['data_bytes']
            while remaining:
                block = original.read(min(remaining, 1024 * 1024))
                _require(bool(block), 'XWMA changed/truncated while copying')
                output.write(block)
                remaining -= len(block)
            if info['data_bytes'] & 1:
                output.write(b'\0')
        _require(_signature(source) == signature, 'XWMA changed while copying')
        output_info = _xwma_info(destination)
        _require(_region_sha(destination, output_info['data_offset'], info['data_bytes']) == payload_sha,
                 'Prepared XWMA compressed payload changed')
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return dict(source=str(source), prepared=str(destination), bank=selected['bank'], bank_index=selected['index'],
                bank_entry_name=selected['name'], compressed_sha256=payload_sha, compressed_bytes_unchanged=True,
                source_block_align=info['block_align'], block_align=selected['block_align'],
                channels=selected['channels'], sample_rate=selected['sample_rate'], frames=selected['frames'],
                packet_count=len(selected['seek_table']), decoded_pcm_bytes=selected['seek_table'][-1])
