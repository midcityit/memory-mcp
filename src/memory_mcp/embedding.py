"""Shared sentence-embedding model. Load once per worker; reuse everywhere."""
from functools import lru_cache

from sentence_transformers import SentenceTransformer

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
VECTOR_DIM = 384
_CACHE_SIZE = 512


class Embedder:
    def __init__(self, model_name: str = EMBEDDING_MODEL):
        self._model = SentenceTransformer(model_name)
        # Per-instance cache (a module-level lru_cache would be shared across instances).
        self._cached = lru_cache(maxsize=_CACHE_SIZE)(self._encode)

    def _encode(self, text: str) -> tuple[float, ...]:
        return tuple(self._model.encode(text).tolist())

    def embed(self, text: str) -> list[float]:
        return list(self._cached(text))

    @property
    def dim(self) -> int:
        return VECTOR_DIM
