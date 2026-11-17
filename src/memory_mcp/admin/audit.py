"""Audit trail interface. A real recorder replaces `NullAudit` in a later task."""
from __future__ import annotations

from typing import Any, Protocol

from memory_mcp.admin.models import Principal


class Audit(Protocol):
    def record(self, principal: Principal, action: str, tenant: str | None, target: str | None,
               outcome: str, detail: dict[str, Any] | None = None) -> None: ...


class NullAudit:
    """Records nothing."""

    def record(self, principal: Principal, action: str, tenant: str | None, target: str | None,
               outcome: str, detail: dict[str, Any] | None = None) -> None:
        return None
