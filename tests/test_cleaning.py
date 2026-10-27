import numpy as np
import pandas as pd
import pytest

from datapatch.cleaning import (
    OPERATIONS,
    CleaningError,
    apply_operation,
    describe_operation,
    parse_bool,
    parse_number,
    suggest_category_mapping,
)


def test_parse_number_handles_common_formats():
    assert parse_number("$1,200.50") == 1200.5
    assert parse_number(" 42 ") == 42
    assert parse_number("(30)") == -30
    assert parse_number("15%") == 15
    assert parse_number(7) == 7.0
    assert np.isnan(parse_number("thirty"))
    assert np.isnan(parse_number(None))
    assert np.isnan(parse_number("inf"))


def test_parse_bool():
    assert parse_bool("Yes") is True
    assert parse_bool(" n ") is False
    assert parse_bool(1) is True
    assert parse_bool("maybe") is pd.NA
    assert parse_bool(np.nan) is pd.NA


def test_operations_never_modify_their_input(messy):
    before = messy.copy()
    for name, params in [
        ("trim_whitespace", {}),
        ("change_case", {"columns": ["name"], "mode": "upper"}),
        ("mark_missing", {}),
        ("remove_duplicates", {}),
        ("convert_type", {"columns": ["age"], "target": "number"}),
        ("fill_missing", {"columns": ["score"], "strategy": "median"}),
    ]:
        apply_operation(messy, name, params)
    pd.testing.assert_frame_equal(messy, before)


def test_trim_whitespace_strips_and_squeezes(messy):
    out = apply_operation(messy, "trim_whitespace", {"columns": ["name", "city"]})
    assert out["name"].tolist()[:3] == ["Alice", "bob", "Carol Ann"]
    assert out["city"].iloc[2] == "TORONTO"


def test_trim_whitespace_turns_blank_cells_into_missing():
    df = pd.DataFrame({"a": ["x", "   ", "y"]})
    out = apply_operation(df, "trim_whitespace", {})
    assert out["a"].isna().tolist() == [False, True, False]


def test_trim_whitespace_rejects_numeric_only_selection(messy):
    with pytest.raises(CleaningError, match="hold text"):
        apply_operation(messy, "trim_whitespace", {"columns": ["score"]})


def test_change_case(messy):
    out = apply_operation(messy, "change_case", {"columns": ["city"], "mode": "lower"})
    assert out["city"].iloc[0] == "toronto"
    with pytest.raises(CleaningError):
        apply_operation(messy, "change_case", {"columns": ["city"], "mode": "shouting"})


def test_suggest_mapping_prefers_mixed_case_spelling():
    mapping = suggest_category_mapping(["toronto", "toronto", "Toronto", "TORONTO", " Toronto", "Ottawa"])
    assert mapping == {"toronto": "Toronto", "TORONTO": "Toronto", " Toronto": "Toronto"}


def test_standardize_categories_auto(messy):
    out = apply_operation(messy, "standardize_categories", {"column": "city"})
    assert out["city"].tolist()[:5] == ["Toronto", "Toronto", "Toronto", "Ottawa", "Ottawa"]


def test_standardize_categories_with_mapping(messy):
    out = apply_operation(
        messy,
        "standardize_categories",
        {"column": "city", "mode": "mapping", "mapping": {"Ottawa": "OTT", "N/A": ""}},
    )
    assert out["city"].iloc[3] == "OTT"
    assert pd.isna(out["city"].iloc[5])


def test_standardize_categories_errors(messy):
    clean = pd.DataFrame({"c": ["a", "b"]})
    with pytest.raises(CleaningError, match="No case"):
        apply_operation(clean, "standardize_categories", {"column": "c"})
    with pytest.raises(CleaningError, match="mapping"):
        apply_operation(messy, "standardize_categories", {"column": "city", "mode": "mapping", "mapping": {}})
    with pytest.raises(CleaningError, match="doesn't hold text"):
        apply_operation(messy, "standardize_categories", {"column": "score"})


def test_mark_missing_defaults_and_numeric_sentinels():
    df = pd.DataFrame({"t": ["ok", " N/A", "-", "unknown"], "n": [1, -999, 3, -999]})
    out = apply_operation(df, "mark_missing", {})
    assert out["t"].isna().tolist() == [False, True, True, True]
    assert out["n"].isna().sum() == 0

    out = apply_operation(df, "mark_missing", {"markers": "-999, ok"})
    assert out["n"].isna().sum() == 2
    assert pd.isna(out["t"].iloc[0])


def test_remove_duplicates(messy):
    out = apply_operation(messy, "remove_duplicates", {})
    assert len(out) == 5
    assert list(out.index) == [0, 1, 2, 3, 5]  # surviving rows keep their original labels

    out = apply_operation(messy, "remove_duplicates", {"columns": ["city"], "keep": "none"})
    assert "Ottawa" not in out["city"].tolist()


def test_remove_duplicates_without_any():
    with pytest.raises(CleaningError, match="No duplicate"):
        apply_operation(pd.DataFrame({"a": [1, 2]}), "remove_duplicates", {})


@pytest.mark.parametrize(
    "strategy, expected",
    [("mean", 22.2), ("median", 4.0), ("mode", 4.0), ("ffill", 2.0), ("bfill", 4.0)],
)
def test_fill_missing_numeric(messy, strategy, expected):
    out = apply_operation(messy, "fill_missing", {"columns": ["score"], "strategy": strategy})
    assert out["score"].iloc[2] == pytest.approx(expected)


def test_fill_missing_constant_and_text(messy):
    out = apply_operation(messy, "fill_missing", {"columns": ["city"], "strategy": "constant", "value": "Unknown city"})
    assert out["city"].iloc[5] == "Unknown city"  # 'N/A' counts as missing

    out = apply_operation(messy, "fill_missing", {"columns": ["score"], "strategy": "constant", "value": "0"})
    assert out["score"].iloc[2] == 0.0


def test_fill_missing_drop_rows(messy):
    out = apply_operation(messy, "fill_missing", {"columns": ["name", "score"], "strategy": "drop_rows"})
    assert len(out) == 4


def test_fill_missing_errors(messy):
    with pytest.raises(CleaningError, match="isn't numeric"):
        apply_operation(messy, "fill_missing", {"columns": ["city"], "strategy": "mean"})
    with pytest.raises(CleaningError, match="Enter a value"):
        apply_operation(messy, "fill_missing", {"columns": ["city"], "strategy": "constant"})
    with pytest.raises(CleaningError, match="has to be a number"):
        apply_operation(messy, "fill_missing", {"columns": ["score"], "strategy": "constant", "value": "lots"})


def test_fill_missing_keeps_integer_columns_integer():
    df = pd.DataFrame({"n": pd.array([1, None, 4], dtype="Int64")})
    out = apply_operation(df, "fill_missing", {"columns": ["n"], "strategy": "mean"})
    assert str(out["n"].dtype) == "Int64"
    assert out["n"].tolist() == [1, 2, 4]


def test_convert_to_number_and_integer(messy):
    out = apply_operation(messy, "convert_type", {"columns": ["age"], "target": "number"})
    assert out["age"].tolist()[:2] == [34.0, 29.0]
    assert np.isnan(out["age"].iloc[2])
    assert out["age"].iloc[5] == 52.0

    out = apply_operation(messy, "convert_type", {"columns": ["age"], "target": "integer"})
    assert str(out["age"].dtype) == "Int64"
    assert out["age"].iloc[0] == 34


def test_convert_to_date_handles_mixed_formats():
    df = pd.DataFrame({"d": ["2024-03-05", "03/07/2024", "Mar 9, 2024", "not a date", None]})
    out = apply_operation(df, "convert_type", {"columns": ["d"], "target": "date"})
    assert pd.api.types.is_datetime64_any_dtype(out["d"])
    assert out["d"].dt.strftime("%Y-%m-%d").tolist()[:3] == ["2024-03-05", "2024-03-07", "2024-03-09"]
    assert out["d"].isna().tolist()[3:] == [True, True]

    dayfirst = apply_operation(df, "convert_type", {"columns": ["d"], "target": "date", "dayfirst": True})
    assert dayfirst["d"].iloc[1].month == 7


def test_convert_numbers_to_date_is_refused(messy):
    with pytest.raises(CleaningError, match="can't be read as dates"):
        apply_operation(messy, "convert_type", {"columns": ["score"], "target": "date"})


def test_convert_to_boolean_and_text():
    df = pd.DataFrame({"flag": ["Yes", "no", "Y", "0", "maybe"], "n": [1.0, 2.5, np.nan, 4.0, 5.0]})
    out = apply_operation(df, "convert_type", {"columns": ["flag"], "target": "boolean"})
    assert str(out["flag"].dtype) == "boolean"
    assert out["flag"].tolist()[:4] == [True, False, True, False]
    assert out["flag"].isna().iloc[4]

    out = apply_operation(df, "convert_type", {"columns": ["n"], "target": "text"})
    assert out["n"].tolist()[:2] == ["1", "2.5"]


def test_replace_values_text_and_numbers(messy):
    out = apply_operation(messy, "replace_values", {"columns": ["city"], "find": "Ottawa", "replace": "Ottawa, ON"})
    assert out["city"].iloc[3] == "Ottawa, ON"

    out = apply_operation(messy, "replace_values", {"columns": ["score"], "find": "100", "replace": ""})
    assert np.isnan(out["score"].iloc[5])

    with pytest.raises(CleaningError, match="has to be a number"):
        apply_operation(messy, "replace_values", {"columns": ["score"], "find": "100", "replace": "lots"})


def test_handle_outliers():
    df = pd.DataFrame({"n": [10, 11, 12, 13, 12, 11, 10, 500]})
    clipped = apply_operation(df, "handle_outliers", {"column": "n"})
    assert clipped["n"].max() < 500
    assert clipped["n"].dtype == df["n"].dtype

    removed = apply_operation(df, "handle_outliers", {"column": "n", "method": "remove"})
    assert len(removed) == 7

    blanked = apply_operation(df, "handle_outliers", {"column": "n", "method": "missing"})
    assert blanked["n"].isna().sum() == 1

    with pytest.raises(CleaningError, match="No outliers"):
        apply_operation(removed, "handle_outliers", {"column": "n"})
    with pytest.raises(CleaningError, match="between 0 and 10"):
        apply_operation(df, "handle_outliers", {"column": "n", "factor": 50})


def test_rename_and_drop_columns(messy):
    out = apply_operation(messy, "rename_column", {"column": "name", "new_name": " full_name "})
    assert "full_name" in out.columns

    with pytest.raises(CleaningError, match="already a column"):
        apply_operation(messy, "rename_column", {"column": "name", "new_name": "city"})

    out = apply_operation(messy, "drop_columns", {"columns": ["score", "age"]})
    assert list(out.columns) == ["name", "city"]

    with pytest.raises(CleaningError, match="at least one column"):
        apply_operation(messy, "drop_columns", {"columns": list(messy.columns)})


def test_bad_requests_raise_cleaning_errors(messy):
    with pytest.raises(CleaningError, match="Unknown operation"):
        apply_operation(messy, "explode", {})
    with pytest.raises(CleaningError, match="takes these parameters"):
        apply_operation(messy, "trim_whitespace", {"colums": ["name"]})
    with pytest.raises(CleaningError, match="no column called"):
        apply_operation(messy, "trim_whitespace", {"columns": ["nope"]})
    with pytest.raises(CleaningError, match="must be an object"):
        apply_operation(messy, "trim_whitespace", ["name"])


def test_every_operation_has_a_readable_description():
    for name in OPERATIONS:
        assert describe_operation(name, {})
    fill = describe_operation("fill_missing", {"columns": ["a"], "strategy": "median"})
    assert fill == "Fill gaps in a with the median"
    assert describe_operation("trim_whitespace", {"columns": list("abcde")}) == "Trim whitespace in a, b, c +2 more"
