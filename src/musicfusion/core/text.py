"""Text normalization utilities used by matching, duplicates and merging.

Normalization is intentionally lossless: it strips tags, accents, articles and
near-duplicate tokens so that two strings describing the same release
normalize together to the same key.
"""

from __future__ import annotations

import re
import unicodedata

EDITIONS = {
    "remastered", "remaster", "edition", "deluxe", "bonus", "expanded",
    "anniversary", "special", "limited", "extended", "super", "reissue",
    "digipak", "collectors", "digipack", "radio", "edited", "single",
    "clean", "explicit", "tour", "live", "demo", "original", "version",
    "collection", "compilation", "best", "greatest", "world", "ltd",
}

_SPACE = re.compile(r"\s+")
_STRIP_ARTICLES = re.compile(r"^(the|a|an|los|las|el|la|le|die|der|das|les|un|une)\s+")

# Translations of common ambiguous tokens to a canonical separator.
_NOISE_CHARS = re.compile(r"[’‘`´\"·,\._\-\/\\!]+")


def strip_accents(text: str) -> str:
    """Remove diacritics from a string, returning ASCII-folded text."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_key(text: str) -> str:
    """Produce a canonical comparison key for a piece of text.

    The key is lowercased, accent-folded, has articles removed, whitespace
    collapsed and "&" expanded to "and". It is used to compare artist,
    album and compilation identifiers.
    """
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", text).lower()
    value = strip_accents(value)
    value = re.sub(r"&", " and ", value)
    value = _NOISE_CHARS.sub(" ", value)
    value = _SPACE.sub(" ", value).strip()
    value = _STRIP_ARTICLES.sub("", value)
    return _SPACE.sub(" ", value).strip()


def normalize_title(text: str) -> str:
    """Normalize a song or album title key (keeps leading articles)."""
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", text or "").lower()
    value = strip_accents(value)
    value = _NOISE_CHARS.sub(" ", value)
    return _SPACE.sub(" ", value).strip()


def has_edition_suffix(text: str) -> bool:
    """Return True when the text carries a common edition marker."""
    words = set(re.findall(r"[a-z]+", (text or "").lower()))
    return bool(words & EDITIONS)


def edition_family(text: str) -> str:
    """Drop edition words and release years, e.g. ``The Wall`` => ``wall``."""
    value = normalize_key(text)
    if not value:
        return value
    parts = [w for w in value.split() if w not in EDITIONS and not re.fullmatch(r"\d{4}", w)]
    return " ".join(parts).strip()


# ---------------------------------------------------------------------------
# folder-structure helpers (Artista/Año - Álbum/NN - Tema.ext layouts)
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_YEAR_TOKEN = re.compile(r"\b((?:19|20)\d{2})\b")
_TRACK_NO_PREFIX = re.compile(r"^\s*(\d{1,3})\s*[-.–_)]?\s+")
_ALBUM_FOLDER_PREFIX = re.compile(r"^\s*(?:19|20)\d{2}\s*[-–]?\s*")


def structure_key(parts: list[str] | tuple[str, ...]) -> str:
    """Accent-, case- and punctuation-free key for a relative path.

    Every path component (artist folder, ``YYYY - Album``, ``NN - Title``)
    folds to lowercase ASCII with non-alphanumeric runs collapsed, so
    ``Babasonicos/1992 - Pasto/01 - Intro.ogg`` and
    ``Babasónicos/1992 - Pasto/01 - Intro.ogg`` produce the SAME key while
    genuinely different names still differ.
    """
    folded: list[str] = []
    for index, part in enumerate(parts):
        name = part
        if index == len(parts) - 1 and "." in name:
            name = name.rsplit(".", 1)[0]
        value = _NON_ALNUM.sub(" ", strip_accents(name.lower())).strip()
        if value:
            folded.append(value)
    return "/".join(folded)


def track_no_from_filename(stem: str) -> int | None:
    """Track number encoded as a ``NN - Title`` filename prefix."""
    match = _TRACK_NO_PREFIX.match(stem or "")
    return int(match.group(1)) if match else None


def title_from_filename(stem: str) -> str:
    """Song title taken from a ``NN - Title`` filename stem."""
    value = stem or ""
    match = _TRACK_NO_PREFIX.match(value)
    if match:
        value = value[match.end():]
    return normalize_title(value)


def album_from_folder(name: str) -> str:
    """Album name from an optional ``YYYY - Album`` folder name."""
    return normalize_title(_ALBUM_FOLDER_PREFIX.sub("", name or ""))


def artist_from_folder(name: str) -> str:
    """Artist name from the artist-level folder name (raw, not normalized)."""
    return (name or "").strip()


def year_from_folder(name: str) -> int | None:
    """Release year found as a standalone YYYY token in a folder name."""
    match = _YEAR_TOKEN.search(name or "")
    return int(match.group(1)) if match else None
