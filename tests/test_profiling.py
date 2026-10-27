import numpy as np
import pandas as pd

from datapatch.cleaning import apply_operation
from datapatch.files import read_csv
from datapatch.profiling import inspect_column, profile_frame, quality_score


def issue_kinds(profile):
    return {issue["kind"] for issue in profile["issues"]}


def test_clean_frame_scores_100():
    df = pd.DataFrame({"id": [1, 2, 3], "city": ["Toronto", "Ottawa", "Hamilton"]})
    report = profile_frame(df)
    assert report["summary"]["score"] == 100.0
    assert report["summary"]["issue_count"] == 0


def test_empty_frame_does_not_crash():
    report = profile_frame(pd.DataFrame({"a": pd.Series([], dtype="object")}))
    assert report["summary"]["rows"] == 0
    assert report["summary"]["score"] == 100.0


def test_text_column_issues(messy):
    profile, stats = inspect_column(messy["city"])
    assert {"missing", "placeholders", "whitespace", "variants"} <= issue_kinds(profile)
    assert profile["missing"] == 1
    assert stats["inconsistent"] == 2  # 'toronto' and ' TORONTO'


def test_numbers_stored_as_text_are_flagged(messy):
    profile, stats = inspect_column(messy["age"])
    issue = next(i for i in profile["issues"] if i["kind"] == "number_text")
    assert issue["fix"]["params"]["target"] == "integer"
    assert stats["validity"] < 1


def test_dates_and_booleans_stored_as_text_are_flagged():
    dates = pd.Series(["2024-01-02", "01/03/2024", "Jan 4, 2024", "2024-01-05"], name="d")
    flags = pd.Series(["yes", "No", "Y", "n", "TRUE"], name="f")
    assert "date_text" in issue_kinds(inspect_column(dates)[0])
    assert "boolean_text" in issue_kinds(inspect_column(flags)[0])


def test_outliers_are_reported_but_do_not_lower_the_score():
    df = pd.DataFrame({"n": [10, 11, 12, 13, 14, 15, 16, 500]})
    report = profile_frame(df)
    assert "outliers" in issue_kinds(report["columns"][0])
    assert report["summary"]["score"] == 100.0


def test_rare_values_need_a_dominant_category():
    values = ["Toronto"] * 12 + ["Ottawa"] * 8 + ["Torontoo"]
    profile, _ = inspect_column(pd.Series(values, name="city"))
    rare = next(i for i in profile["issues"] if i["kind"] == "rare")
    assert rare["examples"] == ["'Torontoo'"]


def test_numeric_summary():
    profile, _ = inspect_column(pd.Series([1.0, 2.0, np.nan, 3.0], name="n"))
    assert profile["summary"] == {"min": 1.0, "max": 3.0, "mean": 2.0, "median": 2.0}
    assert profile["missing_pct"] == 25.0


def test_score_components(messy):
    summary = profile_frame(messy)["summary"]
    assert summary["duplicate_rows"] == 1
    assert set(summary["components"]) == {"completeness", "uniqueness", "consistency", "validity"}
    assert 0 < summary["score"] < 100


def test_cleaning_the_sample_raises_the_score(sample_bytes):
    df = read_csv(sample_bytes)
    before = quality_score(df)
    for op, params in [
        ("remove_duplicates", {}),
        ("trim_whitespace", {}),
        ("mark_missing", {}),
        ("standardize_categories", {"column": "city"}),
        ("standardize_categories", {"column": "plan"}),
        ("change_case", {"columns": ["full_name"], "mode": "title"}),
        ("convert_type", {"columns": ["signup_date"], "target": "date"}),
        ("convert_type", {"columns": ["age"], "target": "integer"}),
        ("convert_type", {"columns": ["annual_spend"], "target": "number"}),
        ("convert_type", {"columns": ["active"], "target": "boolean"}),
    ]:
        df = apply_operation(df, op, params)
    after = profile_frame(df)["summary"]
    assert after["score"] > before + 5
    assert after["duplicate_rows"] == 0
    assert after["type_issues"] == 0
    assert after["components"]["consistency"] == 100.0
