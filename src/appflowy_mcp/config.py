"""Configuration loading for appflowy-mcp.

Settings come from (in increasing precedence):

  1. A YAML/JSON config file pointed at by ``APPFLOWY_MCP_CONFIG``.
  2. Environment variables.

Everything is expressible purely through environment variables so the server
can be configured from Docker ``environment:`` blocks or Helm ``env:`` /
``secretKeyRef`` values without mounting a file.

Access model
------------
The server logs into one AppFlowy backend account (the *service account*) and
performs every API call as that account. MCP clients never see those
credentials; they authenticate to *this* server with an opaque **token**, and
each token carries a list of **scopes** that bound what it may touch.

A scope is a path of AppFlowy ids::

    <workspace_id>                          whole workspace
    <workspace_id>/<view_id>                that page + everything nested under it
    <workspace_id>/<view_id>/<view_id>/...  a page several levels deep + its subtree

The last segment is the *root* of the allowed subtree; intermediate segments
only locate it (AppFlowy view ids are globally unique, so they are optional for
enforcement but handy for humans). A token whose scope list is empty has full
access to every workspace the service account can see.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - yaml is a hard dep, but degrade gracefully
    yaml = None


@dataclass(frozen=True)
class ScopeEntry:
    """One allowed subtree: a workspace, optionally narrowed to a view root."""

    workspace_id: str
    root_view_id: str | None = None  # None => the whole workspace
    # Full human-readable path as configured, for logging/errors.
    path: str = ""

    @classmethod
    def parse(cls, raw: str) -> ScopeEntry:
        parts = [p for p in raw.strip().strip("/").split("/") if p]
        if not parts:
            raise ValueError(f"empty scope: {raw!r}")
        workspace_id = parts[0]
        root_view_id = parts[-1] if len(parts) > 1 else None
        return cls(workspace_id=workspace_id, root_view_id=root_view_id, path="/".join(parts))


@dataclass(frozen=True)
class TokenConfig:
    token: str
    name: str = ""
    scopes: tuple[ScopeEntry, ...] = ()

    @property
    def full_access(self) -> bool:
        return len(self.scopes) == 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TokenConfig:
        token = str(d.get("token") or d.get("value") or "").strip()
        if not token:
            raise ValueError("token entry is missing a non-empty 'token'")
        raw_scopes = d.get("scopes") or []
        if isinstance(raw_scopes, str):
            raw_scopes = _split_list(raw_scopes)
        scopes = tuple(ScopeEntry.parse(s) for s in raw_scopes if str(s).strip())
        return cls(token=token, name=str(d.get("name") or ""), scopes=scopes)


@dataclass
class AppFlowyConfig:
    base_url: str = "https://beta.appflowy.cloud"
    email: str | None = None
    password: str | None = None
    access_token: str | None = None  # pre-minted JWT; overrides email/password


@dataclass
class Settings:
    appflowy: AppFlowyConfig
    tokens: list[TokenConfig] = field(default_factory=list)
    host: str = "0.0.0.0"
    port: int = 8000
    path: str = "/mcp"
    # When True (default) a request without a valid token is rejected. When
    # False *and* no tokens are configured, the server runs wide open — handy
    # for a trusted single-user setup, dangerous on a shared network.
    require_auth: bool = True
    folder_cache_ttl: float = 15.0
    log_level: str = "INFO"


def _split_list(raw: str) -> list[str]:
    """Split a comma/whitespace/newline separated env value into items."""
    out: list[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        item = chunk.strip()
        if item:
            out.append(item)
    return out


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _load_file(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith((".yaml", ".yml")):
        if yaml is None:
            raise RuntimeError("PyYAML is required to read YAML config files")
        return yaml.safe_load(text) or {}
    return json.loads(text or "{}")


def _tokens_from_env() -> list[TokenConfig]:
    """Read token definitions from the environment.

    Two equivalent forms are supported:

    * **JSON blob** — best for a single Helm/Docker secret::

          APPFLOWY_MCP_TOKENS='[{"token":"...","name":"full","scopes":[]},
                                {"token":"...","scopes":["WS/VIEW","WS2"]}]'

    * **Indexed** — avoids embedding JSON::

          APPFLOWY_MCP_TOKEN_0=secret-aaa
          APPFLOWY_MCP_TOKEN_0_NAME=full
          APPFLOWY_MCP_TOKEN_0_SCOPES=                # empty => all workspaces
          APPFLOWY_MCP_TOKEN_1=secret-bbb
          APPFLOWY_MCP_TOKEN_1_SCOPES=WS,WS/VIEW/DEEPVIEW
    """
    tokens: list[TokenConfig] = []

    blob = os.getenv("APPFLOWY_MCP_TOKENS")
    if blob:
        data = json.loads(blob)
        if isinstance(data, dict):
            data = [data]
        for entry in data:
            tokens.append(TokenConfig.from_dict(entry))

    # Indexed form: scan APPFLOWY_MCP_TOKEN_<i>.
    indices = sorted(
        {
            key[len("APPFLOWY_MCP_TOKEN_") :].split("_", 1)[0]
            for key in os.environ
            if key.startswith("APPFLOWY_MCP_TOKEN_")
            and key[len("APPFLOWY_MCP_TOKEN_") :].split("_", 1)[0].isdigit()
        },
        key=int,
    )
    for idx in indices:
        token = os.environ.get(f"APPFLOWY_MCP_TOKEN_{idx}")
        if not token:
            continue
        name = os.environ.get(f"APPFLOWY_MCP_TOKEN_{idx}_NAME", "")
        scopes_raw = os.environ.get(f"APPFLOWY_MCP_TOKEN_{idx}_SCOPES", "")
        tokens.append(
            TokenConfig.from_dict(
                {"token": token, "name": name, "scopes": _split_list(scopes_raw)}
            )
        )
    return tokens


def load_settings() -> Settings:
    """Build :class:`Settings` from the config file and environment."""
    file_data: dict[str, Any] = {}
    cfg_path = os.getenv("APPFLOWY_MCP_CONFIG")
    if cfg_path:
        file_data = _load_file(cfg_path)

    af_file = file_data.get("appflowy", {}) if isinstance(file_data, dict) else {}
    appflowy = AppFlowyConfig(
        base_url=os.getenv("APPFLOWY_BASE_URL")
        or af_file.get("base_url")
        or "https://beta.appflowy.cloud",
        email=os.getenv("APPFLOWY_EMAIL") or af_file.get("email"),
        password=os.getenv("APPFLOWY_PASSWORD") or af_file.get("password"),
        access_token=os.getenv("APPFLOWY_ACCESS_TOKEN") or af_file.get("access_token"),
    )

    # Tokens: file first, then env (env appended; env wins on duplicate token value).
    tokens: list[TokenConfig] = []
    for entry in file_data.get("tokens", []) if isinstance(file_data, dict) else []:
        tokens.append(TokenConfig.from_dict(entry))
    env_tokens = _tokens_from_env()
    by_value = {t.token: t for t in tokens}
    for t in env_tokens:
        by_value[t.token] = t
    tokens = list(by_value.values())

    server_file = file_data.get("server", {}) if isinstance(file_data, dict) else {}
    host = os.getenv("APPFLOWY_MCP_HOST") or server_file.get("host") or "0.0.0.0"
    port = int(os.getenv("APPFLOWY_MCP_PORT") or server_file.get("port") or 8000)
    path = os.getenv("APPFLOWY_MCP_PATH") or server_file.get("path") or "/mcp"
    require_auth = _env_bool(
        "APPFLOWY_MCP_REQUIRE_AUTH", bool(server_file.get("require_auth", True))
    )
    ttl = float(
        os.getenv("APPFLOWY_MCP_FOLDER_CACHE_TTL")
        or server_file.get("folder_cache_ttl")
        or 15.0
    )
    log_level = os.getenv("APPFLOWY_MCP_LOG_LEVEL") or server_file.get("log_level") or "INFO"

    return Settings(
        appflowy=appflowy,
        tokens=tokens,
        host=host,
        port=port,
        path=path,
        require_auth=require_auth,
        folder_cache_ttl=ttl,
        log_level=str(log_level).upper(),
    )
