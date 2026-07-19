"""In-place database row editing via Yjs/CRDT.

AppFlowy stores each row as its own collab (``collab_type`` 4) whose ``data.cells``
maps *field id* -> ``{field_type, data, created_at, last_modified}``; every value is
stored as a string. The row's membership in the table lives elsewhere — in the
database collab (``collab_type`` 1) under ``data.database.views.<view>.row_orders``.
So editing cells touches only the row collab, while deleting a row only strips its
row order, exactly like ``Database::remove_row`` upstream; the orphaned row collab is
left behind, which is what the official client does too.

The REST API has no update-by-row-id or delete route at all, so this is the only way.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from . import blocks

# CollabType values upstream; the value of the enum can't be changed.
COLLAB_DATABASE = 1
COLLAB_DATABASE_ROW = 4

TEXT, NUMBER, DATETIME, SINGLE_SELECT, MULTI_SELECT, CHECKBOX, URL = range(7)

# ponytail: AppFlowy stores a MultiSelect cell as option ids joined by this.
_SELECT_SEPARATOR = ","


def load_collab(payload: Any):
    """Decode a ``get_collab`` response body into a pycrdt Doc."""
    doc = blocks.YDoc()
    doc.apply_update(bytes(payload["doc_state"]))
    return doc


def field_index(fields: list) -> dict:
    """Map both field name and field id -> field, for name-or-id lookups."""
    index: dict = {}
    for f in fields:
        if isinstance(f, dict) and f.get("id"):
            index[f["id"]] = f
            if f.get("name"):
                index.setdefault(f["name"], f)
    return index


def _select_options(field: dict) -> list:
    content = (field.get("type_option") or {}).get("content") or {}
    return content.get("options") or []


def _option_id(field: dict, value: Any) -> str:
    """Resolve an option name (or id) to its option id."""
    options = _select_options(field)
    wanted = str(value).strip()
    for o in options:
        if o.get("id") == wanted or o.get("name") == wanted:
            return o["id"]
    names = ", ".join(repr(o.get("name")) for o in options) or "none defined"
    raise ValueError(f"{field['name']!r} has no option {wanted!r}; available: {names}")


def _timestamp(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value))
    text = str(value).strip()
    if text.isdigit():
        return text
    try:
        return str(int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()))
    except ValueError as exc:
        raise ValueError(f"{text!r} is not a unix timestamp or ISO-8601 date") from exc


def encode_cell(field: dict, value: Any) -> str:
    """Encode a Python value into the string AppFlowy stores for this field type."""
    field_type = field.get("field_type_id")
    if field_type == CHECKBOX:
        if isinstance(value, str):
            return "true" if value.strip().lower() in ("true", "yes", "1") else "false"
        return "true" if value else "false"
    if field_type == DATETIME:
        return _timestamp(value)
    if field_type == SINGLE_SELECT:
        return _option_id(field, value)
    if field_type == MULTI_SELECT:
        values = value if isinstance(value, list) else str(value).split(_SELECT_SEPARATOR)
        return _SELECT_SEPARATOR.join(_option_id(field, v) for v in values)
    if field_type == NUMBER and isinstance(value, bool):
        raise ValueError(f"{field['name']!r} is a Number field; got a boolean")
    return str(value)


def resolve_cells(fields: list, cells: dict) -> dict:
    """Turn {field name-or-id: value} into {field id: (field_type, encoded)}.

    Unknown field names raise — silently dropping them is how a "no-op update"
    looks like a success.
    """
    index = field_index(fields)
    resolved: dict = {}
    for key, value in cells.items():
        field = index.get(key)
        if field is None:
            known = ", ".join(repr(f["name"]) for f in fields if f.get("name"))
            raise ValueError(f"unknown field {key!r}; this database has: {known}")
        resolved[field["id"]] = (field.get("field_type_id"), encode_cell(field, value))
    return resolved


def write_cells(doc, resolved: dict) -> None:
    """Apply resolved cells to a row collab, mirroring the server's own writer.

    A row collab nests the row under ``data.data``; its ``cells`` map is keyed by
    field id.
    """
    root = doc.get("data", type=blocks.YMap)
    if "data" not in blocks.ykeys(root):
        raise ValueError("row collab has no data map")
    data = root["data"]
    if "cells" not in blocks.ykeys(data):
        raise ValueError("row collab has no cells map")
    cells = data["cells"]
    now = int(time.time())
    for field_id, (field_type, encoded) in resolved.items():
        if field_id in blocks.ykeys(cells):
            cell = cells[field_id]
            cell["field_type"] = field_type
            cell["data"] = encoded
            cell["last_modified"] = now
        else:
            cells[field_id] = blocks.YMap(
                {
                    "field_type": field_type,
                    "data": encoded,
                    "created_at": now,
                    "last_modified": now,
                }
            )
    data["last_modified"] = now


def remove_row_orders(doc, row_id: str) -> int:
    """Strip a row's RowOrder from every view; returns how many views changed."""
    database = doc.get("data", type=blocks.YMap)["database"]
    views = database["views"]
    removed = 0
    for view_id in blocks.ykeys(views):
        view = views[view_id]
        if "row_orders" not in blocks.ykeys(view):
            continue
        orders = view["row_orders"]
        for i in range(len(orders) - 1, -1, -1):
            # ponytail: row_orders entries are always RowOrder maps -> dicts.
            if orders[i].get("id") == row_id:
                del orders[i]
                removed += 1
    return removed
