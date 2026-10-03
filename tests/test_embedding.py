from unittest.mock import patch, MagicMock
from memory_mcp.embedding import Embedder, VECTOR_DIM


def test_embedder_caches_and_returns_list():
    with patch("memory_mcp.embedding.SentenceTransformer") as st:
        model = MagicMock()
        model.encode.return_value = MagicMock(tolist=lambda: [0.5] * VECTOR_DIM)
        st.return_value = model
        e = Embedder()
        assert e.embed("hello") == [0.5] * VECTOR_DIM
        e.embed("hello")
        assert model.encode.call_count == 1  # second call served from cache
        assert e.dim == VECTOR_DIM


def test_separate_embedders_do_not_share_cache():
    with patch("memory_mcp.embedding.SentenceTransformer") as st:
        st.return_value.encode.return_value = MagicMock(tolist=lambda: [0.1] * VECTOR_DIM)
        a, b = Embedder(), Embedder()
        a.embed("x"); b.embed("x")
        assert st.return_value.encode.call_count == 2
