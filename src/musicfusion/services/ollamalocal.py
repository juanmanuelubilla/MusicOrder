"""Local-AI assistance through the user's own Ollama server.

The application can consult a local LLM (Ollama REST API) as an extra,
explainable signal when comparing music information - e.g. deciding whether
two similarly named albums are the same release, a deluxe/reissue variant, a
live version or unrelated records.

Design rules:

* The AI is **never** required: every helper degrades gracefully (returns
  ``None``) when the server is off, the model is missing or the answer is
  unparsable. The deterministic engines keep working without it.
* Answers are requested in strict JSON (``format: "json"``) and validated.
* Verdicts are cached as JSON next to the other caches so repeated runs stay
  offline-fast.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "llama3.1:8b"

RELATIONSHIPS = (
    "same_release",
    "deluxe_or_reissue",
    "live_variant",
    "compilation",
    "unrelated",
    "insufficient_data",
)

_SYSTEM_PROMPT = (
    "Eres un experto en catalogación musical. Comparas dos álbumes y respondes "
    "SIEMPRE con un único objeto JSON válido, sin texto adicional: "
    '{"relationship": "same_release" | "deluxe_or_reissue" | "live_variant" | '
    '"compilation" | "unrelated" | "insufficient_data", '
    '"confidence": <0-100>, "reason": "<una frase corta en español>"}. '
    "Criterios: tracklist idéntica => same_release; edición extendida/deluxe/"
    "reedición con temas extra => deluxe_or_reissue; directo/concierto => live_variant; "
    "recopilación de varios periodos => compilation; álbumes distintos del mismo "
    "artista => unrelated. "
    "IMPORTANTE: decide SOLO con la EVIDENCIA recuperada que se te entrega "
    "(tracklists oficiales, resúmenes de Wikipedia); NO uses tu conocimiento "
    "propio sobre discos ni inventes datos. Si la evidencia no alcanza para "
    "decidir, responde insufficient_data."
)


@dataclass
class AiAlbumVerdict:
    """Structured opinion of the local model about two albums."""

    relationship: str
    confidence: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "relationship": self.relationship,
            "confidence": self.confidence,
            "reason": self.reason,
        }


class OllamaClient:
    """Minimal synchronous client for a local Ollama server.

    Resilience against a loaded/busy local server:

    * one automatic retry when the model returns an EMPTY response;
    * JSON salvage from ``<think>`` blocks emitted by reasoning models;
    * a health breaker: after several consecutive unusable answers the client
      reports itself unhealthy so callers skip further calls instead of
      burning minutes on a broken endpoint.
    """

    #: consecutive unusable answers tolerated before declaring unhealthy
    HEALTH_LIMIT = 4

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        model: str = DEFAULT_MODEL,
        timeout: int = 180,
    ) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._consecutive_bad = 0

    # ---------------------------------------------------------------- api
    @property
    def is_healthy(self) -> bool:
        return self._consecutive_bad < self.HEALTH_LIMIT

    # ---------------------------------------------------------------- api
    def _get(self, path: str) -> dict[str, Any] | None:
        try:
            response = requests.get(f"{self.host}{path}", timeout=4)
            response.raise_for_status()
            return dict(response.json())
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Ollama request failed (%s%s): %s", self.host, path, exc)
            return None

    def list_models(self) -> list[str]:
        """Names of the models installed on the server (empty when offline)."""
        data = self._get("/api/tags")
        if not data:
            return []
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]

    def available(self) -> bool:
        """Whether the server answers and the configured model exists."""
        models = self.list_models()
        if not models:
            return False
        if not self.model or self.model == "*":
            return True
        return any(m.startswith(self.model) for m in models)

    def chat_json(self, system: str, user: str) -> dict[str, Any] | None:
        """One-shot JSON chat; ``None`` on any failure (with one retry)."""
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": 0},
        }
        for attempt in (1, 2):
            data = self._post("/api/chat", payload)
            if data is None:
                break
            message = data.get("message") or {}
            raw_content = str(message.get("content") or "")
            parsed = _extract_json(raw_content)
            if parsed is None:
                # reasoning models may stash the answer inside <think> blocks
                parsed = _extract_json(_strip_think(str(message.get("thinking") or "")))
            if parsed is not None and isinstance(parsed, dict):
                self._consecutive_bad = 0
                return parsed
            logger.warning(
                "Ollama devolvió salida no utilizable (intento %s/2): %.160r",
                attempt, raw_content or message.get("thinking") or "",
            )
        self._consecutive_bad += 1
        return None

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            response = requests.post(
                f"{self.host}{path}", json=payload, timeout=self.timeout
            )
            response.raise_for_status()
            return dict(response.json())
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Ollama request failed (%s%s): %s", self.host, path, exc)
            return None


def _strip_think(text: str) -> str:
    """Remove ``<think>...</think>`` reasoning blocks emitted by some models."""
    import re

    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort extraction of the first JSON object inside ``text``."""
    import re

    if not text:
        return None
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        result = json.loads(match.group(0))
        return result if isinstance(result, dict) else None
    except ValueError:
        return None


def configured_client(settings) -> OllamaClient:
    """Build an :class:`OllamaClient` from the ``ollama.*`` YAML settings.

    Keys: ``ollama.enabled`` (bool), ``ollama.host`` (str),
    ``ollama.model`` (str). When no model is configured, the first model
    installed on the server is used so a fresh install just works.
    """
    host = str(settings.get("ollama.host", DEFAULT_HOST) or DEFAULT_HOST)
    model = str(settings.get("ollama.model", "") or "")
    client = OllamaClient(host=host, model=model or "*")
    if not model:
        models = client.list_models()
        if models:
            preferred = next(
                (m for m in models if m.startswith(DEFAULT_MODEL.split(":")[0])), ""
            )
            client.model = preferred or models[0]
    return client


def ai_album_relationship(
    client: OllamaClient,
    album_a: dict[str, Any],
    album_b: dict[str, Any],
    evidence_a: dict[str, Any] | None = None,
    evidence_b: dict[str, Any] | None = None,
) -> AiAlbumVerdict | None:
    """Ask the local model how two albums relate; ``None`` if unavailable.

    Each album descriptor supports ``artist``, ``title``, ``year``, ``tracks``
    (list of titles) and ``track_count``. The optional evidence dicts carry
    RETRIEVED facts (official MusicBrainz tracklists, Wikipedia summaries) so
    the model reasons over fresh data instead of its training memory - this is
    what makes brand-new albums decidable.
    """
    if not client.available():
        logger.info("Ollama no disponible (%s)", client.host)
        return None
    if not client.is_healthy:
        logger.warning(
            "IA omitida: Ollama acumuló %s respuestas inservibles seguidas",
            client._consecutive_bad,
        )
        return None
    user = (
        f"Álbum A: {album_a.get('artist', '')} — {album_a.get('title', '')} "
        f"({album_a.get('year') or 'año ?'}); "
        f"temas locales: {_tracks_line(album_a)}\n"
        f"Álbum B: {album_b.get('artist', '')} — {album_b.get('title', '')} "
        f"({album_b.get('year') or 'año ?'}); "
        f"temas locales: {_tracks_line(album_b)}\n"
        f"EVIDENCIA recuperada (úsala, no tu memoria):\n"
        f"{_evidence_block('A', evidence_a)}\n"
        f"{_evidence_block('B', evidence_b)}\n"
        "Devuelve el JSON con el veredicto."
    )
    raw = client.chat_json(_SYSTEM_PROMPT, user)
    if raw is None:
        return None
    relationship = str(raw.get("relationship", ""))
    if relationship not in RELATIONSHIPS:
        return None
    try:
        confidence = max(0.0, min(100.0, float(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return AiAlbumVerdict(
        relationship=relationship,
        confidence=confidence,
        reason=str(raw.get("reason", ""))[:300],
    )


def _evidence_block(side: str, evidence: dict[str, Any] | None) -> str:
    """Render retrieved evidence for one album side of the prompt."""
    if not evidence:
        return f"[Álbum {side} · evidencia]: ninguna"
    lines = [f"[Álbum {side} · evidencia]:"]
    official = evidence.get("musicbrainz")
    if isinstance(official, dict):
        titles = official.get("titles") or []
        listing = "; ".join(str(t) for t in titles[:40])
        extra = len(titles) - 40
        lines.append(
            f"- MusicBrainz: '{official.get('title', '')}' "
            f"({official.get('year') or '?'}) — tracklist oficial "
            f"({official.get('count') or len(titles)}): {listing}"
            + (f"; (+{extra} más)" if extra > 0 else "")
        )
    else:
        lines.append("- MusicBrainz: sin coincidencia")
    wiki = evidence.get("wikipedia")
    if isinstance(wiki, dict):
        summary = str(wiki.get("summary", ""))[:900]
        lines.append(
            f"- Wikipedia ({wiki.get('source', '')}, página '{wiki.get('page', '')}')"
            f": {summary}"
        )
    return "\n".join(lines)


def _tracks_line(album: dict[str, Any]) -> str:
    tracks = album.get("tracks") or []
    if tracks:
        shown = "; ".join(str(t) for t in tracks[:30])
        extra = len(tracks) - 30
        return shown + (f"; (+{extra} más)" if extra > 0 else "")
    count = album.get("track_count")
    return f"desconocida ({count} temas)" if count else "desconocida"


def verdict_cache_key(a: dict[str, Any], b: dict[str, Any]) -> str:
    """Stable cache key for a pair of album descriptors."""
    def slug(album: dict[str, Any]) -> str:
        parts = [
            str(album.get("artist", "")).lower(),
            str(album.get("title", "")).lower(),
            str(album.get("year") or ""),
            str(album.get("track_count") or len(album.get("tracks") or [])),
        ]
        return "|".join(parts)

    left, right = slug(a), slug(b)
    return "ai|" + "|".join(sorted((left, right)))


def save_verdict(cache_path: Path, key: str, verdict: AiAlbumVerdict | None) -> None:
    """Persist (or record the absence of) a verdict in a small JSON cache."""
    store: dict[str, Any] = {}
    try:
        if cache_path.exists():
            store = dict(json.loads(cache_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        store = {}
    store[key] = {
        "cached_at": __import__("time").time(),
        "verdict": verdict.as_dict() if verdict else None,
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("AI verdict cache write failed: %s", exc)


def load_verdict(cache_path: Path, key: str) -> AiAlbumVerdict | None:
    """Return a previously saved verdict, or ``None`` (also when absent)."""
    try:
        if not cache_path.exists():
            return None
        store = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = store.get(key)
    if not isinstance(entry, dict):
        return None
    verdict = entry.get("verdict")
    if not isinstance(verdict, dict):
        return None
    if verdict.get("relationship") not in RELATIONSHIPS:
        return None
    return AiAlbumVerdict(
        relationship=verdict["relationship"],
        confidence=float(verdict.get("confidence", 0)),
        reason=str(verdict.get("reason", "")),
    )


__all__ = [
    "AiAlbumVerdict",
    "OllamaClient",
    "ai_album_relationship",
    "configured_client",
    "load_verdict",
    "save_verdict",
    "verdict_cache_key",
]
