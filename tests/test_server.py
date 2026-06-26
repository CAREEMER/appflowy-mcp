"""Unit tests for appflowy_mcp.server (tool layer + entrypoint)."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from appflowy_mcp import blocks, server
from appflowy_mcp.access import AccessControl
from appflowy_mcp.appflowy import AppFlowyError
from appflowy_mcp.config import (
    AppFlowyConfig,
    ScopeEntry,
    Settings,
    TokenConfig,
)
from tests.conftest import build_collab


def page_view_for(collab_bytes):
    return {"data": {"data": {"encoded_collab": list(collab_bytes)}}}


@pytest.fixture
def headers():
    return {}


@pytest.fixture(autouse=True)
def server_env(monkeypatch, fake_client, headers):
    server.CLIENT = fake_client
    server.ACCESS = AccessControl(fake_client, [], require_auth=False)
    monkeypatch.setattr(server, "get_http_headers", lambda include_all=False: headers)
    return fake_client


# -- _bearer ---------------------------------------------------------------
def test_bearer_none_without_header():
    assert server._bearer() is None


def test_bearer_strips_prefix(headers):
    headers["authorization"] = "Bearer secret"
    assert server._bearer() == "secret"


def test_bearer_accepts_raw_custom_header(headers):
    headers["x-appflowy-mcp-token"] = "raw-token"
    assert server._bearer() == "raw-token"


# -- _token / guards -------------------------------------------------------
def test_token_raises_when_unauthorized(fake_client):
    server.ACCESS = AccessControl(fake_client, [TokenConfig(token="t")])
    with pytest.raises(ToolError, match="unauthorized"):
        server._token()


def test_guard_workspace_raises_on_denied(fake_client, headers):
    tok = TokenConfig(token="t", scopes=(ScopeEntry("OTHER"),))
    server.ACCESS = AccessControl(fake_client, [tok])
    headers["authorization"] = "Bearer t"
    with pytest.raises(ToolError):
        server._guard_workspace("WS")


async def test_guard_view_raises_on_denied(fake_client, headers):
    tok = TokenConfig(token="t", scopes=(ScopeEntry("OTHER"),))
    server.ACCESS = AccessControl(fake_client, [tok])
    headers["authorization"] = "Bearer t"
    with pytest.raises(ToolError):
        await server._guard_view("WS", "V")


async def test_api_wraps_appflowy_error(fake_client):
    fake_client.request_result = AppFlowyError("boom")
    with pytest.raises(ToolError, match="boom"):
        await server._api("GET", "/x")


# -- workspace + folder ----------------------------------------------------
async def test_get_workspace_list_filters_payload(fake_client):
    fake_client.request_result = {"data": [{"workspace_id": "WS"}]}
    result = await server.get_workspace_list()
    assert result == {"data": [{"workspace_id": "WS"}]}


async def test_get_workspace_folder_without_root_view(fake_client):
    fake_client.request_result = {"data": {"view_id": "root", "children": []}}
    await server.get_workspace_folder("WS")
    assert fake_client.requests[0][2]["params"] == {"depth": 10}


async def test_get_workspace_folder_with_root_view(fake_client):
    fake_client.request_result = {"data": {"view_id": "root", "children": []}}
    await server.get_workspace_folder("WS", depth=3, root_view_id="V")
    assert fake_client.requests[0][2]["params"] == {"depth": 3, "root_view_id": "V"}


# -- page CRUD -------------------------------------------------------------
async def test_create_new_page_with_name_and_data(fake_client):
    await server.create_new_page("WS", "parent", name="Title", page_data={"type": "page"})
    body = fake_client.requests[0][2]["json"]
    assert body["name"] == "Title"
    assert body["page_data"] == {"type": "page"}


async def test_create_new_page_minimal(fake_client):
    await server.create_new_page("WS", "parent")
    body = fake_client.requests[0][2]["json"]
    assert "name" not in body
    assert "page_data" not in body


async def test_update_page_sets_all_fields(fake_client):
    await server.update_page(
        "WS", "P", name="n", icon={"i": 1}, is_locked=True, extra={"e": 1}
    )
    body = fake_client.requests[0][2]["json"]
    assert body == {"name": "n", "icon": {"i": 1}, "is_locked": True, "extra": {"e": 1}}


async def test_update_page_empty_when_nothing_provided(fake_client):
    await server.update_page("WS", "P")
    assert fake_client.requests[0][2]["json"] == {}


async def test_get_page_details_hits_endpoint(fake_client):
    await server.get_page_details("WS", "P")
    assert fake_client.requests[0][1].endswith("/page-view/P")


async def test_append_content_to_page_sends_blocks(fake_client):
    await server.append_content_to_page("WS", "P", [{"type": "paragraph"}])
    assert fake_client.requests[0][2]["json"] == {"blocks": [{"type": "paragraph"}]}


# -- block tools: pycrdt gating -------------------------------------------
async def test_block_tools_require_pycrdt(monkeypatch):
    monkeypatch.setattr(blocks, "HAS_PYCRDT", False)
    with pytest.raises(ToolError, match="pycrdt"):
        await server.get_page_blocks("WS", "P")
    with pytest.raises(ToolError, match="pycrdt"):
        await server.edit_block_text("WS", "P", "b")
    with pytest.raises(ToolError, match="pycrdt"):
        await server.insert_block("WS", "P")
    with pytest.raises(ToolError, match="pycrdt"):
        await server.delete_block("WS", "P", "b")


# -- _load_page_document ---------------------------------------------------
async def test_load_page_document_invalid_collab_raises(fake_client):
    fake_client.page_view_raw = {}
    with pytest.raises(ToolError, match="failed to read page collab"):
        await server.get_page_blocks("WS", "P")


# -- get_page_blocks -------------------------------------------------------
async def test_get_page_blocks_returns_page_id_and_blocks(fake_client):
    collab = build_collab(blocks=[{"id": "b1", "text": "hi"}])
    fake_client.page_view_raw = page_view_for(collab)
    out = await server.get_page_blocks("WS", "P")
    assert out["page_id"] == "root"
    assert any(b["id"] == "b1" for b in out["blocks"])


async def test_get_page_blocks_page_id_none_when_absent(fake_client):
    collab = build_collab(blocks=[{"id": "b1", "text": "hi"}], page_id=None)
    fake_client.page_view_raw = page_view_for(collab)
    out = await server.get_page_blocks("WS", "P")
    assert out["page_id"] is None


# -- edit_block_text -------------------------------------------------------
async def test_edit_block_text_missing_block_raises(fake_client):
    fake_client.page_view_raw = page_view_for(build_collab())
    with pytest.raises(ToolError, match="not found"):
        await server.edit_block_text("WS", "P", "ghost")


async def test_edit_block_text_no_external_id_raises(fake_client):
    collab = build_collab(blocks=[{"id": "b1", "text": None}])
    fake_client.page_view_raw = page_view_for(collab)
    with pytest.raises(ToolError, match="no editable text"):
        await server.edit_block_text("WS", "P", "b1")


async def test_edit_block_text_plain_text(fake_client):
    collab = build_collab(blocks=[{"id": "b1", "text": "old"}])
    fake_client.page_view_raw = page_view_for(collab)
    await server.edit_block_text("WS", "P", "b1", text="new")
    assert len(fake_client.web_updates) == 1


async def test_edit_block_text_empty_text_clears(fake_client):
    collab = build_collab(blocks=[{"id": "b1", "text": "old"}])
    fake_client.page_view_raw = page_view_for(collab)
    await server.edit_block_text("WS", "P", "b1", text="")
    assert len(fake_client.web_updates) == 1


async def test_edit_block_text_delta(fake_client):
    collab = build_collab(blocks=[{"id": "b1", "text": "old"}])
    fake_client.page_view_raw = page_view_for(collab)
    await server.edit_block_text("WS", "P", "b1", delta=[{"insert": "rich"}])
    assert len(fake_client.web_updates) == 1


async def test_edit_block_text_creates_missing_text_entry(fake_client):
    collab = build_collab(blocks=[{"id": "b1", "text": None, "ext_only": True}])
    fake_client.page_view_raw = page_view_for(collab)
    await server.edit_block_text("WS", "P", "b1", text="new")
    assert len(fake_client.web_updates) == 1


# -- insert_block ----------------------------------------------------------
async def test_insert_block_requires_children_map(fake_client):
    collab = build_collab(with_children_map=False)
    fake_client.page_view_raw = page_view_for(collab)
    with pytest.raises(ToolError, match="no children_map"):
        await server.insert_block("WS", "P", text="x")


async def test_insert_block_unknown_parent_raises(fake_client):
    fake_client.page_view_raw = page_view_for(build_collab())
    with pytest.raises(ToolError, match="not found"):
        await server.insert_block("WS", "P", parent_id="ghost")


async def test_insert_block_parent_without_children_array_raises(fake_client):
    collab = build_collab(blocks=[{"id": "b1", "no_children": True}])
    fake_client.page_view_raw = page_view_for(collab)
    with pytest.raises(ToolError, match="no children array"):
        await server.insert_block("WS", "P", parent_id="b1")


async def test_insert_block_appends_with_text(fake_client):
    fake_client.page_view_raw = page_view_for(build_collab())
    out = await server.insert_block("WS", "P", text="hello")
    assert "block_id" in out


async def test_insert_block_with_explicit_index(fake_client):
    collab = build_collab(blocks=[{"id": "b1", "text": "a"}])
    fake_client.page_view_raw = page_view_for(collab)
    out = await server.insert_block("WS", "P", text="b", index=0)
    assert "block_id" in out


async def test_insert_block_with_delta(fake_client):
    fake_client.page_view_raw = page_view_for(build_collab())
    out = await server.insert_block("WS", "P", delta=[{"insert": "x"}])
    assert "block_id" in out


async def test_insert_block_without_text_or_delta(fake_client):
    fake_client.page_view_raw = page_view_for(build_collab())
    out = await server.insert_block("WS", "P")
    assert "block_id" in out


async def test_insert_block_returns_raw_result_on_error(fake_client):
    fake_client.page_view_raw = page_view_for(build_collab())
    fake_client.web_update_result = {"error": "nope"}
    out = await server.insert_block("WS", "P", text="x")
    assert out == {"error": "nope"}


# -- delete_block ----------------------------------------------------------
async def test_delete_block_missing_raises(fake_client):
    fake_client.page_view_raw = page_view_for(build_collab())
    with pytest.raises(ToolError, match="not found"):
        await server.delete_block("WS", "P", "ghost")


async def test_delete_block_with_parent_and_text(fake_client):
    collab = build_collab(blocks=[{"id": "b1", "text": "x"}])
    fake_client.page_view_raw = page_view_for(collab)
    await server.delete_block("WS", "P", "b1")
    assert len(fake_client.web_updates) == 1


async def test_delete_block_without_parent_or_text(fake_client):
    fake_client.page_view_raw = page_view_for(build_collab())
    await server.delete_block("WS", "P", "root")
    assert len(fake_client.web_updates) == 1


async def test_delete_block_skips_non_matching_siblings(fake_client):
    collab = build_collab(
        blocks=[{"id": "b1", "text": "a"}, {"id": "b2", "text": "b"}]
    )
    fake_client.page_view_raw = page_view_for(collab)
    await server.delete_block("WS", "P", "b1")
    assert len(fake_client.web_updates) == 1


async def test_delete_block_when_parent_has_no_children_array(fake_client):
    collab = build_collab(
        blocks=[
            {"id": "b1", "text": None, "no_children": True},
            {"id": "b2", "text": "x", "parent": "b1"},
        ]
    )
    fake_client.page_view_raw = page_view_for(collab)
    await server.delete_block("WS", "P", "b2")
    assert len(fake_client.web_updates) == 1


# -- databases -------------------------------------------------------------
def test_database_view_ids_found():
    payload = {"data": ["bad", {"id": "DB", "views": [{"view_id": "V"}, {"no": 1}, "x"]}]}
    assert server._database_view_ids(payload, "DB") == ["V"]


def test_database_view_ids_found_without_views():
    assert server._database_view_ids({"data": [{"id": "DB"}]}, "DB") == []


def test_database_view_ids_not_found():
    payload = {"data": [{"id": "OTHER", "views": [{"view_id": "V"}]}]}
    assert server._database_view_ids(payload, "DB") == []


def test_database_view_ids_non_list():
    assert server._database_view_ids({"data": {"x": 1}}, "DB") == []


async def test_guard_database_workspace_wide_skips_view_lookup(fake_client):
    await server._guard_database("WS", "DB")
    assert fake_client.requests == []


async def test_guard_database_allows_when_view_in_scope(fake_client, headers):
    tok = TokenConfig(token="t", scopes=(ScopeEntry("WS", root_view_id="V"),))
    server.ACCESS = AccessControl(fake_client, [tok])
    headers["authorization"] = "Bearer t"
    fake_client.results["/database"] = {"data": [{"id": "DB", "views": [{"view_id": "V"}]}]}
    assert await server._guard_database("WS", "DB") is tok


async def test_guard_database_denies_when_database_not_listed(fake_client, headers):
    tok = TokenConfig(token="t", scopes=(ScopeEntry("WS", root_view_id="V"),))
    server.ACCESS = AccessControl(fake_client, [tok])
    headers["authorization"] = "Bearer t"
    fake_client.results["/database"] = {"data": [{"id": "OTHER", "views": [{"view_id": "V"}]}]}
    with pytest.raises(ToolError, match="not allowed to access database"):
        await server._guard_database("WS", "DB")


async def test_guard_database_denies_when_view_out_of_scope(fake_client, headers):
    tok = TokenConfig(token="t", scopes=(ScopeEntry("WS", root_view_id="V"),))
    server.ACCESS = AccessControl(fake_client, [tok])
    headers["authorization"] = "Bearer t"
    fake_client.results["/database"] = {"data": [{"id": "DB", "views": [{"view_id": "X"}]}]}
    with pytest.raises(ToolError, match="not allowed to access database"):
        await server._guard_database("WS", "DB")


async def test_create_database_grid_with_name(fake_client):
    await server.create_database("WS", "parent", name="Tasks", layout=1)
    body = fake_client.requests[0][2]["json"]
    assert body == {"parent_view_id": "parent", "layout": 1, "name": "Tasks"}


async def test_create_database_without_name(fake_client):
    await server.create_database("WS", "parent", layout=2)
    assert "name" not in fake_client.requests[0][2]["json"]


async def test_create_database_invalid_layout_raises(fake_client):
    with pytest.raises(ToolError, match="layout must be"):
        await server.create_database("WS", "parent", layout=0)


async def test_get_workspace_databases_returns_filtered(fake_client):
    fake_client.request_result = {"data": [{"id": "DB", "views": [{"view_id": "V"}]}]}
    out = await server.get_workspace_databases("WS")
    assert out == {"data": [{"id": "DB", "views": [{"view_id": "V"}]}]}


async def test_get_database_fields_hits_endpoint(fake_client):
    await server.get_database_fields("WS", "DB")
    assert fake_client.requests[0][1].endswith("/database/DB/fields")


async def test_get_database_rows_with_details(fake_client):
    fake_client.results["/row"] = {"data": [{"id": "r1"}]}
    fake_client.results["/row/detail"] = {"data": [{"id": "r1", "cells": {"Name": "x"}}]}
    out = await server.get_database_rows("WS", "DB")
    assert out == {"data": [{"id": "r1", "cells": {"Name": "x"}}]}


async def test_get_database_rows_passes_joined_ids(fake_client):
    fake_client.results["/row"] = {"data": [{"id": "r1"}, {"id": "r2"}]}
    fake_client.results["/row/detail"] = {"data": []}
    await server.get_database_rows("WS", "DB")
    detail = next(r for r in fake_client.requests if r[1].endswith("/detail"))
    assert detail[2]["params"]["ids"] == "r1,r2"


async def test_get_database_rows_with_doc_param(fake_client):
    fake_client.results["/row"] = {"data": [{"id": "r1"}]}
    fake_client.results["/row/detail"] = {"data": []}
    await server.get_database_rows("WS", "DB", with_doc=True)
    detail = next(r for r in fake_client.requests if r[1].endswith("/detail"))
    assert detail[2]["params"]["with_doc"] == "true"


async def test_get_database_rows_ids_only(fake_client):
    fake_client.request_result = {"data": [{"id": "r1"}]}
    out = await server.get_database_rows("WS", "DB", with_details=False)
    assert out == {"data": [{"id": "r1"}]}


async def test_get_database_rows_no_ids_returns_listed(fake_client):
    fake_client.results["/row"] = {"data": []}
    out = await server.get_database_rows("WS", "DB")
    assert out == {"data": []}


async def test_get_database_rows_non_list_returns_listed(fake_client):
    fake_client.results["/row"] = {"data": {"weird": 1}}
    out = await server.get_database_rows("WS", "DB")
    assert out == {"data": {"weird": 1}}


async def test_get_database_rows_skips_malformed_rows(fake_client):
    fake_client.results["/row"] = {"data": ["bad", {"no_id": 1}, {"id": "r1"}]}
    fake_client.results["/row/detail"] = {"data": []}
    await server.get_database_rows("WS", "DB")
    detail = next(r for r in fake_client.requests if r[1].endswith("/detail"))
    assert detail[2]["params"]["ids"] == "r1"


async def test_add_database_field_minimal(fake_client):
    await server.add_database_field("WS", "DB", "Notes", field_type=0)
    assert fake_client.requests[0][2]["json"] == {"name": "Notes", "field_type": 0}


async def test_add_database_field_with_type_option(fake_client):
    await server.add_database_field(
        "WS", "DB", "Qty", field_type=1, type_option_data={"format": 1}
    )
    assert fake_client.requests[0][2]["json"]["type_option_data"] == {"format": 1}


async def test_add_database_row_minimal(fake_client):
    await server.add_database_row("WS", "DB", {"Name": "x"})
    assert fake_client.requests[0][2]["json"] == {"cells": {"Name": "x"}}


async def test_add_database_row_with_document(fake_client):
    await server.add_database_row("WS", "DB", {"Name": "x"}, document="# notes")
    assert fake_client.requests[0][2]["json"]["document"] == "# notes"


async def test_update_database_row_minimal(fake_client):
    await server.update_database_row("WS", "DB", "key-1", {"Name": "x"})
    assert fake_client.requests[0][2]["json"] == {"pre_hash": "key-1", "cells": {"Name": "x"}}


async def test_update_database_row_with_document(fake_client):
    await server.update_database_row("WS", "DB", "key-1", {"Name": "x"}, document="d")
    assert fake_client.requests[0][2]["json"]["document"] == "d"


async def test_update_database_row_uses_put(fake_client):
    await server.update_database_row("WS", "DB", "key-1", {})
    assert fake_client.requests[0][0] == "PUT"


# -- trash + favorites -----------------------------------------------------
async def test_move_page_to_trash(fake_client):
    await server.move_page_to_trash("WS", "P")
    assert fake_client.requests[0][1].endswith("/move-to-trash")


async def test_get_trash_filters_list_with_data(fake_client):
    fake_client.request_result = {"data": [{"view_id": "C"}]}
    out = await server.get_trash("WS")
    assert out == {"data": [{"view_id": "C"}]}


async def test_get_trash_filters_bare_list(fake_client):
    fake_client.request_result = [{"view_id": "C"}]
    out = await server.get_trash("WS")
    assert out == [{"view_id": "C"}]


async def test_get_trash_non_list_passthrough(fake_client):
    fake_client.request_result = {"data": {"not": "list"}}
    out = await server.get_trash("WS")
    assert out == {"data": {"not": "list"}}


async def test_restore_page_from_trash(fake_client):
    await server.restore_page_from_trash("WS", "P")
    assert fake_client.requests[0][1].endswith("/restore-from-trash")


async def test_delete_page_from_trash(fake_client):
    await server.delete_page_from_trash("WS", "P")
    assert fake_client.requests[0][0] == "DELETE"


async def test_get_favorite_pages_filters_list_with_data(fake_client):
    fake_client.request_result = {"data": [{"view_id": "C"}]}
    out = await server.get_favorite_pages("WS")
    assert out == {"data": [{"view_id": "C"}]}


async def test_get_favorite_pages_bare_list(fake_client):
    fake_client.request_result = [{"view_id": "C"}]
    out = await server.get_favorite_pages("WS")
    assert out == [{"view_id": "C"}]


async def test_get_favorite_pages_non_list_passthrough(fake_client):
    fake_client.request_result = {"data": {"not": "list"}}
    out = await server.get_favorite_pages("WS")
    assert out == {"data": {"not": "list"}}


async def test_toggle_favorite_page(fake_client):
    await server.toggle_favorite_page("WS", "P", is_favorite=False)
    assert fake_client.requests[0][2]["json"] == {"is_favorite": False}


# -- health check ----------------------------------------------------------
async def test_healthz_returns_ok():
    resp = await server.healthz(None)
    assert resp.body == b'{"status":"ok"}'


# -- build + main ----------------------------------------------------------
def test_build_wires_globals():
    settings = Settings(appflowy=AppFlowyConfig(), tokens=[TokenConfig(token="t")])
    app = server.build(settings)
    assert app is server.mcp
    assert server.CLIENT is not None
    assert server.ACCESS.resolve("t") is not None


def test_main_warns_when_no_tokens_and_auth_required(monkeypatch):
    settings = Settings(appflowy=AppFlowyConfig(), tokens=[], require_auth=True)
    monkeypatch.setattr(server, "load_settings", lambda: settings)
    runs = []
    monkeypatch.setattr(server.mcp, "run", lambda **kw: runs.append(kw))
    server.main()
    assert runs and runs[0]["transport"] == "http"


def test_main_open_mode(monkeypatch):
    settings = Settings(appflowy=AppFlowyConfig(), tokens=[], require_auth=False)
    monkeypatch.setattr(server, "load_settings", lambda: settings)
    runs = []
    monkeypatch.setattr(server.mcp, "run", lambda **kw: runs.append(kw))
    server.main()
    assert runs[0]["host"] == settings.host


def test_package_main_module_is_importable():
    import appflowy_mcp.__main__ as entry

    assert entry.main is server.main
