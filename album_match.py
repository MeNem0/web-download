"""Look up an album's official tracklist and line a playlist up against it.

A YouTube playlist is an unreliable stand-in for an album: songs go missing,
appear twice, arrive out of order, and carry titles like "(Official Video)".
Comparing it against the catalogued release makes those problems visible before
anything is downloaded, and supplies the real per-track artist for compilations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from artist_lookup import title_similarity

SEARCH_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"
REQUEST_TIMEOUT = 15.0
# An album can legitimately be long (box sets, soundtracks).
MAX_ALBUM_TRACKS = 300
# Below this a source song is not the album track, just vaguely similar.
MIN_MATCH_SCORE = 0.62
# Music videos carry intros, so allow drift before runtime counts against a match.
DURATION_GRACE_MS = 25_000


@dataclass(frozen=True)
class AlbumTrack:
    number: int
    title: str
    artist: str
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "artist": self.artist,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class AlbumRef:
    id: str
    name: str
    artist: str
    year: str = ""
    artwork_url: str = ""
    track_count: int = 0
    tracks: tuple[AlbumTrack, ...] = field(default_factory=tuple)

    @property
    def is_various_artists(self) -> bool:
        """Whether the catalogue calls this a compilation.

        Counting distinct track artists would be wrong: an ordinary album
        credits its featured guests per track without being a compilation.
        """
        return "various" in self.artist.lower()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "artist": self.artist,
            "year": self.year,
            "artwork_url": self.artwork_url,
            "track_count": self.track_count or len(self.tracks),
            "various_artists": self.is_various_artists,
            "tracks": [t.as_dict() for t in self.tracks],
        }


def _upgrade_artwork(url: str) -> str:
    import re

    return re.sub(r"\d+x\d+bb", "600x600bb", (url or "").strip(), count=1)


def _get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.json()


def search_albums(query: str, *, limit: int = 8) -> list[AlbumRef]:
    """Album candidates for a name, without their tracklists."""
    term = (query or "").strip()
    if len(term) < 2:
        return []
    payload = _get(
        SEARCH_URL,
        {"term": term, "media": "music", "entity": "album", "limit": max(1, min(limit, 25))},
    )
    out: list[AlbumRef] = []
    for item in payload.get("results") or []:
        collection_id = str(item.get("collectionId") or "").strip()
        name = str(item.get("collectionName") or "").strip()
        if not collection_id or not name:
            continue
        out.append(
            AlbumRef(
                id=collection_id,
                name=name,
                artist=str(item.get("artistName") or "").strip(),
                year=str(item.get("releaseDate") or "")[:4],
                artwork_url=_upgrade_artwork(str(item.get("artworkUrl100") or "")),
                track_count=int(item.get("trackCount") or 0),
            )
        )
    return out


def fetch_album(collection_id: str) -> AlbumRef | None:
    """Full album with its official tracklist."""
    ident = str(collection_id or "").strip()
    if not ident:
        return None
    payload = _get(
        LOOKUP_URL,
        {"id": ident, "entity": "song", "limit": MAX_ALBUM_TRACKS},
    )
    results = payload.get("results") or []
    if not results:
        return None

    header: dict[str, Any] | None = None
    tracks: list[AlbumTrack] = []
    for item in results:
        if item.get("wrapperType") == "collection" or item.get("collectionType"):
            header = item
            continue
        if item.get("wrapperType") == "track" or item.get("kind") == "song":
            title = str(item.get("trackName") or "").strip()
            if not title:
                continue
            tracks.append(
                AlbumTrack(
                    number=int(item.get("trackNumber") or len(tracks) + 1),
                    title=title,
                    artist=str(item.get("artistName") or "").strip(),
                    duration_ms=int(item.get("trackTimeMillis") or 0),
                )
            )
    if header is None and not tracks:
        return None

    header = header or {}
    tracks.sort(key=lambda t: t.number)
    return AlbumRef(
        id=ident,
        name=str(header.get("collectionName") or "").strip(),
        artist=str(header.get("artistName") or "").strip(),
        year=str(header.get("releaseDate") or "")[:4],
        artwork_url=_upgrade_artwork(str(header.get("artworkUrl100") or "")),
        track_count=int(header.get("trackCount") or len(tracks)),
        tracks=tuple(tracks),
    )


# Fetching a tracklist costs a request each, so only probe the top few.
MAX_ALBUM_PROBES = 4

# Re-recorded knock-offs copy a famous album's name exactly, so they beat the
# real release on title alone — the real one usually carries a suffix like
# "(Original Motion Picture Soundtrack)". Their giveaway is the performer.
DERIVATIVE_HINTS = (
    "karaoke",
    "tribute",
    "cover band",
    "covers",
    "piano",
    "instrumental",
    "lullaby",
    "8-bit",
    "8 bit",
    "string quartet",
    "made famous",
    "hit crew",
    "renditions",
    "music box",
    "workout",
    "meditation",
    "sleep",
    "study",
)
# Each step down iTunes' own relevance ranking costs this much, which settles
# near-ties in favour of the release people actually mean.
RANK_PENALTY = 0.03


def _is_single(candidate: AlbumRef) -> bool:
    """A one-track single or EP, which no album playlist should match."""
    name = candidate.name.lower()
    if name.endswith("- single") or name.endswith("- ep"):
        return True
    return candidate.track_count == 1


def _is_derivative(candidate: AlbumRef, query: str) -> bool:
    """A re-recording pretending to be the real album."""
    haystack = f"{candidate.artist} {candidate.name}".lower()
    asked = query.lower()
    return any(hint in haystack and hint not in asked for hint in DERIVATIVE_HINTS)


def _candidate_score(
    name: str,
    candidate: AlbumRef,
    hint: str,
    various: bool,
    rank: int = 0,
) -> float:
    score = title_similarity(name, candidate.name)
    if hint and title_similarity(hint, candidate.artist) > 0.8:
        score += 0.15
    if various and "various" in candidate.artist.lower():
        score += 0.15
    if _is_derivative(candidate, f"{hint} {name}"):
        score -= 0.6
    return score - RANK_PENALTY * rank


def find_album(
    album: str,
    artist: str = "",
    *,
    collection_id: str = "",
    various: bool = False,
) -> AlbumRef | None:
    """Best catalogued release for a name, with its tracklist.

    Tries several candidates because the closest name is often a knock-off
    compilation with no listed songs.
    """
    if collection_id:
        found = fetch_album(collection_id)
        return found if found and found.tracks else found

    name = (album or "").strip()
    if not name:
        return None
    hint = (artist or "").strip()
    # "Various Artists" is a placeholder, not a searchable performer.
    if "various" in hint.lower():
        various = True
        hint = ""

    candidates = search_albums(f"{hint} {name}".strip(), limit=15)
    if not candidates and hint:
        candidates = search_albums(name, limit=15)
    if not candidates:
        return None

    asked_single = "single" in name.lower() or " ep" in name.lower()
    if not asked_single:
        candidates = [c for c in candidates if not _is_single(c)] or candidates

    scored = [
        (_candidate_score(name, c, hint, various, rank), c)
        for rank, c in enumerate(candidates)
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if scored[0][0] < 0.6:
        return None
    ranked = [c for _score, c in scored]

    for candidate in ranked[:MAX_ALBUM_PROBES]:
        try:
            found = fetch_album(candidate.id)
        except Exception:  # noqa: BLE001 - try the next candidate
            continue
        if found and found.tracks:
            return found
    return None


def _pair_score(
    album_track: AlbumTrack,
    source_title: str,
    source_duration_ms: int,
) -> float:
    score = title_similarity(album_track.title, source_title)
    if score < MIN_MATCH_SCORE:
        return 0.0
    if album_track.duration_ms and source_duration_ms:
        delta = abs(album_track.duration_ms - source_duration_ms)
        if delta <= DURATION_GRACE_MS:
            score += 0.3 * (1 - delta / DURATION_GRACE_MS)
        else:
            score -= 0.2
    return score


def match_playlist(
    album: AlbumRef,
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Line album tracks up with playlist entries, best pairs first.

    Greedy on the strongest pair overall rather than in album order, so one
    weak early guess cannot steal the song a later track needed.
    """
    pairs: list[tuple[float, int, int]] = []
    for a_idx, track in enumerate(album.tracks):
        for s_idx, source in enumerate(sources):
            score = _pair_score(
                track,
                str(source.get("title") or ""),
                int(source.get("duration_ms") or 0),
            )
            if score > 0:
                pairs.append((score, a_idx, s_idx))
    pairs.sort(reverse=True)

    taken_album: dict[int, tuple[int, float]] = {}
    taken_source: set[int] = set()
    for score, a_idx, s_idx in pairs:
        if a_idx in taken_album or s_idx in taken_source:
            continue
        taken_album[a_idx] = (s_idx, score)
        taken_source.add(s_idx)

    rows: list[dict[str, Any]] = []
    for a_idx, track in enumerate(album.tracks):
        hit = taken_album.get(a_idx)
        source = sources[hit[0]] if hit else None
        rows.append(
            {
                "kind": "matched" if source else "missing",
                "number": track.number,
                # Position in the album, so the UI can re-assign songs by row.
                "album_index": a_idx,
                "album_track": track.as_dict(),
                "source": source,
                "score": round(hit[1], 3) if hit else 0.0,
                "include": bool(source),
            }
        )

    # Anything the album does not account for: bonus tracks, intros, duplicates.
    extra_number = len(album.tracks)
    for s_idx, source in enumerate(sources):
        if s_idx in taken_source:
            continue
        extra_number += 1
        rows.append(
            {
                "kind": "extra",
                "number": extra_number,
                "album_index": None,
                "album_track": None,
                "source": source,
                "score": 0.0,
                "include": False,
            }
        )
    return rows
