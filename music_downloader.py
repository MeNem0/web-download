#!/usr/bin/env python3
"""Download Spotify/YouTube playlists and songs as tagged MP3 files."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from spotify_scraper import SpotifyClient

from download_playlist import (
    SUPPORTED_BROWSERS,
    BrowserCookieSession,
    CallbackLogger,
    DownloadStrategy,
    _QuietLogger,
    build_js_runtimes,
    build_options,
    download_strategies,
    find_ffmpeg,
    resolve_browser,
    ytdlp_version,
)
from metadata_tags import VARIOUS_ARTISTS, apply_track_metadata, ensure_cover_art

SPOTIFY_PLAYLIST_RE = re.compile(
    r"(?:spotify\.com/(?:intl-[a-z]{2}/)?playlist/|spotify:playlist:)([a-zA-Z0-9]+)"
)
SPOTIFY_TRACK_RE = re.compile(
    r"(?:spotify\.com/(?:intl-[a-z]{2}/)?track/|spotify:track:)([a-zA-Z0-9]+)"
)
SPOTIFY_ALBUM_RE = re.compile(
    r"(?:spotify\.com/(?:intl-[a-z]{2}/)?album/|spotify:album:)([a-zA-Z0-9]+)"
)
YOUTUBE_PLAYLIST_RE = re.compile(
    r"(?:[?&]list=|[/]playlist[/])([a-zA-Z0-9_-]+)"
)
YOUTUBE_VIDEO_RE = re.compile(
    r"(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})"
)
INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DURATION_TOLERANCE_SECONDS = 15
YOUTUBE_SEARCH_RESULTS = 5
MAX_DOWNLOAD_ATTEMPTS = 3
TRACK_RETRY_ROUNDS = 2
# How many download methods (player-client strategies) to try per track, and the
# hard ceiling on attempts so one bad song cannot stall the queue.
MAX_STRATEGIES_PER_TRACK = 4
MAX_TOTAL_ATTEMPTS = 10


_CLIENT_BLOCK_MARKERS = (
    "403",
    "forbidden",
    "drm",
    "requested format is not available",
    "sign in to confirm",
    "login_required",
    "playability",
    "unable to download video data",
    "po token",
    "nsig",
    "player response",
)


class _ErrorCapturingLogger:
    """Forward yt-dlp log lines to the real logger while keeping error text."""

    def __init__(self, inner: object | None, sink: list[str]) -> None:
        self._inner = inner
        self._sink = sink

    def _forward(self, level: str, msg: object) -> None:
        target = getattr(self._inner, level, None)
        if callable(target):
            target(msg)

    def debug(self, msg: object) -> None:
        self._forward("debug", msg)

    def info(self, msg: object) -> None:
        self._forward("info", msg)

    def warning(self, msg: object) -> None:
        text = msg if isinstance(msg, str) else str(msg)
        # Blocked-client hints often arrive as warnings, not errors.
        if _is_client_block(text):
            self._sink.append(text.strip())
        self._forward("warning", msg)

    def error(self, msg: object) -> None:
        text = msg if isinstance(msg, str) else str(msg)
        if text.strip():
            self._sink.append(text.strip())
        self._forward("error", msg)


def _is_client_block(reason: str) -> bool:
    """True when the failure looks like YouTube rejecting the player client.

    Those failures repeat for every upload, so retrying more candidates with the
    same client wastes time — switching download method is what actually helps.
    """
    lowered = (reason or "").lower()
    return any(marker in lowered for marker in _CLIENT_BLOCK_MARKERS)


def strategy_ladder() -> list[DownloadStrategy]:
    """Ordered download methods to try for a single track."""
    ladder = download_strategies()
    cap = MAX_STRATEGIES_PER_TRACK
    raw = os.environ.get("YTDLP_MAX_STRATEGIES", "").strip()
    if raw.isdigit() and int(raw) > 0:
        cap = int(raw)
    return ladder[:cap] if len(ladder) > cap else ladder
# Strip YouTube / upload junk from titles used as filenames (and YT metadata).
_TITLE_JUNK_RE = re.compile(
    r"""
    \s*[\(\[\{]\s*(?:
        official(?:\s+(?:music\s+)?(?:video|audio|lyric(?:s)?(?:\s*video)?))?
        | (?:music|lyric(?:s)?)\s+video
        | audio(?:\s+only)?
        | visualizer
        | topic
        | (?:hd|hq|4k|lyrics?)
        | with\s+lyrics?
        | explicit
        | clean(?:\s+version)?
        | remaster(?:ed)?(?:\s+\d{2,4})?
    )\s*[\)\]\}]
    |
    \s*[-–—|]\s*official(?:\s+(?:music\s+)?(?:video|audio))?
    |
    \s+official(?:\s+(?:music\s+)?(?:video|audio|lyric(?:s)?(?:\s*video)?))\s*$
    |
    \s*[\(\[\{]\s*(?:feat\.?|ft\.?|featuring)\s+[^\]\)\}]+\s*[\)\]\}]
    """,
    re.IGNORECASE | re.VERBOSE,
)


class SourceType(str, Enum):
    SPOTIFY_PLAYLIST = "spotify_playlist"
    SPOTIFY_ALBUM = "spotify_album"
    SPOTIFY_TRACK = "spotify_track"
    YOUTUBE_PLAYLIST = "youtube_playlist"
    YOUTUBE_VIDEO = "youtube_video"
    # Only a name to go on: an album track the playlist did not contain.
    SEARCH = "search"


@dataclass(frozen=True)
class Track:
    index: int
    title: str
    artists: str
    duration_ms: int
    album: str = ""
    cover_url: str | None = None
    source: SourceType = SourceType.SPOTIFY_PLAYLIST
    youtube_url: str | None = None
    # Sequential album TRCK when songs are excluded/skipped (no gaps).
    album_track: int | None = None
    # Untouched source title, kept so compilations can recover "Artist - Song".
    raw_title: str = ""

    @property
    def search_query(self) -> str:
        return f"{self.artists} - {self.title}"

    @property
    def youtube_search_query(self) -> str:
        return f"{self.search_query} official video"

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1000

    @property
    def needs_youtube_search(self) -> bool:
        """No known upload, so the song has to be found by name.

        Covers Spotify sources and album tracks the user asked for that were
        absent from the playlist.
        """
        return self.youtube_url is None

    @property
    def tag_track_number(self) -> int:
        """ID3 track number: sequential album # when set, else playlist index."""
        return self.album_track if self.album_track is not None else self.index


@dataclass
class DownloadCallbacks:
    log: Callable[[str], None] | None = None
    progress: Callable[[int, int], None] | None = None
    file_progress: Callable[[float], None] | None = None
    should_cancel: Callable[[], bool] | None = None
    should_skip_track: Callable[[int], bool] | None = None
    on_tracks: Callable[[list[Track]], None] | None = None
    on_track_model: Callable[[Track], None] | None = None


@dataclass
class TrackDownloadResult:
    status: str  # done | failed | skipped | cancelled
    detail: str = ""


def emit(
    message: str,
    callbacks: DownloadCallbacks | None = None,
    *,
    stderr: bool = False,
) -> None:
    if callbacks and callbacks.log:
        callbacks.log(message)
        return
    print(message, file=sys.stderr if stderr else sys.stdout)


def _collapse_indices(nums: set[int]) -> str:
    if not nums:
        return ""
    ordered = sorted(nums)
    parts: list[str] = []
    start = prev = ordered[0]
    for n in ordered[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


def _parse_track_range_token(raw: str) -> range:
    match = re.match(r"^(\d+)(?:-(\d+))?$", raw)
    if not match:
        raise ValueError(
            f"Invalid track selector '{raw}'. "
            "Use 1-10, !3-5, or !3,4,5"
        )
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    if start < 1 or end < 1:
        raise ValueError("Track numbers must be 1 or greater")
    if end < start:
        start, end = end, start
    if end - start > 50_000:
        raise ValueError("Track range is too large")
    return range(start, end + 1)


def parse_track_selector(spec: str | None) -> tuple[set[int] | None, set[int]]:
    """Parse a track selector into (include_or_None_for_all, exclude).

    Whitespace separates clauses; commas group numbers inside a clause.
    A clause starting with ``!`` or ``-`` is an exclude list.

    Examples:
      ``1-10,15``      → only those playlist #s
      ``!3-5``         → all except 3–5
      ``!3,4,5``       → all except 3, 4, and 5
      ``!6-10``        → all except 6–10
      ``1-20 !3-5,8``  → 1–20 except 3–5 and 8
    """
    text = (spec or "").strip()
    if not text:
        return None, set()

    include: set[int] = set()
    exclude: set[int] = set()
    has_include = False

    for clause in re.split(r"\s+", text):
        if not clause:
            continue
        is_exclude = clause.startswith("!") or clause.startswith("-")
        body = clause[1:] if is_exclude else clause
        if not body:
            raise ValueError(
                f"Invalid track selector '{clause}'. "
                "Use 1-10, !3-5, or !3,4,5"
            )
        for raw in body.split(","):
            raw = raw.strip()
            if not raw:
                continue
            piece = raw[1:] if raw.startswith("!") or raw.startswith("-") else raw
            chunk = _parse_track_range_token(piece)
            if is_exclude or raw.startswith("!") or raw.startswith("-"):
                exclude.update(chunk)
            else:
                has_include = True
                include.update(chunk)

    return (include if has_include else None), exclude


def describe_track_selector(spec: str | None) -> str:
    include, exclude = parse_track_selector(spec)
    bits: list[str] = []
    if include is not None:
        bits.append(f"only {_collapse_indices(include)}")
    if exclude:
        bits.append(f"skip {_collapse_indices(exclude)}")
    return " · ".join(bits) if bits else "all"


def selector_playlist_end(spec: str | None, limit: int | None) -> int | None:
    """Highest playlist index needed when fetching (None = full playlist)."""
    include, exclude = parse_track_selector(spec)
    end: int | None = None
    if include is not None:
        end = max(include) if include else 0
    elif exclude:
        # Exclusions need the full list unless a hard limit is also set.
        end = None
    if limit is not None and limit > 0:
        end = limit if end is None else min(end, limit)
    return end if end and end > 0 else None


def filter_tracks_by_selector(
    tracks: list[Track],
    spec: str | None,
) -> tuple[list[Track], str]:
    include, exclude = parse_track_selector(spec)
    selected = [
        track
        for track in tracks
        if (include is None or track.index in include) and track.index not in exclude
    ]
    return selected, describe_track_selector(spec)


def sanitize_filename(name: str, max_length: int = 180) -> str:
    cleaned = INVALID_PATH_CHARS.sub("_", name).strip(" .")
    if not cleaned:
        cleaned = "track"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")
    return cleaned


def clean_song_title(title: str, artist: str | None = None) -> str:
    """Return a bare song name for filenames (no artist, official-video tags, etc.)."""
    name = (title or "").strip()
    if not name:
        return "track"

    prev = None
    while prev != name:
        prev = name
        name = _TITLE_JUNK_RE.sub(" ", name)
        name = re.sub(r"\s{2,}", " ", name).strip(" -–—|·")

    if artist:
        art = artist.strip()
        if art:
            lowered = name.lower()
            art_l = art.lower()
            for sep in (" - ", " – ", " — ", " | ", ": "):
                prefix = art_l + sep
                if lowered.startswith(prefix):
                    name = name[len(art) + len(sep) :].strip()
                    lowered = name.lower()
                suffix = sep + art_l
                if lowered.endswith(suffix):
                    name = name[: -len(suffix)].strip()
                    lowered = name.lower()

    name = name.strip(" -–—|·\"'")
    return name or (title or "").strip() or "track"


# "Artist - Song" style titles, the usual shape of compilation uploads.
_TITLE_ARTIST_SPLIT_RE = re.compile(r"\s+[-–—|]\s+|\s+[·•]\s+")
# Channel names that are the uploader, never the performing artist.
# VEVO is deliberately unanchored: real channels read "DaftPunkVEVO".
_CHANNEL_NOISE_RE = re.compile(
    r"(?:VEVO|- Topic$|\bRecords\b|\bMusic\b|\bOfficial\b|\bTV\b|\bChannel\b)",
    re.IGNORECASE,
)
# Right-hand sides that are release qualifiers, so the dash is not an
# artist/title separator: "Come As You Are - Remastered 2021".
_TITLE_QUALIFIER_RE = re.compile(
    r"^(?:re-?master|remastered|official|audio|video|lyrics?|hd|hq|4k|live"
    r"|explicit|clean|instrumental|full album|visualizer|mv|m/v)\b",
    re.IGNORECASE,
)


def split_artist_from_title(raw_title: str) -> tuple[str | None, str]:
    """Split "Artist - Song" into its parts.

    Compilation uploads name the performer in the title while the uploader is
    just the channel, so the title is the only per-track artist available.
    """
    text = (raw_title or "").strip()
    if not text:
        return None, text
    parts = _TITLE_ARTIST_SPLIT_RE.split(text, maxsplit=1)
    if len(parts) != 2:
        return None, text
    artist, title = parts[0].strip(" \"'"), parts[1].strip(" \"'")
    if not artist or not title or len(artist) > 60:
        return None, text
    # Guard against titles like "Song - Remastered" where the left side is the song.
    if _TITLE_QUALIFIER_RE.match(title):
        return None, text
    return artist, title


def parse_strip_terms(text: str | None) -> list[str]:
    """Split a user-entered "remove these words" list on commas/newlines."""
    if not text:
        return []
    parts = re.split(r"[,\n]", text)
    seen: list[str] = []
    for part in parts:
        term = part.strip()
        # Longest first so "official music video" wins over "music video".
        if term and term.lower() not in {t.lower() for t in seen}:
            seen.append(term)
    return sorted(seen, key=len, reverse=True)


def strip_terms_from_title(title: str, terms: list[str]) -> str:
    """Remove user-listed phrases from a song title and tidy what is left."""
    text = title or ""
    for term in terms:
        text = re.sub(re.escape(term), " ", text, flags=re.IGNORECASE)
    # Removing the contents of "(Official Music Video)" leaves empty brackets.
    text = re.sub(r"[\(\[\{]\s*[\)\]\}]", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" -–—|·,")
    return text or (title or "").strip()


def apply_strip_terms(tracks: list[Track], terms: list[str]) -> list[Track]:
    if not terms:
        return tracks
    out: list[Track] = []
    for track in tracks:
        cleaned = strip_terms_from_title(track.title, terms)
        out.append(track if cleaned == track.title else replace(track, title=cleaned))
    return out


def looks_like_channel_name(name: str) -> bool:
    """True when an artist value looks like a YouTube channel, not a performer."""
    text = (name or "").strip()
    if not text or text.lower() in {"unknown", "various", VARIOUS_ARTISTS.lower()}:
        return True
    return bool(_CHANNEL_NOISE_RE.search(text))


def resolve_compilation_artists(tracks: list[Track]) -> list[Track]:
    """Give each track its own artist for a Various Artists release.

    Spotify already supplies real per-track artists. YouTube reports the channel
    for every entry, which would collapse the whole album onto one artist, so
    fall back to parsing the performer out of the video title.
    """
    out: list[Track] = []
    for track in tracks:
        if not track.source.value.startswith("youtube"):
            out.append(track)
            continue
        if not looks_like_channel_name(track.artists):
            out.append(track)
            continue
        parsed_artist, parsed_title = split_artist_from_title(track.raw_title or track.title)
        if parsed_artist:
            out.append(
                replace(
                    track,
                    artists=parsed_artist,
                    title=clean_song_title(parsed_title, artist=parsed_artist),
                )
            )
        else:
            out.append(track)
    return out


def verify_track_artists(
    tracks: list[Track],
    callbacks: DownloadCallbacks | None = None,
) -> list[Track]:
    """Look each song up online and adopt the catalogued artist.

    Only touches tracks whose artist is untrustworthy — a YouTube channel or a
    blank — so hand-picked and Spotify-supplied credits are never overwritten.
    """
    try:
        from artist_lookup import ArtistResolver
    except Exception as exc:  # noqa: BLE001
        # Nicer metadata must never cost the user their download.
        emit(f"Artist lookup unavailable ({exc}); keeping source artists", callbacks)
        return tracks

    resolver = ArtistResolver()
    out: list[Track] = []
    checked = 0
    fixed = 0
    for track in tracks:
        trustworthy = not looks_like_channel_name(track.artists)
        if trustworthy or not resolver.available:
            out.append(track)
            continue
        checked += 1
        match = resolver.lookup(
            track.title,
            hint_artist="" if looks_like_channel_name(track.artists) else track.artists,
            duration_ms=track.duration_ms,
        )
        if match and match.artist.lower() != (track.artists or "").lower():
            fixed += 1
            emit(
                f"  [{track.index:03d}] artist matched: {track.artists} → {match.artist}",
                callbacks,
            )
            out.append(replace(track, artists=match.artist))
        else:
            out.append(track)

    if checked:
        emit(f"Artist lookup: matched {fixed}/{checked} uncertain track(s)", callbacks)
    if resolver.disabled_reason:
        emit(
            f"Artist lookup stopped early: {resolver.disabled_reason}",
            callbacks,
            stderr=True,
        )
    return out


def detect_source_type(url: str) -> SourceType | None:
    text = url.strip()
    if SPOTIFY_PLAYLIST_RE.search(text):
        return SourceType.SPOTIFY_PLAYLIST
    if SPOTIFY_ALBUM_RE.search(text):
        return SourceType.SPOTIFY_ALBUM
    if SPOTIFY_TRACK_RE.search(text):
        return SourceType.SPOTIFY_TRACK
    if YOUTUBE_PLAYLIST_RE.search(text):
        return SourceType.YOUTUBE_PLAYLIST
    if YOUTUBE_VIDEO_RE.search(text):
        return SourceType.YOUTUBE_VIDEO
    if "youtube.com" in text or "youtu.be" in text:
        return SourceType.YOUTUBE_VIDEO
    return None


def _spotify_cover_url(track_obj) -> str | None:
    if track_obj.images:
        return track_obj.images[0].url
    if track_obj.album and track_obj.album.images:
        return track_obj.album.images[0].url
    return None


def _title_already_credits_features(title: str) -> bool:
    lowered = title.lower()
    return any(
        marker in lowered
        for marker in ("feat.", "ft.", "featuring", "(with ", " with ")
    )


def format_track_artist_and_title(
    artist_names: list[str],
    raw_title: str,
) -> tuple[str, str]:
    """Return (artists, song title). Title stays the bare song name for filenames."""
    cleaned = [name.strip() for name in artist_names if name and name.strip()]
    title = clean_song_title(raw_title or "Unknown")
    if not cleaned:
        return "Unknown", title
    main_artist = cleaned[0]
    featured = cleaned[1:]
    if not featured:
        return main_artist, title
    # Keep featured credits on the artist tag, not the filename/title.
    if _title_already_credits_features(raw_title or ""):
        return main_artist, title
    featured_text = ", ".join(featured)
    return f"{main_artist} feat. {featured_text}", title


def _spotify_track_from_obj(track_obj, index: int, source: SourceType) -> Track:
    artist_names = [artist.name for artist in track_obj.artists]
    artists, title = format_track_artist_and_title(
        artist_names,
        track_obj.name,
    )
    album = track_obj.album.name if track_obj.album else ""
    return Track(
        index=index,
        title=title,
        artists=artists,
        duration_ms=track_obj.duration_ms or 0,
        album=album,
        cover_url=_spotify_cover_url(track_obj),
        source=source,
    )


def fetch_spotify_playlist(url: str) -> tuple[str, list[Track]]:
    match = SPOTIFY_PLAYLIST_RE.search(url.strip())
    if not match:
        raise ValueError("Invalid Spotify playlist URL.")

    with SpotifyClient() as client:
        # max_tracks=None paginates the full playlist (default caps at 100).
        playlist = client.get_playlist(match.group(1), max_tracks=None)

    tracks: list[Track] = []
    for item in playlist.tracks:
        if not item.track:
            continue
        tracks.append(
            _spotify_track_from_obj(
                item.track,
                len(tracks) + 1,
                SourceType.SPOTIFY_PLAYLIST,
            )
        )

    return playlist.name or "Spotify Playlist", tracks


def fetch_spotify_album(url: str) -> tuple[str, list[Track]]:
    """Album tracklist straight from Spotify, in release order.

    Compilations keep each track's own performer here, which is the whole point
    of using the album rather than scraping titles.
    """
    match = SPOTIFY_ALBUM_RE.search(url.strip())
    if not match:
        raise ValueError("Invalid Spotify album URL.")

    with SpotifyClient() as client:
        album = client.get_album(url.strip())

    album_name = getattr(album, "name", "") or "Spotify Album"
    cover = None
    images = getattr(album, "images", None) or []
    if images:
        cover = getattr(images[0], "url", None)

    tracks: list[Track] = []
    for item in getattr(album, "tracks", None) or []:
        names = [a.name for a in (getattr(item, "artists", None) or [])]
        if not names:
            names = [a.name for a in (getattr(album, "artists", None) or [])]
        artists, title = format_track_artist_and_title(names, getattr(item, "name", ""))
        tracks.append(
            Track(
                index=len(tracks) + 1,
                title=title,
                artists=artists,
                duration_ms=int(getattr(item, "duration_ms", 0) or 0),
                album=album_name,
                cover_url=cover,
                source=SourceType.SPOTIFY_ALBUM,
                album_track=int(getattr(item, "track_number", 0) or 0) or None,
            )
        )

    return album_name, tracks


def fetch_spotify_track(url: str) -> tuple[str, list[Track]]:
    match = SPOTIFY_TRACK_RE.search(url.strip())
    if not match:
        raise ValueError("Invalid Spotify track URL.")

    with SpotifyClient() as client:
        track_obj = client.get_track(match.group(1))

    track = _spotify_track_from_obj(track_obj, 1, SourceType.SPOTIFY_TRACK)
    collection_name = track.search_query
    return collection_name, [track]


def _entry_video_url(entry: dict) -> str | None:
    url = entry.get("webpage_url") or entry.get("url")
    if not url:
        video_id = entry.get("id")
        return f"https://www.youtube.com/watch?v={video_id}" if video_id else None
    if not url.startswith("http"):
        return f"https://www.youtube.com/watch?v={url}"
    return url


def fetch_youtube(
    url: str,
    cookie_session: BrowserCookieSession,
    search_opts: dict,
    limit: int | None = None,
) -> tuple[str, list[Track]]:
    # Flat extraction lists playlist entries without resolving every video,
    # which keeps it fast and lets --limit take effect before downloading.
    opts = dict(search_opts)
    opts["extract_flat"] = "in_playlist"
    if limit is not None and limit > 0:
        opts["playlistend"] = limit

    info = cookie_session.extract_info(url.strip(), opts, download=False)

    if not info:
        raise ValueError("Could not read YouTube URL.")

    if info.get("_type") == "playlist" or info.get("entries"):
        entries = [entry for entry in info.get("entries") or [] if entry]
        collection_name = info.get("title") or "YouTube Playlist"
        source = SourceType.YOUTUBE_PLAYLIST
    else:
        entries = [info]
        collection_name = info.get("title") or "YouTube Video"
        source = SourceType.YOUTUBE_VIDEO

    tracks: list[Track] = []
    for entry in entries:
        video_url = _entry_video_url(entry)
        if not video_url:
            continue
        duration = entry.get("duration") or 0
        artists = (
            entry.get("artist")
            or entry.get("uploader")
            or entry.get("channel")
            or "Unknown"
        )
        raw_title = entry.get("title") or "Unknown"
        tracks.append(
            Track(
                index=len(tracks) + 1,
                title=clean_song_title(raw_title, artist=artists),
                artists=artists,
                duration_ms=int(duration * 1000),
                album=collection_name,
                cover_url=entry.get("thumbnail"),
                source=source,
                youtube_url=video_url,
                raw_title=raw_title,
            )
        )

    return collection_name, tracks


def fetch_tracks(
    url: str,
    cookie_session: BrowserCookieSession,
    search_opts: dict,
    limit: int | None = None,
) -> tuple[str, list[Track], SourceType]:
    source_type = detect_source_type(url)
    if source_type is None:
        raise ValueError(
            "Unsupported URL. Paste a Spotify or YouTube playlist, album, or song link."
        )

    if source_type == SourceType.SPOTIFY_PLAYLIST:
        name, tracks = fetch_spotify_playlist(url)
    elif source_type == SourceType.SPOTIFY_ALBUM:
        name, tracks = fetch_spotify_album(url)
    elif source_type == SourceType.SPOTIFY_TRACK:
        name, tracks = fetch_spotify_track(url)
    else:
        name, tracks = fetch_youtube(url, cookie_session, search_opts, limit)

    if not tracks:
        raise ValueError("No tracks found for that URL.")

    return name, tracks, source_type


def track_filename_stem(track: Track, *, compilation: bool = False) -> str:
    """Filename base: song title only (no artist / official-video junk).

    Compilations prefix the performer instead: one folder holds many artists,
    so bare titles are ambiguous and same-named songs would collide.
    """
    title = clean_song_title(track.title, artist=track.artists)
    artist = (track.artists or "").strip()
    if compilation and artist:
        return sanitize_filename(f"{artist} - {title}")
    return sanitize_filename(title)


def track_output_path(
    output_dir: Path,
    track: Track,
    *,
    compilation: bool = False,
) -> Path:
    stem = track_filename_stem(track, compilation=compilation)
    return output_dir / f"{stem}.mp3"


def _resolve_downloaded_mp3(saved_files: list[str], expected: Path) -> Path | None:
    """Pick the MP3 yt-dlp actually wrote.

    yt-dlp applies its own filename sanitization, so the real output path may
    differ from the one we computed. Prefer a captured ``.mp3`` path; fall back
    to the expected path or any captured file that now exists.
    """
    for candidate in reversed(saved_files):
        path = Path(candidate)
        if path.suffix.lower() == ".mp3" and path.exists():
            return path
    if expected.exists():
        return expected
    for candidate in reversed(saved_files):
        mp3 = Path(candidate).with_suffix(".mp3")
        if mp3.exists():
            return mp3
    return None


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def build_search_options(
    logger: object | None = None,
    verbose: bool = False,
    strategy: DownloadStrategy | None = None,
) -> dict:
    options: dict = {
        "quiet": not verbose,
        "no_warnings": not verbose,
        "verbose": verbose,
        "ignoreerrors": True,
        "extractor_retries": 3,
        "noprogress": True,
        "logger": logger if logger is not None else _QuietLogger(),
    }

    if strategy is not None and strategy.player_clients:
        options["extractor_args"] = {
            "youtube": {"player_client": list(strategy.player_clients)},
        }

    js_runtimes = build_js_runtimes()
    if js_runtimes:
        options["js_runtimes"] = js_runtimes

    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget

        options["impersonate"] = ImpersonateTarget.from_str("chrome")
    except ImportError:
        pass

    return options


def build_download_options(
    ffmpeg: str,
    outtmpl: str,
    keep_original: bool,
    *,
    embed_metadata: bool,
    logger: object | None = None,
    verbose: bool = False,
    strategy: DownloadStrategy | None = None,
) -> dict:
    options = build_options(
        Path("."),
        ffmpeg,
        keep_original,
        None,
        outtmpl=outtmpl,
        noplaylist=True,
        logger=logger,
        verbose=verbose,
        strategy=strategy,
    )
    if embed_metadata:
        options["writethumbnail"] = True
        options["embedthumbnail"] = True
        options["postprocessors"] = [
            *options["postprocessors"],
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ]
    return options


def _is_official_youtube_title(title: str | None) -> bool:
    if not title:
        return False
    lowered = title.lower()
    return any(
        marker in lowered
        for marker in (
            "official video",
            "official audio",
            "official music video",
            "official lyric",
        )
    )


def find_matching_youtube_results(
    cookie_session: BrowserCookieSession,
    track: Track,
    search_opts: dict,
    *,
    tolerance_seconds: float | None = DURATION_TOLERANCE_SECONDS,
    max_results: int = YOUTUBE_SEARCH_RESULTS,
) -> tuple[list[dict], list[dict]]:
    """Return (eligible, all_entries).

    ``eligible`` are search results whose duration is within ``tolerance_seconds``
    of the Spotify track, in search-rank order, so callers can try the next one
    when a download fails. Pass ``tolerance_seconds=None`` to accept any result
    that has a duration (loose match for remediation).

    Search is retried across the strategy ladder: a blocked player client returns
    zero results, which would otherwise fail every track in the album.
    """
    query = f"ytsearch{max_results}:{track.youtube_search_query}"
    entries: list[dict] = []
    for attempt, strategy in enumerate(strategy_ladder()):
        opts = (
            search_opts
            if attempt == 0
            else build_search_options(
                logger=search_opts.get("logger"),
                verbose=bool(search_opts.get("verbose")),
                strategy=strategy,
            )
        )
        try:
            info = cookie_session.extract_info(query, opts, download=False)
        except Exception:  # noqa: BLE001 - try the next client
            info = None
        entries = [entry for entry in (info or {}).get("entries") or [] if entry]
        if entries:
            break

    eligible: list[dict] = []
    for entry in entries:
        duration = entry.get("duration")
        if duration is None:
            if tolerance_seconds is None:
                eligible.append(entry)
            continue
        if tolerance_seconds is None or abs(
            duration - track.duration_seconds
        ) <= tolerance_seconds:
            eligible.append(entry)

    eligible.sort(
        key=lambda entry: _is_official_youtube_title(entry.get("title")),
        reverse=True,
    )

    return eligible, entries


def tag_track_file(
    mp3_path: Path,
    track: Track,
    *,
    embed_metadata: bool,
    extra_cover_urls: list[str | None] | None = None,
    playlist_track_numbers: bool = False,
    album_override: str | None = None,
    artists_override: str | None = None,
    album_artist: str | None = None,
    compilation: bool = False,
    cover_bytes: bytes | None = None,
    cover_mime: str | None = None,
) -> None:
    if not embed_metadata or not mp3_path.exists():
        return

    cover_urls: list[str | None] = []
    if not cover_bytes:
        cover_urls = [track.cover_url]
        if extra_cover_urls:
            cover_urls.extend(extra_cover_urls)

    # Prefer the artist chosen in the UI/job over YouTube uploader / source scrape.
    # Album artist defaults to that same artist when not set explicitly.
    # A compilation is the exception: one artist per track is the whole point,
    # so the job-wide artist only fills the album artist slot.
    if compilation:
        artists = track.artists
        album_artist_tag = (album_artist or "").strip() or VARIOUS_ARTISTS
    else:
        artists = (artists_override or "").strip() or track.artists
        album_artist_tag = (album_artist or "").strip() or artists

    apply_track_metadata(
        mp3_path,
        title=track.title,
        artists=artists,
        album=album_override if album_override is not None else track.album,
        album_artist=album_artist_tag,
        track_number=track.tag_track_number if playlist_track_numbers else None,
        compilation=compilation,
        cover_urls=cover_urls or None,
        cover_bytes=cover_bytes,
        cover_mime=cover_mime,
    )


def track_as_dict(track: Track) -> dict:
    """Plain-data view of a Track for the API and the review UI."""
    return {
        "index": track.index,
        "title": track.title,
        "artists": track.artists,
        "duration_ms": track.duration_ms,
        "album": track.album,
        "cover_url": track.cover_url,
        "source": track.source.value,
        "youtube_url": track.youtube_url,
        "album_track": track.album_track,
        "raw_title": track.raw_title,
    }


def track_from_dict(data: dict) -> Track:
    """Rebuild a Track the user approved, so the job downloads exactly that."""
    try:
        source = SourceType(str(data.get("source") or SourceType.SPOTIFY_PLAYLIST.value))
    except ValueError:
        source = SourceType.SPOTIFY_PLAYLIST
    album_track = data.get("album_track")
    return Track(
        index=int(data.get("index") or 1),
        title=str(data.get("title") or "").strip() or "track",
        artists=str(data.get("artists") or "").strip() or "Unknown",
        duration_ms=int(data.get("duration_ms") or 0),
        album=str(data.get("album") or ""),
        cover_url=data.get("cover_url") or None,
        source=source,
        youtube_url=data.get("youtube_url") or None,
        album_track=int(album_track) if album_track else None,
        raw_title=str(data.get("raw_title") or ""),
    )


def fetch_source_tracks(
    url: str,
    *,
    browser: str | None = None,
    limit: int | None = None,
    tracks_spec: str | None = None,
    strip_terms: str | None = None,
    compilation: bool = False,
) -> tuple[str, list[Track]]:
    """Read a playlist's songs without downloading anything.

    Deliberately free of job state and ffmpeg so the review screen works while
    a download is already running.
    """
    search_opts = build_search_options(logger=_QuietLogger(), verbose=False)
    cookie_session = BrowserCookieSession(browser, on_warn=lambda _msg: None)
    collection_name, tracks, _source = fetch_tracks(
        url,
        cookie_session,
        search_opts,
        limit=limit,
    )
    if limit is not None:
        tracks = [t for t in tracks if t.index <= limit]
    tracks, _desc = filter_tracks_by_selector(tracks, tracks_spec)

    strip_list = parse_strip_terms(strip_terms)
    if strip_list:
        tracks = apply_strip_terms(tracks, strip_list)
    if compilation:
        tracks = resolve_compilation_artists(tracks)
    return collection_name, tracks


def _prepare_download_runtime(
    *,
    browser: str | None,
    debug: bool,
    callbacks: DownloadCallbacks | None,
) -> tuple[str, dict, BrowserCookieSession, CallbackLogger] | int:
    """Return (ffmpeg, search_opts, cookie_session, logger) or an error exit code."""
    ytdlp_logger = CallbackLogger(
        lambda message: emit(message, callbacks),
        debug=debug,
    )

    def debug_log(message: str) -> None:
        if debug:
            emit(f"[debug] {message}", callbacks)

    ffmpeg = find_ffmpeg()
    debug_log(f"ffmpeg: {ffmpeg}")
    if not ffmpeg:
        emit(
            "Error: ffmpeg is required to convert audio to MP3.\n"
            "Install it with: winget install Gyan.FFmpeg",
            callbacks,
            stderr=True,
        )
        return 1

    js_runtimes = build_js_runtimes()
    debug_log(f"js runtimes: {js_runtimes}")
    if not js_runtimes:
        # Not fatal: clients like android_vr work without solving JS challenges.
        emit(
            "Warning: no JavaScript runtime found (install Node.js for best results). "
            "Falling back to clients that do not need one.",
            callbacks,
            stderr=True,
        )

    search_opts = build_search_options(logger=ytdlp_logger, verbose=debug)
    emit(
        f"yt-dlp {ytdlp_version()} · methods: "
        + ", ".join(s.name for s in strategy_ladder()),
        callbacks,
    )

    cookie_session = BrowserCookieSession(
        browser,
        on_warn=lambda message: emit(message, callbacks),
    )
    return ffmpeg, search_opts, cookie_session, ytdlp_logger


def download_single_track(
    track: Track,
    *,
    output_dir: Path,
    cookie_session: BrowserCookieSession,
    search_opts: dict,
    ffmpeg: str,
    keep_original: bool = False,
    skip_existing: bool = True,
    embed_metadata: bool = True,
    playlist_track_numbers: bool = False,
    debug: bool = False,
    callbacks: DownloadCallbacks | None = None,
    ytdlp_logger: object | None = None,
    duration_tolerance: float | None = DURATION_TOLERANCE_SECONDS,
    album_override: str | None = None,
    artists_override: str | None = None,
    album_artist: str | None = None,
    compilation: bool = False,
    cover_bytes: bytes | None = None,
    cover_mime: str | None = None,
) -> TrackDownloadResult:
    """Download and tag one track. Uses ``album_track`` for TRCK when set."""

    def debug_log(message: str) -> None:
        if debug:
            emit(f"[debug] {message}", callbacks)

    target_mp3 = track_output_path(output_dir, track, compilation=compilation)
    if skip_existing and target_mp3.exists():
        emit(f"[{track.index:03d}] duplicate, skipping: {track.title}", callbacks)
        return TrackDownloadResult("skipped", "Already downloaded")

    stem = track_filename_stem(track, compilation=compilation)
    outtmpl = str(output_dir / f"{stem}.%(ext)s")

    def run_candidate(
        video_url: str,
        ydl_opts_base: dict,
        expected_mp3: Path,
    ) -> tuple[Path | None, str]:
        saved_files: list[str] = []

        def _capture(status: dict) -> None:
            if status.get("status") != "finished":
                return
            info = status.get("info_dict") or {}
            filepath = info.get("filepath") or status.get("filename")
            if filepath:
                saved_files.append(filepath)

        def _progress(status: dict) -> None:
            if not (callbacks and callbacks.file_progress):
                return
            if status.get("status") == "downloading":
                total = status.get("total_bytes") or status.get("total_bytes_estimate")
                downloaded = status.get("downloaded_bytes") or 0
                if total:
                    callbacks.file_progress(min(downloaded / total, 1.0))
            elif status.get("status") == "finished":
                callbacks.file_progress(1.0)

        opts = dict(ydl_opts_base)
        opts["postprocessor_hooks"] = [*opts.get("postprocessor_hooks", []), _capture]
        opts["progress_hooks"] = [*opts.get("progress_hooks", []), _progress]

        # Record yt-dlp's own error text so the caller can tell a blocked player
        # client apart from a genuinely unavailable video.
        errors_seen: list[str] = []
        opts["logger"] = _ErrorCapturingLogger(opts.get("logger"), errors_seen)

        try:
            errors = cookie_session.download(video_url, opts)
        except Exception as exc:
            if debug:
                emit(traceback.format_exc(), callbacks, stderr=True)
            return None, str(exc)

        final = _resolve_downloaded_mp3(saved_files, expected_mp3)
        debug_log(f"yt-dlp return={errors}, captured={saved_files}, resolved={final}")
        if errors or final is None:
            if errors_seen:
                return None, errors_seen[-1]
            return None, f"yt-dlp returned {errors}, no MP3 produced"
        return final, ""

    candidates: list[tuple[str, str, str | None]] = []
    if track.needs_youtube_search:
        emit(
            f"[{track.index:03d}] searching: {track.youtube_search_query} "
            f"(want: {format_duration(track.duration_seconds)})",
            callbacks,
        )
        eligible, entries = find_matching_youtube_results(
            cookie_session,
            track,
            search_opts,
            tolerance_seconds=duration_tolerance,
        )
        if not eligible:
            if not entries:
                emit(
                    "  no YouTube search results (search may be blocked; "
                    "try enabling browser cookies or run with debug mode)",
                    callbacks,
                    stderr=True,
                )
                return TrackDownloadResult("failed", "No YouTube search results")
            top = entries[0]
            tol = (
                f"{int(duration_tolerance)}s"
                if duration_tolerance is not None
                else "any duration"
            )
            detail = (
                f"no result within {tol} "
                f"(Spotify: {format_duration(track.duration_seconds)}, "
                f"closest: {format_duration(top.get('duration'))}"
                + (f' "{top.get("title")}"' if top.get("title") else "")
                + ")"
            )
            emit(f"  {detail}", callbacks, stderr=True)
            return TrackDownloadResult("failed", detail)

        for entry in eligible[:MAX_DOWNLOAD_ATTEMPTS]:
            entry_url = entry.get("webpage_url") or entry.get("url")
            if entry_url:
                label = (
                    f"{entry.get('title')} "
                    f"(YouTube: {format_duration(entry.get('duration'))})"
                )
                candidates.append((entry_url, label, entry.get("thumbnail")))
    else:
        emit(f"[{track.index:03d}] downloading: {track.title}", callbacks)
        if track.youtube_url:
            candidates = [(track.youtube_url, track.title, track.cover_url)]

    if not candidates:
        emit(f"  [{track.index:03d}] failed: no download URL", callbacks, stderr=True)
        return TrackDownloadResult("failed", "No download URL")

    final_mp3: Path | None = None
    used_thumbnail: str | None = None
    last_reason = ""
    seen_urls: set[str] = set()
    total_attempts = 0
    searched_fallback = track.needs_youtube_search
    strategies = strategy_ladder()
    tried_strategies: list[str] = []

    # Outer loop = how we ask YouTube; inner loop = which upload we ask for.
    # When a client gets blocked, the next strategy retries the same uploads.
    for strat_idx, strategy in enumerate(strategies, start=1):
        if final_mp3 is not None:
            break
        if strat_idx > 1:
            emit(
                f"  retrying via “{strategy.name}” "
                f"({strat_idx}/{len(strategies)})"
                + (f" — {strategy.note}" if strategy.note else "")
                + "…",
                callbacks,
            )
            time.sleep(min(1.5 * strat_idx, 5.0))

        tried_strategies.append(strategy.name)
        strat_opts = build_download_options(
            ffmpeg,
            outtmpl,
            keep_original,
            embed_metadata=embed_metadata and track.source.value.startswith("youtube"),
            logger=ytdlp_logger,
            verbose=debug,
            strategy=strategy,
        )
        pending = list(candidates)

        while pending and total_attempts < MAX_TOTAL_ATTEMPTS:
            if callbacks and callbacks.should_cancel and callbacks.should_cancel():
                emit("Download cancelled.", callbacks, stderr=True)
                return TrackDownloadResult("cancelled", "Cancelled")
            video_url, label, thumbnail = pending.pop(0)
            if strat_idx == 1:
                if video_url in seen_urls:
                    continue
                seen_urls.add(video_url)
            total_attempts += 1
            emit(f"  attempt {total_attempts} [{strategy.name}]: {label}", callbacks)
            debug_log(f"downloading from: {video_url} via {strategy.name}")
            final_mp3, reason = run_candidate(video_url, strat_opts, target_mp3)
            if final_mp3 is not None:
                used_thumbnail = thumbnail
                emit(f"  source: {strategy.name}", callbacks)
                break
            last_reason = reason
            emit(
                f"  attempt {total_attempts} failed [{strategy.name}]: {reason}",
                callbacks,
                stderr=True,
            )

            if not searched_fallback and not track.needs_youtube_search:
                searched_fallback = True
                emit("  searching for another YouTube upload…", callbacks)
                eligible, _ = find_matching_youtube_results(
                    cookie_session,
                    track,
                    search_opts,
                    tolerance_seconds=duration_tolerance,
                )
                for entry in eligible[:MAX_DOWNLOAD_ATTEMPTS]:
                    entry_url = entry.get("webpage_url") or entry.get("url")
                    if not entry_url or entry_url in seen_urls:
                        continue
                    alt_label = (
                        f"{entry.get('title')} "
                        f"(YouTube: {format_duration(entry.get('duration'))})"
                    )
                    alt = (entry_url, alt_label, entry.get("thumbnail"))
                    pending.append(alt)
                    candidates.append(alt)

            # A blocked client fails identically for every upload — stop
            # burning attempts and move straight to the next method.
            if _is_client_block(last_reason) and strat_idx < len(strategies):
                emit(
                    f"  “{strategy.name}” looks blocked by YouTube — switching method…",
                    callbacks,
                )
                break

        if total_attempts >= MAX_TOTAL_ATTEMPTS:
            break

    if final_mp3 is None:
        ways = ", ".join(tried_strategies) or "none"
        emit(
            f"  [{track.index:03d}] failed after {total_attempts} attempt(s) "
            f"across {len(tried_strategies)} method(s): {track.title}",
            callbacks,
            stderr=True,
        )
        if last_reason:
            emit(f"  last error: {last_reason}", callbacks, stderr=True)
        emit(f"  methods tried: {ways} (yt-dlp {ytdlp_version()})", callbacks, stderr=True)
        return TrackDownloadResult(
            "failed",
            f"Failed after {total_attempts} attempt(s) via {ways}",
        )

    if embed_metadata:
        try:
            tag_track_file(
                final_mp3,
                track,
                embed_metadata=True,
                extra_cover_urls=[used_thumbnail],
                playlist_track_numbers=playlist_track_numbers,
                album_override=album_override,
                artists_override=artists_override,
                album_artist=album_artist,
                compilation=compilation,
                cover_bytes=cover_bytes,
                cover_mime=cover_mime,
            )
            emit(
                f"  [{track.index:03d}] saved with metadata: {final_mp3.name}",
                callbacks,
            )
            return TrackDownloadResult("done", final_mp3.name)
        except Exception as exc:
            emit(
                f"  [{track.index:03d}] saved (metadata failed: {exc}): {final_mp3.name}",
                callbacks,
            )
            if debug:
                emit(traceback.format_exc(), callbacks, stderr=True)
            try:
                ensure_cover_art(final_mp3, [track.cover_url, used_thumbnail])
            except Exception:
                pass
            return TrackDownloadResult("done", final_mp3.name)

    emit(f"  [{track.index:03d}] saved: {final_mp3.name}", callbacks)
    return TrackDownloadResult("done", final_mp3.name)


def download_url(
    url: str,
    output_dir: Path,
    keep_original: bool = False,
    browser: str | None = None,
    skip_existing: bool = True,
    limit: int | None = None,
    tracks_spec: str | None = None,
    embed_metadata: bool = True,
    playlist_track_numbers: bool = False,
    debug: bool = False,
    callbacks: DownloadCallbacks | None = None,
    album_override: str | None = None,
    artists_override: str | None = None,
    album_artist: str | None = None,
    compilation: bool = False,
    strip_terms: str | None = None,
    verify_artists: bool = False,
    track_plan: list[dict] | None = None,
    cover_bytes: bytes | None = None,
    cover_mime: str | None = None,
) -> int:
    prepared = _prepare_download_runtime(
        browser=browser, debug=debug, callbacks=callbacks
    )
    if isinstance(prepared, int):
        return prepared
    ffmpeg, search_opts, cookie_session, ytdlp_logger = prepared

    # An approved plan is the user's decision about what this album contains;
    # re-reading the playlist could quietly download something else.
    if track_plan:
        tracks = [track_from_dict(row) for row in track_plan]
        collection_name = album_override or (tracks[0].album if tracks else "Album")
        source_type = tracks[0].source if tracks else SourceType.YOUTUBE_PLAYLIST
        emit(f"Using reviewed tracklist: {len(tracks)} song(s)", callbacks)
        return _download_tracks(
            tracks=tracks,
            collection_name=collection_name,
            source_type=source_type,
            selector_desc="",
            tracks_spec=None,
            output_dir=output_dir,
            cookie_session=cookie_session,
            search_opts=search_opts,
            ffmpeg=ffmpeg,
            ytdlp_logger=ytdlp_logger,
            keep_original=keep_original,
            skip_existing=skip_existing,
            embed_metadata=embed_metadata,
            playlist_track_numbers=playlist_track_numbers,
            debug=debug,
            callbacks=callbacks,
            browser=browser,
            album_override=album_override,
            artists_override=artists_override,
            album_artist=album_artist,
            compilation=compilation,
            strip_list=[],
            cover_bytes=cover_bytes,
            cover_mime=cover_mime,
            preserve_track_numbers=True,
        )

    try:
        fetch_limit = selector_playlist_end(tracks_spec, limit)
        collection_name, tracks, source_type = fetch_tracks(
            url, cookie_session, search_opts, fetch_limit
        )
    except ValueError as exc:
        emit(f"Error: {exc}", callbacks, stderr=True)
        return 1
    except Exception as exc:
        emit(f"Error reading URL: {exc}", callbacks, stderr=True)
        if debug:
            emit(traceback.format_exc(), callbacks, stderr=True)
        return 1

    if limit is not None:
        tracks = [t for t in tracks if t.index <= limit]

    try:
        tracks, selector_desc = filter_tracks_by_selector(tracks, tracks_spec)
    except ValueError as exc:
        emit(f"Error: {exc}", callbacks, stderr=True)
        return 1

    if not tracks:
        emit("Error: track filter selected 0 songs.", callbacks, stderr=True)
        return 1

    # Clean titles before anything reads them: search queries, tags, filenames.
    strip_list = parse_strip_terms(strip_terms)
    if strip_list:
        tracks = apply_strip_terms(tracks, strip_list)

    if compilation:
        # Resolve before on_tracks so the UI lists the real performers.
        tracks = resolve_compilation_artists(tracks)
        if verify_artists:
            tracks = verify_track_artists(tracks, callbacks)

    return _download_tracks(
        tracks=tracks,
        collection_name=collection_name,
        source_type=source_type,
        selector_desc=selector_desc,
        tracks_spec=tracks_spec,
        output_dir=output_dir,
        cookie_session=cookie_session,
        search_opts=search_opts,
        ffmpeg=ffmpeg,
        ytdlp_logger=ytdlp_logger,
        keep_original=keep_original,
        skip_existing=skip_existing,
        embed_metadata=embed_metadata,
        playlist_track_numbers=playlist_track_numbers,
        debug=debug,
        callbacks=callbacks,
        browser=browser,
        album_override=album_override,
        artists_override=artists_override,
        album_artist=album_artist,
        compilation=compilation,
        strip_list=strip_list,
        cover_bytes=cover_bytes,
        cover_mime=cover_mime,
    )


def _download_tracks(
    *,
    tracks: list[Track],
    collection_name: str,
    source_type: SourceType,
    selector_desc: str,
    tracks_spec: str | None,
    output_dir: Path,
    cookie_session: BrowserCookieSession,
    search_opts: dict,
    ffmpeg: str,
    ytdlp_logger: CallbackLogger,
    keep_original: bool,
    skip_existing: bool,
    embed_metadata: bool,
    playlist_track_numbers: bool,
    debug: bool,
    callbacks: DownloadCallbacks | None,
    browser: str | None,
    album_override: str | None,
    artists_override: str | None,
    album_artist: str | None,
    compilation: bool,
    strip_list: list[str],
    cover_bytes: bytes | None,
    cover_mime: str | None,
    preserve_track_numbers: bool = False,
) -> int:
    """Download an already-decided list of songs."""
    if callbacks and callbacks.on_tracks:
        callbacks.on_tracks(list(tracks))

    output_dir.mkdir(parents=True, exist_ok=True)

    emit(f"Source: {source_type.value.replace('_', ' ')}", callbacks)
    emit(f"Collection: {collection_name}", callbacks)
    emit(f"Tracks: {len(tracks)}", callbacks)
    if tracks_spec and tracks_spec.strip():
        emit(f"Track filter: {selector_desc}", callbacks)
    emit(f"Saving MP3s to: {output_dir.resolve()}", callbacks)
    if cookie_session.browser:
        emit(f"Using cookies from: {cookie_session.browser}", callbacks)
    elif browser:
        emit("Browser cookies unavailable; downloading without cookies.", callbacks)
    if embed_metadata:
        emit("Metadata: artist, album, track number, cover art", callbacks)
    if compilation:
        emit(
            f"Compilation: per-track artists kept, album artist "
            f"'{(album_artist or '').strip() or VARIOUS_ARTISTS}'",
            callbacks,
        )
    if strip_list:
        emit(f"Removing from titles: {', '.join(strip_list)}", callbacks)
    if playlist_track_numbers:
        emit(
            "Track numbers: sequential album order (excluded/skipped songs leave no gap)",
            callbacks,
        )
    emit("", callbacks)

    failed = 0
    skipped = 0
    completed = 0
    album_pos = 0

    for track in tracks:
        if callbacks and callbacks.should_cancel and callbacks.should_cancel():
            emit("Download cancelled.", callbacks, stderr=True)
            return 1

        if callbacks and callbacks.should_skip_track and callbacks.should_skip_track(
            track.index
        ):
            emit(f"[{track.index:03d}] user skipped: {track.title}", callbacks)
            skipped += 1
            completed += 1
            if callbacks.progress:
                callbacks.progress(completed, len(tracks))
            continue

        if callbacks and callbacks.progress:
            callbacks.progress(completed, len(tracks))

        # Assign sequential album #s so exclusions (e.g. !4) don't leave TRCK gaps.
        # A reviewed tracklist keeps the album's real numbering instead, since
        # the user already saw which songs are missing.
        work = track
        if playlist_track_numbers:
            album_pos += 1
            if not (preserve_track_numbers and track.album_track):
                work = replace(track, album_track=album_pos)
            if callbacks and callbacks.on_track_model:
                callbacks.on_track_model(work)

        result = download_single_track(
            work,
            output_dir=output_dir,
            cookie_session=cookie_session,
            search_opts=search_opts,
            ffmpeg=ffmpeg,
            keep_original=keep_original,
            skip_existing=skip_existing,
            embed_metadata=embed_metadata,
            playlist_track_numbers=playlist_track_numbers,
            debug=debug,
            callbacks=callbacks,
            ytdlp_logger=ytdlp_logger,
            album_override=album_override,
            artists_override=artists_override,
            album_artist=album_artist,
            compilation=compilation,
            cover_bytes=cover_bytes,
            cover_mime=cover_mime,
        )
        if result.status == "cancelled":
            return 1
        if result.status == "skipped":
            skipped += 1
        elif result.status == "failed":
            failed += 1
        completed += 1

    if callbacks and callbacks.progress:
        callbacks.progress(completed, len(tracks))

    emit("", callbacks)
    if failed:
        emit(
            f"Finished with {failed} failed track(s), {skipped} skipped.",
            callbacks,
            stderr=True,
        )
        if not debug:
            emit(
                "Tip: enable Debug mode to see the exact yt-dlp error for each failure.",
                callbacks,
                stderr=True,
            )
        if not cookie_session.browser:
            emit(
                "Tip: if you see HTTP 403 errors, try browser cookies "
                "(close the browser first if Windows reports a DPAPI error).",
                callbacks,
                stderr=True,
            )
        return 1

    emit(
        f"Done. Downloaded {len(tracks) - skipped - failed} track(s), skipped {skipped}.",
        callbacks,
    )
    return 0


def remediate_track(
    track: Track,
    output_dir: Path,
    *,
    browser: str | None = None,
    embed_metadata: bool = True,
    playlist_track_numbers: bool = False,
    debug: bool = False,
    youtube_url: str | None = None,
    loose_match: bool = False,
    callbacks: DownloadCallbacks | None = None,
    album_override: str | None = None,
    artists_override: str | None = None,
    album_artist: str | None = None,
    compilation: bool = False,
    cover_bytes: bytes | None = None,
    cover_mime: str | None = None,
) -> TrackDownloadResult:
    """Re-download one failed track, keeping its album track # when set."""
    prepared = _prepare_download_runtime(
        browser=browser, debug=debug, callbacks=callbacks
    )
    if isinstance(prepared, int):
        return TrackDownloadResult("failed", "Download environment not ready")
    ffmpeg, search_opts, cookie_session, ytdlp_logger = prepared

    work = track
    if youtube_url:
        if not YOUTUBE_VIDEO_RE.search(youtube_url):
            emit("Error: that does not look like a YouTube video URL.", callbacks, stderr=True)
            return TrackDownloadResult("failed", "Invalid YouTube URL")
        work = replace(track, youtube_url=youtube_url.strip())

    output_dir.mkdir(parents=True, exist_ok=True)
    number_note = ""
    if playlist_track_numbers:
        number_note = f" (album # {work.tag_track_number})"
    emit(
        f"Remediating track #{track.index}: {track.title}{number_note}",
        callbacks,
    )

    return download_single_track(
        work,
        output_dir=output_dir,
        cookie_session=cookie_session,
        search_opts=search_opts,
        ffmpeg=ffmpeg,
        keep_original=False,
        skip_existing=False,
        embed_metadata=embed_metadata,
        playlist_track_numbers=playlist_track_numbers,
        debug=debug,
        callbacks=callbacks,
        ytdlp_logger=ytdlp_logger,
        duration_tolerance=None if loose_match else DURATION_TOLERANCE_SECONDS,
        album_override=album_override,
        artists_override=artists_override,
        album_artist=album_artist,
        compilation=compilation,
        cover_bytes=cover_bytes,
        cover_mime=cover_mime,
    )


def replace_track_source(
    target_mp3: Path,
    youtube_url: str,
    *,
    browser: str | None = None,
    debug: bool = False,
    callbacks: DownloadCallbacks | None = None,
) -> TrackDownloadResult:
    """Swap a library track's audio for a pasted YouTube video, in place.

    Used to fix a wrong or low-quality match after the fact without losing
    the tags already on the file (the caller re-applies them — this only
    replaces the audio). Downloads into an isolated temp folder rather than
    ``target_mp3``'s own directory: the download's computed filename comes
    from a throwaway title, not the file's real name, so it must never be
    confused with a sibling track when a name collision is possible.
    """
    if not YOUTUBE_VIDEO_RE.search(youtube_url or ""):
        emit("Error: that does not look like a YouTube video URL.", callbacks, stderr=True)
        return TrackDownloadResult("failed", "Invalid YouTube URL")

    prepared = _prepare_download_runtime(browser=browser, debug=debug, callbacks=callbacks)
    if isinstance(prepared, int):
        return TrackDownloadResult("failed", "Download environment not ready")
    ffmpeg, search_opts, cookie_session, ytdlp_logger = prepared

    tmp_dir = Path(tempfile.mkdtemp(prefix="md-replace-"))
    try:
        placeholder = Track(
            index=1,
            title="replacement audio",
            artists="",
            duration_ms=0,
            source=SourceType.YOUTUBE_VIDEO,
            youtube_url=youtube_url.strip(),
        )
        result = download_single_track(
            placeholder,
            output_dir=tmp_dir,
            cookie_session=cookie_session,
            search_opts=search_opts,
            ffmpeg=ffmpeg,
            keep_original=False,
            skip_existing=False,
            embed_metadata=False,
            debug=debug,
            callbacks=callbacks,
            ytdlp_logger=ytdlp_logger,
            duration_tolerance=None,
        )
        if result.status != "done":
            return result

        produced = next(tmp_dir.glob("*.mp3"), None)
        if not produced:
            return TrackDownloadResult("failed", "Download did not produce an MP3")

        target_mp3.parent.mkdir(parents=True, exist_ok=True)
        produced.replace(target_mp3)
        emit(f"Replaced audio: {target_mp3.name}", callbacks)
        return TrackDownloadResult("done", target_mp3.name)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download Spotify or YouTube playlists and songs as MP3 files.",
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="Spotify or YouTube playlist/album/song URL",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("downloads"),
        help="Output folder (default: downloads)",
    )
    parser.add_argument(
        "-k",
        "--keep-original",
        action="store_true",
        help="Keep the downloaded audio file after converting to MP3",
    )
    parser.add_argument(
        "--browser",
        choices=SUPPORTED_BROWSERS,
        help="Load YouTube cookies from this browser",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not load cookies from a browser",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download tracks even if the MP3 already exists",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Only consider the first N playlist tracks",
    )
    parser.add_argument(
        "--tracks",
        metavar="SPEC",
        help=(
            "Which playlist #s to download. "
            "Examples: '1-10', '!3-5' (skip 3–5), '1-20 !3-5,!8'"
        ),
    )
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="Do not embed artist, album, or cover art tags",
    )
    parser.add_argument(
        "--playlist-track-numbers",
        action="store_true",
        help="Set each track's track number to its order in the playlist",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show verbose yt-dlp output and full error tracebacks",
    )
    args = parser.parse_args()

    url = args.url or input("Paste Spotify or YouTube URL: ").strip()
    if not url:
        print("Error: no URL provided.", file=sys.stderr)
        return 1

    return download_url(
        url=url,
        output_dir=args.output,
        keep_original=args.keep_original,
        browser=resolve_browser(args.browser, args.no_browser),
        skip_existing=not args.force,
        limit=args.limit,
        tracks_spec=args.tracks,
        embed_metadata=not args.no_metadata,
        playlist_track_numbers=args.playlist_track_numbers,
        debug=args.debug,
    )


if __name__ == "__main__":
    raise SystemExit(main())
