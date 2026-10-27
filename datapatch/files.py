"""Reading uploads and turning frames into JSON- and CSV-friendly output."""

from __future__ import annotations

import csv
import io
import math
import re
import warnings
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from .cleaning import CleaningError, format_timestamp

ALLOWED_EXTENSIONS = (".csv", ".tsv", ".txt")
_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")
_FIELD_COUNT = re.compile(r"Expected (\d+) fields in line (\d+), saw (\d+)")


def _decode(raw: bytes) -> str:
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")  # pragma: no cover - latin-1 never fails


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _clean_header(columns: list[Any]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for position, column in enumerate(columns, start=1):
        name = str(column).strip()
        if not name or name.startswith("Unnamed:"):
            name = f"column_{position}"
        base, suffix = name, 2
        while name in seen:
            name = f"{base}_{suffix}"
            suffix += 1
        seen.add(name)
        names.append(name)
    return names


def _parser_message(error: str) -> str:
    found = _FIELD_COUNT.search(error)
    if found:
        expected, line, saw = found.groups()
        columns = "column" if expected == "1" else "columns"
        return f"Line {line} has {saw} values but the header has {expected} {columns}."
    first_line = error.strip().splitlines()[0] if error.strip() else "unknown error"
    return f"Couldn't read that CSV: {first_line}"


def read_csv(raw: bytes) -> pd.DataFrame:
    """Parse uploaded bytes into a DataFrame, guessing encoding and delimiter."""
    if not raw or not raw.strip():
        raise CleaningError("That file is empty.")

    text = _decode(raw)
    delimiter = _sniff_delimiter(text[:20_000])
    try:
        with warnings.catch_warnings():
            # index_col=False stops pandas from quietly turning the first column into
            # the index when a row has an extra field; we want to report that instead.
            warnings.simplefilter("error", pd.errors.ParserWarning)
            frame = pd.read_csv(io.StringIO(text), sep=delimiter, index_col=False)
    except pd.errors.EmptyDataError:
        raise CleaningError("That file has no rows or columns.") from None
    except pd.errors.ParserWarning:
        raise CleaningError(
            "Some rows have more values than the header has columns. "
            "Check for unquoted commas or a missing header name."
        ) from None
    except (pd.errors.ParserError, ValueError) as exc:
        raise CleaningError(_parser_message(str(exc))) from None

    if frame.shape[1] == 0:
        raise CleaningError("That file has no columns.")
    frame.columns = _clean_header(list(frame.columns))
    return frame


def to_csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8")


def json_value(value: Any) -> Any:
    """Convert a single cell into something json.dumps accepts."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        return value
    if isinstance(value, pd.Timestamp):
        return format_timestamp(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def frame_rows(frame: pd.DataFrame, offset: int = 0, limit: int = 100) -> list[list[Any]]:
    chunk = frame.iloc[offset : offset + limit]
    return [[json_value(v) for v in row] for row in chunk.itertuples(index=False, name=None)]
