"""Token-scoped access control over AppFlowy workspaces and page subtrees.

Each MCP client presents an opaque token. A token maps to a set of
:class:`~appflowy_mcp.config.ScopeEntry` roots. Enforcement answers two
questions:

* **workspace-level** — may this token see workspace *W* at all?
* **view-level** — is page *V* inside one of this token's allowed subtrees?

The view-level check needs the workspace's folder tree to know ancestry, so we
keep a short-TTL cache of a parent map per workspace and walk upward from the
target view until we either hit an allowed root or run out of ancestors.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from .appflowy import AppFlowyClient, unwrap
from .config import TokenConfig

log = logging.getLogger("appflowy_mcp.access")


class AccessDenied(PermissionError):
    """Raised when a token is missing, unknown, or out of scope."""


@dataclass
class FolderIndex:
    parent: dict[str, str | None]
    name: dict[str, str]
    tree: Any  # the unwrapped folder root node


def _view_id(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    return node.get("view_id") or node.get("id")


def _children(node: Any) -> list:
    if not isinstance(node, dict):
        return []
    return node.get("children") or []


def find_node(tree: Any, view_id: str) -> dict | None:
    """Depth-first search for a node with the given view id."""
    if _view_id(tree) == view_id:
        return tree
    for child in _children(tree):
        found = find_node(child, view_id)
        if found is not None:
            return found
    return None


class AccessControl:
    def __init__(
        self,
        client: AppFlowyClient,
        tokens: list[TokenConfig],
        *,
        require_auth: bool = True,
        folder_cache_ttl: float = 15.0,
    ) -> None:
        self._client = client
        self._tokens = {t.token: t for t in tokens}
        self._require_auth = require_auth
        self._ttl = folder_cache_ttl
        self._cache: dict[str, tuple[float, FolderIndex]] = {}

    @property
    def open_mode(self) -> bool:
        """True when there is no token gate (no tokens AND auth not required)."""
        return not self._tokens and not self._require_auth

    # -- token resolution --------------------------------------------------
    def resolve(self, token_value: str | None) -> TokenConfig | None:
        """Return the matching token config, or a synthetic full-access token
        in open mode. Returns ``None`` when the token is required but invalid.
        """
        if self.open_mode:
            return TokenConfig(token="(open)", name="open", scopes=())
        if not token_value:
            return None
        return self._tokens.get(token_value)

    # -- folder index caching ---------------------------------------------
    def invalidate(self, workspace_id: str) -> None:
        self._cache.pop(workspace_id, None)

    async def _folder_index(self, workspace_id: str) -> FolderIndex:
        now = time.monotonic()
        cached = self._cache.get(workspace_id)
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        raw = await self._client.get_folder(workspace_id)
        tree = unwrap(raw)
        parent: dict[str, str | None] = {}
        name: dict[str, str] = {}

        def walk(node: Any, par: str | None) -> None:
            vid = _view_id(node)
            if vid:
                parent[vid] = par
                name[vid] = node.get("name", "") if isinstance(node, dict) else ""
            for child in _children(node):
                walk(child, vid)

        walk(tree, None)
        index = FolderIndex(parent=parent, name=name, tree=tree)
        self._cache[workspace_id] = (now, index)
        return index

    # -- workspace-level checks -------------------------------------------
    def _workspace_entries(self, token: TokenConfig, workspace_id: str) -> list:
        return [s for s in token.scopes if s.workspace_id == workspace_id]

    def workspace_allowed(self, token: TokenConfig, workspace_id: str) -> bool:
        if token.full_access:
            return True
        return bool(self._workspace_entries(token, workspace_id))

    def assert_workspace(self, token: TokenConfig, workspace_id: str) -> None:
        if not self.workspace_allowed(token, workspace_id):
            raise AccessDenied(
                f"token '{token.name or '?'}' has no access to workspace {workspace_id}"
            )

    def filter_workspaces(self, token: TokenConfig, payload: Any) -> Any:
        """Filter a `list_workspaces` payload to the allowed workspaces."""
        if token.full_access:
            return payload
        items = unwrap(payload)
        if not isinstance(items, list):
            return payload
        allowed = {s.workspace_id for s in token.scopes}
        kept = [
            w
            for w in items
            if isinstance(w, dict)
            and (w.get("workspace_id") or w.get("id")) in allowed
        ]
        if isinstance(payload, dict) and "data" in payload:
            return {**payload, "data": kept}
        return kept

    # -- view-level checks -------------------------------------------------
    async def view_allowed(
        self, token: TokenConfig, workspace_id: str, view_id: str
    ) -> bool:
        if token.full_access:
            return True
        entries = self._workspace_entries(token, workspace_id)
        if not entries:
            return False
        # Any workspace-wide entry (no root) grants the whole workspace.
        roots = [e.root_view_id for e in entries]
        if any(r is None for r in roots):
            return True
        concrete_roots = {r for r in roots if r}
        if view_id in concrete_roots:
            return True
        # Walk ancestry: is view_id a descendant of any allowed root?
        index = await self._folder_index(workspace_id)
        cur: str | None = view_id
        seen: set[str] = set()
        while cur is not None and cur not in seen:
            if cur in concrete_roots:
                return True
            seen.add(cur)
            cur = index.parent.get(cur)
        return False

    async def assert_view(
        self, token: TokenConfig, workspace_id: str, view_id: str
    ) -> None:
        if not await self.view_allowed(token, workspace_id, view_id):
            raise AccessDenied(
                f"token '{token.name or '?'}' is not allowed to access view "
                f"{view_id} in workspace {workspace_id}"
            )

    async def filter_folder(
        self, token: TokenConfig, workspace_id: str, payload: Any
    ) -> Any:
        """Prune a folder payload to the subtrees this token may see.

        Full-access tokens and tokens with a workspace-wide entry get the whole
        tree. View-scoped tokens get a synthetic root whose children are the
        allowed subtree roots, hiding ancestors and siblings entirely.
        """
        if token.full_access:
            return payload
        entries = self._workspace_entries(token, workspace_id)
        if any(e.root_view_id is None for e in entries):
            return payload
        tree = unwrap(payload)
        allowed_roots = [e.root_view_id for e in entries if e.root_view_id]
        subtrees = [n for vid in allowed_roots if (n := find_node(tree, vid))]
        pruned = {
            "view_id": _view_id(tree),
            "name": tree.get("name") if isinstance(tree, dict) else None,
            "children": subtrees,
        }
        if isinstance(payload, dict) and "data" in payload:
            return {**payload, "data": pruned}
        return pruned

    async def filter_views_in_workspace(
        self, token: TokenConfig, workspace_id: str, items: list, key: str = "view_id"
    ) -> list:
        """Keep only list items whose ``key`` view is in scope (best-effort)."""
        if token.full_access:
            return items
        kept = []
        for item in items:
            vid = item.get(key) if isinstance(item, dict) else None
            if vid and await self.view_allowed(token, workspace_id, vid):
                kept.append(item)
        return kept
