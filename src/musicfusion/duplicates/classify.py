"""Classification of duplicate groups based on title and metadata markers.

Hex classification: a "different version" is never deleted automatically; only
truly identical files (or the clearly-lower-quality member of the same
recording) are candidates for removal.
"""

from __future__ import annotations

from musicfusion.core.text import strip_accents

# Version markers typical of edited/heavily-remixed releases.
_RADIO = ("radio edit", "radio version", "single version", "radio mix", "edit")
_LIVE = ("live", "live version", "in concert", "live at", "unplugged")
_DEMO = ("demo", "demo version", "working title")
_REMIX = ("remix", "club mix", "extended mix", "dub mix", "rework", "dance mix", "mix")
_INSTRUMENTAL = ("instrumental", "karaoke", "minus one", "reprise", "orchestral version")
_ACAPELLA = ("acapella", "a capella", "a cappella")

# How strongly the "base" title compares to a version variant.
VERSION_MARKERS = {
    "radio": _RADIO,
    "live": _LIVE,
    "demo": _DEMO,
    "remix": _REMIX,
    "instrumental": _INSTRUMENTAL,
    "acapella": _ACAPELLA,
}


def classify(title_a: str, title_b: str) -> str:
    """Return the classification of the relationship between two titles.

    Values: ``exact``, ``same_recording``, ``remix``, ``live``, ``demo``,
    ``radio_edit``, ``instrumental``, ``acappella`` or ``different_version``.
    """
    na = _clean(title_a)
    nb = _clean(title_b)
    if na == nb:
        return "exact"

    for kind, markers in VERSION_MARKERS.items():
        for marker in markers:
            if marker in na and marker in nb:
                return kind
            if marker in na and _base_match(na, nb, marker):
                return kind
            if marker in nb and _base_match(nb, na, marker):
                return kind
    if _tokens_differ_only_in_markers(na, nb):
        return "same_recording"
    return "different_version"


def _clean(title: str) -> str:
    return strip_accents((title or "").lower())


def _base_match(versioned: str, plain: str, marker: str) -> bool:
    """True when ``plain`` equals ``versioned`` minus its markers.

    Also accepts the plain title being a direct prefix of the versioned title,
    e.g. ``comfortably numb live at pompeii`` vs ``comfortably numb``.
    """
    if plain and versioned.startswith(plain) and marker in versioned:
        remainder = versioned[len(plain):].strip(" -–—()")
        if remainder in marker or marker in remainder:
            return True
    cleaned = versioned.replace(marker, "")
    cleaned = cleaned.replace("()", "").replace("[]", "").strip(" -–—()")
    return cleaned == plain


def _tokens_differ_only_in_markers(na: str, nb: str) -> bool:
    markers = tuple(m for marker_list in VERSION_MARKERS.values() for m in marker_list)
    a = set(na.replace(m, "").strip(" -–—()") for m in markers)
    b = set(nb.replace(m, "").strip(" -–—()") for m in markers)
    return a == b and bool(a)


def flatten_markers() -> tuple[str, ...]:
    """All known version markers flattened."""
    out: list[str] = []
    for marker_list in VERSION_MARKERS.values():
        out.extend(marker_list)
    return tuple(out)
