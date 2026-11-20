from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Tenant:
    id: str
    name: str
    description: str
    collection: str
    status: str  # "active" | "disabled"


@dataclass(frozen=True)
class ApiKey:
    id: str
    agent: str
    secret_hash: str
    tenants: tuple[str, ...]
    access: str  # "read" | "write"
    denied_tools: tuple[str, ...]
    source_repo_patterns: tuple[str, ...]
    expires_at: datetime | None
    created_at: datetime
    created_by: str
    revoked_at: datetime | None
    last_used_at: datetime | None
    rotated_from: str | None


@dataclass(frozen=True)
class RoleMapping:
    group_id: str
    tenant: str
    role: str


@dataclass(frozen=True)
class Principal:
    kind: str  # "key" | "legacy" | "ui"
    id: str  # key id, "legacy", or user id
    agent: str | None  # stamped agent; None for legacy (caller-declared)
    roles: Mapping[str, str]  # tenant -> "viewer" | "editor" | "admin"; "*" allowed
    access: str = "write"  # keys only
    denied_tools: frozenset[str] = frozenset()
    source_repo_patterns: tuple[str, ...] = ()
    user_name: str | None = None
    all_tenants: bool = False  # legacy


class AuthzError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status  # 400 | 401 | 403 | 404 | 503
        self.code = code
        self.message = message
