"""FastMCP server exposing scoped AppFlowy tools over streamable HTTP.

Authentication: clients send their MCP token as ``Authorization: Bearer
<token>`` (an ``X-AppFlowy-MCP-Token`` header is also accepted). The token is
resolved to a scope and every tool enforces it before touching AppFlowy.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import blocks
from .access import AccessControl, AccessDenied
from .appflowy import AppFlowyClient, AppFlowyError, unwrap
from .config import Settings, load_settings

log = logging.getLogger("appflowy_mcp.server")

mcp: FastMCP = FastMCP(
    name="appflowy",
    instructions=(
        "Tools for a self-hosted AppFlowy instance: list workspaces, read the "
        "folder tree, and create/read/update pages and their blocks. Access is "
        "bounded by the caller's token scope; out-of-scope ids return an error."
    ),
)

# Wired up in main(); module-level so tool functions can reach them.
CLIENT: AppFlowyClient
ACCESS: AccessControl


# --------------------------------------------------------------------------
# Auth + scope helpers
# --------------------------------------------------------------------------
def _bearer() -> str | None:
    # include_all=True is required: FastMCP strips `authorization` from the
    # default header view to avoid forwarding it downstream, but that is exactly
    # the header carrying our MCP token.
    headers = get_http_headers(include_all=True)
    raw = headers.get("authorization") or headers.get("x-appflowy-mcp-token")
    if not raw:
        return None
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw.strip()


def _token():
    tok = ACCESS.resolve(_bearer())
    if tok is None:
        raise ToolError("unauthorized: missing or invalid AppFlowy MCP token")
    return tok


async def _guard_view(workspace_id: str, view_id: str):
    tok = _token()
    try:
        await ACCESS.assert_view(tok, workspace_id, view_id)
    except AccessDenied as exc:
        raise ToolError(str(exc)) from exc
    return tok


def _guard_workspace(workspace_id: str):
    tok = _token()
    try:
        ACCESS.assert_workspace(tok, workspace_id)
    except AccessDenied as exc:
        raise ToolError(str(exc)) from exc
    return tok


async def _api(method: str, path: str, **kw) -> Any:
    try:
        return await CLIENT.request(method, path, **kw)
    except AppFlowyError as exc:
        raise ToolError(str(exc)) from exc


# --------------------------------------------------------------------------
# Workspace + folder
# --------------------------------------------------------------------------
@mcp.tool(name="get_workspace_list")
async def get_workspace_list() -> Any:
    """List the workspaces visible to your token.

    The first call to make. Workspaces outside your token's scope are hidden.
    """
    tok = _token()
    payload = await _api("GET", "/api/workspace")
    return ACCESS.filter_workspaces(tok, payload)


@mcp.tool(name="get_workspace_folder")
async def get_workspace_folder(
    workspace_id: str, depth: int = 10, root_view_id: str | None = None
) -> Any:
    """Get the page/folder tree of a workspace, pruned to your token's scope.

    Args:
        workspace_id: The workspace to read.
        depth: Maximum nesting depth to retrieve (default 10).
        root_view_id: Optional view id to use as the tree root.
    """
    tok = _guard_workspace(workspace_id)
    if root_view_id:
        await _guard_view(workspace_id, root_view_id)
    payload = await _api(
        "GET",
        f"/api/workspace/{workspace_id}/folder",
        params={"depth": depth, **({"root_view_id": root_view_id} if root_view_id else {})},
    )
    return await ACCESS.filter_folder(tok, workspace_id, payload)


# --------------------------------------------------------------------------
# Page CRUD
# --------------------------------------------------------------------------
@mcp.tool(name="create_new_page")
async def create_new_page(
    workspace_id: str,
    parent_view_id: str,
    name: str = "",
    view_layout: int = 0,
    page_data: dict | None = None,
) -> Any:
    """Create a new page under a parent view you have access to.

    Args:
        workspace_id: The workspace to create in.
        parent_view_id: The parent view/folder; must be within your scope.
        name: Optional page title.
        view_layout: 0=Document, 1=Grid, 2=Board, 3=Calendar.
        page_data: Optional initial content, AppFlowy page-data format, e.g.
            {"type": "page", "children": [
                {"type": "paragraph", "data": {"delta": [{"insert": "Hi"}]}}]}.
            Block types: paragraph, heading (data.level), bulleted_list,
            numbered_list, todo_list, divider, image (data.url). delta inserts
            accept attributes: bold, italic, underline, strikethrough, code,
            color, href.
    """
    await _guard_view(workspace_id, parent_view_id)
    json_data: dict[str, Any] = {"parent_view_id": parent_view_id, "layout": view_layout}
    if name:
        json_data["name"] = name
    if page_data:
        json_data["page_data"] = page_data
    result = await _api(
        "POST", f"/api/workspace/{workspace_id}/page-view", json=json_data
    )
    # The new child changes the folder tree; drop the cache so scope checks on
    # it resolve immediately.
    ACCESS.invalidate(workspace_id)
    return result


@mcp.tool(name="update_page")
async def update_page(
    workspace_id: str,
    page_id: str,
    name: str | None = None,
    icon: dict | None = None,
    is_locked: bool | None = None,
    extra: dict | None = None,
) -> Any:
    """Update a page's metadata (name, icon, locked state). Only provided
    fields change.

    Args:
        workspace_id: The workspace containing the page.
        page_id: The page to update; must be within your scope.
        name: Optional new title.
        icon: Optional icon object.
        is_locked: Optional lock/unlock.
        extra: Optional extra metadata.
    """
    await _guard_view(workspace_id, page_id)
    data: dict[str, Any] = {}
    if name is not None:
        data["name"] = name
    if icon is not None:
        data["icon"] = icon
    if is_locked is not None:
        data["is_locked"] = is_locked
    if extra is not None:
        data["extra"] = extra
    return await _api(
        "PATCH", f"/api/workspace/{workspace_id}/page-view/{page_id}", json=data
    )


@mcp.tool(name="get_page_details")
async def get_page_details(workspace_id: str, page_id: str) -> Any:
    """Get full details (metadata + content) of a page within your scope.

    Args:
        workspace_id: The workspace containing the page.
        page_id: The page to read.
    """
    await _guard_view(workspace_id, page_id)
    return await _api("GET", f"/api/workspace/{workspace_id}/page-view/{page_id}")


@mcp.tool(name="append_content_to_page")
async def append_content_to_page(
    workspace_id: str, page_id: str, blocks_to_add: list
) -> Any:
    """Append blocks to the END of an existing page (cannot edit existing
    blocks — use 'Edit block text'/'Insert block' for that).

    Args:
        workspace_id: The workspace containing the page.
        page_id: The page to append to; must be within your scope.
        blocks_to_add: List of block objects like
            {"type": "paragraph", "data": {"delta": [{"insert": "Hello"}]}}.
    """
    await _guard_view(workspace_id, page_id)
    return await _api(
        "POST",
        f"/api/workspace/{workspace_id}/page-view/{page_id}/append-block",
        json={"blocks": blocks_to_add},
    )


# --------------------------------------------------------------------------
# In-place block editing (CRDT)
# --------------------------------------------------------------------------
async def _load_page_document(workspace_id: str, page_id: str):
    raw = await CLIENT.get_page_view_raw(workspace_id, page_id)
    try:
        encoded = bytes(raw["data"]["data"]["encoded_collab"])
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"failed to read page collab: {exc}") from exc
    return blocks.load_document(encoded)


@mcp.tool(name="get_page_blocks")
async def get_page_blocks(workspace_id: str, page_id: str) -> Any:
    """List a page's editable blocks in document order (id, type, text, depth).

    Use before 'Edit block text' / 'Delete block' / 'Insert block' to discover
    block ids. ``text`` is null for non-text blocks (divider, image).

    Args:
        workspace_id: The workspace containing the page.
        page_id: The page to read; must be within your scope.
    """
    if not blocks.HAS_PYCRDT:
        raise ToolError("pycrdt is not installed; block tools are unavailable")
    await _guard_view(workspace_id, page_id)
    _doc, document = await _load_page_document(workspace_id, page_id)
    page_id_val = (
        str(document["page_id"]) if "page_id" in blocks.ykeys(document) else None
    )
    return {"page_id": page_id_val, "blocks": blocks.ordered_blocks(document)}


@mcp.tool(name="edit_block_text")
async def edit_block_text(
    workspace_id: str,
    page_id: str,
    block_id: str,
    text: str = "",
    delta: list | None = None,
) -> Any:
    """Replace one existing block's content in place.

    Provide ``text`` (plain) or ``delta`` (rich; takes precedence). Delta ops
    are {"insert": "...", "attributes": {...}} with attributes bold, italic,
    underline, strikethrough, code, color, bg_color, href.

    Args:
        workspace_id: The workspace containing the page.
        page_id: The page containing the block; must be within your scope.
        block_id: The block to edit (from 'Get page blocks').
        text: New plain text (used when delta is omitted).
        delta: Optional rich-text delta (preserves formatting).
    """
    if not blocks.HAS_PYCRDT:
        raise ToolError("pycrdt is not installed; block tools are unavailable")
    await _guard_view(workspace_id, page_id)
    doc, document = await _load_page_document(workspace_id, page_id)
    block_map = document["blocks"]
    if block_id not in blocks.ykeys(block_map):
        raise ToolError(f"block {block_id!r} not found on this page")
    ext = block_map[block_id]["external_id"]
    if not ext:
        raise ToolError(f"block {block_id!r} has no editable text")
    text_map = document["meta"]["text_map"]
    state = doc.get_state()
    with doc.transaction():
        if ext not in blocks.ykeys(text_map):
            text_map[ext] = blocks.YText()
        t = text_map[ext]
        if delta is not None:
            blocks.apply_delta_to_text(t, delta)
        else:
            t.clear()
            if text:
                t.insert(0, text)
    update = doc.get_update(state)
    return await CLIENT.post_web_update(workspace_id, page_id, update)


@mcp.tool(name="insert_block")
async def insert_block(
    workspace_id: str,
    page_id: str,
    text: str = "",
    delta: list | None = None,
    block_type: str = "paragraph",
    parent_id: str = "",
    index: int = -1,
    heading_level: int = 1,
    checked: bool = False,
) -> Any:
    """Insert a NEW block at a specific position (unlike append, which only
    adds at the end).

    Args:
        workspace_id: The workspace containing the page.
        page_id: The page to insert into; must be within your scope.
        text: Plain text (used when delta is omitted).
        delta: Optional rich-text delta (takes precedence over text).
        block_type: paragraph, heading, bulleted_list, numbered_list,
            todo_list, or quote.
        parent_id: Block id to nest under; "" = page root (top level).
        index: 0-based position among the parent's children; -1 = append.
        heading_level: Heading level 1-3 when block_type is heading.
        checked: Initial checked state when block_type is todo_list.
    """
    if not blocks.HAS_PYCRDT:
        raise ToolError("pycrdt is not installed; block tools are unavailable")
    await _guard_view(workspace_id, page_id)
    doc, document = await _load_page_document(workspace_id, page_id)
    block_map = document["blocks"]
    meta = document["meta"]
    if "children_map" not in blocks.ykeys(meta):
        raise ToolError("document has no children_map; cannot position blocks")
    text_map = meta["text_map"]
    children_map = meta["children_map"]
    page_root = str(document["page_id"]) if "page_id" in blocks.ykeys(document) else None
    parent = parent_id or page_root
    if not parent or parent not in blocks.ykeys(block_map):
        raise ToolError(f"parent block {parent!r} not found on this page")
    parent_children_id = block_map[parent]["children"]
    if not parent_children_id or parent_children_id not in blocks.ykeys(children_map):
        raise ToolError(f"parent block {parent!r} has no children array")

    new_block_id = blocks.new_id()
    new_text_id = blocks.new_id()
    new_children_id = blocks.new_id()
    state = doc.get_state()
    with doc.transaction():
        children_map[new_children_id] = blocks.YArray()
        text_map[new_text_id] = blocks.YText()
        t = text_map[new_text_id]
        if delta is not None:
            blocks.apply_delta_to_text(t, delta)
        elif text:
            t.insert(0, text)
        block_map[new_block_id] = blocks.YMap(
            {
                "id": new_block_id,
                "ty": block_type,
                "data": blocks.block_data_json(block_type, heading_level, checked),
                "parent": parent,
                "children": new_children_id,
                "external_id": new_text_id,
                "external_type": "text",
            }
        )
        arr = children_map[parent_children_id]
        n = len(arr)
        pos = n if (index is None or index < 0 or index > n) else index
        arr.insert(pos, new_block_id)
    update = doc.get_update(state)
    result = await CLIENT.post_web_update(workspace_id, page_id, update)
    if isinstance(result, dict) and not result.get("error"):
        return {"block_id": new_block_id, **result}
    return result


@mcp.tool(name="delete_block")
async def delete_block(workspace_id: str, page_id: str, block_id: str) -> Any:
    """Delete a single leaf block in place (nested children are not cascaded —
    delete children first).

    Args:
        workspace_id: The workspace containing the page.
        page_id: The page containing the block; must be within your scope.
        block_id: The block to delete (from 'Get page blocks').
    """
    if not blocks.HAS_PYCRDT:
        raise ToolError("pycrdt is not installed; block tools are unavailable")
    await _guard_view(workspace_id, page_id)
    doc, document = await _load_page_document(workspace_id, page_id)
    block_map = document["blocks"]
    if block_id not in blocks.ykeys(block_map):
        raise ToolError(f"block {block_id!r} not found on this page")
    meta = document["meta"]
    text_map = meta["text_map"]
    children_map = meta["children_map"] if "children_map" in blocks.ykeys(meta) else None
    block = block_map[block_id]
    parent_id = str(block["parent"]) if block["parent"] else None
    ext = block["external_id"]
    state = doc.get_state()
    with doc.transaction():
        if parent_id and parent_id in blocks.ykeys(block_map) and children_map is not None:
            parent_children = block_map[parent_id]["children"]
            if parent_children and parent_children in blocks.ykeys(children_map):
                arr = children_map[parent_children]
                ids = [str(x) for x in list(arr)]
                for i in range(len(ids) - 1, -1, -1):
                    if ids[i] == block_id:
                        del arr[i]
        if ext and ext in blocks.ykeys(text_map):
            del text_map[ext]
        del block_map[block_id]
    update = doc.get_update(state)
    return await CLIENT.post_web_update(workspace_id, page_id, update)


# --------------------------------------------------------------------------
# Databases (grids / boards / calendars)
# --------------------------------------------------------------------------
def _database_view_ids(payload: Any, database_id: str) -> list:
    """Pull a database's view ids out of a `get_workspace_databases` payload."""
    items = unwrap(payload)
    if not isinstance(items, list):
        return []
    for db in items:
        if isinstance(db, dict) and db.get("id") == database_id:
            return [
                v.get("view_id")
                for v in (db.get("views") or [])
                if isinstance(v, dict) and v.get("view_id")
            ]
    return []


async def _guard_database(workspace_id: str, database_id: str):
    """Authorise a database operation against the token's view scope.

    The REST API enforces only workspace-level access, so we add the same
    view-subtree check the page tools use: a database is in scope iff one of its
    views is. Whole-workspace tokens skip the (extra) view lookup.
    """
    tok = _guard_workspace(workspace_id)
    if ACCESS.workspace_wide(tok, workspace_id):
        return tok
    payload = await _api("GET", f"/api/workspace/{workspace_id}/database")
    view_ids = _database_view_ids(payload, database_id)
    if not await ACCESS.view_any_allowed(tok, workspace_id, view_ids):
        raise ToolError(
            f"token '{tok.name or '?'}' is not allowed to access database "
            f"{database_id} in workspace {workspace_id}"
        )
    return tok


@mcp.tool(name="create_database")
async def create_database(
    workspace_id: str, parent_view_id: str, name: str = "", layout: int = 1
) -> Any:
    """Create a new database (grid/board/calendar) under a parent view.

    Returns the new view_id and database_id. The database starts with default
    fields (Name, Type, Done) and a few empty rows; use 'Add database field' and
    'Add database row' to shape and fill it. (For a plain document, use 'Create
    new page' instead.)

    Args:
        workspace_id: The workspace to create in.
        parent_view_id: The parent view/folder; must be within your scope.
        name: Optional database title.
        layout: 1=Grid, 2=Board, 3=Calendar.
    """
    await _guard_view(workspace_id, parent_view_id)
    if layout not in (1, 2, 3):
        raise ToolError("layout must be 1 (Grid), 2 (Board), or 3 (Calendar)")
    json_data: dict[str, Any] = {"parent_view_id": parent_view_id, "layout": layout}
    if name:
        json_data["name"] = name
    result = await _api(
        "POST", f"/api/workspace/{workspace_id}/page-view", json=json_data
    )
    ACCESS.invalidate(workspace_id)
    return result


@mcp.tool(name="get_workspace_databases")
async def get_workspace_databases(workspace_id: str) -> Any:
    """List the databases in a workspace (each with its grid/board/calendar
    views). Use this to find a database_id for the field/row tools.

    Databases whose views are outside your token's scope are hidden.

    Args:
        workspace_id: The workspace to read.
    """
    tok = _guard_workspace(workspace_id)
    payload = await _api("GET", f"/api/workspace/{workspace_id}/database")
    return await ACCESS.filter_databases(tok, workspace_id, payload)


@mcp.tool(name="get_database_fields")
async def get_database_fields(workspace_id: str, database_id: str) -> Any:
    """List a database's fields/columns (id, name, type, is_primary).

    Cells in 'Add/Update database row' are keyed by a field's name or id, so call
    this first to learn the column names.

    Args:
        workspace_id: The workspace containing the database.
        database_id: The database to read; must be within your scope.
    """
    await _guard_database(workspace_id, database_id)
    return await _api(
        "GET", f"/api/workspace/{workspace_id}/database/{database_id}/fields"
    )


@mcp.tool(name="get_database_rows")
async def get_database_rows(
    workspace_id: str,
    database_id: str,
    with_details: bool = True,
    with_doc: bool = False,
) -> Any:
    """List a database's rows. With details (default), each row's cells are
    returned keyed by field name; otherwise just the row ids.

    Args:
        workspace_id: The workspace containing the database.
        database_id: The database to read; must be within your scope.
        with_details: Fetch cell values (one extra request). False = ids only.
        with_doc: Also return each row's attached document, rendered as markdown.
    """
    await _guard_database(workspace_id, database_id)
    base = f"/api/workspace/{workspace_id}/database/{database_id}/row"
    listed = await _api("GET", base)
    if not with_details:
        return listed
    rows = unwrap(listed)
    ids = (
        [r["id"] for r in rows if isinstance(r, dict) and r.get("id")]
        if isinstance(rows, list)
        else []
    )
    if not ids:
        return listed
    # ponytail: ids go in the query string; a database with thousands of rows
    # could exceed URL limits — page by passing with_details=False then batching
    # if that ever bites.
    params: dict[str, Any] = {"ids": ",".join(ids)}
    if with_doc:
        params["with_doc"] = "true"
    return await _api("GET", f"{base}/detail", params=params)


@mcp.tool(name="add_database_field")
async def add_database_field(
    workspace_id: str,
    database_id: str,
    name: str,
    field_type: int = 0,
    type_option_data: dict | None = None,
) -> Any:
    """Add a field/column to a database. Returns the new field id.

    Args:
        workspace_id: The workspace containing the database.
        database_id: The database to edit; must be within your scope.
        name: The column name.
        field_type: 0=Text, 1=Number, 2=DateTime, 3=SingleSelect, 4=MultiSelect,
            5=Checkbox, 6=URL, 7=Checklist, 8=LastEditedTime, 9=CreatedTime,
            10=Relation, 11=Summary, 12=Translate, 13=Time, 14=Media.
        type_option_data: Optional type-specific config, e.g. {"format": 1} for a
            Number field.
    """
    await _guard_database(workspace_id, database_id)
    body: dict[str, Any] = {"name": name, "field_type": field_type}
    if type_option_data is not None:
        body["type_option_data"] = type_option_data
    return await _api(
        "POST", f"/api/workspace/{workspace_id}/database/{database_id}/fields", json=body
    )


@mcp.tool(name="add_database_row")
async def add_database_row(
    workspace_id: str,
    database_id: str,
    cells: dict,
    document: str | None = None,
) -> Any:
    """Add a new row to a database. Returns the new row id.

    Args:
        workspace_id: The workspace containing the database.
        database_id: The database to edit; must be within your scope.
        cells: {field name (or id): value} — string for Text/URL, number for
            Number, bool for Checkbox, ISO-8601 string for DateTime. Unknown
            fields are ignored.
        document: Optional markdown for the row's detail document.
    """
    await _guard_database(workspace_id, database_id)
    body: dict[str, Any] = {"cells": cells}
    if document is not None:
        body["document"] = document
    return await _api(
        "POST", f"/api/workspace/{workspace_id}/database/{database_id}/row", json=body
    )


@mcp.tool(name="update_database_row")
async def update_database_row(
    workspace_id: str,
    database_id: str,
    pre_hash: str,
    cells: dict,
    document: str | None = None,
) -> Any:
    """Create or update a row identified by a stable key (upsert).

    The row id is derived deterministically from ``pre_hash``, so calling this
    again with the same pre_hash updates the same row — use it for idempotent
    syncs. Use 'Add database row' to always create a fresh row instead.

    Args:
        workspace_id: The workspace containing the database.
        database_id: The database to edit; must be within your scope.
        pre_hash: Stable key identifying the row (e.g. an external record id).
        cells: {field name (or id): value}; see 'Add database row'.
        document: Optional markdown for the row's detail document.
    """
    await _guard_database(workspace_id, database_id)
    body: dict[str, Any] = {"pre_hash": pre_hash, "cells": cells}
    if document is not None:
        body["document"] = document
    return await _api(
        "PUT", f"/api/workspace/{workspace_id}/database/{database_id}/row", json=body
    )


# --------------------------------------------------------------------------
# Trash + favorites
# --------------------------------------------------------------------------
@mcp.tool(name="move_page_to_trash")
async def move_page_to_trash(workspace_id: str, page_id: str) -> Any:
    """Move a page to trash (recoverable).

    Args:
        workspace_id: The workspace containing the page.
        page_id: The page to trash; must be within your scope.
    """
    await _guard_view(workspace_id, page_id)
    result = await _api(
        "POST",
        f"/api/workspace/{workspace_id}/page-view/{page_id}/move-to-trash",
        json={},
    )
    ACCESS.invalidate(workspace_id)
    return result


@mcp.tool(name="get_trash")
async def get_trash(workspace_id: str) -> Any:
    """List trashed pages in a workspace (scoped to views you can access).

    Args:
        workspace_id: The workspace to read.
    """
    tok = _guard_workspace(workspace_id)
    payload = await _api("GET", f"/api/workspace/{workspace_id}/trash")
    items = unwrap(payload)
    if isinstance(items, list):
        kept = await ACCESS.filter_views_in_workspace(tok, workspace_id, items)
        if isinstance(payload, dict) and "data" in payload:
            return {**payload, "data": kept}
        return kept
    return payload


@mcp.tool(name="restore_page_from_trash")
async def restore_page_from_trash(workspace_id: str, page_id: str) -> Any:
    """Restore a trashed page.

    Args:
        workspace_id: The workspace containing the page.
        page_id: The page to restore; must be within your scope.
    """
    await _guard_view(workspace_id, page_id)
    result = await _api(
        "POST",
        f"/api/workspace/{workspace_id}/page-view/{page_id}/restore-from-trash",
        json={},
    )
    ACCESS.invalidate(workspace_id)
    return result


@mcp.tool(name="delete_page_from_trash")
async def delete_page_from_trash(workspace_id: str, page_id: str) -> Any:
    """Permanently delete a page from trash (irreversible).

    Args:
        workspace_id: The workspace containing the trashed page.
        page_id: The page to delete forever; must be within your scope.
    """
    await _guard_view(workspace_id, page_id)
    return await _api("DELETE", f"/api/workspace/{workspace_id}/trash/{page_id}")


@mcp.tool(name="get_favorite_pages")
async def get_favorite_pages(workspace_id: str) -> Any:
    """List favorite pages in a workspace (scoped to views you can access).

    Args:
        workspace_id: The workspace to read.
    """
    tok = _guard_workspace(workspace_id)
    payload = await _api("GET", f"/api/workspace/{workspace_id}/favorite")
    items = unwrap(payload)
    if isinstance(items, list):
        kept = await ACCESS.filter_views_in_workspace(tok, workspace_id, items)
        if isinstance(payload, dict) and "data" in payload:
            return {**payload, "data": kept}
        return kept
    return payload


@mcp.tool(name="toggle_favorite_page")
async def toggle_favorite_page(
    workspace_id: str, page_id: str, is_favorite: bool = True
) -> Any:
    """Add or remove a page from favorites.

    Args:
        workspace_id: The workspace containing the page.
        page_id: The page to (un)favorite; must be within your scope.
        is_favorite: True to favorite, False to unfavorite.
    """
    await _guard_view(workspace_id, page_id)
    return await _api(
        "POST",
        f"/api/workspace/{workspace_id}/page-view/{page_id}/favorite",
        json={"is_favorite": is_favorite},
    )


# --------------------------------------------------------------------------
# Health check
# --------------------------------------------------------------------------
@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
def build(settings: Settings) -> FastMCP:
    """Wire global client + access control from settings and return the app."""
    global CLIENT, ACCESS
    CLIENT = AppFlowyClient(settings.appflowy)
    ACCESS = AccessControl(
        CLIENT,
        settings.tokens,
        require_auth=settings.require_auth,
        folder_cache_ttl=settings.folder_cache_ttl,
    )
    return mcp


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not settings.tokens and settings.require_auth:
        log.warning(
            "no tokens configured and APPFLOWY_MCP_REQUIRE_AUTH is true: every "
            "request will be rejected. Configure tokens or set "
            "APPFLOWY_MCP_REQUIRE_AUTH=false for open mode."
        )
    if ACCESS_OPEN := (not settings.tokens and not settings.require_auth):
        log.warning("running in OPEN mode: no token required, full access granted")

    build(settings)
    log.info(
        "starting appflowy-mcp on %s:%s%s (%d token(s)%s)",
        settings.host,
        settings.port,
        settings.path,
        len(settings.tokens),
        ", open-mode" if ACCESS_OPEN else "",
    )
    mcp.run(
        transport="http",
        host=settings.host,
        port=settings.port,
        path=settings.path,
    )


if __name__ == "__main__":
    main()
