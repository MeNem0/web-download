"""Read an MP3's text tags without downloading its embedded artwork.

mutagen reads a file's whole ID3 tag, and a tag usually carries a multi-megabyte
cover image. On a network share that makes listing a folder crawl even though
the title/artist/album/track number are a few hundred bytes. This walks the ID3v2
frame headers instead and seeks straight past the picture, reading only the
text frames it needs.

It is deliberately strict: anything it doesn't fully understand (an old tag
version, unsynchronisation, compressed frames, a corrupt size) returns None and
the caller falls back to mutagen, so the fast path can only ever be faster,
never different.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from metadata_tags import read_track_metadata

CHUNK = 16 * 1024
WANTED = {b"TIT2", b"TPE1", b"TALB", b"TPE2", b"TRCK", b"TCMP"}


class _Reader:
    """Buffered random access: skipping a big frame costs one seek, not a read."""

    def __init__(self, handle: Any) -> None:
        self.handle = handle
        self.start = 0
        self.buffer = b""

    def read(self, pos: int, count: int) -> bytes:
        end = pos + count
        if not (self.start <= pos and end <= self.start + len(self.buffer)):
            self.handle.seek(pos)
            self.buffer = self.handle.read(max(count, CHUNK))
            self.start = pos
        offset = pos - self.start
        return self.buffer[offset : offset + count]


def _syncsafe(raw: bytes) -> int:
    return (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]


def _decode(data: bytes) -> str:
    """First string of a text frame body (encoding byte, then text)."""
    if not data:
        return ""
    encoding, body = data[0], data[1:]
    if encoding == 0:
        text = body.decode("latin-1")
    elif encoding == 1:
        text = body.decode("utf-16")
    elif encoding == 2:
        text = body.decode("utf-16-be")
    elif encoding == 3:
        text = body.decode("utf-8")
    else:
        raise ValueError("unknown text encoding")
    return text.split("\x00", 1)[0].strip()


def _scan(path: Path) -> dict[str, Any] | None:
    with path.open("rb") as handle:
        reader = _Reader(handle)
        header = reader.read(0, 10)
        if len(header) < 10 or header[:3] != b"ID3":
            return None
        major, flags = header[3], header[5]
        # v2.2 uses 3-letter ids; unsynchronisation / extended headers are rare
        # and fiddly. All of those take the slow, always-correct path.
        if major not in (3, 4) or flags & 0xC0:
            return None
        end = 10 + _syncsafe(header[6:10])

        found: dict[bytes, str] = {}
        has_cover = False
        pos = 10
        while pos + 10 <= end:
            frame = reader.read(pos, 10)
            if len(frame) < 10 or frame[0] == 0:  # padding: the frames are over
                break
            frame_id = frame[:4]
            size = _syncsafe(frame[4:8]) if major == 4 else int.from_bytes(frame[4:8], "big")
            frame_flags = int.from_bytes(frame[8:10], "big")
            if size < 0 or pos + 10 + size > end:
                return None
            if frame_id == b"APIC":
                has_cover = True
            elif frame_id in WANTED:
                # compression / encryption / unsynchronisation / data-length
                # indicator all change how the body must be decoded.
                if frame_flags & (0x00FF if major == 3 else 0x000F):
                    return None
                body = reader.read(pos + 10, size)
                if len(body) < size:
                    return None
                found[frame_id] = _decode(body)
            pos += 10 + size

    track = found.get(b"TRCK", "")
    return {
        "title": found.get(b"TIT2") or "",
        "artists": found.get(b"TPE1", ""),
        "album": found.get(b"TALB", ""),
        "album_artist": found.get(b"TPE2", ""),
        "track_number": track.split("/", 1)[0].strip() if track else "",
        "compilation": found.get(b"TCMP", "") in {"1", "true", "True"},
        "has_cover": has_cover,
    }


def read_track_metadata_fast(path: Path) -> dict[str, Any]:
    """Same result as metadata_tags.read_track_metadata, minus the picture download."""
    try:
        scanned = _scan(path)
    except (OSError, ValueError, UnicodeError, IndexError):
        scanned = None
    if scanned is None:
        return read_track_metadata(path)
    if not scanned["title"]:
        scanned["title"] = path.stem  # the same default the slow reader uses
    return scanned
