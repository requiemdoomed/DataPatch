// Chooses where DataPatch's Python code runs.
//
// * ServerEngine: `python app.py` is serving this page, so requests go to Flask.
// * BrowserEngine: there's no server (GitHub Pages or any static host), so the
//   same Python package runs in a Web Worker with Pyodide.
//
// Both expose the same two calls, request() and download(), so app.js doesn't
// care which one it's talking to. Add ?engine=browser to the URL to force the
// in-browser engine while running the Flask server.

export const PYODIDE_VERSION = "314.0.7";
const PYODIDE_CDN = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;

class EngineError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function queryString(query = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

function filenameFrom(disposition, fallback) {
  if (!disposition) return fallback;
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (encoded) return decodeURIComponent(encoded[1]);
  const plain = disposition.match(/filename="?([^";]+)"?/i);
  return plain ? plain[1] : fallback;
}

class ServerEngine {
  constructor() {
    this.kind = "server";
    this.label = "Running on the local DataPatch server";
    this.persistent = true;
    this.ready = Promise.resolve();
  }

  async #fetch(method, path, { query, body, file } = {}) {
    const init = { method, headers: { Accept: "application/json" } };
    if (file) {
      const form = new FormData();
      form.append("file", file);
      init.body = form;
    } else if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    try {
      // Paths are relative so the page also works when served from a sub-path.
      return await fetch(path.replace(/^\//, "") + queryString(query), init);
    } catch {
      throw new EngineError("Can't reach the DataPatch server. Is it still running?", 0);
    }
  }

  async #fail(response) {
    let data = null;
    try {
      data = await response.json();
    } catch {
      // not JSON
    }
    return new EngineError(data?.error || `Request failed (${response.status})`, response.status);
  }

  async request(method, path, options) {
    const response = await this.#fetch(method, path, options);
    if (!response.ok) throw await this.#fail(response);
    return response.json();
  }

  async download(path) {
    const response = await this.#fetch("GET", path);
    if (!response.ok) throw await this.#fail(response);
    const filename = filenameFrom(response.headers.get("Content-Disposition"), "download");
    return { filename, blob: await response.blob() };
  }
}

class BrowserEngine {
  constructor(pyodideUrl, onProgress) {
    this.kind = "browser";
    this.label = "Starting the Python engine in your browser…";
    // Sessions live in the worker's memory, so they end when the page reloads.
    this.persistent = false;
    this.nextId = 1;
    this.pending = new Map();
    this.worker = new Worker(new URL("./engine-worker.js", import.meta.url), { type: "module" });

    this.ready = new Promise((resolve, reject) => {
      this.worker.onmessage = ({ data }) => {
        if (data.type === "progress") onProgress?.(data.text);
        else if (data.type === "ready") {
          this.label = `Running in your browser · ${data.version}`;
          resolve();
        } else if (data.type === "failed") reject(new EngineError(data.error, 503));
        else if (data.type === "response") this.#settle(data);
      };
      this.worker.onerror = (event) => reject(new EngineError(event.message || "The Python engine crashed.", 503));
    });
    this.ready.catch(() => {}); // callers see the error when they await it

    this.worker.postMessage({ type: "boot", baseUrl: document.baseURI, pyodideUrl });
  }

  #settle({ id, result }) {
    const pending = this.pending.get(id);
    if (!pending) return;
    this.pending.delete(id);
    if (result.status >= 400) pending.reject(new EngineError(result.body?.error || "Request failed", result.status));
    else pending.resolve(result);
  }

  async #call(method, path, { query, body, file } = {}) {
    await this.ready;
    const id = this.nextId++;
    const request = { method, path, query, body };
    const message = { type: "request", id, request };
    const transfer = [];
    if (file) {
      request.filename = file.name;
      message.file = await file.arrayBuffer();
      transfer.push(message.file);
    }
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.worker.postMessage(message, transfer);
    });
  }

  async request(method, path, options) {
    return (await this.#call(method, path, options)).body;
  }

  async download(path) {
    const { download } = await this.#call("GET", path);
    const bytes = Uint8Array.from(atob(download.base64), (c) => c.charCodeAt(0));
    return { filename: download.filename, blob: new Blob([bytes], { type: download.mimetype }) };
  }
}

// ?pyodide=path/to/pyodide/ lets a deployment serve its own copy of Pyodide.
// Only same-site relative paths are accepted, so a link can't make the page run
// code from somewhere else.
export function pyodideUrl(params, base = document.baseURI) {
  const custom = params.get("pyodide");
  if (!custom) return PYODIDE_CDN;
  let url;
  try {
    url = new URL(custom, base);
  } catch {
    return PYODIDE_CDN;
  }
  // Compare origins after parsing: "https:", "//host" and "\\host" all resolve elsewhere.
  if (url.origin !== new URL(base).origin) return PYODIDE_CDN;
  const href = url.href.split(/[?#]/)[0];
  return href.endsWith("/") ? href : `${href}/`;
}

// The Flask server marks the page it serves; static hosts such as GitHub Pages
// serve it unchanged, which selects the in-browser engine.
function servedByFlask() {
  return document.querySelector('meta[name="datapatch-engine"]')?.content === "server";
}

export async function createEngine({ onProgress } = {}) {
  const params = new URLSearchParams(location.search);
  if (params.get("engine") !== "browser" && servedByFlask()) return new ServerEngine();
  return new BrowserEngine(pyodideUrl(params), onProgress);
}
