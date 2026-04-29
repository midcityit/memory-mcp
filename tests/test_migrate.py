import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from memory_mcp.migrate import parse_memory_file, derive_source_repo, run_migration


def write_md(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "file.md"
    p.write_text(content)
    return p


def test_parse_memory_file_extracts_frontmatter(tmp_path):
    p = write_md(tmp_path, textwrap.dedent("""\
        ---
        name: test memory
        type: project
        description: a test desc
        ---
        Body content here.
    """))
    result = parse_memory_file(p)
    assert result["name"] == "test memory"
    assert result["type"] == "project"
    assert result["content"] == "Body content here.\n"


def test_parse_memory_file_missing_frontmatter(tmp_path):
    p = write_md(tmp_path, "Just plain text with no frontmatter.")
    result = parse_memory_file(p)
    assert result["name"] == "file"
    assert result["type"] == "reference"
    assert "plain text" in result["content"]


def test_derive_source_repo_from_path():
    path = Path("/Users/ccastro/.claude/projects/-Users-ccastro-GitHub-centroculturaltechantit-web/memory/foo.md")
    assert derive_source_repo(path) == "centroculturaltechantit-web"


def test_derive_source_repo_unknown():
    path = Path("/some/unknown/path/foo.md")
    assert derive_source_repo(path) == "global"


def test_run_migration_counts_and_skips_memory_md(tmp_path):
    # Create directory structure: tmp_path/-Users-proj1/memory/
    proj = tmp_path / "-Users-proj1"
    mem_dir = proj / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("# index")
    (mem_dir / "note1.md").write_text("---\nname: note1\ntype: project\n---\nContent 1")
    (mem_dir / "note2.md").write_text("---\nname: note2\ntype: user\n---\nContent 2")

    store = MagicMock()
    count = run_migration(str(tmp_path), store)

    assert count == 2
    assert store.upsert.call_count == 2
    # MEMORY.md must be skipped
    calls = store.upsert.call_args_list
    names = [c[0][0].name for c in calls]
    assert "MEMORY.md" not in names
    assert "note1" in names
    assert "note2" in names
