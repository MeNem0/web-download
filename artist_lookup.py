"""Resolve the real performer for a song title via the iTunes Search API.

Compilation uploads on YouTube credit the channel rather than the artist, and
the title is not always in "Artist - Song" form. Looking the song up gives a
canonical artist name for those cases. No API key is required.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher

import httpx

from text_norm import romanize_variants

SEARCH_URL = "https://itunes.apple.com/search"
# iTunes throttles unauthenticated clients at roughly 20 calls/minute, so pace
# requests instead of getting the whole playlist rate-limited part way through.
MIN_INTERVAL_SECONDS = 0.35
REQUEST_TIMEOUT = 8.0
# Below this the candidate is a different song that happens to share words.
MIN_TITLE_SCORE = 0.72
# Without a duration or an artist hint, only a near-exact title is safe: plenty
# of unrelated songs share a title like "Loser".
MIN_BLIND_TITLE_SCORE = 0.9
# Music videos carry intros/outros, so allow drift before calling it a
# different recording. Beyond this the candidate is rejected outright.
MAX_DURATION_DELTA_MS = 25_000
RESULT_LIMIT = 25

_PUNCT_RE = re.compile(r"[^\w\s]+")
_PAREN_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_WS_RE = re.compile(r"\s+")
_FEAT_RE = re.compile(r"\b(?:feat|ft|featuring|with)\b.*$", re.IGNORECASE)


@dataclass(frozen=True)
class ArtistMatch:
    artist: str
    title: str
    score: float


def _normalize_reading(text: str) -> str:
    out = (text or "").lower()
    out = _PAREN_RE.sub(" ", out)
    out = _FEAT_RE.sub(" ", out)
    out = _PUNCT_RE.sub(" ", out)
    return _WS_RE.sub(" ", out).strip()


def _normalize(text: str) -> str:
    """Primary Latin reading of a title, lowercased with bracketed noise gone."""
    return _normalize_reading(romanize_variants(text)[0])


def _normalized_variants(text: str) -> tuple[str, ...]:
    """Every Latin reading of a title (a Han-only title may be Chinese or Japanese)."""
    out: list[str] = []
    for reading in romanize_variants(text):
        norm = _normalize_reading(reading)
        if norm and norm not in out:
            out.append(norm)
    return tuple(out)


def normalize_title(text: str) -> str:
    """Public alias so matching code shares one notion of title equality."""
    return _normalize(text)


def _pair_score(a: str, b: str, *, compact: bool) -> float:
    if a == b:
        return 1.0
    # A remix/edit is still the same song, so reward containment either way.
    if a in b or b in a:
        return 0.92
    score = SequenceMatcher(None, a, b).ratio()
    if compact:
        # Romanisations disagree on spacing ("yoruni kakeru" / "yoru ni kakeru").
        ca, cb = a.replace(" ", ""), b.replace(" ", "")
        if ca == cb:
            return 0.97
        score = max(score, SequenceMatcher(None, ca, cb).ratio())
    return score


def _title_score(want: str, got: str) -> float:
    # Compare in Latin script so "夜に駆ける" can match "Yoru ni Kakeru"; the
    # best pairing of readings counts. Plain English titles take the old path.
    want, got = want or "", got or ""
    compact = not (want.isascii() and got.isascii())
    best = 0.0
    for a in _normalized_variants(want):
        for b in _normalized_variants(got):
            best = max(best, _pair_score(a, b, compact=compact))
    return best


def title_similarity(want: str, got: str) -> float:
    """0..1 similarity between two song titles, ignoring bracketed noise."""
    return _title_score(want, got)


class ArtistResolver:
    """Cached, rate-limited iTunes lookups for one download run.

    Repeated failures disable the resolver so a network outage or a rate-limit
    block costs one delay rather than one per track.
    """

    def __init__(self, *, max_failures: int = 3) -> None:
        self._cache: dict[str, ArtistMatch | None] = {}
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._failures = 0
        self._max_failures = max_failures
        self.disabled_reason = ""

    @property
    def available(self) -> bool:
        return not self.disabled_reason

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < MIN_INTERVAL_SECONDS:
            time.sleep(MIN_INTERVAL_SECONDS - elapsed)
        self._last_call = time.monotonic()

    def _search(self, term: str) -> list[dict]:
        self._throttle()
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.get(
                SEARCH_URL,
                params={
                    "term": term,
                    "media": "music",
                    "entity": "song",
                    "limit": RESULT_LIMIT,
                },
            )
            response.raise_for_status()
            payload = response.json()
        return list(payload.get("results") or [])

    def lookup(
        self,
        title: str,
        *,
        hint_artist: str = "",
        duration_ms: int = 0,
    ) -> ArtistMatch | None:
        """Best-matching (artist, title) for a song, or None when unsure."""
        term = " ".join(part for part in (hint_artist.strip(), title.strip()) if part)
        term = _WS_RE.sub(" ", term).strip()
        if len(term) < 3 or not self.available:
            return None

        # Duration is part of the answer, not just the query, so two tracks
        # sharing a title must not share a cache entry.
        key = f"{term.lower()}|{duration_ms // 1000}"
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        try:
            results = self._search(term)
        except Exception as exc:  # noqa: BLE001 - lookup is best effort
            self._failures += 1
            if self._failures >= self._max_failures:
                self.disabled_reason = str(exc) or "lookup unavailable"
            return None

        scored: list[ArtistMatch] = []
        for item in results:
            artist = str(item.get("artistName") or "").strip()
            got_title = str(item.get("trackName") or "").strip()
            if not artist or not got_title:
                continue
            score = _title_score(title, got_title)
            if score < MIN_TITLE_SCORE:
                continue

            # Runtime is what separates same-titled songs by different artists,
            # so weight it above the title once both are known.
            candidate_ms = int(item.get("trackTimeMillis") or 0)
            if duration_ms and candidate_ms:
                delta = abs(candidate_ms - duration_ms)
                if delta > MAX_DURATION_DELTA_MS:
                    continue
                score += 0.4 * (1 - delta / MAX_DURATION_DELTA_MS)
            elif not hint_artist.strip() and score < MIN_BLIND_TITLE_SCORE:
                continue

            scored.append(ArtistMatch(artist=artist, title=got_title, score=score))

        # Stable sort keeps iTunes' own relevance order as the tiebreaker, which
        # is what separates the original from covers and karaoke versions when
        # the title alone matches several songs equally well.
        scored.sort(key=lambda m: m.score, reverse=True)
        best = scored[0] if scored else None

        with self._lock:
            self._cache[key] = best
        return best
