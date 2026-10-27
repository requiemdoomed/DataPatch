// Run with: node --test tests/js/*.test.mjs
// The ?pyodide= option must never load Pyodide from another site.
import assert from "node:assert/strict";
import test from "node:test";

globalThis.document = { baseURI: "https://me.github.io/datapatch/" };
const { pyodideUrl, PYODIDE_VERSION } = await import("../../static/js/engine.js");

const BASE = "https://me.github.io/datapatch/";
const CDN = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
const resolve = (value) => pyodideUrl(new URLSearchParams(value === null ? {} : { pyodide: value }), BASE);

test("defaults to the pinned jsDelivr release", () => {
  assert.equal(resolve(null), CDN);
  assert.equal(resolve(""), CDN);
});

test("accepts paths on the same site", () => {
  assert.equal(resolve("pyodide/"), `${BASE}pyodide/`);
  assert.equal(resolve("vendor/py"), `${BASE}vendor/py/`);
  assert.equal(resolve("/datapatch/pyodide/"), `${BASE}pyodide/`);
  assert.equal(resolve("pyodide/?x=1#y"), `${BASE}pyodide/`);
});

test("rejects anything that resolves to another origin", () => {
  for (const hostile of [
    "https://evil.com/",
    "//evil.com/",
    "\\\\evil.com/",
    "/\\evil.com/",
    "\\/evil.com/",
    " https://evil.com",
    "javascript:alert(1)",
    "data:text/javascript,x",
    "http://me.github.io/datapatch/pyodide/",
  ]) {
    assert.equal(resolve(hostile), CDN, hostile);
  }
});
