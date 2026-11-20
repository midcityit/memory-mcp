"""Audit trail: rows go to the admin store, sensitive values are scrubbed.

`NullAudit` stays for legacy mode (no admin store). `Audit` is the real recorder.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from memory_mcp.admin.authz import READ_ACTIONS
from memory_mcp.admin.models import Principal
from memory_mcp.admin.store import AdminStore
from memory_mcp.kg.models import GRAPH_ID_RE

log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 90
PURGE_INTERVAL_S = 24 * 60 * 60
PURGE_STARTUP_DELAY_S = 30.0

_SENSITIVE_KEYS = frozenset({"content", "secret", "key", "token", "authorization", "password", "secret_hash"})
_FULL_KEY = re.compile(r"mt_[A-Za-z0-9]{8}_\S+")


def audit_tenant(tenant: Any) -> str | None:
    """A caller-supplied tenant/graph name fit for an audit row: kept only if it is a well-formed
    id, so arbitrary input never lands in the log (the same rule as the /admin audit rows)."""
    return tenant if isinstance(tenant, str) and GRAPH_ID_RE.match(tenant) else None


class AuditSink(Protocol):
    def record(self, principal: Principal, action: str, tenant: str | None, target: str | None,
               outcome: str, detail: dict[str, Any] | None = None) -> None: ...


class NullAudit:
    """Records nothing."""

    def record(self, principal: Principal, action: str, tenant: str | None, target: str | None,
               outcome: str, detail: dict[str, Any] | None = None) -> None:
        return None


def _scrub_value(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _scrub_value(x) for k, x in v.items() if str(k).lower() not in _SENSITIVE_KEYS}
    if isinstance(v, (list, tuple)):
        return [_scrub_value(x) for x in v]
    if isinstance(v, str) and _FULL_KEY.search(v):
        return "[redacted]"
    return v


def _scrub(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    """Copy of `detail` without sensitive keys or API-key-shaped strings; input is untouched."""
    if detail is None:
        return None
    return _scrub_value(detail)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Audit:
    def __init__(self, store: AdminStore, settings: Callable[[], dict],
                 clock: Callable[[], datetime] = _utcnow):
        self._store = store
        self._settings = settings
        self._clock = clock

    def record(self, principal: Principal, action: str, tenant: str | None, target: str | None,
               outcome: str, detail: dict[str, Any] | None = None) -> None:
        """Store one row. Never raises: a failed audit write must not fail the request."""
        try:
            if (action in READ_ACTIONS and outcome == "allowed"
                    and self._settings().get("audit_reads") is not True):
                return
            if principal.kind == "ui":
                agent = f"ui:{principal.user_name}"
            else:
                agent = principal.agent
            self._store.add_audit({
                "ts": self._clock(),
                "principal_type": principal.kind,
                "principal_id": principal.id,
                "agent": agent,
                "acting_user": principal.user_name if principal.kind == "ui" else None,
                "tenant": tenant,
                "action": action,
                "target": target,
                "outcome": outcome,
                "detail": _scrub(detail),
            })
        except Exception as e:  # noqa: BLE001 - audit must never break the caller
            log.warning("audit write failed (action=%s outcome=%s): %s", action, outcome, type(e).__name__)

    def purge(self) -> int:
        """Delete rows older than `audit_retention_days` (default 90); returns the count."""
        days = self._settings().get("audit_retention_days")
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            days = DEFAULT_RETENTION_DAYS
        return self._store.purge_audit(self._clock() - timedelta(days=days))


async def run_purge_loop(audit: Audit, *, interval_s: float = PURGE_INTERVAL_S,
                         first_delay_s: float = PURGE_STARTUP_DELAY_S) -> None:
    """Purge once after `first_delay_s`, then every `interval_s`, until cancelled."""
    delay = first_delay_s
    while True:
        await asyncio.sleep(delay)
        try:
            removed = await asyncio.to_thread(audit.purge)
            if removed:
                log.info("audit purge removed %d rows", removed)
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            log.warning("audit purge failed: %s", type(e).__name__)
        delay = interval_s
