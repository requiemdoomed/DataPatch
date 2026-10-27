import pandas as pd
import pytest

from datapatch.cleaning import CleaningError
from datapatch.workspace import Workspace, WorkspaceNotFound, WorkspaceStore


def test_apply_undo_redo_round_trip(messy):
    ws = Workspace(messy, "people.csv")
    ws.apply("trim_whitespace", {"columns": ["name"]})
    ws.apply("remove_duplicates", {})
    assert len(ws.current) == 5
    assert [s.op for s in ws.steps] == ["trim_whitespace", "remove_duplicates"]

    undone = ws.undo()
    assert undone.op == "remove_duplicates"
    assert len(ws.current) == 6
    assert ws.current["name"].iloc[0] == "Alice"

    ws.redo()
    assert len(ws.current) == 5
    assert not ws.can_redo


def test_new_step_clears_redo_stack(messy):
    ws = Workspace(messy)
    ws.apply("trim_whitespace", {})
    ws.undo()
    assert ws.can_redo
    ws.apply("remove_duplicates", {})
    assert not ws.can_redo


def test_reset_keeps_steps_redoable_in_order(messy):
    ws = Workspace(messy)
    ws.apply("trim_whitespace", {})
    ws.apply("remove_duplicates", {})
    assert ws.reset() == 2
    pd.testing.assert_frame_equal(ws.current, messy)
    assert ws.redo().op == "trim_whitespace"
    assert ws.redo().op == "remove_duplicates"


def test_no_op_steps_are_rejected(messy):
    ws = Workspace(messy)
    ws.apply("trim_whitespace", {})
    with pytest.raises(CleaningError, match="wouldn't change"):
        ws.apply("trim_whitespace", {})
    assert len(ws.steps) == 1


def test_failed_step_leaves_state_untouched(messy):
    ws = Workspace(messy)
    with pytest.raises(CleaningError):
        ws.apply("fill_missing", {"columns": ["city"], "strategy": "mean"})
    assert ws.steps == []
    assert ws.current is ws.original


def test_undo_and_redo_with_empty_history(messy):
    ws = Workspace(messy)
    with pytest.raises(CleaningError, match="Nothing to undo"):
        ws.undo()
    with pytest.raises(CleaningError, match="Nothing to redo"):
        ws.redo()
    with pytest.raises(CleaningError, match="Already at the original"):
        ws.reset()


def test_params_are_copied(messy):
    ws = Workspace(messy)
    params = {"columns": ["name"]}
    ws.apply("trim_whitespace", params)
    params["columns"].append("city")
    assert ws.steps[0].params == {"columns": ["name"]}


def test_profiles_are_cached_and_refreshed(messy):
    ws = Workspace(messy)
    assert ws.current_profile is ws.original_profile
    ws.apply("remove_duplicates", {})
    assert ws.current_profile["summary"]["rows"] == 5
    assert ws.original_profile["summary"]["rows"] == 6


def test_store_get_and_eviction(messy):
    store = WorkspaceStore(max_items=2)
    first = store.create(messy, "a.csv")
    store.create(messy, "b.csv")
    store.create(messy, "c.csv")
    assert len(store) == 2
    with pytest.raises(WorkspaceNotFound):
        store.get(first.id)


def test_store_drops_expired_sessions(messy):
    store = WorkspaceStore(ttl_seconds=0)
    old = store.create(messy, "a.csv")
    old.last_used -= 10
    store.create(messy, "b.csv")
    with pytest.raises(WorkspaceNotFound):
        store.get(old.id)


def test_changes_are_tracked_against_the_original(messy):
    ws = Workspace(messy)
    assert ws.changes["cells_changed"] == 0

    ws.apply("trim_whitespace", {"columns": ["name"]})
    ws.apply("remove_duplicates", {})
    ws.apply("rename_column", {"column": "name", "new_name": "full_name"})
    ws.apply("drop_columns", {"columns": ["score"]})

    changes = ws.changes
    assert changes["rows_removed"] == 1
    assert changes["columns_removed"] == ["score"]
    assert changes["columns_renamed"] == {"name": "full_name"}
    assert changes["by_column"] == {"full_name": 3}  # '  Alice', 'bob ', 'Carol  Ann'

    page = ws.page_changes(0, 10)
    assert page["row_numbers"] == [1, 2, 3, 4, 6]  # row 5 was the duplicate
    assert [0, 0, "  Alice"] in page["cells"]


def test_type_conversion_counts_as_a_change(messy):
    ws = Workspace(messy)
    ws.apply("convert_type", {"columns": ["score"], "target": "integer"})
    assert ws.changes["cells_changed"] == 0  # 1.0 -> 1 is the same value
    ws.apply("convert_type", {"columns": ["age"], "target": "number"})
    assert ws.changes["by_column"]["age"] == 6  # every text value became a number or missing


def test_single_undo_uses_the_saved_frame(messy):
    ws = Workspace(messy)
    ws.apply("trim_whitespace", {})
    middle = ws.current
    ws.apply("remove_duplicates", {})
    ws.undo()
    assert ws.current is middle
    ws.undo()
    assert ws.current is ws.original


def test_unchanged_columns_reuse_cached_profiles(messy):
    ws = Workspace(messy)
    city_before = next(p for p in ws.original_profile["columns"] if p["name"] == "city")
    ws.apply("trim_whitespace", {"columns": ["name"]})
    city_after = next(p for p in ws.current_profile["columns"] if p["name"] == "city")
    name_after = next(p for p in ws.current_profile["columns"] if p["name"] == "name")
    assert city_after is city_before
    assert not any(i["kind"] == "whitespace" for i in name_after["issues"])
