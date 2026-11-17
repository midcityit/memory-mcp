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
    PayloadSchemaType,
)

from memory_mcp.embedding import Embedder, VECTOR_DIM

COLLECTION = "memories"

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
    def __init__(self, qdrant_url: str, stale_days: int = 30, embedder: Embedder | None = None,
                 *, client: QdrantClient | None = None):
        self._client = client if client is not None else QdrantClient(url=qdrant_url)
        self._embedder = embedder or Embedder()
        self._stale_days = stale_days
        self.ensure_collection(COLLECTION)
        # Register observable gauge for memory count
        global _memory_count_gauge
        if _memory_count_gauge is None:
            _memory_count_gauge = _meter.create_observable_gauge(
                "memory_mcp_memory_count",
                callbacks=[self._observe_memory_count],
                description="Total memories stored",
            )

    @property
    def stale_days(self) -> int:
        """The configured staleness threshold (STALE_DAYS)."""
        return self._stale_days

    @property
    def client(self) -> QdrantClient:
        return self._client

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    def ensure_collection(self, collection: str) -> None:
        """Create `collection` (vectors plus the name/source_repo payload indexes) if missing."""
        existing = [c.name for c in self._client.get_collections().collections]
        if collection not in existing:
            try:
                self._client.create_collection(
                    collection_name=collection,
                    vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
                )
            except Exception:
                # Another process (e.g. a second uvicorn worker) may have created it between
                # the check and the create (Qdrant answers 409); that counts as success.
                if not self._client.collection_exists(collection):
                    raise
        # Ensure payload indexes exist for efficient filtering.
        # create_payload_index is idempotent — safe to call on every startup.
        self._client.create_payload_index(
            collection_name=collection,
            field_name="name",
            field_schema=PayloadSchemaType.KEYWORD,
        )
        self._client.create_payload_index(
            collection_name=collection,
            field_name="source_repo",
            field_schema=PayloadSchemaType.KEYWORD,
        )

    def count(self, collection: str) -> int:
        return self._client.get_collection(collection).points_count or 0

    def _observe_memory_count(self, options):
        try:
            info = self._client.get_collection(COLLECTION)
            yield metrics.Observation(info.points_count)
        except Exception:
            pass

    def _embed(self, text: str) -> list[float]:
        return self._embedder.embed(text)

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

    def find_by_name(self, name: str, source_repo: str, *, collection: str = COLLECTION) -> Optional[MemoryRecord]:
        """Find a memory by exact name + source_repo using payload indexes.

        This replaces the O(n) pattern of listing all memories and scanning
        in Python. With keyword indexes on both fields, Qdrant resolves this
        in O(1) via inverted index lookup.
        """
        results, _ = self._client.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=[
                FieldCondition(key="name", match=MatchValue(value=name)),
                FieldCondition(key="source_repo", match=MatchValue(value=source_repo)),
            ]),
            limit=1,
            with_payload=True,
        )
        if not results:
            return None
        return self._hit_to_record(results[0])

    def upsert(self, record: MemoryRecord, *, collection: str = COLLECTION) -> MemoryRecord:
        vector = self._embed(f"{record.name} {record.content}")
        self._client.upsert(
            collection_name=collection,
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
        *,
        collection: str = COLLECTION,
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
            collection_name=collection,
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
        limit: int = 1000,
        offset: Optional[str] = None,
        *,
        collection: str = COLLECTION,
    ) -> tuple[list[MemoryRecord], Optional[str]]:
        """List memories with filtering and cursor-based pagination.

        Returns a tuple of (records, next_offset). If next_offset is None,
        there are no more results. Pass next_offset as the offset parameter
        to retrieve the next page.
        """
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
        results, next_offset = self._client.scroll(
            collection_name=collection,
            scroll_filter=qdrant_filter,
            limit=limit,
            offset=offset,
            with_payload=True,
        )
        records = [self._hit_to_record(r) for r in results]
        next_cursor = str(next_offset) if next_offset else None
        return self.annotate_staleness(records, self._stale_days), next_cursor

    def get(self, memory_id: str, *, collection: str = COLLECTION) -> Optional[MemoryRecord]:
        results = self._client.retrieve(
            collection_name=collection,
            ids=[memory_id],
            with_payload=True,
        )
        if not results:
            return None
        r = self._hit_to_record(results[0])
        r.stale = self.is_stale(r.updated_at)
        return r

    def update(self, memory_id: str, content: str, tags: list[str], type: str | None = None,
               *, collection: str = COLLECTION) -> Optional[MemoryRecord]:
        existing = self.get(memory_id, collection=collection)
        if not existing:
            return None
        existing.content = content
        existing.tags = tags
        if type is not None:
            existing.type = type
        existing.updated_at = datetime.now(timezone.utc).isoformat()
        return self.upsert(existing, collection=collection)

    def delete(self, memory_id: str, *, collection: str = COLLECTION) -> bool:
        existing = self.get(memory_id, collection=collection)
        if not existing:
            return False
        self._client.delete(
            collection_name=collection,
            points_selector=PointIdsList(points=[memory_id]),
        )
        return True

    def get_point(self, memory_id: str, collection: str) -> tuple[MemoryRecord, list[float]] | None:
        """Fetch a record together with its stored vector, or None."""
        results = self._client.retrieve(
            collection_name=collection, ids=[memory_id], with_payload=True, with_vectors=True,
        )
        if not results:
            return None
        return self._hit_to_record(results[0]), list(results[0].vector)

    def put_point(self, record: MemoryRecord, vector: list[float], collection: str,
                  extra_payload: dict | None = None) -> None:
        """Upsert `record` with exactly `vector` (no re-embedding), merging `extra_payload`."""
        payload = {
            **(extra_payload or {}),
            "type": record.type, "name": record.name, "content": record.content,
            "source_repo": record.source_repo, "agent": record.agent, "tags": record.tags,
            "created_at": record.created_at, "updated_at": record.updated_at,
        }
        self._client.upsert(
            collection_name=collection,
            points=[PointStruct(id=record.id, vector=vector, payload=payload)],
        )

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
