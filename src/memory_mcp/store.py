import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from opentelemetry import metrics
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter,
    FieldCondition, MatchValue, MatchAny, PointIdsList,
)
from sentence_transformers import SentenceTransformer

COLLECTION = "memories"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
VECTOR_DIM = 384

_meter = metrics.get_meter("memory_mcp")
_upsert_counter = _meter.create_counter("memory_mcp_upsert_total", description="Total upserts")
_search_histogram = _meter.create_histogram("memory_mcp_search_duration_seconds", unit="s", description="Search latency")
_memory_count_gauge: metrics.ObservableGauge | None = None


@dataclass
class MemoryRecord:
    id: str
    type: str
    name: str
    content: str
    source_repo: str
    agent: str
    tags: list[str]
    created_at: str
    updated_at: str
    stale: bool = False


class MemoryStore:
    def __init__(self, qdrant_url: str, stale_days: int = 30):
        self._client = QdrantClient(url=qdrant_url)
        self._model = SentenceTransformer(EMBEDDING_MODEL)
        self._stale_days = stale_days
        self._ensure_collection()
        # Register observable gauge for memory count
        global _memory_count_gauge
        if _memory_count_gauge is None:
            _memory_count_gauge = _meter.create_observable_gauge(
                "memory_mcp_memory_count",
                callbacks=[self._observe_memory_count],
                description="Total memories stored",
            )

    def _ensure_collection(self) -> None:
        existing = [c.name for c in self._client.get_collections().collections]
        if COLLECTION not in existing:
            self._client.create_collection(
                collection_name=COLLECTION,
                vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
            )

    def _observe_memory_count(self, options):
        try:
            info = self._client.get_collection(COLLECTION)
            yield metrics.Observation(info.points_count)
        except Exception:
            pass

    def _embed(self, text: str) -> list[float]:
        return self._model.encode(text).tolist()

    def is_stale(self, updated_at: str) -> bool:
        updated = datetime.fromisoformat(updated_at)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - updated
        # Use a 1-second grace period so that a timestamp set to exactly
        # `stale_days` ago is not considered stale (boundary is exclusive).
        return age > timedelta(days=self._stale_days, seconds=1)

    def annotate_staleness(
        self, records: list[MemoryRecord], stale_days: int
    ) -> list[MemoryRecord]:
        age_limit = timedelta(days=stale_days, seconds=1)
        for r in records:
            updated = datetime.fromisoformat(r.updated_at)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            r.stale = (datetime.now(timezone.utc) - updated) > age_limit
        return records

    def upsert(self, record: MemoryRecord) -> MemoryRecord:
        vector = self._embed(f"{record.name} {record.content}")
        self._client.upsert(
            collection_name=COLLECTION,
            points=[PointStruct(id=record.id, vector=vector, payload={
                "type": record.type,
                "name": record.name,
                "content": record.content,
                "source_repo": record.source_repo,
                "agent": record.agent,
                "tags": record.tags,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            })],
        )
        _upsert_counter.add(1)
        return record

    def search(
        self,
        query: str,
        limit: int = 10,
        filter_type: Optional[str] = None,
        filter_source_repo: Optional[str] = None,
    ) -> list[MemoryRecord]:
        t0 = time.monotonic()
        vector = self._embed(query)
        conditions = []
        if filter_type:
            conditions.append(FieldCondition(key="type", match=MatchValue(value=filter_type)))
        if filter_source_repo:
            conditions.append(FieldCondition(key="source_repo", match=MatchValue(value=filter_source_repo)))
        qdrant_filter = Filter(must=conditions) if conditions else None
        result = self._client.query_points(
            collection_name=COLLECTION,
            query=vector,
            limit=limit,
            query_filter=qdrant_filter,
            with_payload=True,
        )
        _search_histogram.record(
            time.monotonic() - t0,
            {"filter_type": filter_type or "", "filter_source_repo": filter_source_repo or ""},
        )
        records = [self._hit_to_record(h) for h in result.points]
        return self.annotate_staleness(records, self._stale_days)

    def list_memories(
        self,
        filter_type: Optional[str] = None,
        filter_source_repo: Optional[str] = None,
        filter_agent: Optional[str] = None,
        filter_tags: Optional[list[str]] = None,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        conditions = []
        if filter_type:
            conditions.append(FieldCondition(key="type", match=MatchValue(value=filter_type)))
        if filter_source_repo:
            conditions.append(FieldCondition(key="source_repo", match=MatchValue(value=filter_source_repo)))
        if filter_agent:
            conditions.append(FieldCondition(key="agent", match=MatchValue(value=filter_agent)))
        if filter_tags:
            conditions.append(FieldCondition(key="tags", match=MatchAny(any=filter_tags)))
        qdrant_filter = Filter(must=conditions) if conditions else None
        results, _ = self._client.scroll(
            collection_name=COLLECTION,
            scroll_filter=qdrant_filter,
            limit=limit,
            with_payload=True,
        )
        records = [self._hit_to_record(r) for r in results]
        return self.annotate_staleness(records, self._stale_days)

    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        results = self._client.retrieve(
            collection_name=COLLECTION,
            ids=[memory_id],
            with_payload=True,
        )
        if not results:
            return None
        r = self._hit_to_record(results[0])
        r.stale = self.is_stale(r.updated_at)
        return r

    def update(self, memory_id: str, content: str, tags: list[str], type: str | None = None) -> Optional[MemoryRecord]:
        existing = self.get(memory_id)
        if not existing:
            return None
        existing.content = content
        existing.tags = tags
        if type is not None:
            existing.type = type
        existing.updated_at = datetime.now(timezone.utc).isoformat()
        return self.upsert(existing)

    def delete(self, memory_id: str) -> bool:
        existing = self.get(memory_id)
        if not existing:
            return False
        self._client.delete(
            collection_name=COLLECTION,
            points_selector=PointIdsList(points=[memory_id]),
        )
        return True

    @staticmethod
    def _hit_to_record(hit) -> MemoryRecord:
        p = hit.payload
        return MemoryRecord(
            id=str(hit.id),
            type=p["type"],
            name=p["name"],
            content=p["content"],
            source_repo=p["source_repo"],
            agent=p["agent"],
            tags=p.get("tags", []),
            created_at=p["created_at"],
            updated_at=p["updated_at"],
        )

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
