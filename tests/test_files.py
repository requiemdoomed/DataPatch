import math

import numpy as np
import pandas as pd
import pytest

from datapatch.cleaning import CleaningError
from datapatch.files import frame_rows, json_value, read_csv, to_csv_bytes


def test_extra_fields_are_reported_not_shifted():
    # Without care pandas would turn the first column into the index and shift
    # every value one column to the left.
    with pytest.raises(CleaningError, match="more values than the header"):
        read_csv(b"a,b\n1,2,3\n4\n")
    with pytest.raises(CleaningError, match="Line 3 has 3 values but the header has 2 columns"):
        read_csv(b"a,b\n1,2\n3,4,5\n")


def test_header_cleanup():
    frame = read_csv(b"a,a,,b\n1,2,3,4\n")
    assert list(frame.columns) == ["a", "a.1", "column_3", "b"]


def test_trailing_delimiter_becomes_an_empty_column():
    frame = read_csv(b"a,b,\n1,2,\n")
    assert list(frame.columns) == ["a", "b", "column_3"]
    assert frame["a"].tolist() == [1]


@pytest.mark.parametrize(
    "raw, delimiter_columns",
    [(b"a;b\n1;2\n", 2), (b"a\tb\n1\t2\n", 2), (b"a|b|c\n1|2|3\n", 3)],
)
def test_delimiters_are_detected(raw, delimiter_columns):
    assert read_csv(raw).shape[1] == delimiter_columns


def test_empty_and_header_only():
    with pytest.raises(CleaningError, match="empty"):
        read_csv(b"   \n")
    frame = read_csv(b"a,b\n")
    assert frame.shape == (0, 2)


def test_json_value_handles_awkward_cells():
    assert json_value(np.nan) is None
    assert json_value(float("inf")) is None
    assert json_value(pd.NA) is None
    assert json_value(pd.NaT) is None
    assert json_value(np.int64(3)) == 3
    assert json_value(np.bool_(True)) is True
    assert json_value(pd.Timestamp("2024-03-05")) == "2024-03-05"
    assert json_value(pd.Timestamp("2024-03-05 10:30")) == "2024-03-05 10:30:00"


def test_frame_rows_and_csv_output():
    frame = pd.DataFrame({"n": pd.array([1, None], dtype="Int64"), "f": [math.inf, 2.5]})
    assert frame_rows(frame) == [[1, None], [None, 2.5]]
    assert to_csv_bytes(frame).decode().splitlines() == ["n,f", "1,inf", ",2.5"]
