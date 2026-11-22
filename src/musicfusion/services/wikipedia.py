"""Wikipedia retrieval used as secondary evidence for the local AI.

When MusicBrainz has no match for an album (very new releases, obscure
regional pressings...), the app fetches a summary from the MediaWiki API and
hands it to the Ollama model as grounding context, so the model reasons over
retrieved facts instead of its training memory.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

USER_AGENT = "MusicFusionPro/0.1 (https://example.invalid/musicfusion)"
_LANGS = ("es", "en")


class WikipediaClient:
    """Tiny MediaWiki API client (search + intro extract)."""

    def __init__(self, timeout: int = 10) -> None:
        self.timeout = timeout

    def album_summary(self, artist: str, album: str) -> dict[str, Any] | None:
        """Best-effort page summary for ``artist``/``album``, or ``None``."""
        if not artist and not album:
            return None
        query = " ".join(part for part in (artist, album) if part)
        for lang in _LANGS:
            found = self._search(query, lang)
            if not found:
                continue
            summary = self._extract(found[0], lang)
            if summary:
                return {
                    "source": f"{lang}.wikipedia.org",
                    "page": found[0],
                    "summary": summary[:1600],
                }
        return None

    # ---------------------------------------------------------------- api
    def _api(self, lang: str, params: dict[str, Any]) -> dict[str, Any] | None:
        url = f"https://{lang}.wikipedia.org/w/api.php"
        headers = {"User-Agent": USER_AGENT}
        try:
            response = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            return dict(response.json())
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Wikipedia query failed (%s): %s", lang, exc)
            return None

    def _search(self, query: str, lang: str) -> list[str]:
        data = self._api(lang, {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": 5,
            "format": "json",
        })
        if not data:
            return []
        return [
            item.get("title", "")
            for item in data.get("query", {}).get("search", [])
            if item.get("title")
        ]

    def _extract(self, title: str, lang: str) -> str:
        data = self._api(lang, {
            "action": "query",
            "prop": "extracts",
            "exintro": 1,
            "explaintext": 1,
            "titles": title,
            "format": "json",
        })
        if not data:
            return ""
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            text = page.get("extract", "") or ""
            if text.strip():
                return text.strip()
        return ""


__all__ = ["WikipediaClient"]
