import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    qdrant_url: str
    api_token: str | None
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
    # RCL-50 dual-write window: when true, writes fan out to BOTH stores (primary =
    # kg_backend, secondary = the other) so they stay consistent during cutover.
    kg_dual_write: bool = False
    kg_dual_write_strict: bool = False
    # Tenants / scoped API keys / RBAC (RCL-73)
    admin_pg_dsn: str | None = None
    ui_token: str | None = None
    admin_group_ids: tuple[str, ...] = ()
    bootstrap_tenant_id: str | None = None
    bootstrap_tenant_name: str | None = None


def load_config() -> Config:
    api_token = os.environ.get("API_TOKEN") or None
    ui_token = os.environ.get("UI_TOKEN") or None
    if not api_token and not ui_token:
        raise ValueError("API_TOKEN or UI_TOKEN required")
    return Config(
        qdrant_url=os.environ["QDRANT_URL"],
        api_token=api_token,
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
        kg_dual_write=os.environ.get("KG_DUAL_WRITE", "false").strip().lower() in ("1", "true", "yes"),
        kg_dual_write_strict=os.environ.get("KG_DUAL_WRITE_STRICT", "false").strip().lower() in ("1", "true", "yes"),
        admin_pg_dsn=os.environ.get("ADMIN_PG_DSN") or os.environ.get("KG_PG_DSN") or None,
        ui_token=ui_token,
        admin_group_ids=tuple(
            g.strip() for g in os.environ.get("ADMIN_GROUP_IDS", "").split(",") if g.strip()
        ),
        bootstrap_tenant_id=os.environ.get("BOOTSTRAP_TENANT_ID") or None,
        bootstrap_tenant_name=os.environ.get("BOOTSTRAP_TENANT_NAME") or None,
    )
