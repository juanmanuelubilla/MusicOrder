"""Normalization layer for the matching engine.

Every source string is converted into a lossless comparison profile. The
original value is always preserved; only derived keys are used for scoring.

The layer handles:
  * case folding and accent (diacritic) removal;
  * punctuation, separators and whitespace collapsing;
  * ``&`` -> ``and`` expansion and feat/feature splitting;
  * separation of *significant* tokens from *edition* markers (remaster,
    official audio, 320 kbps, ...) and *version* markers (live, remix,
    instrumental, ...).

Edition markers are dropped from the comparison key; version markers are kept
so ``"Enjoy the Silence (Live)"`` never compares equal to the studio cut.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from musicfusion.core.text import strip_accents

_AND_RE = re.compile(r"&")
_WORD_SPLIT = re.compile(r"[^0-9a-z]+")
_TOKEN_IS_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_TOKEN_IS_SPEC = re.compile(r"^\d{1,4}(?:bit(?:s)?|kbps|khz|hz|bitrate)$")

#: Feature collaboration markers that should never split a primary artist.
FEAT_RE = re.compile(r"\b(?:feat\.?|featuring|ft\.?|ft)\b", re.IGNORECASE)

# tokens that do not determine song identity (dropped from comparisons)
EDITIONS_TOKENS: frozenset[str] = frozenset(
    {
        "album", "original", "official", "audio", "lyrics", "videoclip",
        "video", "quality", "hq", "lossless", "cd", "rip", "vinyl", "remaster",
        "remastered", "remasterizado", "remastering", "version", "clean",
        "explicit", "single", "deluxe", "expanded", "anniversary", "special",
        "limited", "super", "reissue", "digipak", "collectors", "digipack",
        "bonus", "edition", "high", "bit", "kbps", "khz", "bitrate", "hz",
        "flac", "mp3", "aac", "wav", "opus", "m4a", "vocal", "karaoke",
    }
)

# tokens that represent a DIFFERENT version of the same song
VERSION_TOKENS: frozenset[str] = frozenset(
    {
        "live", "acoustic", "instrumental", "remix", "extended", "dub",
        "demo", "alternate", "alternative", "edit", "radio", "rework",
        "reprise", "mix", "unplugged", "concert", "orchestral",
        "orchestra", "reduced", "8d", "sped", "slowed", "a cappella",
        "reggae",
        # Spanish / Portuguese equivalents (folded, accent-free)
        "vivo", "directo", "acustico", "concierto", "recital", "mezcla",
    }
)

#: common "generic" token patterns that identify anonymous audio files
GENERIC_TITLE_RE = re.compile(
    r"^(?:track[\s_]?\d+|\d+[.\-]\d+|\d+|untitled|unknown|intro|outro|"
    r"side\s?[ab]|song|recording|demo|audio[_\s-]?track|track\s?\d*)$",
    re.IGNORECASE,
)

#: tokens that alone (or with digits) mean "no real song title"
GENERIC_TERMS: frozenset[str] = frozenset(
    {
        "intro", "outro", "untitled", "unknown", "track", "song", "audio",
        "demo", "recording", "side", "music", "cancion", "canción",
        "various", "track", "nr",
    }
)

#: album artist markers that label a compilation/VA release (compact form)
COMPILATION_ARTIST_RE = re.compile(
    r"^(?:variousartists?|various|va|varios|compilation|soundtrack|ost|misc|miscellaneous|variousartists)$",
    re.IGNORECASE,
)

_MULTI_SPACE = re.compile(r"\s{2,}")

#: apostrophes and curly/smart quotes are dropped so "Don't Stop" folds to
#: the same key as "DONT STOP" / "Dont Stop" (spec §1).
_APOSTROPHE = re.compile(r"[\u0027\u2018\u2019\u02bc]")


def fold(text: str) -> str:
    """Fully fold a string: NFKC, case-insensitive and accent-free.

    Apostrophes/quotes are removed (not split) so ``Don't Stop Me Now`` and
    ``DONT STOP ME NOW`` produce the same normalized key.
    """
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", text)
    value = _APOSTROPHE.sub("", value)
    value = strip_accents(value).lower()
    return _MULTI_SPACE.sub(" ", value).strip()


def normalize_music_text(value: str) -> str:
    """THE canonical comparison form for any music text (artist/album/title).

    Single source of truth used by candidate generation, matching and
    persistence - never create ad-hoc normalizers elsewhere. Applies Unicode
    normalization, lowercasing, diacritic removal (``ñ -> n``, mandatory),
    apostrophe dropping and punctuation/separator collapsing to spaces:

        normalize_music_text("Babasónicos") == normalize_music_text("BABASONICOS")
        normalize_music_text("Poder Ñandú") == normalize_music_text("Poder Nandu")
        normalize_music_text("Don't Stop") == normalize_music_text("Dont Stop")

    Comparison-only: callers must never rewrite tags or filenames with this.
    """
    return " ".join(split_words(fold(value or "")))


def music_tokens(value: str) -> tuple[str, ...]:
    """Canonical tokens of ``normalize_music_text`` (separator-insensitive).

    ``"D-Generación"`` / ``"D Generación"`` / ``"D_Generacion"`` all yield
    ``(d, generacion)``; the joined tuple is the token key while the
    concatenated form (see :meth:`TextProfile.compact`) tolerates fully
    glued spellings such as ``DGeneracion``.
    """
    return tuple(split_words(fold(value or "")))


def split_base_and_version(value: str) -> tuple[str, tuple[str, ...]]:
    """Split a title into ``(base_title, version_tokens)``.

    Version information is preserved, not destroyed::

        "Los Calientes (Remix)" -> ("los calientes", ("remix",))
        "Los Calientes"         -> ("los calientes", ())
    """
    profile = text_profile(value or "")
    return profile.base_key, profile.version_tokens


def split_words(folded: str) -> list[str]:
    """Split a folded string into alphabetic/numeric words."""
    return [w for w in _WORD_SPLIT.split(folded) if w]


def is_edition_token(token: str) -> bool:
    """Return True for tokens that never determine song identity."""
    if token in EDITIONS_TOKENS:
        return True
    if _TOKEN_IS_SPEC.match(token):
        return True
    return False


def is_version_token(token: str) -> bool:
    return token in VERSION_TOKENS


def is_year_token(token: str) -> bool:
    return bool(_TOKEN_IS_YEAR.match(token))


@dataclass(frozen=True)
class TextProfile:
    """A lossless, derived representation of one piece of text.

    ``original`` is the untouched input; every other attribute is derived
    strictly for comparison purposes.
    """

    original: str
    folded: str
    words: tuple[str, ...]
    significant: tuple[str, ...]
    dropped: tuple[str, ...]
    version_tokens: tuple[str, ...]
    has_edition_marker: bool
    unique_tokens: tuple[str, ...] = field(default_factory=tuple)
    base_tokens: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "unique_tokens", tuple(dict.fromkeys(self.significant)))
        object.__setattr__(
            self,
            "base_tokens",
            tuple(token for token in self.significant if token not in VERSION_TOKENS),
        )

    @property
    def base_key(self) -> str:
        """Significant tokens excluding version markers (same song identity)."""
        return " ".join(self.base_tokens)

    @property
    def compact(self) -> str:
        """Concatenate significant tokens into a separator-free key."""
        return "".join(self.significant)

    @property
    def key(self) -> str:
        """Space-joined significant tokens (canonical comparison key)."""
        return " ".join(self.significant)

    @property
    def has_version_marker(self) -> bool:
        return bool(self.version_tokens)

    @property
    def is_generic(self) -> bool:
        if not self.original:
            return False
        if GENERIC_TITLE_RE.match(self.key or self.original):
            return True
        return bool(self.significant) and all(
            token.isdigit() or token in GENERIC_TERMS for token in self.significant
        )


def text_profile(raw: str) -> TextProfile:
    """Build a :class:`TextProfile` for ``raw``."""
    original = raw or ""
    folded_ = fold(raw or "")
    words = tuple(split_words(folded_))
    significant: list[str] = []
    dropped: list[str] = []
    version: list[str] = []
    for word in words:
        # years inside a title qualify the *release* ("2011 Remaster"), not the
        # song identity, so they are treated exactly like edition markers
        if is_version_token(word):
            version.append(word)
            significant.append(word)
        elif is_edition_token(word) or is_year_token(word):
            dropped.append(word)
        else:
            significant.append(word)
    return TextProfile(
        original=original.strip(),
        folded=folded_,
        words=words,
        significant=tuple(significant),
        dropped=tuple(dropped),
        version_tokens=tuple(version),
        has_edition_marker=bool(dropped),
    )


def split_featured(name: str) -> tuple[str, str]:
    """Split ``"... feat. X"`` into primary artist and featured collaborator.

    Returns a ``(primary, featured)`` pair; ``featured`` is empty when no
    collaboration marker is found.
    """
    cleaned = re.sub(r"[\t\r]+", " ", name or "")
    match = FEAT_RE.search(cleaned)
    if not match:
        return cleaned.strip(), ""
    primary = cleaned[: match.start()].strip(" -–—")
    featured = cleaned[match.end():].strip(" -–—()[]")
    return primary, featured


def primary_artist(name: str) -> str:
    """Return the main artist, ignoring any collaborating musicians."""
    return split_featured(name)[0]


def leading_article(name: str) -> str:
    """Remove a leading article token from a space-separated folded key."""
    words = name.split()
    if words and words[0] in {"the", "a", "an", "los", "las", "el", "la", "le", "die", "der", "das", "les", "un", "une"}:
        return " ".join(words[1:])
    return name


def is_compilation_artist(artist: str) -> bool:
    """Return True when ``artist`` denotes a compilation / Various Artists."""
    if not artist:
        return False
    compact = "".join(split_words(fold(artist)))
    return bool(COMPILATION_ARTIST_RE.match(compact))


__all__ = [
    "TextProfile",
    "fold",
    "normalize_music_text",
    "music_tokens",
    "split_base_and_version",
    "text_profile",
    "is_compilation_artist",
    "primary_artist",
    "split_featured",
    "leading_article",
    "is_year_token",
    "EDITIONS_TOKENS",
    "VERSION_TOKENS",
]
