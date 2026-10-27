"""The framework-free API that both Flask and the in-browser engine call."""

import base64
import json

import pytest

from datapatch.api import DataPatchAPI
from tests.conftest import SAMPLE


@pytest.fixture
def api():
    return DataPatchAPI(SAMPLE)


def call(api, method, path, query=None, body=None, upload_path=None, filename=None):
    request = {"method": method, "path": path, "query": query or {}, "body": body}
    if filename:
        request["filename"] = filename
    return json.loads(api.handle_json(json.dumps(request), upload_path))


def test_dispatch_routes_and_errors(api):
    assert api.dispatch("GET", "/api/health").body["status"] == "ok"
    assert api.dispatch("GET", "/api/nothing").status == 404
    assert api.dispatch("GET", "/api/upload").status == 405
    assert api.dispatch("DELETE", "/api/sample").status == 405
    assert api.dispatch("GET", "/api/workspaces/abc123").status == 404
    assert api.dispatch("GET", "/api/workspaces/../etc").status == 404


def test_handle_json_runs_a_whole_session(api, tmp_path):
    upload = tmp_path / "upload"
    upload.write_bytes(b"name,city\n Ann ,toronto\nBob,Toronto\nBob,Toronto\n")
    state = call(api, "POST", "/api/upload", {"limit": "50"}, upload_path=str(upload), filename="people.csv")
    assert state["status"] == 201
    workspace = state["body"]["id"]
    assert state["body"]["filename"] == "people.csv"

    state = call(api, "POST", f"/api/workspaces/{workspace}/steps", body={"op": "remove_duplicates"})
    assert state["status"] == 200 and state["body"]["page"]["total"] == 2

    exported = call(api, "GET", f"/api/workspaces/{workspace}/export")
    assert exported["download"]["filename"] == "people_cleaned.csv"
    assert exported["download"]["mimetype"] == "text/csv"
    assert base64.b64decode(exported["download"]["base64"]).decode().splitlines()[0] == "name,city"

    recipe = call(api, "GET", f"/api/workspaces/{workspace}/recipe")
    steps = json.loads(base64.b64decode(recipe["download"]["base64"]))["steps"]
    assert [s["op"] for s in steps] == ["remove_duplicates"]


def test_handle_json_reports_errors_as_json(api):
    reply = call(api, "POST", "/api/upload")
    assert reply == {"status": 400, "body": {"error": "Choose a CSV file to upload."}}


def test_upload_limit_and_windows_paths(tmp_path):
    api = DataPatchAPI(SAMPLE, max_upload_bytes=10)
    reply = api.dispatch("POST", "/api/upload", upload=(r"C:\\Users\\me\\big.csv", b"a,b\n1,2\n3,4\n"))
    assert reply.status == 413 and "too big" in reply.body["error"]

    api = DataPatchAPI(SAMPLE)
    reply = api.dispatch("POST", "/api/upload", upload=(r"C:\\Users\\me\\data.csv", b"a,b\n1,2\n"))
    assert reply.body["filename"] == "data.csv"


def test_unexpected_errors_become_500(api, monkeypatch):
    def boom(**_):
        raise RuntimeError("boom")

    monkeypatch.setattr(api, "_routes", [("GET", api._routes[0][1], boom)])
    reply = api.dispatch("GET", "/api/health")
    assert reply.status == 500 and "went wrong" in reply.body["error"]


def test_server_serves_files_for_the_browser_engine(client):
    assert b"DataPatchAPI" in client.get("/datapatch/api.py").data
    assert client.get("/datapatch/server.txt").status_code == 404
    assert client.get("/datapatch/missing.py").status_code == 404
    assert client.get("/sample_data/messy_customers.csv").data.startswith(b"customer_id")
    assert client.get("/sample_data/nope.txt").status_code == 404
