from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from datapatch.server import create_app

SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "messy_customers.csv"


@pytest.fixture
def messy() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "name": ["  Alice", "bob ", "Carol  Ann", "Dave", "Dave", None],
            "city": ["Toronto", "toronto", " TORONTO", "Ottawa", "Ottawa", "N/A"],
            "age": ["34", "29", "unknown", "41", "41", "$52"],
            "score": [1.0, 2.0, np.nan, 4.0, 4.0, 100.0],
        }
    )


@pytest.fixture
def app():
    return create_app({"TESTING": True})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def sample_bytes() -> bytes:
    return SAMPLE.read_bytes()
