import os
import pytest
from memory_mcp.config import load_config


def test_load_config_reads_env_vars(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("API_TOKEN", "test-token")
    monkeypatch.delenv("STALE_DAYS", raising=False)
    cfg = load_config()
    assert cfg.qdrant_url == "http://localhost:6333"
    assert cfg.api_token == "test-token"
    assert cfg.stale_days == 30


def test_load_config_custom_stale_days(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("API_TOKEN", "tok")
    monkeypatch.setenv("STALE_DAYS", "14")
    cfg = load_config()
    assert cfg.stale_days == 14


def test_load_config_missing_required_raises(monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("API_TOKEN", raising=False)
    with pytest.raises(KeyError):
        load_config()
