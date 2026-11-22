"""Tests for the local-Ollama assistance service (no network, no server)."""

from __future__ import annotations

from pathlib import Path

from musicfusion.config.settings import Settings
from musicfusion.services.ollamalocal import (
    OllamaClient,
    ai_album_relationship,
    configured_client,
    load_verdict,
    save_verdict,
    verdict_cache_key,
)


class FakeOllama(OllamaClient):
    """Offline stand-in with canned /api/tags and /api/chat answers."""

    def __init__(self, models: list[str] | None = None, answer: dict | None = None) -> None:
        super().__init__(host="http://fake", model="llama3.1:8b")
        self._models = models or []
        self._answer = answer
        self.chat_calls = 0

    def _get(self, path: str) -> dict | None:
        if path == "/api/tags":
            return {"models": [{"name": m} for m in self._models]}
        return None

    def _post(self, path: str, payload: dict) -> dict | None:
        self.chat_calls += 1
        if self._answer is None:
            return None
        import json

        return {"message": {"content": json.dumps(self._answer)}}


def test_available_requires_server_and_model() -> None:
    assert FakeOllama(models=["llama3.1:8b", "mistral:latest"]).available() is True
    assert FakeOllama(models=[]).available() is False
    picky = FakeOllama(models=["mistral:latest"])
    picky.model = "llama3.1"
    assert picky.available() is False


def test_list_models_returns_names() -> None:
    client = FakeOllama(models=["a:latest", "b:latest"])
    assert client.list_models() == ["a:latest", "b:latest"]
    assert FakeOllama().list_models() == []


def test_ai_album_relationship_parses_verdict() -> None:
    client = FakeOllama(
        models=["llama3.1:8b"],
        answer={"relationship": "deluxe_or_reissue", "confidence": 87, "reason": "temas extra"},
    )
    a = {"artist": "Babasónicos", "title": "Mucho", "year": 2008, "track_count": 10}
    b = {"artist": "Babasónicos", "title": "Mucho +", "year": 2009, "track_count": 13}
    verdict = ai_album_relationship(client, a, b)
    assert verdict is not None
    assert verdict.relationship == "deluxe_or_reissue"
    assert verdict.confidence == 87.0
    assert "extra" in verdict.reason
    assert client.chat_calls == 1


def test_ai_album_relationship_rejects_invalid_and_offline() -> None:
    bad = FakeOllama(models=["m"], answer={"relationship": "no-se", "confidence": 50})
    offline = FakeOllama(models=[])
    a = {"artist": "X", "title": "A"}
    b = {"artist": "X", "title": "B"}
    assert ai_album_relationship(bad, a, b) is None
    assert ai_album_relationship(offline, a, b) is None


def test_ai_album_relationship_grounds_prompt_with_evidence() -> None:
    """Retrieved facts must reach the prompt (RAG), not model memory."""
    captured: dict = {}

    class Spy(FakeOllama):
        def chat_json(self, system: str, user: str):
            captured["system"] = system
            captured["user"] = user
            return super().chat_json(system, user)

    client = Spy(
        models=["llama3.1:8b"],
        answer={"relationship": "insufficient_data", "confidence": 20, "reason": "pocos datos"},
    )
    evidence = {
        "musicbrainz": {
            "title": "Mucho +", "year": 2026, "count": 13,
            "titles": ["Formidable", "Todo Dicho"],
        },
        "wikipedia": {
            "source": "es.wikipedia.org", "page": "Mucho +",
            "summary": "Edición extendida publicada en 2026.",
        },
    }
    verdict = ai_album_relationship(
        client,
        {"artist": "X", "title": "A"},
        {"artist": "X", "title": "B"},
        evidence_a=evidence, evidence_b=None,
    )
    assert verdict is not None
    assert verdict.relationship == "insufficient_data"
    assert "insufficient_data" in captured["system"]
    assert "MusicBrainz: 'Mucho +' (2026)" in captured["user"]
    assert "Formidable" in captured["user"]
    assert "es.wikipedia.org" in captured["user"]
    assert "[Álbum B · evidencia]: ninguna" in captured["user"]


class FakeWiki:
    """Offline WikipediaClient stand-in."""

    def __init__(self, summary: str | None = None) -> None:
        self.summary = summary

    def album_summary(self, artist: str, album: str) -> dict | None:
        if self.summary is None:
            return None
        return {
            "source": "es.wikipedia.org",
            "page": f"{artist} - {album}",
            "summary": self.summary,
        }


def test_verdict_cache_round_trip(tmp_path: Path) -> None:
    cache = tmp_path / "ai.json"
    a = {"artist": "x", "title": "mucho", "year": 2008, "track_count": 10}
    b = {"artist": "x", "title": "mucho +", "year": 2009, "track_count": 13}
    key = verdict_cache_key(a, b)
    save_verdict(cache, key, None)
    assert load_verdict(cache, key) is None
    from musicfusion.services.ollamalocal import AiAlbumVerdict

    save_verdict(cache, key, AiAlbumVerdict("same_release", 90.0, "idéntico"))
    loaded = load_verdict(cache, key)
    assert loaded is not None and loaded.relationship == "same_release"


def test_configured_client_reads_settings(tmp_path: Path) -> None:
    settings = Settings(config_dir=tmp_path / "cfg")
    settings.set("ollama.host", "http://127.0.0.1:11500")
    settings.set("ollama.model", "hermes-128k:latest")
    client = configured_client(settings)
    assert client.host == "http://127.0.0.1:11500"
    assert client.model == "hermes-128k:latest"

    settings.set("ollama.model", "")
    resolver = configured_client(settings)
    # no server running in tests: model stays the wildcard/first fallback
    assert isinstance(resolver, OllamaClient)


# ---------------------------------------------------------------------------
# resiliencia: salida vacía, <think> y health breaker
# ---------------------------------------------------------------------------

class FlakyEmpty(FakeOllama):
    """Devuelve contenido vacío las primeras N veces, luego JSON válido."""

    def __init__(self, empty_times: int, answer: dict) -> None:
        super().__init__(models=["llama3.1:8b"], answer=answer)
        self.empty_left = empty_times
        self.raw_calls = 0

    def _post(self, path: str, payload: dict) -> dict | None:
        import json as _json

        self.chat_calls += 1
        if self.empty_left > 0:
            self.empty_left -= 1
            return {"message": {"content": ""}}
        return {"message": {"content": _json.dumps(self._answer)}}


def test_empty_content_is_retried_then_parses() -> None:
    client = FlakyEmpty(1, {"relationship": "same_release", "confidence": 90,
                            "reason": "idéntico"})
    out = client.chat_json("sys", "usr")
    assert out is not None and out["relationship"] == "same_release"
    assert client.is_healthy


def test_thinking_block_is_salvaged() -> None:
    class Thinker(FakeOllama):
        def _post(self, path: str, payload: dict) -> dict | None:

            self.chat_calls += 1
            thinking = "<think>dudo...</think> {\"relationship\": \"unrelated\", \"confidence\": 60, \"reason\": \"distintos\"}"
            return {"message": {"content": "", "thinking": thinking}}

    client = Thinker(models=["llama3.1:8b"])
    out = ai_album_relationship(client, {"artist": "A", "title": "X"},
                                {"artist": "B", "title": "Y"})
    assert out is not None and out.relationship == "unrelated"


def test_health_breaker_stops_after_repeated_failures() -> None:
    from musicfusion.services.albumverify import (
        ai_pair_verdicts,
    )

    class AlwaysEmpty(OllamaClient):
        def __init__(self):
            super().__init__(host="http://fake", model="m")
            self.calls = 0

        def available(self):
            return True

        def chat_json(self, system, user):
            self.calls += 1
            return None   # contenido inservible siempre

    make = _result_factory()
    pairs = [(make("A1"), make("A2")), (make("B1"), make("B2")),
             (make("C1"), make("C2"))]
    stub = AlwaysEmpty()
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        out = ai_pair_verdicts(pairs, ai_client=stub, mb_client=StubMB(),
                               wiki_client=StubWiki(),
                               verdict_cache=Path(td) / "v.json")
    # después del límite de salud deja de llamar (no quema minutos)
    assert stub.calls <= OllamaClient.HEALTH_LIMIT + 1
    assert all(v is None for _, _, v, _ in out)


class StubMB:
    def release_group_tracklist(self, rg_id):
        return None


class StubWiki:
    def album_summary(self, artist, album):
        return None


def _result_factory():
    from musicfusion.services.albumverify import AlbumVerification

    def make(name):
        return AlbumVerification(folder=Path("/x") / name, artist="B",
                                 album_name=name, year=None, local_tracks=3,
                                 status="ok")
    return make

