import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    qdrant_url: str
    api_token: str
    stale_days: int
    otlp_endpoint: str
    default_list_limit: int
    kg_enabled: bool = False
    kg_config_dir: str | None = None
    kg_dup_threshold: float = 0.90
    kg_max_chars: int = 6000
    # KG 2.0 (RCL-44): storage backend for the knowledge graph. "qdrant" (default)
    # keeps the per-graph Qdrant collections; "postgres" uses PostgresGraphStore
    # against kg_pg_dsn (pgvector + Apache AGE). The memories collection always
    # stays on Qdrant regardless.
    kg_backend: str = "qdrant"
    kg_pg_dsn: str | None = None


def load_config() -> Config:
    return Config(
        qdrant_url=os.environ["QDRANT_URL"],
        api_token=os.environ["API_TOKEN"],
        stale_days=int(os.environ.get("STALE_DAYS", "30")),
        otlp_endpoint=os.environ.get(
            "OTLP_ENDPOINT",
            "http://otel-collector.monitoring.svc.cluster.local:4317",
        ),
        default_list_limit=int(os.environ.get("DEFAULT_LIST_LIMIT", "1000")),
        kg_enabled=os.environ.get("KG_ENABLED", "false").strip().lower() in ("1", "true", "yes"),
        kg_config_dir=os.environ.get("KG_CONFIG_DIR") or None,
        kg_dup_threshold=float(os.environ.get("KG_DUP_THRESHOLD", "0.90")),
        kg_max_chars=int(os.environ.get("KG_MAX_CHARS", "6000")),
        kg_backend=os.environ.get("KG_BACKEND", "qdrant").strip().lower(),
        kg_pg_dsn=os.environ.get("KG_PG_DSN") or None,
    )
