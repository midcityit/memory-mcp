from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest
from memory_mcp.store import MemoryStore, MemoryRecord


def make_store():
    with patch("memory_mcp.store.QdrantClient"), \
         patch("memory_mcp.store.SentenceTransformer"):
        store = MemoryStore(qdrant_url="http://localhost:6333", stale_days=30)
        store._client = MagicMock()
        store._model = MagicMock()
        store._model.encode.return_value = [0.1] * 384
        return store


def test_is_stale_old_memory():
    store = make_store()
    old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    assert store.is_stale(old_date) is True


def test_is_stale_fresh_memory():
    store = make_store()
    fresh_date = datetime.now(timezone.utc).isoformat()
    assert store.is_stale(fresh_date) is False


def test_is_stale_exactly_threshold():
    store = make_store()
    boundary = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    assert store.is_stale(boundary) is False


def test_memory_record_stale_field():
    store = make_store()
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    record = MemoryRecord(
        id="abc", type="project", name="test", content="body",
        source_repo="repo", agent="claude-code", tags=[],
        created_at=old, updated_at=old,
    )
    assert store.annotate_staleness([record], stale_days=30)[0].stale is True
