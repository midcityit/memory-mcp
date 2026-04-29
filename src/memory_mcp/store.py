import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter,
    FieldCondition, MatchValue, MatchAny,
)
from sentence_transformers import SentenceTransformer

COLLECTION = "memories"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
VECTOR_DIM = 384


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

    def _ensure_collection(self) -> None:
        existing = [c.name for c in self._client.get_collections().collections]
        if COLLECTION not in existing:
            self._client.create_collection(
                collection_name=COLLECTION,
                vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
            )

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
        return record

    def search(
        self,
        query: str,
        limit: int = 10,
        filter_type: Optional[str] = None,
        filter_source_repo: Optional[str] = None,
    ) -> list[MemoryRecord]:
        vector = self._embed(query)
        conditions = []
        if filter_type:
            conditions.append(FieldCondition(key="type", match=MatchValue(value=filter_type)))
        if filter_source_repo:
            conditions.append(FieldCondition(key="source_repo", match=MatchValue(value=filter_source_repo)))
        qdrant_filter = Filter(must=conditions) if conditions else None
        hits = self._client.search(
            collection_name=COLLECTION,
            query_vector=vector,
            limit=limit,
            query_filter=qdrant_filter,
            with_payload=True,
        )
        records = [self._hit_to_record(h) for h in hits]
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

    def update(self, memory_id: str, content: str, tags: list[str]) -> Optional[MemoryRecord]:
        existing = self.get(memory_id)
        if not existing:
            return None
        existing.content = content
        existing.tags = tags
        existing.updated_at = datetime.now(timezone.utc).isoformat()
        return self.upsert(existing)

    def delete(self, memory_id: str) -> bool:
        if not self.get(memory_id):
            return False
        self._client.delete(
            collection_name=COLLECTION,
            points_selector=[memory_id],
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
