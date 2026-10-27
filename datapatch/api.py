"""The DataPatch API, independent of any web framework.

Both front ends call into this module:

* ``server.py`` wraps it in Flask for ``python app.py``;
* the GitHub Pages build runs this same package inside the browser with
  Pyodide, and its Web Worker calls :meth:`DataPatchAPI.handle_json`.

Keeping the request handling here means the hosted demo and the local server
can't drift apart.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .cleaning import CleaningError, column_kind
from .files import ALLOWED_EXTENSIONS, frame_rows, read_csv, to_csv_bytes
from .recipe import make_recipe
from .workspace import Workspace, WorkspaceNotFound, WorkspaceStore

PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

log = logging.getLogger(__name__)


@dataclass
class Download:
    filename: str
    content: bytes
    mimetype: str


@dataclass
class Reply:
    status: int
    body: dict[str, Any] | None = None
    download: Download | None = None


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _int_arg(query: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(query.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _columns(frame) -> list[dict[str, str]]:
    return [{"name": str(c), "kind": column_kind(frame[c])} for c in frame.columns]


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else plural or word + 's'}"


def build_state(workspace: Workspace, query: dict[str, Any] | None = None, message: str | None = None) -> dict:
    """Everything the page needs to redraw itself."""
    query = query or {}
    view = "original" if query.get("view") == "original" else "cleaned"
    frame = workspace.original if view == "original" else workspace.current
    profile = workspace.original_profile if view == "original" else workspace.current_profile

    limit = _int_arg(query, "limit", PAGE_SIZE, 1, MAX_PAGE_SIZE)
    offset = _int_arg(query, "offset", 0, 0, 10**9)
    if offset >= len(frame):
        offset = max(0, (len(frame) - 1) // limit * limit)

    if view == "original":
        page = {"row_numbers": list(range(offset + 1, min(offset + limit, len(frame)) + 1)), "cells": []}
    else:
        page = workspace.page_changes(offset, limit)

    redo = workspace.next_redo
    return {
        "id": workspace.id,
        "filename": workspace.filename,
        "view": view,
        "page": {"offset": offset, "limit": limit, "total": len(frame)},
        "columns": _columns(frame),
        "cleaned_columns": _columns(workspace.current),
        "rows": frame_rows(frame, offset, limit),
        "row_numbers": page["row_numbers"],
        "changed_cells": page["cells"],
        "changes": workspace.changes,
        "profiles": profile["columns"],
        "scores": {
            "original": workspace.original_profile["summary"],
            "cleaned": workspace.current_profile["summary"],
        },
        "history": [step.to_dict() for step in workspace.steps],
        "can_undo": workspace.can_undo,
        "can_redo": workspace.can_redo,
        "redo_label": redo.label if redo else None,
        "message": message,
    }


def change_note(before: dict, after: dict) -> str:
    notes = []
    rows = after["rows"] - before["rows"]
    if rows:
        notes.append(f"{_plural(abs(rows), 'row')} {'removed' if rows < 0 else 'added'}")
    # Dropped rows take their gaps with them, which isn't "filling" anything.
    gaps = 0 if rows else after["missing_cells"] - before["missing_cells"]
    if gaps > 0:
        notes.append(f"{_plural(gaps, 'value')} became missing")
    elif gaps < 0:
        notes.append(f"{_plural(-gaps, 'gap')} filled")
    return " · ".join(notes)


class DataPatchAPI:
    def __init__(
        self,
        sample_file: str | Path,
        store: WorkspaceStore | None = None,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
    ) -> None:
        self.sample_file = Path(sample_file)
        self.store = store or WorkspaceStore()
        self.max_upload_bytes = max_upload_bytes
        workspace = r"/api/workspaces/(?P<workspace_id>[A-Za-z0-9]+)"
        self._routes: list[tuple[str, re.Pattern, Callable[..., Reply]]] = [
            ("GET", re.compile(r"/api/health"), self.health),
            ("POST", re.compile(r"/api/upload"), self.upload),
            ("POST", re.compile(r"/api/sample"), self.sample),
            ("GET", re.compile(workspace), self.get_state),
            ("POST", re.compile(workspace + "/steps"), self.add_step),
            ("POST", re.compile(workspace + "/undo"), self.undo),
            ("POST", re.compile(workspace + "/redo"), self.redo),
            ("POST", re.compile(workspace + "/reset"), self.reset),
            ("GET", re.compile(workspace + "/export"), self.export),
            ("GET", re.compile(workspace + "/recipe"), self.download_recipe),
            ("POST", re.compile(workspace + "/recipe"), self.run_recipe),
        ]

    # -- dispatching --------------------------------------------------------

    def dispatch(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: Any = None,
        upload: tuple[str, bytes] | None = None,
    ) -> Reply:
        """Route one request. ``body`` is parsed JSON; ``upload`` is ``(filename, bytes)``."""
        method = method.upper()
        allowed = False
        for route_method, pattern, handler in self._routes:
            match = pattern.fullmatch(path)
            if not match:
                continue
            allowed = True
            if route_method != method:
                continue
            context = {"query": query or {}, "body": body, "upload": upload}
            try:
                return handler(**match.groupdict(), **context)
            except ApiError as exc:
                return Reply(exc.status, {"error": str(exc)})
            except CleaningError as exc:
                return Reply(400, {"error": str(exc)})
            except WorkspaceNotFound:
                return Reply(404, {"error": "That session has expired. Open the file again."})
            except Exception:
                log.exception("Unhandled error in %s %s", method, path)
                return Reply(500, {"error": "Something went wrong. Check the console or terminal for details."})
        if allowed:
            return Reply(405, {"error": "That method isn't allowed here."})
        return Reply(404, {"error": "Not found."})

    def handle_json(self, request_json: str, upload_path: str | None = None) -> str:
        """JSON in, JSON out. Used by the in-browser worker.

        ``request_json`` holds ``method``, ``path``, ``query``, ``body`` and, for
        uploads, ``filename``; the file itself is read from ``upload_path``.
        Downloads come back base64-encoded.
        """
        request = json.loads(request_json)
        upload = None
        if upload_path:
            upload = (request.get("filename") or "data.csv", Path(upload_path).read_bytes())
        reply = self.dispatch(request["method"], request["path"], request.get("query"), request.get("body"), upload)
        result: dict[str, Any] = {"status": reply.status, "body": reply.body}
        if reply.download is not None:
            result["download"] = {
                "filename": reply.download.filename,
                "mimetype": reply.download.mimetype,
                "base64": base64.b64encode(reply.download.content).decode("ascii"),
            }
        return json.dumps(result, ensure_ascii=False, allow_nan=False)

    # -- endpoints ----------------------------------------------------------

    def _state(self, workspace: Workspace, query: dict, message: str | None = None, status: int = 200) -> Reply:
        return Reply(status, build_state(workspace, query, message))

    def health(self, **_) -> Reply:
        return Reply(200, {"status": "ok", "workspaces": len(self.store)})

    def upload(self, query: dict, upload: tuple[str, bytes] | None, **_) -> Reply:
        if upload is None or not upload[0]:
            raise CleaningError("Choose a CSV file to upload.")
        filename = Path(upload[0].replace("\\", "/")).name
        if not filename.lower().endswith(ALLOWED_EXTENSIONS):
            raise CleaningError("DataPatch only opens .csv, .tsv or .txt files.")
        if len(upload[1]) > self.max_upload_bytes:
            limit = self.max_upload_bytes // (1024 * 1024)
            raise ApiError(413, f"That file is too big. The limit is {limit} MB.")
        workspace = self.store.create(read_csv(upload[1]), filename)
        return self._state(workspace, query, f"Opened {filename}", status=201)

    def sample(self, query: dict, **_) -> Reply:
        workspace = self.store.create(read_csv(self.sample_file.read_bytes()), self.sample_file.name)
        return self._state(workspace, query, "Loaded the messy sample dataset", status=201)

    def get_state(self, workspace_id: str, query: dict, **_) -> Reply:
        return self._state(self.store.get(workspace_id), query)

    def add_step(self, workspace_id: str, query: dict, body: Any, **_) -> Reply:
        workspace = self.store.get(workspace_id)
        if not isinstance(body, dict) or not isinstance(body.get("op"), str):
            raise CleaningError('Send a JSON body like {"op": ..., "params": {...}}.')
        before = workspace.current_profile["summary"]
        step = workspace.apply(body["op"], body.get("params") or {})
        note = change_note(before, workspace.current_profile["summary"])
        return self._state(workspace, query, step.label + (f" ({note})" if note else ""))

    def undo(self, workspace_id: str, query: dict, **_) -> Reply:
        workspace = self.store.get(workspace_id)
        return self._state(workspace, query, f"Undid: {workspace.undo().label}")

    def redo(self, workspace_id: str, query: dict, **_) -> Reply:
        workspace = self.store.get(workspace_id)
        return self._state(workspace, query, f"Redid: {workspace.redo().label}")

    def reset(self, workspace_id: str, query: dict, **_) -> Reply:
        workspace = self.store.get(workspace_id)
        count = workspace.reset()
        return self._state(workspace, query, f"Back to the original ({_plural(count, 'step')} undone)")

    def export(self, workspace_id: str, **_) -> Reply:
        workspace = self.store.get(workspace_id)
        stem = Path(workspace.filename).stem or "data"
        return Reply(200, download=Download(f"{stem}_cleaned.csv", to_csv_bytes(workspace.current), "text/csv"))

    def download_recipe(self, workspace_id: str, **_) -> Reply:
        workspace = self.store.get(workspace_id)
        if not workspace.steps:
            raise CleaningError("There are no steps to save yet.")
        recipe = make_recipe(workspace.steps, source=workspace.filename)
        content = json.dumps(recipe, indent=2, ensure_ascii=False).encode("utf-8")
        stem = Path(workspace.filename).stem or "data"
        return Reply(200, download=Download(f"{stem}_recipe.json", content, "application/json"))

    def run_recipe(self, workspace_id: str, query: dict, body: Any, **_) -> Reply:
        workspace = self.store.get(workspace_id)
        if body is None:
            raise CleaningError("Send the recipe as JSON.")
        report = workspace.apply_recipe(body)
        return self._state(workspace, query, report.summary() + " from the recipe")
