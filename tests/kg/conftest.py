import hashlib
import math

import pytest
from qdrant_client import QdrantClient

DIM = 384


class StubEmbedder:
    """Deterministic, model-free embedder: same text -> same unit vector.
    Texts sharing words get similar vectors (bag of hashed tokens)."""
    dim = DIM

    def embed(self, text: str) -> list[float]:
        v = [0.0] * DIM
        for tok in text.lower().replace("/", " ").replace("-", " ").split():
            h = int(hashlib.sha256(tok.encode()).hexdigest(), 16)
            v[h % DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


@pytest.fixture
def stub_embedder():
    return StubEmbedder()


@pytest.fixture
def qdrant():
    c = QdrantClient(":memory:")
    yield c
    c.close()
