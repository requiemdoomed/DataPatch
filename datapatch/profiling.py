"""Column profiling and the overall data-quality score.

The score is a weighted blend of four ratios, each between 0 and 1:

* completeness - share of cells that aren't missing (placeholders count as missing)
* uniqueness   - share of rows that aren't exact duplicates
* consistency  - share of text cells without stray whitespace or case/spacing variants
* validity     - per column, 1.0 if the stored type fits the values; text columns that
                 are really numbers, dates or yes/no flags get half credit at best

Outliers and rare values are reported but don't move the score, since they
are often legitimate.
"""

from __future__ import annotations

import math
import re
from typing import Any

import pandas as pd

from .cleaning import (
    FALSE_WORDS,
    TRUE_WORDS,
    category_key,
    column_kind,
    finite_numbers,
    format_timestamp,
    iqr_bounds,
    is_marker,
    looks_like_date,
    mapping_from_counts,
    missing_mask,
    parse_number,
    tidy_text,
)
from .files import json_value

WEIGHTS = {"completeness": 0.4, "uniqueness": 0.2, "consistency": 0.2, "validity": 0.2}
EXAMPLE_LIMIT = 4
SAMPLE_SIZE = 2000
BOOL_WORDS = TRUE_WORDS | FALSE_WORDS


def _issue(kind: str, count: int, message: str, examples=None, fix=None) -> dict[str, Any]:
    return {
        "kind": kind,
        "count": int(count),
        "message": message,
        "examples": list(examples or [])[:EXAMPLE_LIMIT],
        "fix": fix,
    }


def _fix(op: str, label: str, **params: Any) -> dict[str, Any]:
    return {"op": op, "label": label, "params": params}


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _verb(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _shape(value: str) -> str:
    """'2024-03-05' -> '9999-99-99', 'Mar 5, 2024' -> 'aaa 9, 9999'."""
    return re.sub(r"[a-zA-Z]+", "a", re.sub(r"\d+", "9", value.strip()))


def _missing_issue(name: str, kind: str, missing: int, present: pd.Series) -> dict[str, Any]:
    if present.empty:
        fix = _fix("drop_columns", "Drop column", columns=[name])
    elif kind in ("number", "integer"):
        fix = _fix("fill_missing", "Fill with median", columns=[name], strategy="median")
    else:
        fix = _fix("fill_missing", "Fill with most common", columns=[name], strategy="mode")
    return _issue("missing", missing, _plural(missing, "missing value"), fix=fix)


def _inspect_text(name: str, series: pd.Series, issues: list, stats: dict) -> None:
    # Work on distinct values with their counts. Real columns repeat a lot, so this is
    # much cheaper than looking at every cell, and the counts keep the numbers exact.
    counts = series.value_counts()
    pairs = list(zip(counts.index.tolist(), counts.tolist(), strict=True))
    markers = {v: int(c) for v, c in pairs if is_marker(v)}
    if markers:
        total = sum(markers.values())
        issues.append(
            _issue(
                "placeholders",
                total,
                f"{_plural(total, 'placeholder')} standing in for missing data",
                [repr(v) for v in markers],
                _fix("mark_missing", "Mark as missing", columns=[name]),
            )
        )

    text = {v: int(c) for v, c in pairs if isinstance(v, str) and v not in markers}
    stats["text_cells"] = sum(text.values())
    if not text:
        return

    tidied = {v: tidy_text(v) for v in text}
    untidy = {v: c for v, c in text.items() if tidied[v] != v}
    if untidy:
        total = sum(untidy.values())
        issues.append(
            _issue(
                "whitespace",
                total,
                f"{_plural(total, 'value')} with extra spaces",
                [repr(v) for v in untidy],
                _fix("trim_whitespace", "Trim", columns=[name]),
            )
        )

    mapping = mapping_from_counts(text)
    variants = {v: c for v, c in text.items() if v in mapping and tidied[v] != mapping[v]}
    if variants:
        total = sum(variants.values())
        examples = dict.fromkeys(f"{tidied[v]} → {mapping[v]}" for v in variants)
        issues.append(
            _issue(
                "variants",
                total,
                f"{_plural(total, 'value')} {_verb(total, 'differs', 'differ')} only by case or spacing",
                list(examples),
                _fix("standardize_categories", "Merge variants", column=name, mode="auto"),
            )
        )
    stats["inconsistent"] = sum(c for v, c in text.items() if v in untidy or v in variants)

    items = list(text.items())
    _inspect_text_type(name, items, issues, stats)
    _inspect_rare(items, issues)


def _share(items: list[tuple[str, int]], predicate, threshold: float) -> float:
    """Count-weighted share of values that pass ``predicate``.

    For columns with many distinct values a spread-out sample is checked first, and
    the full pass is skipped when the sample is clearly below ``threshold``.
    """
    if len(items) > SAMPLE_SIZE:
        step = len(items) // SAMPLE_SIZE
        sample = items[::step]
        quick = sum(1 for v, _ in sample if predicate(v)) / len(sample)
        if quick < threshold - 0.15:
            return quick
    total = sum(c for _, c in items)
    return sum(c for v, c in items if predicate(v)) / total if total else 0.0


def _is_bool_word(value: str) -> bool:
    return value.strip().casefold() in BOOL_WORDS


def _parses_as_number(value: str) -> bool:
    return not math.isnan(parse_number(value))


def _inspect_text_type(name: str, items: list[tuple[str, int]], issues: list, stats: dict) -> None:
    cells = sum(c for _, c in items)
    number_ratio = _share(items, _parses_as_number, 0.8)
    bool_ratio = _share(items, _is_bool_word, 0.9)

    if bool_ratio >= 0.9 and number_ratio < 1:
        spellings = sorted({v.strip().casefold() for v, _ in items if _is_bool_word(v)})
        issues.append(
            _issue(
                "boolean_text",
                cells,
                f"Yes/no values stored as text in {len(spellings)} spellings",
                [repr(s) for s in spellings],
                _fix("convert_type", "Convert to true/false", columns=[name], target="boolean"),
            )
        )
        stats["validity"] = 0.5 * bool_ratio
        return

    if number_ratio >= 0.8:
        parsed = {v: parse_number(v) for v, _ in items}
        bad = [(v, c) for v, c in items if math.isnan(parsed[v])]
        whole = all(n.is_integer() for n in parsed.values() if not math.isnan(n))
        target = "integer" if whole else "number"
        bad_cells = sum(c for _, c in bad)
        message = "Numbers stored as text"
        if bad_cells:
            message += f"; {_plural(bad_cells, 'value')} won't parse"
        examples = [repr(v) for v, _ in bad] or [repr(v) for v, _ in items]
        issues.append(
            _issue(
                "number_text",
                cells,
                message,
                examples,
                _fix("convert_type", f"Convert to {target}", columns=[name], target=target),
            )
        )
        stats["validity"] = 0.5 * number_ratio
        return

    date_ratio = _share(items, looks_like_date, 0.8)
    if date_ratio >= 0.8:
        shapes = len({_shape(v) for v, _ in items[: SAMPLE_SIZE * 5] if looks_like_date(v)})
        message = "Dates stored as text"
        if shapes > 1:
            message += f" in {shapes} different formats"
        issues.append(
            _issue(
                "date_text",
                cells,
                message,
                [repr(v) for v, _ in items],
                _fix("convert_type", "Convert to date", columns=[name], target="date"),
            )
        )
        stats["validity"] = 0.5 * date_ratio


def _inspect_rare(items: list[tuple[str, int]], issues: list) -> None:
    if sum(c for _, c in items) < 20:
        return
    groups: dict[str, int] = {}
    spelling: dict[str, str] = {}
    for value, count in items:
        key = category_key(value)
        groups[key] = groups.get(key, 0) + count
        spelling.setdefault(key, tidy_text(value))
        if len(groups) > 15:
            return
    if len(groups) < 2 or max(groups.values()) < 5:
        return
    rare = [spelling[k] for k, c in groups.items() if c == 1]
    if not rare:
        return
    issues.append(
        _issue(
            "rare",
            len(rare),
            f"{_plural(len(rare), 'value')} {_verb(len(rare), 'appears', 'appear')} only once, possibly typos",
            [repr(v) for v in rare],
        )
    )


def _inspect_numbers(name: str, present: pd.Series, issues: list) -> None:
    if len(present) < 8:
        return
    bounds = iqr_bounds(present)
    if bounds is None:
        return
    low, high = bounds
    numbers = finite_numbers(present)
    outside = numbers[(numbers < low) | (numbers > high)]
    if outside.empty:
        return
    examples = [f"{v:g}" for v in sorted(pd.unique(outside), key=lambda v: -abs(v))]
    issues.append(
        _issue(
            "outliers",
            len(outside),
            f"{_plural(len(outside), 'value')} outside the usual range ({low:,.4g} to {high:,.4g})",
            examples,
            _fix("handle_outliers", "Clip", column=name, method="clip"),
        )
    )


def _summary(kind: str, present: pd.Series) -> dict[str, Any] | None:
    if present.empty:
        return None
    if kind in ("number", "integer"):
        numbers = finite_numbers(present)
        if numbers.empty:
            return None
        return {
            "min": json_value(round(float(numbers.min()), 4)),
            "max": json_value(round(float(numbers.max()), 4)),
            "mean": json_value(round(float(numbers.mean()), 4)),
            "median": json_value(round(float(numbers.median()), 4)),
        }
    if kind == "date":
        return {"min": format_timestamp(present.min()), "max": format_timestamp(present.max())}
    return None


def inspect_column(series: pd.Series) -> tuple[dict[str, Any], dict[str, Any]]:
    """Profile one column. Returns the public profile and the raw numbers behind the score."""
    name = str(series.name)
    kind = column_kind(series)
    missing = missing_mask(series)
    present = series[~missing.to_numpy(dtype=bool)]
    missing_count = int(missing.sum())

    stats = {"missing": missing_count, "text_cells": 0, "inconsistent": 0, "validity": 1.0}
    issues: list[dict[str, Any]] = []
    if missing_count:
        issues.append(_missing_issue(name, kind, missing_count, present))
    if kind == "text":
        _inspect_text(name, series, issues, stats)
    elif kind in ("number", "integer"):
        _inspect_numbers(name, present, issues)

    counts = present.value_counts() if len(present) else pd.Series(dtype="int64")
    unique = int(present.nunique())
    profile = {
        "name": name,
        "kind": kind,
        "count": int(len(series)),
        "missing": missing_count,
        "missing_pct": round(100 * missing_count / len(series), 1) if len(series) else 0.0,
        "unique": unique,
        "repeated": int(len(present) - unique),
        "top": [{"value": json_value(v), "count": int(c)} for v, c in counts.head(5).items()],
        "summary": _summary(kind, present),
        "issues": issues,
    }
    return profile, stats


class ColumnCache:
    """Remembers column profiles so unchanged columns aren't profiled again.

    Most cleaning steps touch one or two columns. Comparing a column with a
    cached copy is a fast vectorised check, while profiling it is not, so a
    workspace keeps one of these and passes it to :func:`profile_frame`.
    """

    def __init__(self, per_column: int = 4) -> None:
        self.per_column = per_column
        self._entries: dict[str, list[tuple[pd.Series, dict, dict]]] = {}

    def get(self, series: pd.Series) -> tuple[dict, dict] | None:
        for cached, profile, stats in self._entries.get(str(series.name), []):
            if cached.dtype == series.dtype and len(cached) == len(series) and _same_values(cached, series):
                return profile, stats
        return None

    def put(self, series: pd.Series, profile: dict, stats: dict) -> None:
        bucket = self._entries.setdefault(str(series.name), [])
        bucket.insert(0, (series, profile, stats))
        del bucket[self.per_column :]


def _same_values(a: pd.Series, b: pd.Series) -> bool:
    # Profiles don't depend on the index, so compare the values only.
    return a.reset_index(drop=True).equals(b.reset_index(drop=True))


def profile_frame(frame: pd.DataFrame, cache: ColumnCache | None = None) -> dict[str, Any]:
    rows, cols = frame.shape
    profiles = []
    missing_cells = text_cells = inconsistent = type_issues = 0
    validity: list[float] = []

    for column in frame.columns:
        series = frame[column]
        found = cache.get(series) if cache is not None else None
        if found is None:
            profile, stats = inspect_column(series)
            if cache is not None:
                cache.put(series, profile, stats)
        else:
            profile, stats = found
        profiles.append(profile)
        missing_cells += stats["missing"]
        text_cells += stats["text_cells"]
        inconsistent += stats["inconsistent"]
        validity.append(stats["validity"])
        if stats["validity"] < 1:
            type_issues += 1

    duplicate_rows = int(frame.duplicated().sum()) if rows else 0
    total_cells = rows * cols
    ratios = {
        "completeness": 1 - missing_cells / total_cells if total_cells else 1.0,
        "uniqueness": 1 - duplicate_rows / rows if rows else 1.0,
        "consistency": 1 - inconsistent / text_cells if text_cells else 1.0,
        "validity": sum(validity) / len(validity) if validity else 1.0,
    }
    score = 100 * sum(WEIGHTS[k] * v for k, v in ratios.items())

    return {
        "summary": {
            "rows": rows,
            "columns": cols,
            "missing_cells": missing_cells,
            "duplicate_rows": duplicate_rows,
            "inconsistent_cells": inconsistent,
            "type_issues": type_issues,
            "issue_count": sum(len(p["issues"]) for p in profiles),
            "score": round(score, 1),
            "components": {k: round(v * 100, 1) for k, v in ratios.items()},
        },
        "columns": profiles,
    }


def quality_score(frame: pd.DataFrame) -> float:
    return profile_frame(frame)["summary"]["score"]
