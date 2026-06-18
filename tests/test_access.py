"""Unit tests for appflowy_mcp.access."""

from __future__ import annotations

import pytest

from appflowy_mcp.access import (
    AccessControl,
    AccessDenied,
    FolderIndex,
    _children,
    _view_id,
    find_node,
)
from appflowy_mcp.config import ScopeEntry, TokenConfig

# A nested folder tree: root -> A -> B -> C, plus a node missing a view id.
TREE = {
    "view_id": "WS-root",
    "name": "root",
    "children": [
        {
            "view_id": "A",
            "name": "A",
            "children": [
                {
                    "view_id": "B",
                    "name": "B",
                    "children": [{"view_id": "C", "name": "C", "children": []}],
                }
            ],
        },
        {"name": "no-id", "children": [{"view_id": "D", "name": "D", "children": []}]},
    ],
}


class FolderClient:
    def __init__(self, tree=TREE):
        self.tree = tree
        self.calls = 0

    async def get_folder(self, workspace_id):
        self.calls += 1
        return {"data": self.tree}


def full_token():
    return TokenConfig(token="full")


def scoped_token(*scopes):
    return TokenConfig(token="t", name="scoped", scopes=tuple(scopes))


def make_access(tokens=(), require_auth=True, ttl=15.0, client=None):
    return AccessControl(
        client or FolderClient(),
        list(tokens),
        require_auth=require_auth,
        folder_cache_ttl=ttl,
    )


def test_view_id_from_view_id_key():
    assert _view_id({"view_id": "x"}) == "x"


def test_view_id_falls_back_to_id_key():
    assert _view_id({"id": "y"}) == "y"


def test_view_id_none_for_non_dict():
    assert _view_id("nope") is None


def test_children_empty_for_non_dict():
    assert _children("nope") == []


def test_children_empty_when_absent():
    assert _children({}) == []


def test_find_node_matches_root():
    assert find_node(TREE, "WS-root")["name"] == "root"


def test_find_node_finds_nested():
    assert find_node(TREE, "C")["name"] == "C"


def test_find_node_returns_none_when_absent():
    assert find_node(TREE, "missing") is None


def test_folder_index_dataclass_holds_maps():
    idx = FolderIndex(parent={}, name={}, tree=None)
    assert idx.parent == {}


def test_open_mode_true_without_tokens_and_no_auth():
    assert make_access(require_auth=False).open_mode is True


def test_open_mode_false_when_tokens_present():
    assert make_access(tokens=[full_token()], require_auth=False).open_mode is False


def test_resolve_open_mode_returns_synthetic_full_token():
    tok = make_access(require_auth=False).resolve(None)
    assert tok.full_access is True


def test_resolve_returns_none_for_missing_value():
    assert make_access(tokens=[full_token()]).resolve(None) is None


def test_resolve_returns_matching_token():
    tok = full_token()
    assert make_access(tokens=[tok]).resolve("full") is tok


def test_resolve_returns_none_for_unknown_token():
    assert make_access(tokens=[full_token()]).resolve("bogus") is None


def test_workspace_allowed_for_full_access():
    assert make_access().workspace_allowed(full_token(), "WS") is True


def test_workspace_allowed_with_matching_scope():
    ac = make_access()
    assert ac.workspace_allowed(scoped_token(ScopeEntry("WS")), "WS") is True


def test_workspace_denied_without_matching_scope():
    ac = make_access()
    assert ac.workspace_allowed(scoped_token(ScopeEntry("OTHER")), "WS") is False


def test_assert_workspace_raises_when_denied():
    ac = make_access()
    with pytest.raises(AccessDenied):
        ac.assert_workspace(scoped_token(ScopeEntry("OTHER")), "WS")


def test_filter_workspaces_passthrough_for_full_access():
    payload = {"data": [{"workspace_id": "WS"}]}
    assert make_access().filter_workspaces(full_token(), payload) is payload


def test_filter_workspaces_non_list_passthrough():
    payload = {"data": {"not": "a list"}}
    out = make_access().filter_workspaces(scoped_token(ScopeEntry("WS")), payload)
    assert out is payload


def test_filter_workspaces_keeps_allowed_in_data():
    payload = {"data": [{"workspace_id": "WS"}, {"id": "OTHER"}]}
    out = make_access().filter_workspaces(scoped_token(ScopeEntry("WS")), payload)
    assert out["data"] == [{"workspace_id": "WS"}]


def test_filter_workspaces_keeps_allowed_bare_list():
    payload = [{"workspace_id": "WS"}, {"workspace_id": "OTHER"}]
    out = make_access().filter_workspaces(scoped_token(ScopeEntry("WS")), payload)
    assert out == [{"workspace_id": "WS"}]


async def test_view_allowed_for_full_access():
    assert await make_access().view_allowed(full_token(), "WS", "C") is True


async def test_view_allowed_false_without_workspace_entry():
    ac = make_access()
    assert await ac.view_allowed(scoped_token(ScopeEntry("OTHER")), "WS", "C") is False


async def test_view_allowed_true_for_workspace_wide_entry():
    ac = make_access()
    assert await ac.view_allowed(scoped_token(ScopeEntry("WS")), "WS", "C") is True


async def test_view_allowed_true_when_view_is_a_root():
    ac = make_access()
    tok = scoped_token(ScopeEntry("WS", root_view_id="C"))
    assert await ac.view_allowed(tok, "WS", "C") is True


async def test_view_allowed_true_for_descendant_of_root():
    ac = make_access()
    tok = scoped_token(ScopeEntry("WS", root_view_id="A"))
    assert await ac.view_allowed(tok, "WS", "C") is True


async def test_view_allowed_false_for_unrelated_view():
    ac = make_access()
    tok = scoped_token(ScopeEntry("WS", root_view_id="A"))
    assert await ac.view_allowed(tok, "WS", "unknown") is False


async def test_assert_view_raises_when_denied():
    ac = make_access()
    tok = scoped_token(ScopeEntry("WS", root_view_id="A"))
    with pytest.raises(AccessDenied):
        await ac.assert_view(tok, "WS", "unknown")


async def test_folder_index_is_cached_within_ttl():
    client = FolderClient()
    ac = make_access(client=client, ttl=100)
    tok = scoped_token(ScopeEntry("WS", root_view_id="A"))
    await ac.view_allowed(tok, "WS", "C")
    await ac.view_allowed(tok, "WS", "C")
    assert client.calls == 1


async def test_folder_index_rebuilds_after_invalidate():
    client = FolderClient()
    ac = make_access(client=client, ttl=100)
    tok = scoped_token(ScopeEntry("WS", root_view_id="A"))
    await ac.view_allowed(tok, "WS", "C")
    ac.invalidate("WS")
    await ac.view_allowed(tok, "WS", "C")
    assert client.calls == 2


async def test_filter_folder_passthrough_for_full_access():
    payload = {"data": TREE}
    assert await make_access().filter_folder(full_token(), "WS", payload) is payload


async def test_filter_folder_passthrough_for_workspace_wide_entry():
    payload = {"data": TREE}
    tok = scoped_token(ScopeEntry("WS"))
    assert await make_access().filter_folder(tok, "WS", payload) is payload


async def test_filter_folder_prunes_to_allowed_subtrees_in_data():
    payload = {"data": TREE}
    tok = scoped_token(ScopeEntry("WS", root_view_id="A"))
    out = await make_access().filter_folder(tok, "WS", payload)
    kids = out["data"]["children"]
    assert [c["view_id"] for c in kids] == ["A"]


async def test_filter_folder_prunes_bare_payload():
    tok = scoped_token(ScopeEntry("WS", root_view_id="B"))
    out = await make_access().filter_folder(tok, "WS", TREE)
    assert [c["view_id"] for c in out["children"]] == ["B"]


async def test_filter_folder_skips_missing_root():
    tok = scoped_token(ScopeEntry("WS", root_view_id="ghost"))
    out = await make_access().filter_folder(tok, "WS", TREE)
    assert out["children"] == []


async def test_filter_views_passthrough_for_full_access():
    items = [{"view_id": "C"}]
    assert await make_access().filter_views_in_workspace(full_token(), "WS", items) is items


async def test_filter_views_keeps_in_scope_items():
    items = [{"view_id": "C"}, {"view_id": "unknown"}, {"no_view": True}]
    tok = scoped_token(ScopeEntry("WS", root_view_id="A"))
    out = await make_access().filter_views_in_workspace(tok, "WS", items)
    assert out == [{"view_id": "C"}]
