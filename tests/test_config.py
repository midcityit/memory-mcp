import os
import pytest
from memory_mcp.config import load_config


def test_load_config_reads_env_vars(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("API_TOKEN", "test-token")
    monkeypatch.delenv("STALE_DAYS", raising=False)
    monkeypatch.delenv("OTLP_ENDPOINT", raising=False)
    cfg = load_config()
    assert cfg.qdrant_url == "http://localhost:6333"
    assert cfg.api_token == "test-token"
    assert cfg.stale_days == 30
    assert cfg.otlp_endpoint == "http://otel-collector.monitoring.svc.cluster.local:4317"


def test_load_config_custom_stale_days(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("API_TOKEN", "tok")
    monkeypatch.setenv("STALE_DAYS", "14")
    cfg = load_config()
    assert cfg.stale_days == 14


def test_load_config_missing_qdrant_url_raises(monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.setenv("API_TOKEN", "tok")
    with pytest.raises(KeyError):
        load_config()


def test_load_config_missing_api_token_raises(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.delenv("API_TOKEN", raising=False)
    with pytest.raises(KeyError):
        load_config()


def test_kg_settings_default_off(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://q:6333")
    monkeypatch.setenv("API_TOKEN", "t")
    for v in ("KG_ENABLED", "KG_CONFIG_DIR", "KG_DUP_THRESHOLD", "KG_MAX_CHARS"):
        monkeypatch.delenv(v, raising=False)
    from memory_mcp.config import load_config
    c = load_config()
    assert (c.kg_enabled, c.kg_config_dir, c.kg_dup_threshold, c.kg_max_chars) == (False, None, 0.90, 6000)


def test_kg_settings_from_env(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://q:6333")
    monkeypatch.setenv("API_TOKEN", "t")
    monkeypatch.setenv("KG_ENABLED", "True")
    monkeypatch.setenv("KG_CONFIG_DIR", "/etc/kg")
    monkeypatch.setenv("KG_DUP_THRESHOLD", "0.8")
    monkeypatch.setenv("KG_MAX_CHARS", "9000")
    from memory_mcp.config import load_config
    c = load_config()
    assert (c.kg_enabled, c.kg_config_dir, c.kg_dup_threshold, c.kg_max_chars) == (True, "/etc/kg", 0.8, 9000)
