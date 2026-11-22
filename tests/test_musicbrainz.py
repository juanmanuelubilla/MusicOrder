"""Tests for the MusicBrainz / Cover Art Archive client (offline, mocked)."""

from __future__ import annotations

from musicfusion.services import musicbrainz


def _fake_release() -> musicbrainz.ReleaseInfo:
    return musicbrainz.ReleaseInfo(
        title="Gold",
        artist="Queen",
        release_id="release-123",
        year=1981,
        track_count=17,
        score=100,
    )


def test_fetch_front_cover_requires_artist_or_album() -> None:
    assert musicbrainz.fetch_front_cover("", "") is None


def test_fetch_front_cover_resolves_release_and_downloads_art(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_fetch_release(client, artist: str, album: str) -> musicbrainz.ReleaseInfo:
        calls["artist"] = artist
        calls["album"] = album
        return _fake_release()

    def fake_cover_art(self, release_id: str) -> bytes | None:
        calls["release_id"] = release_id
        return b"\xff\xd8\xffMOCKJPEG"

    monkeypatch.setattr(musicbrainz, "fetch_release_group", fake_fetch_release)
    monkeypatch.setattr(musicbrainz.MusicBrainzClient, "cover_art", fake_cover_art)

    data = musicbrainz.fetch_front_cover("Queen", "Gold")
    assert data == b"\xff\xd8\xffMOCKJPEG"
    assert calls["artist"] == "Queen"
    assert calls["album"] == "Gold"
    assert calls["release_id"] == "release-123"


def test_fetch_front_cover_returns_none_without_release(monkeypatch) -> None:
    def fake_fetch_release(client, artist: str, album: str):
        return None

    monkeypatch.setattr(musicbrainz, "fetch_release_group", fake_fetch_release)
    assert musicbrainz.fetch_front_cover("Ghost", "Unknown") is None


# ---------------------------------------------------------------------------
# rate limiting + retries (the 503-storm protection)
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}

    def json(self) -> dict:
        if self._payload == {}:
            raise ValueError("no body")
        return self._payload

    def raise_for_status(self) -> None:
        import requests

        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class _FlakyRequests:
    """Scripted requests.get: raises/errors the first N times, then succeeds."""

    def __init__(self, failures: int, statuses: list[int] | None = None) -> None:
        self.remaining = failures
        self.statuses = list(statuses or [503])
        self.calls = 0

    def __call__(self, url, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            return _Response(self.statuses[0])
        return _Response(200, {"release-groups": []})


def test_transient_503_is_retried_and_then_succeeds(monkeypatch) -> None:
    flaky = _FlakyRequests(failures=2)
    monkeypatch.setattr(musicbrainz.requests, "get", flaky)
    time_module = __import__("time")
    monkeypatch.setattr(time_module, "sleep", lambda s: None)

    client = musicbrainz.MusicBrainzClient(min_interval=0.0, retries=3, backoff=0.1)
    result = client.search_releasegroup("Queen", "Gold")
    assert result is not None and result == []   # success after retries
    assert flaky.calls == 3                      # 2 failures + 1 success


def test_permanent_failure_returns_none_without_raising(monkeypatch) -> None:
    flaky = _FlakyRequests(failures=99)          # always failing
    monkeypatch.setattr(musicbrainz.requests, "get", flaky)
    sleeps: list[float] = []
    time_mod = __import__("time")
    monkeypatch.setattr(time_mod, "sleep", lambda s: sleeps.append(s))

    client = musicbrainz.MusicBrainzClient(min_interval=0.0, retries=2, backoff=0.5)
    assert client.search_releasegroup("Queen", "Gold") is None
    assert client.lookup_release_group("rg-1") == {}
    assert client.lookup_release("rel-1") == {}
    assert len(sleeps) >= 4                      # exponential backoff ran


def test_rate_limiter_enforces_minimum_interval(monkeypatch) -> None:
    import time as time_module

    stamps: list[float] = []
    monkeypatch.setattr(
        musicbrainz.requests, "get",
        lambda *a, **k: stamps.append(time_module.monotonic()) or _Response(200,
            {"release-groups": []}),
    )
    # reset shared limiter state for a deterministic window
    musicbrainz._LIMITER._last = time_module.monotonic()

    client = musicbrainz.MusicBrainzClient(min_interval=0.25, retries=1)
    client.search_releasegroup("A", "B")
    client.search_releasegroup("C", "D")

    assert len(stamps) == 2
    gap = stamps[1] - stamps[0]
    assert gap >= 0.24, f"pacing no aplicado: {gap:.3f}s"
