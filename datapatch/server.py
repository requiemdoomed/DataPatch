"""Flask wrapper around :mod:`datapatch.api` for running DataPatch locally."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from .api import MAX_UPLOAD_BYTES, DataPatchAPI

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = ROOT / "datapatch"
STATIC_DIR = ROOT / "static"
SAMPLE_DIR = ROOT / "sample_data"
SAMPLE_FILE = SAMPLE_DIR / "messy_customers.csv"


def create_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    # A little headroom over the upload limit for the multipart envelope; the API
    # itself rejects files over MAX_UPLOAD_BYTES with a friendly message.
    app.config.update(MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES + 64 * 1024, SAMPLE_FILE=str(SAMPLE_FILE))
    if config:
        app.config.update(config)

    api = DataPatchAPI(app.config["SAMPLE_FILE"])
    app.extensions["datapatch_api"] = api

    @app.get("/")
    def index():
        # Tell the page a server is available so it sends requests here instead of
        # starting the in-browser engine.
        page = (ROOT / "index.html").read_text(encoding="utf-8")
        page = page.replace('name="datapatch-engine" content="browser"', 'name="datapatch-engine" content="server"')
        return Response(page, mimetype="text/html")

    # The page can also run the engine in the browser (?engine=browser), the way the
    # GitHub Pages build does. These two routes serve what that mode downloads.
    @app.get("/datapatch/<name>")
    def python_source(name: str):
        if not name.endswith(".py") or not (PACKAGE_DIR / name).is_file():
            abort(404)
        return send_from_directory(PACKAGE_DIR, name, mimetype="text/plain")

    @app.get("/sample_data/<name>")
    def sample_data(name: str):
        if not name.endswith(".csv") or not (SAMPLE_DIR / name).is_file():
            abort(404)
        return send_from_directory(SAMPLE_DIR, name, mimetype="text/csv")

    @app.route("/api/<path:rest>", methods=["GET", "POST"])
    def api_route(rest: str):
        upload = None
        sent = request.files.get("file")
        if sent is not None:
            upload = (sent.filename or "", sent.read())
        body = request.get_json(silent=True)
        reply = api.dispatch(request.method, "/api/" + rest, request.args.to_dict(), body, upload)
        if reply.download is not None:
            return send_file(
                io.BytesIO(reply.download.content),
                mimetype=reply.download.mimetype,
                as_attachment=True,
                download_name=reply.download.filename,
            )
        return jsonify(reply.body), reply.status

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        if exc.code == 413:
            return jsonify(error="That file is too big. The limit is 25 MB."), 413
        if request.path.startswith("/api/"):
            return jsonify(error=exc.description or exc.name), exc.code
        return exc

    return app
