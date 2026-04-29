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
