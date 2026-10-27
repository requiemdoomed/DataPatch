import json

import pandas as pd

from datapatch.__main__ import main


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_profile_only(tmp_path, capsys):
    data = write(tmp_path / "people.csv", "name,city\n Ann ,toronto\nBob,Toronto\n")
    assert main([str(data)]) == 0
    out = capsys.readouterr().out
    assert "Original: score" in out
    assert "name: 1 value with extra spaces" in out
    assert not (tmp_path / "people_cleaned.csv").exists()


def test_clean_with_recipe(tmp_path, capsys):
    data = write(tmp_path / "people.csv", "name,city\n Ann ,toronto\nBob,Toronto\nBob,Toronto\n")
    recipe = tmp_path / "steps.json"
    recipe.write_text(
        json.dumps(
            {
                "steps": [
                    {"op": "trim_whitespace"},
                    {"op": "remove_duplicates"},
                    {"op": "standardize_categories", "params": {"column": "city"}},
                ]
            }
        )
    )
    assert main([str(data), "--recipe", str(recipe)]) == 0
    cleaned = pd.read_csv(tmp_path / "people_cleaned.csv")
    assert cleaned.to_dict("list") == {"name": ["Ann", "Bob"], "city": ["Toronto", "Toronto"]}
    out = capsys.readouterr().out
    assert "Applied 3 steps" in out and "Cleaned: score 100.0" in out


def test_custom_output_quiet_and_strict(tmp_path, capsys):
    data = write(tmp_path / "a.csv", "x\n1\n2\n")
    recipe = write(tmp_path / "r.json", json.dumps({"steps": [{"op": "remove_duplicates"}]}))
    out_file = tmp_path / "out" / "clean.csv"
    out_file.parent.mkdir()

    assert main([str(data), "-r", str(recipe), "-o", str(out_file), "--quiet"]) == 0
    assert out_file.exists()
    assert capsys.readouterr().out == ""

    assert main([str(data), "-r", str(recipe), "--strict"]) == 1
    assert "No duplicate rows" in capsys.readouterr().err


def test_errors_exit_non_zero(tmp_path, capsys):
    assert main([str(tmp_path / "missing.csv")]) == 2
    assert "can't read" in capsys.readouterr().err

    data = write(tmp_path / "a.csv", "x\n1\n")
    bad = write(tmp_path / "bad.json", "{oops")
    assert main([str(data), "-r", str(bad)]) == 1
    assert "isn't valid JSON" in capsys.readouterr().err

    empty = write(tmp_path / "empty.csv", "")
    assert main([str(empty)]) == 2
