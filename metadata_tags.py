#!/usr/bin/env python3
"""Shared helpers for reading/writing MP3 metadata tags."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import httpx
from mutagen.id3 import APIC, ID3, TALB, TPE1, TIT2, TRCK, ID3NoHeaderError


def _text_frame(tags: ID3, key: str) -> str:
    frames = tags.getall(key)
    if not frames:
        return ""
    text = getattr(frames[0], "text", None)
    if not text:
        return ""
    return str(text[0]).strip()


def read_track_metadata(mp3_path: Path) -> dict[str, Any]:
    """Read common ID3 tags from an MP3 file."""
    result: dict[str, Any] = {
        "title": mp3_path.stem,
        "artists": "",
        "album": "",
        "track_number": "",
        "has_cover": False,
    }
    if not mp3_path.exists():
        return result
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        return result
    except Exception:
        return result

    title = _text_frame(tags, "TIT2")
    artists = _text_frame(tags, "TPE1")
    album = _text_frame(tags, "TALB")
    track = _text_frame(tags, "TRCK")
    if title:
        result["title"] = title
    result["artists"] = artists
    result["album"] = album
    result["track_number"] = track.split("/", 1)[0].strip() if track else ""
    result["has_cover"] = bool(tags.getall("APIC"))
    return result


def read_cover_bytes(mp3_path: Path) -> tuple[bytes, str] | None:
    """Return (image_bytes, mime) for the first embedded cover, if any."""
    try:
        tags = ID3(mp3_path)
    except Exception:
        return None
    pictures = tags.getall("APIC")
    if not pictures:
        return None
    pic = pictures[0]
    data = getattr(pic, "data", None) or b""
    mime = (getattr(pic, "mime", None) or "image/jpeg").split(";", 1)[0]
    if not data:
        return None
    if mime not in {"image/jpeg", "image/png", "image/webp"}:
        mime = "image/jpeg"
    return data, mime


def _fetch_cover(cover_urls: Iterable[str | None]) -> tuple[bytes, str] | None:
    """Download the first cover image that works. Returns (data, mime)."""
    seen: set[str] = set()
    for url in cover_urls:
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            response = httpx.get(url, follow_redirects=True, timeout=30)
            response.raise_for_status()
        except httpx.HTTPError:
            continue
        if not response.content:
            continue
        mime = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
        if mime not in {"image/jpeg", "image/png"}:
            mime = "image/jpeg"
        return response.content, mime
    return None


def apply_track_metadata(
    mp3_path: Path,
    *,
    title: str,
    artists: str,
    album: str = "",
    track_number: int | None = None,
    cover_urls: Iterable[str | None] | None = None,
) -> None:
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()

    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artists))
    if album:
        tags.add(TALB(encoding=3, text=album))
    if track_number is not None:
        tags.add(TRCK(encoding=3, text=str(track_number)))

    cover = _fetch_cover(cover_urls or [])
    if cover:
        data, mime = cover
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))

    tags.save(mp3_path)


def update_track_metadata(
    mp3_path: Path,
    *,
    title: str | None = None,
    artists: str | None = None,
    album: str | None = None,
    track_number: str | int | None = None,
    cover_urls: Iterable[str | None] | None = None,
    cover_bytes: bytes | None = None,
    cover_mime: str | None = None,
    remove_cover: bool = False,
) -> dict[str, Any]:
    """Update selected ID3 fields. Pass None to leave a text field unchanged."""
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()

    if title is not None:
        tags.add(TIT2(encoding=3, text=title.strip() or mp3_path.stem))
    if artists is not None:
        tags.add(TPE1(encoding=3, text=artists.strip()))
    if album is not None:
        if album.strip():
            tags.add(TALB(encoding=3, text=album.strip()))
        else:
            tags.delall("TALB")
    if track_number is not None:
        text = str(track_number).strip()
        if text:
            tags.add(TRCK(encoding=3, text=text))
        else:
            tags.delall("TRCK")

    if remove_cover:
        tags.delall("APIC")
    else:
        cover: tuple[bytes, str] | None = None
        if cover_bytes:
            mime = (cover_mime or "image/jpeg").split(";", 1)[0]
            if mime not in {"image/jpeg", "image/png", "image/webp"}:
                mime = "image/jpeg"
            cover = (cover_bytes, mime)
        elif cover_urls:
            cover = _fetch_cover(cover_urls)
        if cover:
            data, mime = cover
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))

    tags.save(mp3_path)
    return read_track_metadata(mp3_path)


def ensure_cover_art(mp3_path: Path, cover_urls: Iterable[str | None]) -> bool:
    """Embed a cover only if the file has none. Returns True if one is present."""
    if not mp3_path.exists():
        return False
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()

    if tags.getall("APIC"):
        return True

    cover = _fetch_cover(cover_urls)
    if not cover:
        return False

    data, mime = cover
    tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    tags.save(mp3_path)
    return True
