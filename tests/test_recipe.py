import json

import pandas as pd
import pytest

from datapatch.cleaning import CleaningError, NothingToDo
from datapatch.recipe import RECIPE_VERSION, apply_recipe, make_recipe, parse_recipe
from datapatch.workspace import Workspace

STEPS = [
    {"op": "trim_whitespace", "params": {}},
    {"op": "remove_duplicates", "params": {}},
    {"op": "standardize_categories", "params": {"column": "city"}},
]


def test_make_recipe_round_trips_through_json(messy):
    ws = Workspace(messy, "people.csv")
    ws.apply("trim_whitespace", {})
    ws.apply("remove_duplicates", {})
    recipe = json.loads(json.dumps(make_recipe(ws.steps, source=ws.filename)))

    assert recipe["datapatch_recipe"] == RECIPE_VERSION
    assert recipe["source"] == "people.csv"
    assert [s["op"] for s in recipe["steps"]] == ["trim_whitespace", "remove_duplicates"]
    assert parse_recipe(recipe) == [("trim_whitespace", {}), ("remove_duplicates", {})]


def test_apply_recipe_reproduces_the_session(messy):
    ws = Workspace(messy)
    for step in STEPS:
        ws.apply(step["op"], step["params"])
    cleaned, report = apply_recipe(messy, make_recipe(ws.steps))
    pd.testing.assert_frame_equal(cleaned, ws.current)
    assert len(report.applied) == 3 and not report.skipped


def test_apply_recipe_skips_steps_with_nothing_to_do():
    df = pd.DataFrame({"city": ["Toronto", "Ottawa"]})
    cleaned, report = apply_recipe(df, {"steps": STEPS})
    pd.testing.assert_frame_equal(cleaned, df)
    assert report.applied == []
    assert len(report.skipped) == 3
    assert report.summary() == "Applied 0 steps, skipped 3 with nothing to do"

    with pytest.raises(NothingToDo):
        apply_recipe(df, {"steps": STEPS}, skip_noops=False)


def test_apply_recipe_names_the_failing_step(messy):
    recipe = {"steps": [{"op": "trim_whitespace"}, {"op": "drop_columns", "params": {"columns": ["nope"]}}]}
    with pytest.raises(CleaningError, match=r"Recipe step 2 \(Drop nope\) failed: There's no column"):
        apply_recipe(messy, recipe)


@pytest.mark.parametrize(
    "recipe, message",
    [
        ("{not json", "isn't valid JSON"),
        ([], "doesn't look like a DataPatch recipe"),
        ({"steps": []}, "no steps"),
        ({"datapatch_recipe": 99, "steps": STEPS}, "version 99"),
        ({"steps": [{"params": {}}]}, "missing its 'op'"),
        ({"steps": [{"op": "launch"}]}, "unknown operation"),
        ({"steps": [{"op": "trim_whitespace", "params": [1]}]}, "aren't an object"),
    ],
)
def test_parse_recipe_rejects_bad_input(recipe, message):
    with pytest.raises(CleaningError, match=message):
        parse_recipe(recipe)


def test_parse_recipe_accepts_bytes_with_bom():
    raw = "﻿" + json.dumps({"steps": STEPS})
    assert len(parse_recipe(raw.encode("utf-8"))) == 3


def test_workspace_recipe_is_all_or_nothing(messy):
    ws = Workspace(messy)
    ws.apply("trim_whitespace", {"columns": ["name"]})
    before = ws.current

    bad = {
        "steps": [
            {"op": "remove_duplicates"},
            {"op": "fill_missing", "params": {"columns": ["city"], "strategy": "mean"}},
        ]
    }
    with pytest.raises(CleaningError, match="Recipe step 2"):
        ws.apply_recipe(bad)
    assert len(ws.steps) == 1
    assert ws.current is before

    report = ws.apply_recipe({"steps": STEPS})
    assert len(report.applied) == 3 and report.skipped == []
    assert len(ws.steps) == 4
    ws.undo()
    assert len(ws.steps) == 3
