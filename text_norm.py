"""Turn song titles in any script into Latin text so they can be compared.

A playlist titled ``夜に駆ける`` should match an album listing ``Yoru ni Kakeru``,
and ``Группа крови`` should match ``Gruppa krovi``. Each text yields one or more
romanised readings; matching code takes the best pairing between the two sides.

Japanese needs its own reader: the same kanji read as Chinese by a generic
transliterator come out as nonsense (``夜に駆ける`` -> ``Ye niQu keru``), so any
text with kana is also read as Japanese. Han-only text is ambiguous (Chinese or
Japanese), so both readings are kept.
"""

from __future__ import annotations

import re
from functools import lru_cache

try:  # generic Latin transliteration: Cyrillic, Greek, Hangul, accents, pinyin
    from unidecode import unidecode as _unidecode
except ImportError:  # pragma: no cover - optional dependency
    _unidecode = None

try:  # Japanese kanji + kana -> romaji
    import pykakasi

    _KAKASI = pykakasi.kakasi()
except Exception:  # noqa: BLE001  # pragma: no cover - optional dependency
    _KAKASI = None

_CJK_RUN_RE = re.compile(r"([぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]+)")
_KANA_RE = re.compile(r"[぀-ヿｦ-ﾟ]")
_ACCENT_FALLBACK = str.maketrans("", "", "".join(chr(c) for c in range(0x300, 0x370)))


def _ascii(text: str) -> str:
    if _unidecode is not None:
        return _unidecode(text)
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", text)
    return decomposed.translate(_ACCENT_FALLBACK)


def _japanese(text: str) -> str:
    """Read kanji/kana runs as Japanese, leaving everything else to unidecode."""
    parts: list[str] = []
    for chunk in _CJK_RUN_RE.split(text):
        if not chunk:
            continue
        if _CJK_RUN_RE.fullmatch(chunk):
            romaji = " ".join(item["hepburn"] for item in _KAKASI.convert(chunk))
            parts.append(f" {romaji} ")
        else:
            parts.append(_ascii(chunk))
    return "".join(parts)


@lru_cache(maxsize=8192)
def romanize_variants(text: str) -> tuple[str, ...]:
    """Latin readings of ``text``, most likely first. ASCII text is returned as is."""
    text = text or ""
    if text.isascii():
        return (text,)

    variants = [_ascii(text)]
    if _KAKASI is not None and _CJK_RUN_RE.search(text):
        try:
            japanese = _japanese(text)
        except Exception:  # noqa: BLE001 - never let a reader bug break matching
            japanese = ""
        if japanese:
            # With kana present it is certainly Japanese, so read it that way first.
            if _KANA_RE.search(text):
                variants.insert(0, japanese)
            else:
                variants.append(japanese)

    seen: set[str] = set()
    unique = []
    for variant in variants:
        cleaned = " ".join(variant.split())
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)
    return tuple(unique) or (text,)
