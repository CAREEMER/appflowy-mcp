"""Shared fixtures and lightweight fakes for the appflowy-mcp test suite."""

from __future__ import annotations

import pytest
from pycrdt import Array, Doc, Map, Text


def build_collab(blocks=None, page_id="root", with_children_map=True):
    """Encode a minimal AppFlowy document collab into an ``encoded_collab`` blob.

    ``blocks`` is a list of dicts describing each block::

        {"id", "ty", "text", "parent", "children_of": [child_ids]}

    The page root block is created automatically with id ``page_id``.
    """
    doc = Doc()
    data = doc.get("data", type=Map)
    document = Map()
    data["document"] = document
    block_map = Map()
    document["blocks"] = block_map
    meta = Map()
    document["meta"] = meta
    text_map = Map()
    meta["text_map"] = text_map
    children_map = Map()
    if with_children_map:
        meta["children_map"] = children_map
    if page_id is not None:
        document["page_id"] = page_id

    def make_block(bid, ty, text, parent, children_id, ext_only=False, no_children=False):
        ext_id = f"ext-{bid}"
        has_ext = text is not None or ext_only
        block = Map(
            {
                "id": bid,
                "ty": ty,
                "data": "{}",
                "parent": parent or "",
                "children": "" if no_children else children_id,
                "external_id": ext_id if has_ext else "",
                "external_type": "text",
            }
        )
        block_map[bid] = block
        if text is not None:
            t = Text()
            text_map[ext_id] = t
            t.insert(0, text)

    if page_id is not None:
        root_children_id = f"ch-{page_id}"
        make_block(page_id, "page", None, "", root_children_id)
        if with_children_map:
            children_map[root_children_id] = Array()

    for b in blocks or []:
        ch_id = f"ch-{b['id']}"
        make_block(
            b["id"],
            b.get("ty", "paragraph"),
            b.get("text", ""),
            b.get("parent", page_id),
            ch_id,
            ext_only=b.get("ext_only", False),
            no_children=b.get("no_children", False),
        )
        if with_children_map:
            children_map[ch_id] = Array()
            parent = b.get("parent", page_id)
            parent_ch = f"ch-{parent}"
            if parent_ch in children_map:
                children_map[parent_ch].append(b["id"])

    return doc.get_update()


def build_row_collab(cells=None):
    """Encode a DatabaseRow collab (collab_type 4).

    A row nests its fields under ``data.data``; ``cells`` maps *field id* ->
    ``{field_type, data, created_at, last_modified}``. ``cells`` here is
    ``{field_id: {"field_type": int, "data": str}}``.
    """
    doc = Doc()
    root = doc.get("data", type=Map)
    row = Map()
    root["data"] = row
    cell_map = Map()
    row["cells"] = cell_map
    row["last_modified"] = 0
    for field_id, cell in (cells or {}).items():
        cell_map[field_id] = Map(
            {
                "field_type": cell.get("field_type", 0),
                "data": cell.get("data", ""),
                "created_at": 0,
                "last_modified": 0,
            }
        )
    return {"doc_state": list(doc.get_update())}


def build_database_collab(rows_by_view):
    """Encode a Database collab (collab_type 1).

    ``rows_by_view`` is ``{view_id: [row_id, ...]}``; each row becomes a RowOrder
    under ``data.database.views.<view_id>.row_orders``. A view id mapped to
    ``None`` gets no ``row_orders`` array at all (to exercise that branch).
    """
    doc = Doc()
    root = doc.get("data", type=Map)
    database = Map()
    root["database"] = database
    views = Map()
    database["views"] = views
    for view_id, row_ids in rows_by_view.items():
        view = Map()
        views[view_id] = view
        if row_ids is None:
            continue
        orders = Array()
        view["row_orders"] = orders
        for row_id in row_ids:
            orders.append(Map({"id": row_id, "height": 60}))
    return {"doc_state": list(doc.get_update())}


class FakeResponse:
    """Minimal stand-in for ``httpx.Response``."""

    def __init__(self, status_code=200, json_data=None, content=b"x", text="", raise_json=False):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.content = content
        self.text = text
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Captures calls made by server tools and returns scripted results."""

    def __init__(self):
        self.requests = []
        self.web_updates = []
        self.request_result = {"code": 0, "data": "ok"}
        # path-suffix -> scripted result (or Exception); longest match wins,
        # falling back to request_result. Lets one tool make several calls.
        self.results = {}
        self.page_view_raw = None
        self.web_update_result = {"ok": True}
        self.folder_result = {"data": {"view_id": "root", "children": []}}
        # object_id -> scripted get_collab payload (or Exception).
        self.collabs = {}
        self.collab_calls = []

    async def request(self, method, path, **kw):
        self.requests.append((method, path, kw))
        match = None
        for suffix, val in self.results.items():
            if path.endswith(suffix) and (match is None or len(suffix) > len(match[0])):
                match = (suffix, val)
        result = match[1] if match else self.request_result
        if isinstance(result, Exception):
            raise result
        return result

    async def get_folder(self, workspace_id):
        return self.folder_result

    async def get_page_view_raw(self, workspace_id, page_id):
        return self.page_view_raw

    async def get_collab(self, workspace_id, object_id, collab_type):
        self.collab_calls.append((workspace_id, object_id, collab_type))
        result = self.collabs.get(object_id)
        if isinstance(result, Exception):
            raise result
        return result

    async def post_web_update(self, workspace_id, object_id, update, collab_type=0):
        self.web_updates.append((workspace_id, object_id, bytes(update), collab_type))
        return self.web_update_result


@pytest.fixture
def fake_client():
    return FakeClient()
