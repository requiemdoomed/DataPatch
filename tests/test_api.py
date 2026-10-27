import io

import pandas as pd


def upload(client, data: bytes, name: str = "data.csv"):
    return client.post(
        "/api/upload",
        data={"file": (io.BytesIO(data), name)},
        content_type="multipart/form-data",
    )


def test_index_and_static_files_are_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert b'src="static/js/app.js"' in page.data
    assert b'name="datapatch-engine" content="server"' in page.data
    assert client.get("/static/js/app.js").status_code == 200
    assert client.get("/static/js/engine.js").status_code == 200
    assert client.get("/static/js/engine-worker.js").status_code == 200
    assert client.get("/static/css/style.css").status_code == 200
    assert client.get("/api/health").get_json()["status"] == "ok"


def test_upload_returns_full_state(client, sample_bytes):
    response = upload(client, sample_bytes, "messy_customers.csv")
    assert response.status_code == 201
    state = response.get_json()
    assert state["filename"] == "messy_customers.csv"
    assert state["page"] == {"offset": 0, "limit": 100, "total": 84}
    assert len(state["rows"]) == 84
    assert len(state["columns"]) == 9
    assert state["columns"] == state["cleaned_columns"]
    assert state["scores"]["original"] == state["scores"]["cleaned"]
    assert state["history"] == [] and not state["can_undo"]


def test_sample_endpoint(client):
    response = client.post("/api/sample")
    assert response.status_code == 201
    assert response.get_json()["filename"] == "messy_customers.csv"


def test_semicolon_files_and_latin1_text(client):
    data = "name;city\nJosé;Montréal\nAnn;Québec\n".encode("cp1252")
    state = upload(client, data).get_json()
    assert [c["name"] for c in state["columns"]] == ["name", "city"]
    assert state["rows"][0] == ["José", "Montréal"]


def test_upload_rejects_bad_files(client):
    assert upload(client, b"", "empty.csv").status_code == 400
    assert upload(client, b"a,b\n1,2\n", "data.xlsx").status_code == 400
    response = client.post("/api/upload", data={}, content_type="multipart/form-data")
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_upload_too_large(app, client):
    app.config["MAX_CONTENT_LENGTH"] = 100
    response = upload(client, b"a,b\n" + b"1,2\n" * 200)
    assert response.status_code == 413
    assert "too big" in response.get_json()["error"]


def test_full_cleaning_flow(client, sample_bytes):
    state = upload(client, sample_bytes).get_json()
    ws = state["id"]
    original_score = state["scores"]["original"]["score"]

    response = client.post(f"/api/workspaces/{ws}/steps", json={"op": "remove_duplicates", "params": {}})
    assert response.status_code == 200
    state = response.get_json()
    assert state["page"]["total"] == 78
    assert "6 rows removed" in state["message"]

    for op, params in [
        ("trim_whitespace", {}),
        ("standardize_categories", {"column": "city"}),
        ("convert_type", {"columns": ["annual_spend"], "target": "number"}),
    ]:
        state = client.post(f"/api/workspaces/{ws}/steps", json={"op": op, "params": params}).get_json()

    assert len(state["history"]) == 4
    assert state["scores"]["cleaned"]["score"] > original_score
    assert state["scores"]["original"]["score"] == original_score

    state = client.post(f"/api/workspaces/{ws}/undo").get_json()
    assert len(state["history"]) == 3 and state["can_redo"]
    assert state["redo_label"].startswith("Convert annual_spend")

    state = client.post(f"/api/workspaces/{ws}/redo").get_json()
    assert len(state["history"]) == 4

    original_view = client.get(f"/api/workspaces/{ws}?view=original").get_json()
    assert original_view["view"] == "original"
    assert original_view["page"]["total"] == 84
    assert len(original_view["cleaned_columns"]) == 9

    export = client.get(f"/api/workspaces/{ws}/export")
    assert export.status_code == 200
    assert export.mimetype == "text/csv"
    assert "attachment" in export.headers["Content-Disposition"]
    assert "data_cleaned.csv" in export.headers["Content-Disposition"]
    cleaned = pd.read_csv(io.BytesIO(export.data))
    assert len(cleaned) == 78
    assert pd.api.types.is_float_dtype(cleaned["annual_spend"])

    state = client.post(f"/api/workspaces/{ws}/reset").get_json()
    assert state["history"] == [] and state["scores"]["cleaned"]["score"] == original_score


def test_pagination_is_clamped(client, sample_bytes):
    ws = upload(client, sample_bytes).get_json()["id"]
    state = client.get(f"/api/workspaces/{ws}?offset=50&limit=20").get_json()
    assert state["page"]["offset"] == 50 and len(state["rows"]) == 20
    state = client.get(f"/api/workspaces/{ws}?offset=9999&limit=20").get_json()
    assert state["page"]["offset"] == 80 and len(state["rows"]) == 4
    state = client.get(f"/api/workspaces/{ws}?limit=banana").get_json()
    assert state["page"]["limit"] == 100


def test_step_errors_come_back_as_json(client, sample_bytes):
    ws = upload(client, sample_bytes).get_json()["id"]
    bad = [
        {"op": "explode"},
        {"op": "fill_missing", "params": {"columns": ["city"], "strategy": "mean"}},
        {"op": "trim_whitespace", "params": {"columns": ["missing_column"]}},
        {"params": {}},
    ]
    for body in bad:
        response = client.post(f"/api/workspaces/{ws}/steps", json=body)
        assert response.status_code == 400
        assert response.get_json()["error"]

    response = client.post(f"/api/workspaces/{ws}/steps", data="not json", content_type="text/plain")
    assert response.status_code == 400
    assert client.post(f"/api/workspaces/{ws}/undo").status_code == 400


def test_unknown_workspace_and_routes(client):
    assert client.get("/api/workspaces/nope").status_code == 404
    assert "expired" in client.get("/api/workspaces/nope").get_json()["error"]
    assert client.get("/api/nothing-here").get_json()["error"]
    assert client.get("/api/upload").status_code == 405


def test_json_is_valid_with_missing_dates_and_booleans(client, sample_bytes):
    ws = upload(client, sample_bytes).get_json()["id"]
    for op, params in [
        ("convert_type", {"columns": ["signup_date"], "target": "date"}),
        ("convert_type", {"columns": ["active"], "target": "boolean"}),
        ("convert_type", {"columns": ["age"], "target": "integer"}),
    ]:
        response = client.post(f"/api/workspaces/{ws}/steps", json={"op": op, "params": params})
        assert response.status_code == 200
    state = response.get_json()
    kinds = {c["name"]: c["kind"] for c in state["columns"]}
    assert kinds["signup_date"] == "date" and kinds["active"] == "boolean" and kinds["age"] == "integer"
    dates = [row[4] for row in state["rows"] if row[4] is not None]
    assert all(len(d) == 10 for d in dates)
    assert b"NaN" not in response.data


def test_changes_and_row_numbers_in_state(client, sample_bytes):
    state = upload(client, sample_bytes).get_json()
    ws = state["id"]
    assert state["row_numbers"][:3] == [1, 2, 3]
    assert state["changes"]["cells_changed"] == 0

    state = client.post(f"/api/workspaces/{ws}/steps", json={"op": "remove_duplicates"}).get_json()
    assert state["changes"]["rows_removed"] == 6
    assert len(state["row_numbers"]) == 78
    assert state["row_numbers"] != list(range(1, 79))

    state = client.post(f"/api/workspaces/{ws}/steps", json={"op": "trim_whitespace"}).get_json()
    assert state["changed_cells"]
    row, col, was = state["changed_cells"][0]
    assert state["rows"][row][col] != was

    original = client.get(f"/api/workspaces/{ws}?view=original&offset=80&limit=10").get_json()
    assert original["row_numbers"] == [81, 82, 83, 84]
    assert original["changed_cells"] == []


def test_recipe_download_and_apply(client, sample_bytes):
    first = upload(client, sample_bytes).get_json()["id"]
    assert client.get(f"/api/workspaces/{first}/recipe").status_code == 400

    for op, params in [
        ("remove_duplicates", {}),
        ("trim_whitespace", {}),
        ("convert_type", {"columns": ["age"], "target": "integer"}),
    ]:
        client.post(f"/api/workspaces/{first}/steps", json={"op": op, "params": params})
    response = client.get(f"/api/workspaces/{first}/recipe")
    assert response.status_code == 200
    assert "data_recipe.json" in response.headers["Content-Disposition"]
    recipe = response.get_json()
    assert len(recipe["steps"]) == 3

    second = upload(client, sample_bytes, "next_month.csv").get_json()["id"]
    state = client.post(f"/api/workspaces/{second}/recipe", json=recipe).get_json()
    assert state["message"] == "Applied 3 steps from the recipe"
    assert len(state["history"]) == 3
    first_state = client.get(f"/api/workspaces/{first}").get_json()
    assert state["scores"]["cleaned"] == first_state["scores"]["cleaned"]

    bad = client.post(
        f"/api/workspaces/{second}/recipe", json={"steps": [{"op": "drop_columns", "params": {"columns": ["x"]}}]}
    )
    assert bad.status_code == 400 and "Recipe step 1" in bad.get_json()["error"]
    assert len(client.get(f"/api/workspaces/{second}").get_json()["history"]) == 3

    assert client.post(f"/api/workspaces/{second}/recipe", data="nope", content_type="text/plain").status_code == 400
