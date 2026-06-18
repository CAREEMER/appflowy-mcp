"""Unit tests for appflowy_mcp.config."""

from __future__ import annotations

import json

import pytest

from appflowy_mcp import config
from appflowy_mcp.config import (
    AppFlowyConfig,
    ScopeEntry,
    Settings,
    TokenConfig,
    _env_bool,
    _load_file,
    _split_list,
    _tokens_from_env,
    load_settings,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("APPFLOWY"):
            monkeypatch.delenv(key, raising=False)


def test_scope_parse_workspace_only():
    entry = ScopeEntry.parse("WS")
    assert entry.workspace_id == "WS"
    assert entry.root_view_id is None


def test_scope_parse_nested_uses_last_segment_as_root():
    entry = ScopeEntry.parse("WS/MID/LEAF")
    assert entry.workspace_id == "WS"
    assert entry.root_view_id == "LEAF"


def test_scope_parse_strips_surrounding_slashes():
    assert ScopeEntry.parse("/WS/").path == "WS"


def test_scope_parse_empty_raises():
    with pytest.raises(ValueError, match="empty scope"):
        ScopeEntry.parse("  /  ")


def test_token_from_dict_uses_token_key():
    tok = TokenConfig.from_dict({"token": "abc"})
    assert tok.token == "abc"


def test_token_from_dict_falls_back_to_value_key():
    assert TokenConfig.from_dict({"value": "xyz"}).token == "xyz"


def test_token_from_dict_missing_token_raises():
    with pytest.raises(ValueError, match="missing a non-empty"):
        TokenConfig.from_dict({"name": "x"})


def test_token_from_dict_scopes_as_string():
    tok = TokenConfig.from_dict({"token": "t", "scopes": "WS, WS2/V"})
    assert [s.workspace_id for s in tok.scopes] == ["WS", "WS2"]


def test_token_from_dict_scopes_as_list_skips_blanks():
    tok = TokenConfig.from_dict({"token": "t", "scopes": ["WS", "  "]})
    assert len(tok.scopes) == 1


def test_token_full_access_when_no_scopes():
    assert TokenConfig(token="t").full_access is True


def test_token_not_full_access_with_scope():
    assert TokenConfig(token="t", scopes=(ScopeEntry("WS"),)).full_access is False


def test_split_list_separators():
    assert _split_list("a, b\nc,,d") == ["a", "b", "c", "d"]


def test_env_bool_default_when_unset(monkeypatch):
    assert _env_bool("APPFLOWY_X", True) is True


def test_env_bool_true_value(monkeypatch):
    monkeypatch.setenv("APPFLOWY_X", "Yes")
    assert _env_bool("APPFLOWY_X", False) is True


def test_env_bool_false_value(monkeypatch):
    monkeypatch.setenv("APPFLOWY_X", "nope")
    assert _env_bool("APPFLOWY_X", True) is False


def test_load_file_json(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"a": 1}))
    assert _load_file(str(p)) == {"a": 1}


def test_load_file_empty_json(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("")
    assert _load_file(str(p)) == {}


def test_load_file_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("a: 1\n")
    assert _load_file(str(p)) == {"a": 1}


def test_load_file_empty_yaml_returns_dict(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("")
    assert _load_file(str(p)) == {}


def test_load_file_yaml_without_pyyaml_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "yaml", None)
    p = tmp_path / "c.yaml"
    p.write_text("a: 1\n")
    with pytest.raises(RuntimeError, match="PyYAML is required"):
        _load_file(str(p))


def test_tokens_from_env_json_blob_list(monkeypatch):
    monkeypatch.setenv(
        "APPFLOWY_MCP_TOKENS", json.dumps([{"token": "a"}, {"token": "b"}])
    )
    tokens = _tokens_from_env()
    assert [t.token for t in tokens] == ["a", "b"]


def test_tokens_from_env_json_blob_single_dict(monkeypatch):
    monkeypatch.setenv("APPFLOWY_MCP_TOKENS", json.dumps({"token": "solo"}))
    assert _tokens_from_env()[0].token == "solo"


def test_tokens_from_env_indexed(monkeypatch):
    monkeypatch.setenv("APPFLOWY_MCP_TOKEN_0", "secret")
    monkeypatch.setenv("APPFLOWY_MCP_TOKEN_0_NAME", "full")
    monkeypatch.setenv("APPFLOWY_MCP_TOKEN_0_SCOPES", "WS,WS/V")
    tokens = _tokens_from_env()
    assert tokens[0].name == "full"
    assert len(tokens[0].scopes) == 2


def test_tokens_from_env_indexed_skips_empty_value(monkeypatch):
    monkeypatch.setenv("APPFLOWY_MCP_TOKEN_0", "")
    assert _tokens_from_env() == []


def test_load_settings_defaults_without_file():
    settings = load_settings()
    assert settings.appflowy.base_url == "https://beta.appflowy.cloud"
    assert settings.tokens == []
    assert settings.require_auth is True


def test_load_settings_reads_file_and_env_override(tmp_path, monkeypatch):
    cfg = {
        "appflowy": {"base_url": "https://file", "email": "f@f", "password": "p"},
        "tokens": [{"token": "file-tok"}],
        "server": {
            "host": "1.2.3.4",
            "port": 9000,
            "path": "/x",
            "require_auth": False,
            "folder_cache_ttl": 5,
            "log_level": "debug",
        },
    }
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("APPFLOWY_MCP_CONFIG", str(p))
    monkeypatch.setenv("APPFLOWY_BASE_URL", "https://env")
    settings = load_settings()
    assert settings.appflowy.base_url == "https://env"
    assert settings.host == "1.2.3.4"
    assert settings.port == 9000
    assert settings.path == "/x"
    assert settings.require_auth is False
    assert settings.folder_cache_ttl == 5.0
    assert settings.log_level == "DEBUG"
    assert [t.token for t in settings.tokens] == ["file-tok"]


def test_load_settings_env_token_overrides_file_duplicate(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"tokens": [{"token": "dup", "name": "from-file"}]}))
    monkeypatch.setenv("APPFLOWY_MCP_CONFIG", str(p))
    monkeypatch.setenv("APPFLOWY_MCP_TOKEN_0", "dup")
    monkeypatch.setenv("APPFLOWY_MCP_TOKEN_0_NAME", "from-env")
    settings = load_settings()
    assert len(settings.tokens) == 1
    assert settings.tokens[0].name == "from-env"


def test_load_settings_non_dict_file_data(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(["not", "a", "dict"]))
    monkeypatch.setenv("APPFLOWY_MCP_CONFIG", str(p))
    settings = load_settings()
    assert settings.tokens == []
    assert settings.host == "0.0.0.0"


def test_settings_dataclass_field_defaults():
    settings = Settings(appflowy=AppFlowyConfig())
    assert settings.tokens == []
    assert settings.port == 8000
