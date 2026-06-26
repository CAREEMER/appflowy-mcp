# appflowy-mcp

<p align="center">
  <a href="https://hub.docker.com/r/m2n2/appflowy-mcp"><img alt="Docker Image Version" src="https://img.shields.io/docker/v/m2n2/appflowy-mcp?sort=semver&logo=docker&logoColor=white&label=docker%20hub&color=2496ED"></a>
  <a href="https://hub.docker.com/r/m2n2/appflowy-mcp"><img alt="Docker Pulls" src="https://img.shields.io/docker/pulls/m2n2/appflowy-mcp?logo=docker&logoColor=white&color=2496ED"></a>
  <a href="https://hub.docker.com/r/m2n2/appflowy-mcp/tags"><img alt="Docker Image Size" src="https://img.shields.io/docker/image-size/m2n2/appflowy-mcp/latest?logo=docker&logoColor=white&label=image%20size&color=2496ED"></a>
  <img alt="Architectures" src="https://img.shields.io/badge/arch-amd64%20%7C%20arm64-blue?logo=linux&logoColor=white">
  <a href="https://modelcontextprotocol.io"><img alt="MCP" src="https://img.shields.io/badge/MCP-Model%20Context%20Protocol-7C3AED?logo=anthropic&logoColor=white"></a>
  <a href="./AGENTS.md"><img alt="Coverage" src="https://img.shields.io/badge/coverage-100%25-brightgreen?logo=pytest&logoColor=white"></a>
  <a href="./LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green.svg"></a>
</p>

<p align="center">
  🐳 <a href="https://hub.docker.com/r/m2n2/appflowy-mcp"><strong><code>m2n2/appflowy-mcp:latest</code> on Docker Hub</strong></a>
</p>

A **self-hosted, token-scoped [Model Context Protocol](https://modelcontextprotocol.io)
server for [AppFlowy](https://appflowy.io)**. It gives AI agents (Claude, or any
MCP client) tools to read and edit your AppFlowy workspaces — list workspaces,
walk the page tree, create/update/read pages, and edit individual blocks in
place — while **bounding each client to exactly the pages you allow** via
per-token tree-shaped scopes.

- 🔒 **Token-scoped access.** The server logs into AppFlowy once as a service
  account. Clients never see those credentials — they present an opaque token,
  and each token is restricted to a set of workspaces / page subtrees.
- 🌳 **Tree-shaped scopes.** Grant a whole workspace, a top-level page and
  everything under it, or a page four levels deep and its descendants. Mix and
  match several grants per token.
- 🐳 **Runs anywhere.** Streamable-HTTP transport, small multi-arch image
  ([`m2n2/appflowy-mcp`](https://hub.docker.com/r/m2n2/appflowy-mcp), amd64 +
  arm64) on Docker Hub, ready for Docker Compose, Kubernetes, or a Helm chart.
- ✏️ **Real editing.** Append blocks, insert blocks at any position, edit block
  text (rich formatting preserved), and delete blocks — via the same Yjs/CRDT
  path the official web client uses.

## How access works

```
            ┌─────────────┐   token: scopes               ┌──────────────┐
 MCP client │  Authorization: Bearer <token>  ──────────▶ │  appflowy-mcp │
 (Claude)   └─────────────┘                               │  enforces     │
                                                          │  scope, then  │
                                                          │  acts as the  │
            service account (email+password / JWT) ◀──────│  service acct │
                                                          └──────┬───────┘
                                                                 ▼
                                                          AppFlowy Cloud REST
```

Two layers of auth, kept separate:

1. **Backend auth (one service account).** `APPFLOWY_BASE_URL` +
   `APPFLOWY_EMAIL`/`APPFLOWY_PASSWORD` (or a pre-minted `APPFLOWY_ACCESS_TOKEN`).
   The server logs in once and refreshes automatically on expiry.
2. **Client auth (many tokens).** Each MCP client presents a token. The token
   decides *what* it can touch — the backend credentials are never exposed.

### Scopes

A scope is a path of AppFlowy ids:

| Scope                                   | Grants                                            |
| --------------------------------------- | ------------------------------------------------- |
| *(empty list)*                          | **everything** the service account can see        |
| `WORKSPACE`                             | the whole workspace                               |
| `WORKSPACE/VIEW`                        | that page **and everything nested under it**      |
| `WORKSPACE/VIEW_L1/VIEW_L2/VIEW_L3`     | a page several levels deep **and its subtree**    |

The **last** id is the root of the allowed subtree; earlier ids only help locate
it (AppFlowy view ids are globally unique, so intermediate ids are optional). A
token may list **several** scopes to grant multiple disjoint subtrees at once.

Enforcement is by ancestry: for any page a tool touches, the server walks up the
folder tree; if it reaches one of the token's allowed roots, the call proceeds,
otherwise it's rejected. `Get workspace list` and `Get workspace folder` are
pruned to what the token may see.

## Configuration

Everything is configurable by **environment variables** (ideal for Docker /
Helm) and/or a **YAML/JSON file**. Env wins over the file.

### Environment variables

| Variable | Description |
| --- | --- |
| `APPFLOWY_BASE_URL` | AppFlowy Cloud base URL, e.g. `https://appflowy.example.com` |
| `APPFLOWY_EMAIL` / `APPFLOWY_PASSWORD` | Service-account login (GoTrue password grant) |
| `APPFLOWY_ACCESS_TOKEN` | Pre-minted JWT instead of email/password (takes precedence) |
| `APPFLOWY_MCP_CONFIG` | Optional path to a YAML/JSON config file |
| `APPFLOWY_MCP_HOST` / `APPFLOWY_MCP_PORT` / `APPFLOWY_MCP_PATH` | Listen address (default `0.0.0.0:8000/mcp`) |
| `APPFLOWY_MCP_REQUIRE_AUTH` | `true` (default) rejects unauthenticated requests; `false` + no tokens = open mode |
| `APPFLOWY_MCP_FOLDER_CACHE_TTL` | Seconds to cache folder trees for scope checks (default `15`) |
| `APPFLOWY_MCP_LOG_LEVEL` | `INFO` (default), `DEBUG`, … |

**Tokens via env** — two equivalent forms.

JSON blob (best as a single Helm/Docker secret):

```bash
APPFLOWY_MCP_TOKENS='[
  {"token":"sk-full",    "name":"full",    "scopes":[]},
  {"token":"sk-teamws",  "name":"team",    "scopes":["WORKSPACE_ID"]},
  {"token":"sk-project", "name":"project", "scopes":["WORKSPACE_ID/ROOT_VIEW_ID",
                                                     "WORKSPACE_ID/A/B/DEEP_VIEW_ID"]}
]'
```

Indexed (no embedded JSON):

```bash
APPFLOWY_MCP_TOKEN_0=sk-full
APPFLOWY_MCP_TOKEN_0_NAME=full
APPFLOWY_MCP_TOKEN_0_SCOPES=                       # empty => all workspaces

APPFLOWY_MCP_TOKEN_1=sk-project
APPFLOWY_MCP_TOKEN_1_NAME=project
APPFLOWY_MCP_TOKEN_1_SCOPES=WORKSPACE_ID/ROOT_VIEW_ID,WORKSPACE_ID/A/B/DEEP_VIEW_ID
```

### Config file

```yaml
appflowy:
  base_url: https://appflowy.example.com
  email: service@example.com
  password: ${APPFLOWY_PASSWORD}   # plain string; env is not interpolated — set real value
server:
  host: 0.0.0.0
  port: 8000
  path: /mcp
  require_auth: true
tokens:
  - token: sk-full
    name: full
    scopes: []                     # all workspaces
  - token: sk-project
    name: project
    scopes:
      - WORKSPACE_ID/ROOT_VIEW_ID            # a page + its whole subtree
      - WORKSPACE_ID/A/B/DEEP_VIEW_ID        # a deep page + its subtree
```

See [`config.example.yaml`](./config.example.yaml) and [`.env.example`](./.env.example).

## Running

### Docker

```bash
docker run --rm -p 8000:8000 \
  -e APPFLOWY_BASE_URL=https://appflowy.example.com \
  -e APPFLOWY_EMAIL=service@example.com \
  -e APPFLOWY_PASSWORD=secret \
  -e APPFLOWY_MCP_TOKENS='[{"token":"sk-full","scopes":[]}]' \
  m2n2/appflowy-mcp:latest
```

### Docker Compose

```bash
cp .env.example .env   # fill in values
docker compose up -d
```

### Kubernetes / Helm

A minimal chart lives in [`deploy/helm`](./deploy/helm):

```bash
helm install appflowy-mcp ./deploy/helm \
  --set appflowy.baseUrl=https://appflowy.example.com \
  --set appflowy.email=service@example.com \
  --set appflowy.password=secret \
  --set-json 'tokens=[{"token":"sk-full","scopes":[]}]'
```

### From source

```bash
uv run appflowy-mcp
```

## Connecting a client

The server speaks **streamable HTTP** at `http://HOST:PORT/mcp`. Point your MCP
client at it and send the token as a bearer header. For Claude Code:

```json
{
  "mcpServers": {
    "appflowy": {
      "type": "http",
      "url": "https://appflowy-mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer sk-full" }
    }
  }
}
```

Health check: `GET /healthz` → `{"status":"ok"}`.

## Tools

| Tool | Purpose |
| --- | --- |
| `Get workspace list` | List workspaces visible to the token |
| `Get workspace folder` | Page tree of a workspace, pruned to scope |
| `Create new page` | Create a page under an allowed parent |
| `Update page` | Rename / set icon / lock |
| `Get page details` | Full page metadata + content |
| `Append content to page` | Append blocks to the end |
| `Get page blocks` | List a page's blocks in order (ids + text) |
| `Insert block` | Insert a new block at any position |
| `Edit block text` | Replace a block's text/rich content in place |
| `Delete block` | Delete a leaf block |
| `Create database` | Create a grid/board/calendar database under a parent |
| `Get workspace databases` | List databases (+ their views), scoped |
| `Get database fields` | List a database's fields/columns |
| `Get database rows` | List rows, with cell values keyed by field name |
| `Add database field` | Add a column (text, number, select, date, …) |
| `Add database row` | Add a row from `{field: value}` cells |
| `Update database row` | Upsert a row by a stable key (idempotent) |
| `Move page to trash` / `Restore page from trash` / `Delete page from trash` | Trash lifecycle |
| `Get trash` / `Get favorite pages` | Listings, scoped |
| `Toggle favorite page` | (Un)favorite a page |

## Notes & limits

- Block-editing tools require `pycrdt` (bundled). They mirror the web client's
  CRDT `web-update`; there is no official per-block REST endpoint.
- Database row cells are keyed by field **name or id**; values follow the field
  type (string for text/URL, number for Number, bool for Checkbox, ISO-8601 for
  DateTime). `Update database row` derives the row id from `pre_hash`, so reusing
  a key updates the same row — there is no REST endpoint to edit an arbitrary
  existing row by its UUID.
- Scope checks rely on the workspace folder tree, cached for
  `APPFLOWY_MCP_FOLDER_CACHE_TTL` seconds. Newly created pages invalidate the
  cache for their workspace.
- Open mode (`APPFLOWY_MCP_REQUIRE_AUTH=false` with no tokens) grants full
  access to anyone who can reach the port — only use on a trusted network.

## Development

```bash
uv sync            # install runtime + dev dependencies
uv run pytest      # run the test suite with the 100% coverage gate
uv run ruff check  # lint
```

The suite enforces **100% line and branch coverage** (`--cov-fail-under=100` in
[`pyproject.toml`](./pyproject.toml)). CI runs it as the `test` job in
[`.github/workflows/docker.yml`](./.github/workflows/docker.yml); the Docker
image build `needs: test`, so a failing test or a coverage drop blocks the
image from ever being built. See [`AGENTS.md`](./AGENTS.md) for the testing
definition of done.

## License

MIT — see [LICENSE](./LICENSE).

This project began as a self-hosting-focused rework of
[LucasXu0/appflowy_mcp](https://github.com/LucasXu0/appflowy_mcp).
