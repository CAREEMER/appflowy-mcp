"""In-place block editing of AppFlowy documents via Yjs/CRDT.

AppFlowy stores a document as a Yjs collab. A block's editable rich text lives
in ``document.meta.text_map`` as a ``Y.Text`` keyed by the block's
``external_id``; the block tree/order lives in ``document.meta.children_map``.
The web client mutates these locally and ships a Yrs *update* to the
``web-update`` endpoint. We do exactly the same — read the collab, edit the Yjs
structure with :mod:`pycrdt`, and POST one update. The REST API exposes no
per-block replace endpoint, so this is the only way to edit existing content.
"""

from __future__ import annotations

import json
import secrets
import string

try:
    from pycrdt import Array as YArray
    from pycrdt import Doc as YDoc
    from pycrdt import Map as YMap
    from pycrdt import Text as YText

    HAS_PYCRDT = True
except Exception:  # noqa: BLE001
    HAS_PYCRDT = False

# AppFlowy block/text ids are 10-char nanoids over this URL-safe alphabet.
_ID_ALPHABET = string.ascii_letters + string.digits + "_-"


def new_id(length: int = 10) -> str:
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


def ykeys(node) -> list:
    """List keys of a pycrdt Map (stable across pycrdt versions)."""
    return list(node.keys())


def apply_delta_to_text(t, delta) -> None:
    """Replace a Y.Text's content from an AppFlowy delta.

    Each op looks like ``{"insert": "Hi", "attributes": {"bold": true}}``.
    Supported attributes: bold, italic, underline, strikethrough, code, color,
    bg_color, href. We pass the union of all attributes on every op (unused ones
    set to None) to avoid Yjs formatting bleed into following plain text.
    """
    t.clear()
    all_keys: set = set()
    for op in delta or []:
        all_keys |= set((op.get("attributes") or {}).keys())
    idx = 0
    for op in delta or []:
        s = op.get("insert")
        if not isinstance(s, str) or s == "":
            continue
        op_attrs = op.get("attributes") or {}
        attrs = {k: op_attrs.get(k, None) for k in all_keys} if all_keys else None
        t.insert(idx, s, attrs)
        idx += len(s)


def block_data_json(block_type: str, heading_level: int, checked: bool) -> str:
    """Build the per-type ``data`` JSON string stored on a block."""
    if block_type == "heading":
        return json.dumps({"level": heading_level or 1})
    if block_type == "todo_list":
        return json.dumps({"checked": bool(checked)})
    return "{}"


def load_document(encoded_collab: bytes):
    """Decode an ``encoded_collab`` blob into ``(doc, document_map)``."""
    doc = YDoc()
    doc.apply_update(encoded_collab)
    document = doc.get("data", type=YMap)["document"]
    return doc, document


def block_text(document, block_id: str):
    blocks = document["blocks"]
    text_map = document["meta"]["text_map"]
    ext = blocks[block_id]["external_id"]
    if ext and ext in ykeys(text_map):
        return str(text_map[ext])
    return None


def ordered_blocks(document) -> list:
    """Return the page's blocks as a flat, document-ordered list with depth."""
    blocks = document["blocks"]
    meta = document["meta"]
    children_map = meta["children_map"] if "children_map" in ykeys(meta) else None
    page_id = document["page_id"] if "page_id" in ykeys(document) else None
    block_ids = ykeys(blocks)

    out: list = []

    def entry(bid: str, depth: int) -> dict:
        b = blocks[bid]
        return {"id": bid, "type": str(b["ty"]), "text": block_text(document, bid), "depth": depth}

    def walk(bid: str, depth: int) -> None:
        out.append(entry(bid, depth))
        ch = blocks[bid]["children"]
        if children_map is not None and ch and ch in ykeys(children_map):
            for cid in list(children_map[ch]):
                if str(cid) in block_ids:
                    walk(str(cid), depth + 1)

    if page_id and page_id in block_ids and children_map is not None:
        root_children = blocks[page_id]["children"]
        if root_children and root_children in ykeys(children_map):
            for cid in list(children_map[root_children]):
                if str(cid) in block_ids:
                    walk(str(cid), 0)
            return out
    for bid in block_ids:
        if bid != page_id:
            out.append(entry(bid, 0))
    return out


__all__ = [
    "HAS_PYCRDT",
    "YArray",
    "YMap",
    "YText",
    "apply_delta_to_text",
    "block_data_json",
    "block_text",
    "load_document",
    "new_id",
    "ordered_blocks",
    "ykeys",
]
