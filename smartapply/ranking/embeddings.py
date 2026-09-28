"""Embeddings providers — modular: OpenAI, local (sentence-transformers), mock.

Switch via ``EMBEDDINGS_PROVIDER`` env var. The pipeline never imports a
concrete provider directly — it goes through ``get_embeddings_provider()``.
"""

from __future__ import annotations

import hashlib
import math
import os
from abc import ABC, abstractmethod

from openai import APIConnectionError, APITimeoutError, RateLimitError
from sqlalchemy import select
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from smartapply.config import get_settings
from smartapply.database import session_scope
from smartapply.database.models import EmbeddingCache
from smartapply.database.repository import record_usage
from smartapply.llm.usage import estimate_cost_usd
from smartapply.logging_setup import get_logger

logger = get_logger(__name__)
_RETRYABLE_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError)


class EmbeddingsProvider(ABC):
    name: str = ""

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def _cache_key(self, text: str) -> str:
        payload = f"{self.model_name}\0{text}".encode()
        return hashlib.sha256(payload).hexdigest()

    def _read_cache(self, keys: list[str]) -> dict[str, list[float]]:
        try:
            with session_scope() as session:
                rows = session.scalars(
                    select(EmbeddingCache).where(EmbeddingCache.cache_key.in_(set(keys)))
                ).all()
                return {row.cache_key: list(row.vector) for row in rows}
        except Exception as exc:
            logger.warning("Embedding cache lookup failed (continuing): %s", exc)
            return {}

    def _write_cache(self, vectors: dict[str, list[float]]) -> None:
        if not vectors:
            return
        try:
            with session_scope() as session:
                existing = set(
                    session.scalars(
                        select(EmbeddingCache.cache_key).where(
                            EmbeddingCache.cache_key.in_(set(vectors))
                        )
                    )
                )
                session.add_all(
                    EmbeddingCache(cache_key=key, model=self.model_name, vector=vector)
                    for key, vector in vectors.items()
                    if key not in existing
                )
        except Exception as exc:
            logger.warning("Embedding cache write failed (continuing): %s", exc)


# -------------------- OpenAI --------------------


class OpenAIEmbeddingsProvider(EmbeddingsProvider):
    name = "openai"

    def __init__(self, model: str | None = None, batch_size: int = 96):
        settings = get_settings()
        self._model = model or settings.openai_model_embed
        self.batch_size = batch_size
        self._client = None  # lazy

    @property
    def model_name(self) -> str:
        return self._model

    def _client_lazy(self):
        if self._client is None:
            from openai import OpenAI

            api_key = get_settings().openai_api_key or os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                raise RuntimeError(
                    "OPENAI_API_KEY is not set — cannot create OpenAI embeddings provider"
                )
            self._client = OpenAI(api_key=api_key)
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        keys = [self._cache_key(text) for text in texts]
        cached = self._read_cache(keys)
        missing_by_key: dict[str, str] = {}
        for key, source_text in zip(keys, texts, strict=True):
            if key not in cached:
                missing_by_key.setdefault(key, source_text)

        if not missing_by_key:
            return [cached[key] for key in keys]

        client = self._client_lazy()
        missing_items = list(missing_by_key.items())
        generated: dict[str, list[float]] = {}
        for start in range(0, len(missing_items), self.batch_size):
            batch_items = missing_items[start : start + self.batch_size]
            batch = [text for _, text in batch_items]
            response = self._create_embeddings(client, batch)
            data = sorted(
                response.data,
                key=lambda item: getattr(item, "index", 0),
            )
            if len(data) != len(batch_items):
                raise RuntimeError(
                    "OpenAI embeddings returned a different number of vectors than inputs"
                )
            for (key, _), item in zip(batch_items, data, strict=True):
                generated[key] = list(item.embedding)
            self._record_usage(response)

        self._write_cache(generated)
        cached.update(generated)
        return [cached[key] for key in keys]

    @retry(
        retry=retry_if_exception_type(_RETRYABLE_ERRORS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _create_embeddings(self, client, batch: list[str]):  # noqa: ANN001
        return client.embeddings.create(model=self._model, input=batch)

    def _record_usage(self, response) -> None:  # noqa: ANN001
        usage = getattr(response, "usage", None)
        prompt_tokens = (
            getattr(usage, "prompt_tokens", None) or getattr(usage, "total_tokens", 0) or 0
        )
        if prompt_tokens <= 0:
            return
        try:
            with session_scope() as session:
                record_usage(
                    session,
                    purpose="embeddings",
                    model=self._model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=0,
                    cost_usd=estimate_cost_usd(self._model, prompt_tokens, 0),
                )
        except Exception as exc:
            logger.warning("Embedding usage recording failed (continuing): %s", exc)


# -------------------- Local (sentence-transformers) --------------------


class LocalEmbeddingsProvider(EmbeddingsProvider):
    name = "local"

    def __init__(self, model: str | None = None):
        self._model_name = model or get_settings().local_embeddings_model
        self._model = None  # lazy

    @property
    def model_name(self) -> str:
        return self._model_name

    def _model_lazy(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as e:  # pragma: no cover - optional dep
                raise RuntimeError(
                    "Install 'sentence-transformers' to use the local provider "
                    "(pip install -e '.[local-embeddings]')."
                ) from e
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        keys = [self._cache_key(text) for text in texts]
        cached = self._read_cache(keys)
        missing_by_key: dict[str, str] = {}
        for key, source_text in zip(keys, texts, strict=True):
            if key not in cached:
                missing_by_key.setdefault(key, source_text)
        if not missing_by_key:
            return [cached[key] for key in keys]

        model = self._model_lazy()
        missing_items = list(missing_by_key.items())
        missing_texts = [text for _, text in missing_items]
        vectors = model.encode(missing_texts, normalize_embeddings=True)
        generated = {
            key: list(vector.tolist())
            for (key, _), vector in zip(missing_items, vectors, strict=True)
        }
        self._write_cache(generated)
        cached.update(generated)
        return [cached[key] for key in keys]


# -------------------- Mock (for tests) --------------------


class MockEmbeddingsProvider(EmbeddingsProvider):
    """Deterministic, fast, offline. Vectors are derived from a hash of the
    text so similar texts get similar vectors (within reason).
    """

    name = "mock"
    DIM = 64

    @property
    def model_name(self) -> str:
        return f"mock-embed-{self.DIM}d"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> list[float]:
        # Build a sparse-ish vector from token hashes — keeps cosine-sim
        # meaningful for tests without any model dependency.
        v = [0.0] * self.DIM
        tokens = (text or "").lower().split()
        if not tokens:
            return v
        for tok in tokens:
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            idx = h % self.DIM
            sign = 1.0 if (h >> 8) % 2 == 0 else -1.0
            v[idx] += sign
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


# -------------------- Factory --------------------


_PROVIDERS = {
    "openai": OpenAIEmbeddingsProvider,
    "local": LocalEmbeddingsProvider,
    "mock": MockEmbeddingsProvider,
}


def get_embeddings_provider(name: str | None = None) -> EmbeddingsProvider:
    settings = get_settings()
    chosen = (name or settings.embeddings_provider).lower()
    if chosen not in _PROVIDERS:
        raise ValueError(f"Unknown embeddings provider {chosen!r}. Available: {list(_PROVIDERS)}")
    return _PROVIDERS[chosen]()


# -------------------- Vector utilities --------------------


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
