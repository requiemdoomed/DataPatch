"""Cleaning operations for DataPatch.

Every operation takes a DataFrame plus keyword arguments and returns a new
DataFrame. Inputs are never modified, so any point in a session's history
can be rebuilt by replaying the steps on top of the original upload. That
is all undo really is.

Operations that drop rows keep the surviving rows' index labels, so a cleaned
row can always be traced back to its row in the original data.
"""

from __future__ import annotations

import inspect
import math
import re
import warnings
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd


class CleaningError(ValueError):
    """An operation couldn't run with the parameters it was given."""


class NothingToDo(CleaningError):
    """The operation is valid but the data has nothing for it to change."""


# Strings people commonly type when they mean "no value". Compared after
# stripping and case-folding.
MISSING_MARKERS = frozenset(
    {
        "",
        "na",
        "n/a",
        "n.a.",
        "#n/a",
        "nan",
        "null",
        "none",
        "nil",
        "-",
        "--",
        "?",
        "missing",
        "unknown",
        "not available",
    }
)

TRUE_WORDS = frozenset({"true", "t", "yes", "y", "1", "on"})
FALSE_WORDS = frozenset({"false", "f", "no", "n", "0", "off"})

_NUMBER_NOISE = re.compile(r"[\s,$€£¥₹%]")
_DATE_SHAPE = re.compile(
    r"""^\s*(
          \d{4}[-/.]\d{1,2}[-/.]\d{1,2}
        | \d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}
        | \d{1,2}\s+[a-z]{3,9}\.?,?\s+\d{2,4}
        | [a-z]{3,9}\.?\s+\d{1,2}(st|nd|rd|th)?,?\s+\d{2,4}
    )([ T]\d{1,2}:\d{2}(:\d{2})?)?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Small value helpers (also used by the profiler)
# ---------------------------------------------------------------------------


def is_blank(value: Any) -> bool:
    """True for None, NaN, pd.NA and NaT."""
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, (float, np.floating)):
        return math.isnan(value)
    return False


_LONGEST_MARKER = max(map(len, MISSING_MARKERS))


def is_marker(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if len(value) > _LONGEST_MARKER + 8:  # quick exit for ordinary long text
        return False
    return value.strip().casefold() in MISSING_MARKERS


def tidy_text(value: str) -> str:
    """Collapse runs of whitespace and strip the ends."""
    return " ".join(value.split())


def looks_like_date(value: str) -> bool:
    return bool(_DATE_SHAPE.match(value))


def parse_number(value: Any) -> float:
    """Best-effort number parsing: '$1,200.50' -> 1200.5, '(30)' -> -30."""
    if is_blank(value):
        return np.nan
    if isinstance(value, (bool, np.bool_)):
        return float(value)
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)
    if not isinstance(value, str):
        return np.nan

    text = value.strip()
    negative = text.startswith("(") and text.endswith(")")
    text = _NUMBER_NOISE.sub("", text.strip("()"))
    try:
        number = float(text)
    except ValueError:
        return np.nan
    if not math.isfinite(number):
        return np.nan
    return -number if negative else number


def parse_bool(value: Any) -> Any:
    if is_blank(value):
        return pd.NA
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float, np.integer, np.floating)):
        return bool(value) if value in (0, 1) else pd.NA
    if isinstance(value, str):
        word = value.strip().casefold()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
    return pd.NA


def format_timestamp(value: pd.Timestamp) -> str:
    if value.tzinfo is None and value == value.normalize():
        return value.strftime("%Y-%m-%d")
    return value.isoformat(sep=" ")


def column_kind(series: pd.Series) -> str:
    """A friendly name for the column's dtype: text, number, integer, date or boolean."""
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "date"
    if pd.api.types.is_integer_dtype(dtype):
        return "integer"
    if pd.api.types.is_numeric_dtype(dtype):
        return "number"
    return "text"


def is_text(series: pd.Series) -> bool:
    return column_kind(series) == "text"


def missing_mask(series: pd.Series) -> pd.Series:
    """Real NaNs plus placeholder strings such as 'N/A' or '-'."""
    mask = series.isna()
    if is_text(series) and len(series):
        markers = [v for v in pd.unique(series.dropna()) if is_marker(v)]
        if markers:
            mask = mask | series.isin(markers)
    return mask.astype(bool)


def to_numbers(series: pd.Series) -> pd.Series:
    """Return the column as float64, parsing text where possible."""
    if pd.api.types.is_numeric_dtype(series.dtype) or pd.api.types.is_bool_dtype(series.dtype):
        values = series.to_numpy(dtype="float64", na_value=np.nan)
    else:
        lookup = {v: parse_number(v) for v in pd.unique(series.dropna())}
        values = series.map(lookup).to_numpy(dtype="float64", na_value=np.nan)
    return pd.Series(values, index=series.index, name=series.name)


def finite_numbers(series: pd.Series) -> pd.Series:
    """The column as floats with missing and infinite values dropped."""
    numbers = to_numbers(series)
    return numbers[np.isfinite(numbers.to_numpy())]


def iqr_bounds(series: pd.Series, factor: float = 1.5) -> tuple[float, float] | None:
    """Tukey fences for a numeric column, or None when there isn't enough spread."""
    values = finite_numbers(series)
    if len(values) < 4:
        return None
    q1, q3 = values.quantile([0.25, 0.75])
    spread = q3 - q1
    if spread <= 0:
        return None
    return float(q1 - factor * spread), float(q3 + factor * spread)


def category_key(value: str) -> str:
    return tidy_text(value).casefold()


def suggest_category_mapping(values: Iterable[Any]) -> dict[str, str]:
    """Group spellings that only differ by case or spacing.

    Spellings are first tidied, so ' Toronto' counts towards 'Toronto'.
    Within a group, a mixed-case form ('Toronto') beats all-lower or
    all-upper ones ('toronto', 'TORONTO'); after that the most common
    spelling wins. Every other spelling maps onto the winner.
    """
    counts: dict[str, int] = {}
    for value in values:
        if isinstance(value, str):
            counts[value] = counts.get(value, 0) + 1
    return mapping_from_counts(counts)


def mapping_from_counts(counts: dict[str, int]) -> dict[str, str]:
    """Same as :func:`suggest_category_mapping`, from ``{spelling: count}``."""
    tidied = {value: tidy_text(value) for value in counts}

    groups: dict[str, dict[str, int]] = {}
    for value, count in counts.items():
        tidy = tidied[value]
        if tidy:
            forms = groups.setdefault(tidy.casefold(), {})
            forms[tidy] = forms.get(tidy, 0) + count

    winners: dict[str, str] = {}
    for key, forms in groups.items():
        if len(forms) == 1:
            winners[key] = next(iter(forms))
        else:
            winners[key] = max(forms, key=lambda f, forms=forms: (not (f.isupper() or f.islower()), forms[f], f))

    mapping: dict[str, str] = {}
    for value, tidy in tidied.items():
        if tidy:
            canonical = winners[tidy.casefold()]
            if value != canonical:
                mapping[value] = canonical
    return mapping


# ---------------------------------------------------------------------------
# Parameter checking
# ---------------------------------------------------------------------------


def _columns(df: pd.DataFrame, columns: Any, *, allow_all: bool = False) -> list:
    if columns is None or (isinstance(columns, (list, tuple, str)) and len(columns) == 0):
        if allow_all:
            return list(df.columns)
        raise CleaningError("Pick at least one column.")
    if isinstance(columns, str):
        columns = [columns]
    if not isinstance(columns, (list, tuple)):
        raise CleaningError("Columns must be a list of column names.")
    for name in columns:
        _column(df, name)
    return list(dict.fromkeys(columns))


def _column(df: pd.DataFrame, column: Any):
    if isinstance(column, (list, dict, set)) or column is None:
        raise CleaningError("Pick a column.")
    if column not in df.columns:
        raise CleaningError(f"There's no column called {column!r}.")
    return column


def _text_only(df: pd.DataFrame, columns: list) -> list:
    text = [c for c in columns if is_text(df[c])]
    if not text:
        raise CleaningError("None of the selected columns hold text.")
    return text


def _choice(value: Any, options: Iterable[str], label: str) -> str:
    options = tuple(options)
    if value not in options:
        raise CleaningError(f"{label} must be one of: {', '.join(options)}.")
    return value


def _map_text(series: pd.Series, func: Callable[[str], Any]) -> pd.Series:
    """Apply ``func`` to every string in the column, leaving other values alone.

    Each distinct string is only processed once, which matters for big files.
    """
    done = {v: func(v) for v in pd.unique(series.dropna()) if isinstance(v, str)}
    return series.map(lambda v: done[v] if isinstance(v, str) else v)


def _blank_markers(series: pd.Series) -> pd.Series:
    if not is_text(series):
        return series
    return series.mask(missing_mask(series) & series.notna())


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def trim_whitespace(df: pd.DataFrame, columns: Any = None) -> pd.DataFrame:
    """Strip the ends and squeeze inner runs of spaces. Cells that end up empty become missing."""
    cols = _text_only(df, _columns(df, columns, allow_all=True))
    out = df.copy()
    for col in cols:
        out[col] = _map_text(out[col], lambda v: tidy_text(v) or np.nan)
    return out


_CASES: dict[str, Callable[[str], str]] = {
    "lower": str.lower,
    "upper": str.upper,
    "title": str.title,
    "sentence": lambda s: s[:1].upper() + s[1:].lower(),
}


def change_case(df: pd.DataFrame, columns: Any, mode: str = "title") -> pd.DataFrame:
    _choice(mode, _CASES, "Case")
    cols = _text_only(df, _columns(df, columns))
    out = df.copy()
    for col in cols:
        out[col] = _map_text(out[col], _CASES[mode])
    return out


def standardize_categories(df: pd.DataFrame, column: Any, mode: str = "auto", mapping: Any = None) -> pd.DataFrame:
    """Merge spelling variants, either automatically or with an explicit old -> new mapping."""
    col = _column(df, column)
    if not is_text(df[col]):
        raise CleaningError(f"'{col}' doesn't hold text, so there are no categories to merge.")
    _choice(mode, ("auto", "mapping"), "Mode")

    if mode == "auto":
        mapping = suggest_category_mapping(df[col])
        if not mapping:
            raise NothingToDo(f"No case or spacing variants found in '{col}'.")
    else:
        if not isinstance(mapping, dict) or not mapping:
            raise CleaningError("Add at least one 'old => new' line to the mapping.")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
            raise CleaningError("Mapping keys and values must be text.")

    lookup = {old: (new if new != "" else np.nan) for old, new in mapping.items()}
    out = df.copy()
    out[col] = _map_text(out[col], lambda v: lookup.get(v, v))
    return out


def mark_missing(df: pd.DataFrame, columns: Any = None, markers: Any = None) -> pd.DataFrame:
    """Turn placeholder values ('N/A', '-', -999, ...) into real missing values."""
    cols = _columns(df, columns, allow_all=True)

    if markers is None or markers == "" or markers == []:
        text_markers = MISSING_MARKERS
        number_markers: set[float] = set()
    else:
        if isinstance(markers, str):
            markers = markers.split(",")
        if not isinstance(markers, (list, tuple)):
            raise CleaningError("Markers must be a list of values.")
        text_markers = frozenset(str(m).strip().casefold() for m in markers)
        number_markers = {n for n in map(parse_number, markers) if not math.isnan(n)}

    out = df.copy()
    for col in cols:
        series = out[col]
        kind = column_kind(series)
        if kind == "text":
            out[col] = series.map(
                lambda v: np.nan if isinstance(v, str) and v.strip().casefold() in text_markers else v
            )
        elif kind in ("number", "integer") and number_markers:
            hits = series.isin(number_markers)
            out[col] = series.mask(hits.to_numpy(dtype=bool))
    return out


def remove_duplicates(df: pd.DataFrame, columns: Any = None, keep: str = "first") -> pd.DataFrame:
    subset = _columns(df, columns, allow_all=True)
    _choice(keep, ("first", "last", "none"), "Keep")
    dupes = df.duplicated(subset=subset, keep=False if keep == "none" else keep)
    if not dupes.any():
        raise NothingToDo("No duplicate rows found.")
    return df.loc[~dupes]


FILL_STRATEGIES = ("mean", "median", "mode", "constant", "ffill", "bfill", "drop_rows")


def fill_missing(df: pd.DataFrame, columns: Any, strategy: str = "mode", value: Any = None) -> pd.DataFrame:
    """Fill gaps (including placeholder strings) or drop the rows that have them."""
    cols = _columns(df, columns)
    _choice(strategy, FILL_STRATEGIES, "Strategy")

    if strategy == "drop_rows":
        gaps = np.zeros(len(df), dtype=bool)
        for col in cols:
            gaps |= missing_mask(df[col]).to_numpy(dtype=bool)
        return df.loc[~gaps]

    out = df.copy()
    for col in cols:
        out[col] = _fill_series(_blank_markers(out[col]), strategy, value)
    return out


def _fill_series(series: pd.Series, strategy: str, value: Any) -> pd.Series:
    name = series.name
    kind = column_kind(series)

    if strategy in ("ffill", "bfill"):
        return series.ffill() if strategy == "ffill" else series.bfill()

    if strategy in ("mean", "median"):
        if kind not in ("number", "integer"):
            raise CleaningError(f"'{name}' isn't numeric, so it has no {strategy}. Convert it to a number first.")
        stat = getattr(to_numbers(series), strategy)()
        if is_blank(stat):
            raise CleaningError(f"'{name}' has no values to compute a {strategy} from.")
        if kind == "integer":
            stat = int(round(stat))
        return series.fillna(stat)

    if strategy == "mode":
        modes = series.dropna().mode()
        if modes.empty:
            raise CleaningError(f"'{name}' has no values to take the most common one from.")
        return series.fillna(modes.iloc[0])

    # constant
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise CleaningError("Enter a value to fill the gaps with.")
    return series.fillna(_coerce_constant(value, kind, name))


def _coerce_constant(value: Any, kind: str, name: Any) -> Any:
    if kind in ("number", "integer"):
        number = parse_number(value)
        if math.isnan(number):
            raise CleaningError(f"'{name}' is numeric, so the fill value has to be a number.")
        if kind == "integer":
            if not number.is_integer():
                raise CleaningError(f"'{name}' holds whole numbers, so the fill value must be one too.")
            return int(number)
        return number
    if kind == "boolean":
        flag = parse_bool(value)
        if flag is pd.NA:
            raise CleaningError(f"'{name}' is true/false, so the fill value has to be yes or no.")
        return flag
    if kind == "date":
        stamp = pd.to_datetime(str(value), errors="coerce")
        if pd.isna(stamp):
            raise CleaningError(f"Couldn't read {value!r} as a date.")
        return stamp
    return str(value)


CONVERT_TARGETS = ("number", "integer", "date", "boolean", "text")


def convert_type(df: pd.DataFrame, columns: Any, target: str = "number", dayfirst: bool = False) -> pd.DataFrame:
    """Change column types. Values that can't be converted become missing."""
    cols = _columns(df, columns)
    _choice(target, CONVERT_TARGETS, "Target type")
    out = df.copy()
    for col in cols:
        out[col] = _convert(out[col], target, bool(dayfirst))
    return out


def _convert(series: pd.Series, target: str, dayfirst: bool) -> pd.Series:
    kind = column_kind(series)

    if target == "number":
        return to_numbers(series)

    if target == "integer":
        numbers = to_numbers(series).round()
        if (numbers.abs() > 2**53).any():
            raise CleaningError(f"'{series.name}' has values too large to store as whole numbers.")
        return numbers.astype("Int64")

    if target == "boolean":
        return series.map(parse_bool).astype("boolean")

    if target == "text":
        return series.map(_as_text)

    # date
    if kind == "date":
        return series
    if kind != "text":
        raise CleaningError(f"'{series.name}' holds {kind} values, which can't be read as dates.")
    text = series.map(lambda v: None if is_blank(v) or is_marker(v) else str(v).strip())
    return _parse_dates(text, dayfirst)


def _parse_dates(text: pd.Series, dayfirst: bool) -> pd.Series:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            parsed = pd.to_datetime(text, errors="coerce", format="mixed", dayfirst=dayfirst)
        except (ValueError, TypeError):
            # Mixed time zones; normalise everything to UTC.
            parsed = pd.to_datetime(text, errors="coerce", format="mixed", dayfirst=dayfirst, utc=True)
    if getattr(parsed.dt, "tz", None) is not None:
        parsed = parsed.dt.tz_convert("UTC").dt.tz_localize(None)
    return parsed


def _as_text(value: Any) -> Any:
    if is_blank(value):
        return np.nan
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, np.bool_)):
        return "true" if value else "false"
    if isinstance(value, (float, np.floating)) and float(value).is_integer():
        return str(int(value))
    if isinstance(value, pd.Timestamp):
        return format_timestamp(value)
    return str(value)


def replace_values(df: pd.DataFrame, columns: Any, find: Any, replace: Any = "") -> pd.DataFrame:
    """Swap one exact value for another. An empty replacement means 'make it missing'."""
    cols = _columns(df, columns)
    if find is None or str(find) == "":
        raise CleaningError("Enter the value to find.")
    find = str(find)
    replace = "" if replace is None else str(replace)

    out = df.copy()
    for col in cols:
        series = out[col]
        kind = column_kind(series)
        if kind == "text":
            new = np.nan if replace == "" else replace
            out[col] = series.map(lambda v, new=new: new if isinstance(v, str) and v == find else v)
        elif kind in ("number", "integer"):
            needle = parse_number(find)
            if math.isnan(needle):
                continue
            new_value = np.nan if replace == "" else parse_number(replace)
            if replace != "" and math.isnan(new_value):
                raise CleaningError(f"'{col}' is numeric, so the replacement has to be a number.")
            if kind == "integer" and not math.isnan(new_value) and not new_value.is_integer():
                series = to_numbers(series)
            hits = series.eq(needle).fillna(False).to_numpy(dtype=bool)
            out[col] = series.mask(hits, new_value)
    return out


def handle_outliers(df: pd.DataFrame, column: Any, method: str = "clip", factor: Any = 1.5) -> pd.DataFrame:
    """Deal with values outside the IQR fences: clip them, blank them or drop their rows."""
    col = _column(df, column)
    series = df[col]
    kind = column_kind(series)
    if kind not in ("number", "integer"):
        raise CleaningError(f"'{col}' isn't numeric. Convert it to a number first.")
    _choice(method, ("clip", "missing", "remove"), "Method")
    try:
        factor = float(factor)
    except (TypeError, ValueError):
        raise CleaningError("The IQR factor has to be a number.") from None
    if not 0 < factor <= 10:
        raise CleaningError("The IQR factor should be between 0 and 10.")

    bounds = iqr_bounds(series, factor)
    if bounds is None:
        raise NothingToDo(f"'{col}' doesn't have enough spread to find outliers.")
    low, high = bounds
    numbers = to_numbers(series)
    outside = ((numbers < low) | (numbers > high)).to_numpy(dtype=bool)
    if not outside.any():
        raise NothingToDo(f"No outliers in '{col}' at {factor:g}x IQR.")

    out = df.copy()
    if method == "remove":
        return out.loc[~outside]
    if method == "missing":
        out[col] = series.mask(outside)
        return out
    if kind == "integer":
        low, high = math.ceil(low), math.floor(high)
    out[col] = series.clip(low, high)
    return out


def rename_column(df: pd.DataFrame, column: Any, new_name: Any) -> pd.DataFrame:
    col = _column(df, column)
    new_name = str(new_name or "").strip()
    if not new_name:
        raise CleaningError("Enter a new column name.")
    if new_name == col:
        raise CleaningError("That's already the column's name.")
    if new_name in df.columns:
        raise CleaningError(f"There's already a column called '{new_name}'.")
    return df.rename(columns={col: new_name})


def drop_columns(df: pd.DataFrame, columns: Any) -> pd.DataFrame:
    cols = _columns(df, columns)
    if len(cols) >= df.shape[1]:
        raise CleaningError("Keep at least one column.")
    return df.drop(columns=cols)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Operation:
    name: str
    label: str
    func: Callable[..., pd.DataFrame]
    describe: Callable[[dict], str]


def _names(columns: Any) -> str:
    if columns is None or columns == [] or columns == "":
        return "all columns"
    if isinstance(columns, str):
        columns = [columns]
    names = [str(c) for c in columns]
    if len(names) > 3:
        return f"{', '.join(names[:3])} +{len(names) - 3} more"
    return ", ".join(names)


def _describe_fill(p: dict) -> str:
    strategy = p.get("strategy", "mode")
    if strategy == "drop_rows":
        return f"Drop rows missing {_names(p.get('columns'))}"
    how = {
        "mean": "the mean",
        "median": "the median",
        "mode": "the most common value",
        "ffill": "the previous value",
        "bfill": "the next value",
        "constant": repr(str(p.get("value"))),
    }[strategy]
    return f"Fill gaps in {_names(p.get('columns'))} with {how}"


OPERATIONS: dict[str, Operation] = {
    op.name: op
    for op in (
        Operation(
            "trim_whitespace",
            "Trim whitespace",
            trim_whitespace,
            lambda p: f"Trim whitespace in {_names(p.get('columns'))}",
        ),
        Operation(
            "change_case",
            "Change case",
            change_case,
            lambda p: f"{p.get('mode', 'title').capitalize()} case for {_names(p.get('columns'))}",
        ),
        Operation(
            "standardize_categories",
            "Standardize categories",
            standardize_categories,
            lambda p: (
                f"Merge spelling variants in {p.get('column')}"
                if p.get("mode", "auto") == "auto"
                else f"Remap {len(p.get('mapping') or {})} value(s) in {p.get('column')}"
            ),
        ),
        Operation(
            "mark_missing",
            "Mark placeholders as missing",
            mark_missing,
            lambda p: f"Mark placeholders as missing in {_names(p.get('columns'))}",
        ),
        Operation("fill_missing", "Handle missing values", fill_missing, _describe_fill),
        Operation(
            "remove_duplicates",
            "Remove duplicate rows",
            remove_duplicates,
            lambda p: (
                "Remove duplicate rows" if not p.get("columns") else f"Remove duplicates by {_names(p.get('columns'))}"
            ),
        ),
        Operation(
            "convert_type",
            "Convert type",
            convert_type,
            lambda p: f"Convert {_names(p.get('columns'))} to {p.get('target', 'number')}",
        ),
        Operation(
            "replace_values",
            "Replace a value",
            replace_values,
            lambda p: (
                f"Replace {str(p.get('find'))!r} with "
                f"{repr(str(p['replace'])) if p.get('replace') else 'missing'} "
                f"in {_names(p.get('columns'))}"
            ),
        ),
        Operation(
            "handle_outliers",
            "Handle outliers",
            handle_outliers,
            lambda p: {
                "clip": f"Clip outliers in {p.get('column')}",
                "missing": f"Blank out outliers in {p.get('column')}",
                "remove": f"Drop rows with outliers in {p.get('column')}",
            }.get(p.get("method", "clip"), f"Handle outliers in {p.get('column')}"),
        ),
        Operation(
            "rename_column",
            "Rename column",
            rename_column,
            lambda p: f"Rename {p.get('column')} to {str(p.get('new_name', '')).strip()}",
        ),
        Operation(
            "drop_columns",
            "Drop columns",
            drop_columns,
            lambda p: f"Drop {_names(p.get('columns'))}",
        ),
    )
}


def apply_operation(df: pd.DataFrame, name: str, params: dict | None = None) -> pd.DataFrame:
    operation = OPERATIONS.get(name)
    if operation is None:
        raise CleaningError(f"Unknown operation: {name!r}.")
    params = {} if params is None else params
    if not isinstance(params, dict):
        raise CleaningError("Operation parameters must be an object.")
    signature = inspect.signature(operation.func)
    try:
        signature.bind(df, **params)
    except TypeError:
        accepted = ", ".join(list(signature.parameters)[1:])
        raise CleaningError(f"{operation.label} takes these parameters: {accepted}.") from None
    return operation.func(df, **params)


def apply_step(df: pd.DataFrame, name: str, params: dict | None = None) -> pd.DataFrame:
    """Like :func:`apply_operation`, but refuse steps that leave the data unchanged."""
    result = apply_operation(df, name, params)
    if result.equals(df) and list(result.columns) == list(df.columns):
        raise NothingToDo("That step wouldn't change anything.")
    return result


def describe_operation(name: str, params: dict) -> str:
    return OPERATIONS[name].describe(params or {})
