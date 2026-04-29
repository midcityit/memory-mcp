import argparse
import glob
import os
from pathlib import Path
from datetime import datetime, timezone

import frontmatter

from memory_mcp.store import MemoryStore, MemoryRecord


def parse_memory_file(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    try:
        post = frontmatter.loads(raw)
        name = post.get("name") or path.stem
        mem_type = post.get("type") or "reference"
        body = post.content.strip()
        if body:
            # Restore the trailing newline that python-frontmatter strips from the body
            content = body + "\n"
        else:
            content = post.get("description", "").strip() or raw.strip()
    except Exception:
        name = path.stem
        mem_type = "reference"
        content = raw.strip()
    return {"name": name, "type": mem_type, "content": content}


def derive_source_repo(path: Path) -> str:
    """Extract the repo name from the project directory in a Claude projects path.

    The directory at path.parts[-3] encodes the original filesystem path with
    slashes replaced by hyphens, e.g.:
      -Users-ccastro-GitHub-centroculturaltechantit-web

    We split on known separators like '-GitHub-' or '-cctech-' to isolate the
    repo name that follows. Falls back to 'global' if no known separator is found.
    """
    try:
        project_dir = path.parts[-3]
        for separator in ("-GitHub-", "-cctech-", "-Documents-GitHub-"):
            if separator in project_dir:
                return project_dir.split(separator, 1)[1]
    except (IndexError, AttributeError):
        pass
    return "global"


def run_migration(source_dir: str, store: MemoryStore) -> int:
    pattern = os.path.join(source_dir, "*", "memory", "*.md")
    files = glob.glob(pattern)
    count = 0
    for filepath in files:
        path = Path(filepath)
        if path.name == "MEMORY.md":
            continue
        parsed = parse_memory_file(path)
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        record = MemoryRecord(
            id=MemoryStore.new_id(),
            type=parsed["type"],
            name=parsed["name"],
            content=parsed["content"],
            source_repo=derive_source_repo(path),
            agent="migration",
            tags=[],
            created_at=mtime,
            updated_at=mtime,
        )
        store.upsert(record)
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description="Migrate flat-file memories to Qdrant")
    parser.add_argument("--source", required=True, help="Path to ~/.claude/projects")
    parser.add_argument("--qdrant-url", required=True)
    parser.add_argument("--stale-days", type=int, default=30)
    args = parser.parse_args()
    store = MemoryStore(qdrant_url=args.qdrant_url, stale_days=args.stale_days)
    count = run_migration(args.source, store)
    print(f"Migrated {count} memories.")


if __name__ == "__main__":
    main()
