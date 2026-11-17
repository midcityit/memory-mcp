#!/usr/bin/env python3
"""One-off: move some tenants' memories out of a shared Qdrant collection.

Before multi-tenant mode, one collection (default `memories`) can hold memories that
belong to several tenants. The bootstrap tenant adopts that collection as-is; every
other tenant gets its own collection (`<prefix><tenant>`, default `memories_<tenant>`).
This script moves the other tenants' points into their collections, keeping each
point's id, payload and vector so links that refer to memory ids stay valid.

Nothing is built in: tenants, repo patterns and tags all come from the command line.

Rules (repeatable, first match wins, in the order given):
  --rule 'source_repo=<glob>:<tenant>'   fnmatch glob, case-sensitive
  --rule 'tag=<tag>:<tenant>'            exact tag match
Points that match no rule stay with --default (the bootstrap tenant) and never move.
--overrides is a CSV with columns `id,tenant` and beats the rules for single points.

Dry run (default) only reads: it prints counts and, with --out, writes a CSV
(id,name,source_repo,tags,tenant) to review. --apply re-evaluates everything against
live data, takes a snapshot of the source (the rollback; it aborts if that fails),
copies the points, verifies they all arrived intact, and only then deletes them from the
source, re-checking each batch just before deleting. Pause writes for the tenants being moved.
A rerun after a failure is safe: points already in the target are skipped.

Example:
  python scripts/split_memories.py --qdrant-url http://qdrant:6333 \\
    --rule 'source_repo=org-b/*:beta' --rule 'tag=beta:beta' \\
    --default alpha --out plan.csv
  # review plan.csv, add corrections in overrides.csv, then:
  python scripts/split_memories.py --qdrant-url http://qdrant:6333 \\
    --rule 'source_repo=org-b/*:beta' --rule 'tag=beta:beta' \\
    --default alpha --overrides overrides.csv --apply
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable, Iterable, Iterator, Mapping

from qdrant_client import QdrantClient
from qdrant_client.models import PointIdsList, PointStruct

from memory_mcp.store import MemoryStore

TENANT_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
BATCH = 256
RULE_KINDS = ("source_repo", "tag")


class ApplyError(RuntimeError):
    """Apply stopped before deleting anything from the source."""


@dataclass(frozen=True)
class Rule:
    kind: str  # "source_repo" | "tag"
    pattern: str
    tenant: str

    def matches(self, payload: Mapping) -> bool:
        if self.kind == "source_repo":
            return fnmatch.fnmatchcase(str(payload.get("source_repo") or ""), self.pattern)
        return self.pattern in (payload.get("tags") or [])


def check_tenant(tenant: str) -> str:
    if not TENANT_RE.match(tenant or ""):
        raise ValueError(
            f"invalid tenant id {tenant!r}: must match {TENANT_RE.pattern} "
            "(lowercase letter, then 1-31 lowercase letters, digits or underscores)"
        )
    return tenant


def parse_rule(text: str) -> Rule:
    kind, sep, rest = text.partition("=")
    if not sep or kind not in RULE_KINDS:
        raise ValueError(
            f"malformed rule {text!r}: expected 'source_repo=<glob>:<tenant>' or 'tag=<tag>:<tenant>'"
        )
    pattern, sep, tenant = rest.rpartition(":")
    if not sep or not pattern:
        raise ValueError(f"malformed rule {text!r}: expected '<value>:<tenant>' after '{kind}='")
    return Rule(kind, pattern, check_tenant(tenant))


def plan(points: Iterable, rules: list[Rule], default: str,
         overrides: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """Return (id, tenant) for every point. Override, then first matching rule, then default."""
    overrides = overrides or {}
    out: list[tuple[str, str]] = []
    for p in points:
        pid = str(p.id)
        if pid in overrides:
            out.append((pid, overrides[pid]))
            continue
        payload = p.payload or {}
        out.append((pid, next((r.tenant for r in rules if r.matches(payload)), default)))
    return out


def scroll_all(client: QdrantClient, collection: str, *, with_payload=True) -> Iterator:
    offset = None
    while True:
        records, offset = client.scroll(
            collection_name=collection, limit=BATCH, offset=offset,
            with_payload=with_payload, with_vectors=False,
        )
        yield from records
        if offset is None:
            return


def _chunks(items: list, n: int = BATCH) -> Iterator[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _ensure_collection(client: QdrantClient, name: str) -> None:
    # Same vector config and payload indexes as the server; only `_client` is used.
    MemoryStore.ensure_collection(SimpleNamespace(_client=client), name)


VECTOR_TOL = 1e-6
CHANGED_MSG = "Some memories changed during the split; rerun --apply to finish."


def _same(a, b) -> bool:
    """Same payload and (within VECTOR_TOL) same vector."""
    if (a.payload or {}) != (b.payload or {}):
        return False
    va, vb = a.vector, b.vector
    return len(va) == len(vb) and all(abs(x - y) <= VECTOR_TOL for x, y in zip(va, vb))


def _get(client: QdrantClient, collection: str, ids: list[str]) -> dict[str, object]:
    recs = client.retrieve(collection, ids=ids, with_payload=True, with_vectors=True)
    return {str(r.id): r for r in recs}


def _confirm_unchanged(client: QdrantClient, source: str, target: str,
                       batch: list[str]) -> tuple[list[str], list[str]]:
    """Re-read `batch` on both sides; return (identical ids, ids changed or missing)."""
    src, dst = _get(client, source, batch), _get(client, target, batch)
    same = [i for i in batch if i in src and i in dst and _same(src[i], dst[i])]
    keep = set(same)
    # A point already gone from the source needs no delete and is not "changed".
    return same, [i for i in batch if i not in keep and i in src]


def _check_unnamed_vectors(client: QdrantClient, source: str) -> None:
    vectors = client.get_collection(source).config.params.vectors
    if isinstance(vectors, dict):
        raise ApplyError(
            f"{source!r} uses named vectors, which this script does not support; nothing written"
        )


def apply(client: QdrantClient, source: str, plan: list[tuple[str, str]],
          collections: dict[str, str], snapshot_fn: Callable[[str], str]) -> dict:
    """Copy planned points to their tenants' collections, verify, then delete from source.

    `collections` maps tenant -> collection. A tenant whose collection is the source
    keeps its points. The source is authoritative until a point is deleted: a target
    point with the same id but different content is overwritten. Before each delete
    batch the source points are re-read and only those still identical to their target
    copy are deleted; the others stay and are reported.

    Returns {tenant: {copied, skipped_existing, overwritten, deleted,
    changed_after_copy, changed_ids}}. Raises ApplyError (or the snapshot error) before
    any delete if something is off.
    """
    moves: dict[str, list[str]] = {}
    for pid, tenant in plan:
        if tenant not in collections:
            raise ApplyError(f"no collection given for tenant {tenant!r}")
        if collections[tenant] != source:
            moves.setdefault(tenant, []).append(pid)

    if not moves:
        print("nothing to move; no snapshot taken")
        return {}
    _check_unnamed_vectors(client, source)
    for tenant in sorted(collections):
        n = len(moves.get(tenant, []))
        print(f"{tenant} -> {collections[tenant]}: " + (f"{n} to move" if n else "stays"))

    snapshot_fn(source)  # raises on failure: nothing written yet

    result = {t: {"copied": 0, "skipped_existing": 0, "overwritten": 0, "deleted": 0,
                  "changed_after_copy": 0, "changed_ids": []} for t in moves}
    for tenant, ids in moves.items():
        target = collections[tenant]
        _ensure_collection(client, target)
        for batch in _chunks(ids):
            src, dst = _get(client, source, batch), _get(client, target, batch)
            missing = [i for i in batch if i not in src]
            if missing:
                raise ApplyError(
                    f"{len(missing)} planned point(s) for {tenant!r} vanished from "
                    f"{source!r} during the copy; nothing deleted"
                )
            todo = []
            for i in batch:
                if i not in dst:
                    result[tenant]["copied"] += 1
                    todo.append(i)
                elif _same(src[i], dst[i]):
                    result[tenant]["skipped_existing"] += 1
                else:
                    result[tenant]["overwritten"] += 1
                    todo.append(i)
            if todo:
                client.upsert(target, points=[
                    PointStruct(id=src[i].id, vector=src[i].vector, payload=src[i].payload)
                    for i in todo
                ])
            # Verify what the target now holds against what was just read from the source.
            after = _get(client, target, batch)
            bad = [i for i in batch if i not in after or not _same(src[i], after[i])]
            if bad:
                raise ApplyError(
                    f"verify failed for {tenant!r}: {len(bad)} of {len(batch)} points in "
                    f"{target!r} are missing or differ from the source; nothing deleted"
                )

    for tenant, ids in moves.items():
        for batch in _chunks(ids):
            same, changed = _confirm_unchanged(client, source, collections[tenant], batch)
            if same:
                client.delete(source, points_selector=PointIdsList(points=same))
            result[tenant]["deleted"] += len(same)
            result[tenant]["changed_after_copy"] += len(changed)
            result[tenant]["changed_ids"] += changed
    return result


def make_snapshot_fn(client: QdrantClient) -> Callable[[str], str]:
    def snapshot(collection: str) -> str:
        desc = client.create_snapshot(collection_name=collection)
        name = getattr(desc, "name", None)
        if not name:
            raise ApplyError(f"snapshot of {collection!r} returned no name")
        print(f"snapshot of {collection!r}: {name}")
        return name
    return snapshot


def unmatched_overrides(planned: list[tuple[str, str]], overrides: Mapping[str, str]) -> list[str]:
    known = {pid for pid, _ in planned}
    return sorted(i for i in overrides if i not in known)


def read_overrides(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not {"id", "tenant"} <= set(reader.fieldnames):
            raise ValueError(f"{path}: overrides CSV needs columns id,tenant")
        return {row["id"].strip(): check_tenant(row["tenant"].strip()) for row in reader}


def write_plan_csv(path: str, points: list, planned: list[tuple[str, str]]) -> None:
    tenant_of = dict(planned)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "source_repo", "tags", "tenant"])
        for p in points:
            pl = p.payload or {}
            w.writerow([str(p.id), pl.get("name", ""), pl.get("source_repo", ""),
                        ";".join(pl.get("tags") or []), tenant_of[str(p.id)]])


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Move other tenants' memories out of a shared collection. "
                    "Dry run unless --apply is given.",
        epilog="Example: --rule 'source_repo=org-b/*:beta' --rule 'tag=beta:beta' --default alpha",
    )
    ap.add_argument("--qdrant-url", default=os.environ.get("QDRANT_URL"),
                    help="Qdrant URL (default: env QDRANT_URL)")
    ap.add_argument("--source", default="memories", help="shared collection (default: memories)")
    ap.add_argument("--rule", action="append", default=[], metavar="RULE",
                    help="'source_repo=<glob>:<tenant>' or 'tag=<tag>:<tenant>'; repeatable, first match wins")
    ap.add_argument("--default", required=True, metavar="TENANT",
                    help="tenant that keeps unmatched points (the bootstrap tenant)")
    ap.add_argument("--overrides", help="CSV with columns id,tenant; beats the rules")
    ap.add_argument("--out", help="write the dry-run plan CSV here")
    ap.add_argument("--apply", action="store_true", help="copy, verify and delete (default: dry run)")
    ap.add_argument("--collection-prefix", default="memories_",
                    help="target collection = prefix + tenant (default: memories_)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rules = [parse_rule(r) for r in args.rule]
        default = check_tenant(args.default)
        overrides = read_overrides(args.overrides)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not args.qdrant_url:
        print("error: --qdrant-url or QDRANT_URL required", file=sys.stderr)
        return 2

    client = QdrantClient(url=args.qdrant_url)
    points = list(scroll_all(client, args.source))
    planned = plan(points, rules, default, overrides)
    counts = Counter(t for _, t in planned)
    for tenant, n in sorted(counts.items()):
        print(f"{tenant}: {n}" + ("  (stays)" if tenant == default else ""))
    for pid in unmatched_overrides(planned, overrides):
        print(f"warning: override id {pid} matches no point in {args.source!r}", file=sys.stderr)

    if args.out:
        write_plan_csv(args.out, points, planned)
        print(f"plan written to {args.out}")
    if not args.apply:
        print("dry run: nothing written to Qdrant. Re-run with --apply to move points.")
        return 0

    # Re-evaluate against live data (writes since the dry run are included).
    collections = {t: args.source if t == default else f"{args.collection_prefix}{t}"
                   for t in counts | {default: 0}}
    try:
        result = apply(client, args.source, planned, collections, make_snapshot_fn(client))
    except Exception as e:
        print(f"error: {e}; nothing deleted from {args.source!r}", file=sys.stderr)
        return 1
    changed = False
    for tenant, r in sorted(result.items()):
        print(f"{tenant}: copied={r['copied']} skipped_existing={r['skipped_existing']} "
              f"overwritten={r['overwritten']} deleted={r['deleted']} "
              f"changed_after_copy={r['changed_after_copy']}")
        for pid in r["changed_ids"]:
            changed = True
            print(f"  changed after copy, left in {args.source!r}: {pid}")
    if changed:
        print(CHANGED_MSG, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
