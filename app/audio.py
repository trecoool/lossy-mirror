# Audio engine: format classes, source ranking, and verbatim tag transfer to vorbis-comment outputs.
# Converters only produce audio; tags always come from here so multi-value tags survive untouched.
# A source whose tag system is not understood is refused rather than converted with lost tags.

import os
import struct

import mutagen
from mutagen.apev2 import TEXT
from mutagen.flac import FLAC
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack

LOSSLESS = {".flac", ".wav", ".aiff", ".aif", ".wv", ".ape"}
LOSSY = {".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wma", ".mpc"}
AUDIO_EXTS = LOSSLESS | LOSSY
OUTPUTS = {".ogg", ".opus", ".flac"}  # vorbis-comment containers we can tag
PICTURE_KEYS = {"METADATA_BLOCK_PICTURE", "COVERART", "COVERARTMIME"}
VORBIS_TYPES = (FLAC, OggVorbis, OggOpus)
APE_TYPES = (MonkeysAudio, WavPack)

# APEv2 keys as written by Picard / foobar2000 that differ from their vorbis-comment names
APE_TO_VORBIS = {
    "ALBUM ARTIST": "ALBUMARTIST",
    "YEAR": "DATE",
    "MIXARTIST": "REMIXER",
    "DEBUT ALBUM": "ORIGINALALBUM",
}

# RIFF INFO ids (wav); ids not listed here are kept under their own name
RIFF_TO_VORBIS = {
    "INAM": "TITLE", "IART": "ARTIST", "IPRD": "ALBUM", "ICRD": "DATE", "IGNR": "GENRE",
    "ITRK": "TRACKNUMBER", "IPRT": "TRACKNUMBER", "ICMT": "COMMENT", "ICOP": "COPYRIGHT",
    "ICMS": "COMMISSIONED", "IENG": "ENGINEER", "ISFT": "ENCODER", "ISRC": "SOURCE",
}


class UnsupportedTags(Exception):
    pass


def is_audio(ext):
    return ext.lower() in AUDIO_EXTS


def rank(path):
    """Sort key, higher is better: lossless first, then PCM rate (lossless) or bitrate (lossy)."""
    info = mutagen.File(path).info
    if os.path.splitext(path)[1].lower() in LOSSLESS:
        bits = getattr(info, "bits_per_sample", 16) or 16
        return 1, info.sample_rate * bits * info.channels
    return 0, getattr(info, "bitrate", 0) or 0


def best(paths, priority):
    """Best source among same-name audio files. Headers are read only when the format class ties."""
    lossless = [p for p in paths if os.path.splitext(p)[1].lower() in LOSSLESS]
    pool = lossless or paths
    if len(pool) == 1:
        return pool[0]

    def key(p):
        ext = os.path.splitext(p)[1].lower()
        order = priority.index(ext) if ext in priority else len(priority)
        try:
            r = rank(p)
        except Exception:
            r = (-1, 0)
        return r, -order, p

    return max(pool, key=key)


def sample_rate(path):
    return mutagen.File(path).info.sample_rate


def _split_ape_number(key, value, out):
    """APE 'Track'/'Disc' hold '3/12'; vorbis splits number and total."""
    base = "TRACK" if key == "TRACK" else "DISC"
    number, _, total = value.partition("/")
    out.append((base + "NUMBER", number))
    if total:
        out.append((base + "TOTAL", total))


def read_riff_info(path):
    """RIFF LIST/INFO text chunks of a wav, which mutagen does not read."""
    out = []
    with open(path, "rb") as f:
        f.seek(12)  # RIFF header
        while len(header := f.read(8)) == 8:
            cid, size = header[:4], struct.unpack("<I", header[4:])[0]
            if cid == b"LIST" and f.read(4) == b"INFO":
                body, pos = f.read(size - 4), 0
                while pos + 8 <= len(body):
                    sub, sub_size = body[pos:pos + 4].decode("latin-1"), struct.unpack("<I", body[pos + 4:pos + 8])[0]
                    value = body[pos + 8:pos + 8 + sub_size].split(b"\x00")[0].decode("utf-8", "replace").strip()
                    if value:
                        out.append((RIFF_TO_VORBIS.get(sub, sub), value))
                    pos += 8 + sub_size + (sub_size & 1)
                f.seek(size & 1, 1)
            else:
                f.seek(size + (size & 1) - (4 if cid == b"LIST" else 0), 1)
    return out


def read_tags(path):
    """Source tags as vorbis-comment (key, value) pairs, pictures excluded.
    Only containers whose every tag system is understood are accepted: an unknown one may hide tags."""
    f = mutagen.File(path)
    if isinstance(f, VORBIS_TYPES):
        return [(k, v) for k, v in f.tags or [] if k.upper() not in PICTURE_KEYS]
    if isinstance(f, WAVE):
        if f.tags:
            raise UnsupportedTags(f"ID3 tags in wav not supported: {path}")
        return read_riff_info(path)
    if isinstance(f, APE_TYPES):
        out = []
        tags = f.tags or {}
        for key, value in tags.items():
            if value.kind != TEXT:
                continue  # embedded cover art
            k = key.upper()
            for v in value:
                if k in ("TRACK", "DISC"):
                    _split_ape_number(k, v, out)
                else:
                    out.append((APE_TO_VORBIS.get(k, k), v))
        return out
    raise UnsupportedTags(f"{type(f).__name__ if f else 'unknown'} files not supported as conversion input: {path}")


def write_tags(path, pairs):
    """Replaces every tag of a vorbis-comment file with `pairs`."""
    f = mutagen.File(path)
    if f.tags is None:
        f.add_tags()
    f.tags.clear()
    for key, value in pairs:
        f.tags.append((key, value))
    f.save()
