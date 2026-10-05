"""Find the YouTube playlist that best matches a catalogued album.

The album's official tracklist (see album_match) is the yardstick: each
candidate playlist found on YouTube is lined up against it, so a playlist that
really contains the album's songs outranks one that only has a similar name.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote_plus

import yt_dlp

import album_match as matcher
from music_downloader import build_search_options
from text_norm import romanize_variants

# YouTube's "Type: Playlist" search filter (the sp value is double-encoded
# because yt-dlp passes the URL through as written).
PLAYLIST_FILTER = "&sp=EgIQAw%253D%253D"
SEARCH_RESULTS_PER_QUERY = 10
MAX_CANDIDATES = 14
# Long mixes and radio playlists are never an album; skip them before probing.
MAX_PLAYLIST_ENTRIES = 150
# A playlist that lines up with fewer than this share of the album is noise...
MIN_COVERAGE = 0.3
# ...unless its name is a strong match for the album, in which case a smaller
# share of songs is still worth offering (the user can match the rest by hand).
NAME_ONLY_SIMILARITY = 0.7
NAME_ONLY_COVERAGE = 0.15
MAX_RESULTS = 6


def _flat_opts(limit: int) -> dict[str, Any]:
    opts = build_search_options()
    opts["extract_flat"] = True
    opts["playlistend"] = limit
    return opts


def _queries(album: matcher.AlbumRef) -> list[str]:
    various = album.is_various_artists
    artist = "" if various else album.artist
    # "(Deluxe Edition)" and similar suffixes are rarely in a playlist's title.
    plain = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", album.name).strip() or album.name
    # Keep the words inside brackets ("Persona 5 (Original Soundtrack)" ->
    # "Persona 5 Original Soundtrack"): that is how uploads usually title it.
    flat = " ".join(re.sub(r"[()\[\]]", " ", album.name).split())
    raw = [
        f"{artist} {album.name} full album",
        f"{flat} full album",
        f"{artist} {plain} album",
        f"{flat}",
        f"{artist} {plain}",
    ]
    seen: set[str] = set()
    out: list[str] = []
    for query in raw:
        cleaned = " ".join(query.split())
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            out.append(cleaned)
    return out


def _search_playlists(query: str) -> list[dict[str, Any]]:
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}{PLAYLIST_FILTER}"
    try:
        with yt_dlp.YoutubeDL(_flat_opts(SEARCH_RESULTS_PER_QUERY)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:  # noqa: BLE001 - another query may still find something
        return []
    found: list[dict[str, Any]] = []
    for entry in (info or {}).get("entries") or []:
        link = str((entry or {}).get("url") or "")
        if "list=" in link:
            found.append(entry)
    return found


_WORD_RE = re.compile(r"[a-z0-9]+")
# Words that say what kind of upload it is, not which album: they should not
# count for or against a name match.
_GENERIC_WORDS = frozenset(
    {"the", "a", "an", "of", "and", "album", "full", "deluxe", "edition",
     "version", "hq", "hd", "official", "playlist", "complete", "remastered"}
)


def _words(text: str) -> set[str]:
    # Every Latin reading counts, so a Japanese title still shares words with a
    # romanised album name.
    words: set[str] = set()
    for reading in romanize_variants(text):
        words |= set(_WORD_RE.findall(reading.lower()))
    if "ost" in words:  # "OST" is how uploads abbreviate "Original Soundtrack"
        words |= {"original", "soundtrack"}
    return words


def _name_similarity(
    album: matcher.AlbumRef,
    playlist_title: str,
    uploader: str,
) -> float:
    """0..1: how much the playlist's title reads like this album's name.

    Share of the album name's meaningful words found in the title, nudged up
    when the artist is named too (in the title or as the uploader/topic).
    """
    wanted = _words(album.name) - _GENERIC_WORDS or _words(album.name)
    if not wanted:
        return 0.0
    title_words = _words(playlist_title)
    overlap = len(wanted & title_words) / len(wanted)
    artist_hit = 0.0
    if not album.is_various_artists:
        artist_words = _words(album.artist) - _GENERIC_WORDS
        if artist_words and artist_words <= (title_words | _words(uploader)):
            artist_hit = 1.0
    return min(1.0, 0.8 * overlap + 0.2 * artist_hit)


def _score_candidate(
    album: matcher.AlbumRef,
    entry: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        with yt_dlp.YoutubeDL(_flat_opts(MAX_PLAYLIST_ENTRIES)) as ydl:
            playlist = ydl.extract_info(str(entry["url"]), download=False)
    except Exception:  # noqa: BLE001 - private/deleted playlists just drop out
        return None

    videos = [e for e in (playlist or {}).get("entries") or [] if e]
    if not videos:
        return None
    sources = [
        {
            "title": str(v.get("title") or ""),
            "duration_ms": int((v.get("duration") or 0) * 1000),
        }
        for v in videos
    ]
    rows = matcher.match_playlist(album, sources)
    matched = sum(1 for row in rows if row["kind"] == "matched")
    total = len(album.tracks) or 1
    coverage = matched / total
    title = str(entry.get("title") or playlist.get("title") or "Untitled playlist")
    uploader = str(entry.get("uploader") or entry.get("channel") or "")
    name_sim = _name_similarity(album, title, uploader)

    # Gaps are expected - the review step lets the user match or drop songs -
    # so a playlist is kept on either good song overlap or a name that clearly
    # says it's this album with at least some of its songs inside.
    if coverage < MIN_COVERAGE and not (
        name_sim >= NAME_ONLY_SIMILARITY and coverage >= NAME_ONLY_COVERAGE
    ):
        return None
    precision = matched / len(videos)
    missing = [row["album_track"]["title"] for row in rows if row["kind"] == "missing"]
    return {
        "url": str(entry["url"]),
        "title": title,
        "uploader": uploader,
        "videos": len(videos),
        "matched": matched,
        "album_tracks": len(album.tracks),
        "missing": len(missing),
        "missing_titles": missing[:12],
        "extra": sum(1 for row in rows if row["kind"] == "extra"),
        "coverage": round(coverage, 3),
        "name_match": round(name_sim, 3),
        "score": round(0.5 * coverage + 0.35 * name_sim + 0.15 * precision, 3),
    }


def find_playlists(album: matcher.AlbumRef) -> list[dict[str, Any]]:
    """Playlists that best reproduce ``album``, strongest match first."""
    if not album.tracks:
        return []

    candidates: dict[str, dict[str, Any]] = {}
    for query in _queries(album):
        for entry in _search_playlists(query):
            match = re.search(r"list=([\w-]+)", str(entry["url"]))
            if match and match.group(1) not in candidates:
                candidates[match.group(1)] = entry
        if len(candidates) >= MAX_CANDIDATES:
            break

    probes = list(candidates.values())[:MAX_CANDIDATES]
    if not probes:
        return []
    with ThreadPoolExecutor(max_workers=4) as pool:
        scored = [r for r in pool.map(lambda e: _score_candidate(album, e), probes) if r]
    scored.sort(key=lambda r: (r["score"], r["matched"]), reverse=True)
    return scored[:MAX_RESULTS]
