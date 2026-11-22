"""Tests for the Wikipedia evidence client (network mocked at _api level)."""

from __future__ import annotations

from musicfusion.services.wikipedia import WikipediaClient


class StubWiki(WikipediaClient):
    """Offline stand-in that replays canned API payloads."""

    def __init__(self, responses: dict[tuple[str, str], dict | None]) -> None:
        super().__init__()
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def _api(self, lang: str, params: dict) -> dict | None:
        self.calls.append((lang, params))
        return self.responses.get((lang, params.get("list", "extracts")))


def test_album_summary_searches_then_extracts() -> None:
    wiki = StubWiki({
        ("es", "search"): {"query": {"search": [{"title": "Mucho + (álbum)"}, {"title": "Otro"}]}},
        ("es", "extracts"): {"query": {"pages": {"1": {"extract": "Edición extendida de 2026."}}}},
        ("en", "search"): None,
        ("en", "extracts"): None,
    })
    info = wiki.album_summary("Babasónicos", "Mucho +")
    assert info is not None
    assert info["source"] == "es.wikipedia.org"
    assert info["page"] == "Mucho + (álbum)"
    assert "extendida" in info["summary"]
    # Spanish first: English never queried after a hit
    assert [lang for lang, _ in wiki.calls] == ["es", "es"]


def test_album_summary_falls_back_to_english_then_none() -> None:
    wiki = StubWiki({
        ("es", "search"): {"query": {"search": []}},
        ("en", "search"): {"query": {"search": [{"title": "Some Album"}]}},
        ("en", "extracts"): {"query": {"pages": {"9": {"extract": "An EP released in 2026."}}}},
    })
    info = wiki.album_summary("Unknown Band", "Some Album")
    assert info is not None and info["source"] == "en.wikipedia.org"

    empty = StubWiki({})
    assert empty.album_summary("X", "Y") is None
