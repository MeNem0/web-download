#!/usr/bin/env python3
"""Download Spotify/YouTube playlists and songs as tagged MP3 files."""

from __future__ import annotations

import argparse
import re
import sys
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
    _QuietLogger,
    build_js_runtimes,
    build_options,
    find_ffmpeg,
    resolve_browser,
)
from metadata_tags import apply_track_metadata, ensure_cover_art

SPOTIFY_PLAYLIST_RE = re.compile(
    r"(?:spotify\.com/(?:intl-[a-z]{2}/)?playlist/|spotify:playlist:)([a-zA-Z0-9]+)"
)
SPOTIFY_TRACK_RE = re.compile(
    r"(?:spotify\.com/(?:intl-[a-z]{2}/)?track/|spotify:track:)([a-zA-Z0-9]+)"
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
    SPOTIFY_TRACK = "spotify_track"
    YOUTUBE_PLAYLIST = "youtube_playlist"
    YOUTUBE_VIDEO = "youtube_video"


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
        return self.youtube_url is None and self.source.value.startswith("spotify")


@dataclass
class DownloadCallbacks:
    log: Callable[[str], None] | None = None
    progress: Callable[[int, int], None] | None = None
    file_progress: Callable[[float], None] | None = None
    should_cancel: Callable[[], bool] | None = None
    on_tracks: Callable[[list[Track]], None] | None = None


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


def detect_source_type(url: str) -> SourceType | None:
    text = url.strip()
    if SPOTIFY_PLAYLIST_RE.search(text):
        return SourceType.SPOTIFY_PLAYLIST
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
    elif source_type == SourceType.SPOTIFY_TRACK:
        name, tracks = fetch_spotify_track(url)
    else:
        name, tracks = fetch_youtube(url, cookie_session, search_opts, limit)

    if not tracks:
        raise ValueError("No tracks found for that URL.")

    return name, tracks, source_type


def track_filename_stem(track: Track) -> str:
    """Filename base: song title only (no artist / official-video junk)."""
    return sanitize_filename(clean_song_title(track.title, artist=track.artists))


def track_output_path(output_dir: Path, track: Track) -> Path:
    return output_dir / f"{track_filename_stem(track)}.mp3"


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


def build_search_options(logger: object | None = None, verbose: bool = False) -> dict:
    options: dict = {
        "quiet": not verbose,
        "no_warnings": not verbose,
        "verbose": verbose,
        "ignoreerrors": True,
        "extractor_retries": 3,
        "noprogress": True,
        "logger": logger if logger is not None else _QuietLogger(),
        "extractor_args": {
            "youtube": {
                # Exclude TV client — it frequently returns DRM-only formats.
                "player_client": [
                    "default",
                    "-tv",
                    "web_safari",
                    "web_embedded",
                ],
            },
        },
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
    """
    info = cookie_session.extract_info(
        f"ytsearch{max_results}:{track.youtube_search_query}",
        search_opts,
        download=False,
    )
    entries = [entry for entry in (info or {}).get("entries") or [] if entry]

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
) -> None:
    if not embed_metadata or not mp3_path.exists():
        return

    cover_urls: list[str | None] = [track.cover_url]
    if extra_cover_urls:
        cover_urls.extend(extra_cover_urls)

    apply_track_metadata(
        mp3_path,
        title=track.title,
        artists=track.artists,
        album=track.album,
        track_number=track.index if playlist_track_numbers else None,
        cover_urls=cover_urls,
    )


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
        emit(
            "Error: a JavaScript runtime is required for YouTube downloads.\n"
            "Install Node.js from https://nodejs.org/ or run: winget install OpenJS.NodeJS.LTS",
            callbacks,
            stderr=True,
        )
        return 1

    search_opts = build_search_options(logger=ytdlp_logger, verbose=debug)
    try:
        import yt_dlp

        debug_log(f"yt-dlp version: {yt_dlp.version.__version__}")
    except Exception:  # pragma: no cover - informational only
        pass

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
) -> TrackDownloadResult:
    """Download and tag one track. Preserves ``track.index`` for playlist numbering."""

    def debug_log(message: str) -> None:
        if debug:
            emit(f"[debug] {message}", callbacks)

    target_mp3 = track_output_path(output_dir, track)
    if skip_existing and target_mp3.exists():
        emit(f"[{track.index:03d}] duplicate, skipping: {track.title}", callbacks)
        return TrackDownloadResult("skipped", "Already downloaded")

    outtmpl = str(output_dir / f"{track_filename_stem(track)}.%(ext)s")
    ydl_opts = build_download_options(
        ffmpeg,
        outtmpl,
        keep_original,
        embed_metadata=embed_metadata and track.source.value.startswith("youtube"),
        logger=ytdlp_logger,
        verbose=debug,
    )

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

        try:
            errors = cookie_session.download(video_url, opts)
        except Exception as exc:
            if debug:
                emit(traceback.format_exc(), callbacks, stderr=True)
            return None, str(exc)

        final = _resolve_downloaded_mp3(saved_files, expected_mp3)
        debug_log(f"yt-dlp return={errors}, captured={saved_files}, resolved={final}")
        if errors or final is None:
            return None, f"yt-dlp returned {errors}, no MP3 produced"
        return final, ""

    candidates: list[tuple[str, str, str | None]] = []
    if track.needs_youtube_search:
        emit(
            f"[{track.index:03d}] searching: {track.youtube_search_query} "
            f"(Spotify: {format_duration(track.duration_seconds)})",
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
        emit("  failed: no download URL", callbacks, stderr=True)
        return TrackDownloadResult("failed", "No download URL")

    final_mp3: Path | None = None
    used_thumbnail: str | None = None
    last_reason = ""
    seen_urls: set[str] = set()
    total_attempts = 0
    pending = list(candidates)
    searched_fallback = track.needs_youtube_search

    for round_idx in range(1, TRACK_RETRY_ROUNDS + 1):
        if final_mp3 is not None:
            break
        if round_idx > 1:
            emit(
                f"  retrying track (round {round_idx}/{TRACK_RETRY_ROUNDS})…",
                callbacks,
            )
            time.sleep(1.5 * round_idx)
            pending = list(candidates)
            ydl_opts = build_download_options(
                ffmpeg,
                outtmpl,
                keep_original,
                embed_metadata=embed_metadata
                and track.source.value.startswith("youtube"),
                logger=ytdlp_logger,
                verbose=debug,
            )
            ydl_opts["extractor_args"] = {
                "youtube": {
                    "player_client": [
                        "web_safari",
                        "web_embedded",
                        "mweb",
                        "-tv",
                    ],
                },
            }

        while pending and total_attempts < MAX_DOWNLOAD_ATTEMPTS * TRACK_RETRY_ROUNDS + 2:
            if callbacks and callbacks.should_cancel and callbacks.should_cancel():
                emit("Download cancelled.", callbacks, stderr=True)
                return TrackDownloadResult("cancelled", "Cancelled")
            video_url, label, thumbnail = pending.pop(0)
            if video_url in seen_urls and round_idx == 1:
                continue
            if round_idx == 1:
                seen_urls.add(video_url)
            total_attempts += 1
            emit(f"  attempt {total_attempts}: {label}", callbacks)
            debug_log(f"downloading from: {video_url}")
            if total_attempts >= 2 or "drm" in last_reason.lower():
                ydl_opts = dict(ydl_opts)
                ydl_opts["extractor_args"] = {
                    "youtube": {
                        "player_client": [
                            "web_safari",
                            "web_embedded",
                            "mweb",
                            "-tv",
                        ],
                    },
                }
            final_mp3, reason = run_candidate(video_url, ydl_opts, target_mp3)
            if final_mp3 is not None:
                used_thumbnail = thumbnail
                break
            last_reason = reason
            emit(
                f"  attempt {total_attempts} failed: {reason}",
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

        if final_mp3 is not None:
            break

    if final_mp3 is None:
        emit(
            f"  failed after {total_attempts} attempt(s): {track.title}",
            callbacks,
            stderr=True,
        )
        return TrackDownloadResult(
            "failed",
            f"Failed after {total_attempts} attempt(s)",
        )

    if embed_metadata:
        try:
            tag_track_file(
                final_mp3,
                track,
                embed_metadata=True,
                extra_cover_urls=[used_thumbnail],
                playlist_track_numbers=playlist_track_numbers,
            )
            emit(f"  saved with metadata: {final_mp3.name}", callbacks)
            return TrackDownloadResult("done", final_mp3.name)
        except Exception as exc:
            emit(f"  saved (metadata failed: {exc}): {final_mp3.name}", callbacks)
            if debug:
                emit(traceback.format_exc(), callbacks, stderr=True)
            try:
                ensure_cover_art(final_mp3, [track.cover_url, used_thumbnail])
            except Exception:
                pass
            return TrackDownloadResult("done", final_mp3.name)

    emit(f"  saved: {final_mp3.name}", callbacks)
    return TrackDownloadResult("done", final_mp3.name)


def download_url(
    url: str,
    output_dir: Path,
    keep_original: bool = False,
    browser: str | None = None,
    skip_existing: bool = True,
    limit: int | None = None,
    embed_metadata: bool = True,
    playlist_track_numbers: bool = False,
    debug: bool = False,
    callbacks: DownloadCallbacks | None = None,
) -> int:
    prepared = _prepare_download_runtime(
        browser=browser, debug=debug, callbacks=callbacks
    )
    if isinstance(prepared, int):
        return prepared
    ffmpeg, search_opts, cookie_session, ytdlp_logger = prepared

    try:
        collection_name, tracks, source_type = fetch_tracks(
            url, cookie_session, search_opts, limit
        )
    except Exception as exc:
        emit(f"Error reading URL: {exc}", callbacks, stderr=True)
        if debug:
            emit(traceback.format_exc(), callbacks, stderr=True)
        return 1

    if limit is not None:
        tracks = tracks[:limit]

    if callbacks and callbacks.on_tracks:
        callbacks.on_tracks(list(tracks))

    output_dir.mkdir(parents=True, exist_ok=True)

    emit(f"Source: {source_type.value.replace('_', ' ')}", callbacks)
    emit(f"Collection: {collection_name}", callbacks)
    emit(f"Tracks: {len(tracks)}", callbacks)
    emit(f"Saving MP3s to: {output_dir.resolve()}", callbacks)
    if cookie_session.browser:
        emit(f"Using cookies from: {cookie_session.browser}", callbacks)
    elif browser:
        emit("Browser cookies unavailable; downloading without cookies.", callbacks)
    if embed_metadata:
        emit("Metadata: artist, album, track number, cover art", callbacks)
    if playlist_track_numbers:
        emit("Track numbers: playlist order", callbacks)
    emit("", callbacks)

    failed = 0
    skipped = 0
    completed = 0

    for track in tracks:
        if callbacks and callbacks.should_cancel and callbacks.should_cancel():
            emit("Download cancelled.", callbacks, stderr=True)
            return 1

        if callbacks and callbacks.progress:
            callbacks.progress(completed, len(tracks))

        result = download_single_track(
            track,
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
) -> TrackDownloadResult:
    """Re-download one failed track, keeping its original playlist index/track #."""
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
    emit(
        f"Remediating track #{track.index}: {track.title}"
        + (f" (playlist # preserved)" if playlist_track_numbers else ""),
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
    )


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
        help="Only download the first N tracks",
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
        embed_metadata=not args.no_metadata,
        playlist_track_numbers=args.playlist_track_numbers,
        debug=args.debug,
    )


if __name__ == "__main__":
    raise SystemExit(main())
