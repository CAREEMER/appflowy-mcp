"""Unit tests for appflowy_mcp.rows (CRDT row editing helpers)."""

from __future__ import annotations

import time

import pytest

from appflowy_mcp import blocks, rows
from tests.conftest import build_database_collab, build_row_collab

TEXT = {"id": "f1", "name": "Name", "field_type_id": rows.TEXT}
NUMBER = {"id": "n1", "name": "Qty", "field_type_id": rows.NUMBER}
DATETIME = {"id": "d1", "name": "When", "field_type_id": rows.DATETIME}
CHECKBOX = {"id": "c1", "name": "Done", "field_type_id": rows.CHECKBOX}
SELECT = {
    "id": "s1",
    "name": "Status",
    "field_type_id": rows.SINGLE_SELECT,
    "type_option": {"content": {"options": [
        {"id": "o1", "name": "Open"},
        {"id": "o2", "name": "Done"},
    ]}},
}
MULTI = {**SELECT, "id": "m1", "name": "Tags", "field_type_id": rows.MULTI_SELECT}


# -- load_collab -----------------------------------------------------------
def test_load_collab_decodes_doc_state():
    doc = rows.load_collab(build_row_collab())
    assert "data" in blocks.ykeys(doc.get("data", type=blocks.YMap))


# -- field_index -----------------------------------------------------------
def test_field_index_maps_name_and_id():
    index = rows.field_index([TEXT])
    assert index["Name"] is TEXT and index["f1"] is TEXT


def test_field_index_skips_fields_without_id():
    assert rows.field_index([{"name": "x"}]) == {}


def test_field_index_skips_non_dict():
    assert rows.field_index(["nope"]) == {}


def test_field_index_field_without_name_maps_id_only():
    field = {"id": "f9"}
    assert rows.field_index([field]) == {"f9": field}


# -- _option_id ------------------------------------------------------------
def test_option_id_resolves_by_name():
    assert rows._option_id(SELECT, "Open") == "o1"


def test_option_id_resolves_by_id():
    assert rows._option_id(SELECT, "o2") == "o2"


def test_option_id_unknown_lists_available():
    with pytest.raises(ValueError, match="'Open', 'Done'"):
        rows._option_id(SELECT, "Nope")


def test_option_id_no_options_says_none_defined():
    with pytest.raises(ValueError, match="none defined"):
        rows._option_id({"name": "Status"}, "x")


# -- _timestamp ------------------------------------------------------------
def test_timestamp_from_int():
    assert rows._timestamp(1700000000) == "1700000000"


def test_timestamp_from_float_truncates():
    assert rows._timestamp(1700000000.9) == "1700000000"


def test_timestamp_from_digit_string():
    assert rows._timestamp("1700000000") == "1700000000"


def test_timestamp_from_iso_string():
    assert rows._timestamp("2023-11-14T00:00:00+00:00") == "1699920000"


def test_timestamp_invalid_raises():
    with pytest.raises(ValueError, match="not a unix timestamp"):
        rows._timestamp("someday")


# -- encode_cell -----------------------------------------------------------
def test_encode_cell_text():
    assert rows.encode_cell(TEXT, "hello") == "hello"


def test_encode_cell_number():
    assert rows.encode_cell(NUMBER, 12.5) == "12.5"


def test_encode_cell_number_rejects_bool():
    with pytest.raises(ValueError, match="is a Number field"):
        rows.encode_cell(NUMBER, True)


def test_encode_cell_checkbox_from_bool():
    assert rows.encode_cell(CHECKBOX, True) == "true"
    assert rows.encode_cell(CHECKBOX, 0) == "false"


def test_encode_cell_checkbox_from_string():
    assert rows.encode_cell(CHECKBOX, "yes") == "true"
    assert rows.encode_cell(CHECKBOX, "off") == "false"


def test_encode_cell_datetime():
    assert rows.encode_cell(DATETIME, 1700000000) == "1700000000"


def test_encode_cell_single_select_by_name():
    assert rows.encode_cell(SELECT, "Open") == "o1"


def test_encode_cell_multi_select_from_list():
    assert rows.encode_cell(MULTI, ["Open", "Done"]) == "o1,o2"


def test_encode_cell_multi_select_from_string():
    assert rows.encode_cell(MULTI, "Open,Done") == "o1,o2"


# -- resolve_cells ---------------------------------------------------------
def test_resolve_cells_maps_field_id_to_encoded():
    assert rows.resolve_cells([TEXT], {"Name": "x"}) == {"f1": (rows.TEXT, "x")}


def test_resolve_cells_unknown_field_raises():
    with pytest.raises(ValueError, match="unknown field 'Ghost'"):
        rows.resolve_cells([TEXT], {"Ghost": "x"})


# -- write_cells -----------------------------------------------------------
def _cells(doc):
    return doc.get("data", type=blocks.YMap)["data"]["cells"]


def test_write_cells_creates_new_cell():
    doc = rows.load_collab(build_row_collab())
    rows.write_cells(doc, {"f1": (rows.TEXT, "hi")})
    assert _cells(doc)["f1"]["data"] == "hi"
    assert _cells(doc)["f1"]["created_at"] > 0


def test_write_cells_updates_existing_cell():
    doc = rows.load_collab(build_row_collab({"f1": {"field_type": 0, "data": "old"}}))
    rows.write_cells(doc, {"f1": (rows.TEXT, "new")})
    assert _cells(doc)["f1"]["data"] == "new"


def test_write_cells_bumps_last_modified():
    doc = rows.load_collab(build_row_collab())
    rows.write_cells(doc, {"f1": (rows.TEXT, "x")})
    assert doc.get("data", type=blocks.YMap)["data"]["last_modified"] >= int(time.time()) - 5


def test_write_cells_no_data_map_raises():
    doc = blocks.YDoc()
    doc.get("data", type=blocks.YMap)["other"] = "x"
    with pytest.raises(ValueError, match="no data map"):
        rows.write_cells(doc, {"f1": (rows.TEXT, "x")})


def test_write_cells_no_cells_map_raises():
    doc = blocks.YDoc()
    doc.get("data", type=blocks.YMap)["data"] = blocks.YMap()
    with pytest.raises(ValueError, match="no cells map"):
        rows.write_cells(doc, {"f1": (rows.TEXT, "x")})


# -- remove_row_orders -----------------------------------------------------
def test_remove_row_orders_strips_matching_from_each_view():
    doc = rows.load_collab(build_database_collab({"v1": ["r1", "r2"], "v2": ["r1"]}))
    assert rows.remove_row_orders(doc, "r1") == 2


def test_remove_row_orders_returns_zero_when_absent():
    doc = rows.load_collab(build_database_collab({"v1": ["r2"]}))
    assert rows.remove_row_orders(doc, "r1") == 0


def test_remove_row_orders_skips_views_without_row_orders():
    doc = rows.load_collab(build_database_collab({"v1": None}))
    assert rows.remove_row_orders(doc, "r1") == 0
