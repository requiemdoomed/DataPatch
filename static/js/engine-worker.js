// Runs the DataPatch Python package inside the browser with Pyodide.
//
// The page (static/js/engine.js) sends API requests here instead of to a
// server. This worker downloads Pyodide and pandas, copies the package's .py
// files from the site into Pyodide's virtual file system and hands every
// request to DataPatchAPI.handle_json, the same code the Flask server calls.

// Every module datapatch.api imports. server.py and __main__.py aren't needed here.
const PACKAGE_FILES = ["__init__.py", "cleaning.py", "files.py", "profiling.py", "recipe.py", "workspace.py", "api.py"];
const SAMPLE_FILE = "messy_customers.csv";
const APP_DIR = "/home/pyodide/app";

let pyodide = null;
let handleJson = null;
let booting = null;

function post(message) {
  self.postMessage(message);
}

function progress(text) {
  post({ type: "progress", text });
}

async function download(url, kind) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Couldn't download ${kind} from ${url} (${response.status})`);
  return kind === "text" ? response.text() : new Uint8Array(await response.arrayBuffer());
}

async function boot({ baseUrl, pyodideUrl }) {
  progress("Downloading the Python runtime…");
  // Pyodide only runs in module workers, so it's loaded with a dynamic import.
  const { loadPyodide } = await import(`${pyodideUrl}pyodide.mjs`);
  pyodide = await loadPyodide({ indexURL: pyodideUrl });

  progress("Loading pandas…");
  await pyodide.loadPackage(["pandas"], { messageCallback: () => {} });

  progress("Loading DataPatch…");
  const fs = pyodide.FS;
  fs.mkdirTree(`${APP_DIR}/datapatch`);
  fs.mkdirTree(`${APP_DIR}/sample_data`);
  const sources = await Promise.all(
    PACKAGE_FILES.map((name) => download(new URL(`datapatch/${name}`, baseUrl), "text")),
  );
  PACKAGE_FILES.forEach((name, i) => fs.writeFile(`${APP_DIR}/datapatch/${name}`, sources[i]));
  fs.writeFile(
    `${APP_DIR}/sample_data/${SAMPLE_FILE}`,
    await download(new URL(`sample_data/${SAMPLE_FILE}`, baseUrl), "bytes"),
  );

  pyodide.runPython(`
import sys
sys.path.insert(0, "${APP_DIR}")
from datapatch.api import DataPatchAPI
_datapatch_api = DataPatchAPI("${APP_DIR}/sample_data/${SAMPLE_FILE}")
`);
  handleJson = pyodide.globals.get("_datapatch_api").handle_json;

  return pyodide.runPython(`
import sys, pandas
from datapatch import __version__
f"DataPatch {__version__} · Python {sys.version.split()[0]} · pandas {pandas.__version__}"
`);
}

function handle({ id, request, file }) {
  let uploadPath = null;
  try {
    if (file) {
      uploadPath = "/tmp/datapatch-upload";
      pyodide.FS.writeFile(uploadPath, new Uint8Array(file));
    }
    const result = JSON.parse(handleJson(JSON.stringify(request), uploadPath));
    post({ type: "response", id, result });
  } catch (error) {
    console.error(error);
    post({ type: "response", id, result: { status: 500, body: { error: `The Python engine failed: ${error.message}` } } });
  } finally {
    if (uploadPath) {
      try {
        pyodide.FS.unlink(uploadPath);
      } catch {
        // already gone
      }
    }
  }
}

self.onmessage = async (event) => {
  const message = event.data;
  if (message.type === "boot") {
    booting = boot(message);
    try {
      post({ type: "ready", version: await booting });
    } catch (error) {
      console.error(error);
      post({ type: "failed", error: error.message || String(error) });
    }
    return;
  }
  if (message.type === "request") {
    try {
      await booting;
    } catch {
      post({ type: "response", id: message.id, result: { status: 503, body: { error: "The Python engine didn't start." } } });
      return;
    }
    handle(message);
  }
};
