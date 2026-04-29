# tests/test_staleness.py
from datetime import datetime, timezone, timedelta
from memory_mcp.store import MemoryStore, MemoryRecord
from unittest.mock import patch, MagicMock


def make_store(stale_days=30):
    with patch("memory_mcp.store.QdrantClient"), \
         patch("memory_mcp.store.SentenceTransformer"):
        store = MemoryStore(qdrant_url="http://localhost:6333", stale_days=stale_days)
        store._client = MagicMock()
        store._model = MagicMock()
        store._model.encode.return_value = [0.1] * 384
        return store


def make_record(days_old: int) -> MemoryRecord:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return MemoryRecord(
        id="x", type="project", name="n", content="c",
        source_repo="repo", agent="claude-code", tags=[],
        created_at=ts, updated_at=ts,
    )


def test_stale_record_has_warning_field():
    from memory_mcp.server import _record_dict
    record = make_record(31)
    record.stale = True
    d = _record_dict(record)
    assert "stale_warning" in d
    assert "⚠️" in d["stale_warning"]


def test_fresh_record_has_no_warning():
    from memory_mcp.server import _record_dict
    record = make_record(1)
    record.stale = False
    d = _record_dict(record)
    assert "stale_warning" not in d


def test_custom_stale_days_respected():
    store = make_store(stale_days=7)
    record = make_record(8)
    annotated = store.annotate_staleness([record], stale_days=7)
    assert annotated[0].stale is True


def test_exactly_threshold_not_stale():
    store = make_store(stale_days=30)
    record = make_record(30)
    annotated = store.annotate_staleness([record], stale_days=30)
    assert annotated[0].stale is False
