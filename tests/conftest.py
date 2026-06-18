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
        self.page_view_raw = None
        self.web_update_result = {"ok": True}

    async def request(self, method, path, **kw):
        self.requests.append((method, path, kw))
        if isinstance(self.request_result, Exception):
            raise self.request_result
        return self.request_result

    async def get_page_view_raw(self, workspace_id, page_id):
        return self.page_view_raw

    async def post_web_update(self, workspace_id, object_id, update):
        self.web_updates.append((workspace_id, object_id, bytes(update)))
        return self.web_update_result


@pytest.fixture
def fake_client():
    return FakeClient()
