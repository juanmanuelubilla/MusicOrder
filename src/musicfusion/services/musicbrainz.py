"""Music metadata services: MusicBrainz and Cover Art Archive clients."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

USER_AGENT = "MusicFusionPro/0.1 (https://example.invalid/musicfusion)"
MB_BASE = "https://musicbrainz.org/ws/2"
COVER_BASE = "https://coverartarchive.org"

#: status codes that mean "slow down / try again", not "does not exist"
_TRANSIENT = {429, 500, 502, 503, 504}


@dataclass
class ReleaseInfo:
    """A MusicBrainz release-group result."""

    title: str
    artist: str
    release_id: str
    year: int | None
    track_count: int | None = None
    score: int = 0
    first_release_date: str = ""


class _RateLimiter:
    """Process-wide 1 req/s pacing shared by every MusicBrainzClient."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self, min_interval: float) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            remaining = min_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
            self._last = time.monotonic()


_LIMITER = _RateLimiter()


@dataclass
class MusicBrainzClient:
    """Thin REST client for MusicBrainz and cover art.

    Resilience built in (spec: rate-limit friendly):

    * global pacing of at least ``min_interval`` seconds between requests;
    * automatic retries with exponential backoff on transient errors
      (503/429/timeouts), so a flaky network degrades instead of failing;
    * ``None`` (never an exception) after exhausting attempts.
    """

    user_agent: str = USER_AGENT
    base: str = MB_BASE
    cover_base: str = COVER_BASE
    timeout: int = 20
    min_interval: float = 1.1
    retries: int = 3
    backoff: float = 2.0

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept": "application/json"}

    # ------------------------------------------------------------- transport
    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """GET with pacing + retries; ``None`` when all attempts fail."""
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            _LIMITER.wait(self.min_interval)
            try:
                response = requests.get(
                    url, params=params, headers=self._headers(), timeout=self.timeout
                )
                if response.status_code in _TRANSIENT:
                    logger.info(
                        "MusicBrainz %s -> %s (intento %s/%s)",
                        response.status_code, url.split("/ws/2/")[-1], attempt, self.retries,
                    )
                    last_error = requests.HTTPError(f"{response.status_code}")
                    time.sleep(self.backoff * (2 ** (attempt - 1)))
                    continue
                response.raise_for_status()
                return dict(response.json())
            except ValueError as exc:  # unparsable body: not transient
                logger.warning("MusicBrainz devolvió JSON inválido (%s): %s", url, exc)
                return None
            except requests.RequestException as exc:
                last_error = exc
                logger.info(
                    "MusicBrainz fallo de red (intento %s/%s): %s",
                    attempt, self.retries, exc.__class__.__name__,
                )
                time.sleep(self.backoff * (2 ** (attempt - 1)))
        logger.warning("MusicBrainz agotó %s intentos para %s: %s",
                       self.retries, url.split("/ws/2/")[-1], last_error)
        return None

    def search_releasegroup(
        self, artist: str, album: str, limit: int = 5
    ) -> list[ReleaseInfo] | None:
        """Search release-groups; ``None`` on network failure (not the same
        as an empty result - callers use that to avoid caching errors)."""
        query: dict[str, str | int] = {
            "query": self._build_query(artist, album),
            "fmt": "json",
            "limit": limit,
        }
        payload = self._get_json(f"{self.base}/release-group", params=query)
        if payload is None:
            return None
        results: list[ReleaseInfo] = []
        for item in payload.get("release-groups", []):
            first_date = item.get("first-release-date", "") or ""
            year = _text_year(first_date)
            artist_name = " / ".join(
                credit.get("name", "") for credit in item.get("artist-credit", [])
            )
            results.append(
                ReleaseInfo(
                    title=item.get("title", ""),
                    artist=artist_name or artist,
                    release_id=item.get("id", ""),
                    year=year,
                    track_count=None,
                    score=int(item.get("score", 0) or 0),
                    first_release_date=first_date,
                )
            )
        return results

    def lookup_release_group(self, group_id: str) -> dict[str, Any]:
        """Fetch release-group details including its releases."""
        url = f"{MB_BASE}/release-group/{group_id}"
        params = {"inc": "releases+artist-credits", "fmt": "json"}
        payload = self._get_json(url, params=params)
        return payload if payload else {}

    def lookup_release(self, release_id: str) -> dict[str, Any]:
        """Fetch the full release details including recordings."""
        url = f"{MB_BASE}/release/{release_id}"
        params = {"inc": "recordings+artist-credits+labels", "fmt": "json"}
        payload = self._get_json(url, params=params)
        return payload if payload else {}

    def release_group_track_count(self, group_id: str) -> int | None:
        """Official track count of a release-group's canonical (earliest) release."""
        listing = self.release_group_tracklist(group_id)
        return listing["count"] if listing else None

    def release_group_tracklist(self, group_id: str) -> dict[str, Any] | None:
        """Official ``{count, titles}`` of a release-group's canonical release.

        Works for brand-new releases too: everything comes from the live
        MusicBrainz database, never from model memory.
        """
        group = self.lookup_release_group(group_id)
        releases = group.get("releases") or []
        if not releases:
            return None
        best = min(
            releases,
            key=lambda rel: rel.get("date") or "9999-99-99",
        )
        release = self.lookup_release(best.get("id", ""))
        if not release:
            return None
        titles: list[str] = []
        for medium in release.get("media") or []:
            for track in medium.get("tracks") or []:
                titles.append(str(track.get("title", "")).strip())
        return {"count": len(titles) or None, "titles": titles}

    def cover_art(self, release_id: str) -> bytes | None:
        """Download the front cover art for a release, or None."""
        url = f"{self.cover_base}/release/{release_id}/front"
        try:
            response = requests.get(url, timeout=self.timeout)
            if response.status_code != 200:
                return None
            if len(response.content) > 20 * 1024 * 1024:
                return None
            return response.content
        except requests.RequestException as exc:
            logger.warning("Cover art fetch failed for %s: %s", release_id, exc)
            return None

    @staticmethod
    def _build_query(artist: str, album: str) -> str:
        parts = [f'artist:"{artist}"' if artist else "", f'release:"{album}"' if album else ""]
        return " AND ".join(part for part in parts if part)


def _text_year(text: str) -> int | None:
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits[:4])
    except ValueError:
        return None


def fetch_release_group(
    client: MusicBrainzClient, artist: str, album: str
) -> ReleaseInfo | None:
    """Convenience helper: return the best-matching release-group."""
    results = client.search_releasegroup(artist=artist, album=album)
    if not results:
        return None
    return max(results, key=lambda release: release.score)


def fetch_front_cover(artist: str, album: str) -> bytes | None:
    """Download the front cover art for ``artist`` / ``album``, or ``None``.

    Requires an album (or artist+album) to look up the release; returns the raw
    image bytes so callers can embed them with ``artwork.embed_art``.
    """
    if not artist and not album:
        return None
    client = MusicBrainzClient()
    release = fetch_release_group(client, artist, album)
    if release is None or not release.release_id:
        return None
    return client.cover_art(release.release_id)
