// DataPatch front end. The Python side (datapatch/api.py) runs either on the
// local Flask server or inside the browser with Pyodide; see engine.js.

import { createEngine } from "./engine.js";

const PAGE_SIZE = 100;
const STORAGE_KEY = "datapatch:workspace";
const PLACEHOLDERS = new Set([
  "", "na", "n/a", "n.a.", "#n/a", "nan", "null", "none", "nil",
  "-", "--", "?", "missing", "unknown", "not available",
]);

const state = {
  ws: null,
  view: "cleaned",
  offset: 0,
  busy: false,
  focus: null,
  opName: "trim_whitespace",
  showChanges: true,
};

const $ = (selector, root = document) => root.querySelector(selector);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "className") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key in node && typeof value !== "string") node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

const fmt = (n) => (typeof n === "number" ? n.toLocaleString() : String(n));

// ---------------------------------------------------------------------------
// Operations shown in the Transform panel. The server validates everything,
// this only describes how to build the form.
// ---------------------------------------------------------------------------

const TEXT = ["text"];
const NUMERIC = ["number", "integer"];

const OPERATIONS = [
  {
    op: "trim_whitespace",
    label: "Trim whitespace",
    help: "Strips spaces from both ends and squeezes double spaces. Cells left empty become missing.",
    fields: [{ name: "columns", type: "columns", kinds: TEXT, optional: true, hint: "Leave empty to trim every text column." }],
  },
  {
    op: "standardize_categories",
    label: "Standardize categories",
    help: "Merges spellings that only differ by case or spacing, or applies your own mapping.",
    fields: [
      { name: "column", type: "column", kinds: TEXT },
      { name: "mode", type: "select", label: "How", options: [["auto", "Merge case/spacing variants automatically"], ["mapping", "Use my mapping"]] },
      {
        name: "mapping",
        type: "textarea",
        label: "Mapping (one per line)",
        placeholder: "Torontoo => Toronto\nBrampon => Brampton",
        hint: "Leave the right side empty to make a value missing.",
        showIf: (v) => v.mode === "mapping",
        parse: parseMapping,
      },
    ],
  },
  {
    op: "change_case",
    label: "Change case",
    help: "Rewrites text in the chosen case.",
    fields: [
      { name: "columns", type: "columns", kinds: TEXT },
      { name: "mode", type: "select", label: "Case", options: [["title", "Title Case"], ["lower", "lowercase"], ["upper", "UPPERCASE"], ["sentence", "Sentence case"]] },
    ],
  },
  {
    op: "mark_missing",
    label: "Mark placeholders as missing",
    help: "Turns stand-ins like N/A, -, unknown or a sentinel such as -999 into real missing values.",
    fields: [
      { name: "columns", type: "columns", optional: true, hint: "Leave empty to check every column." },
      {
        name: "markers",
        type: "text",
        label: "Placeholder values",
        placeholder: "N/A, -, unknown, -999",
        hint: "Comma separated. Leave empty for the common ones.",
        parse: (v) => (v.trim() ? v.split(",").map((s) => s.trim()) : null),
      },
    ],
  },
  {
    op: "fill_missing",
    label: "Handle missing values",
    help: "Fills gaps (placeholders included) or drops the rows that have them.",
    fields: [
      { name: "columns", type: "columns" },
      {
        name: "strategy",
        type: "select",
        label: "Strategy",
        options: [
          ["mode", "Most common value"],
          ["median", "Median (numbers)"],
          ["mean", "Mean (numbers)"],
          ["constant", "A fixed value"],
          ["ffill", "Copy the previous row"],
          ["bfill", "Copy the next row"],
          ["drop_rows", "Drop rows with gaps"],
        ],
      },
      { name: "value", type: "text", label: "Fill value", showIf: (v) => v.strategy === "constant" },
    ],
  },
  {
    op: "remove_duplicates",
    label: "Remove duplicate rows",
    help: "Drops repeated rows. Pick columns to treat rows as duplicates when only those match.",
    fields: [
      { name: "columns", type: "columns", optional: true, label: "Compare on", hint: "Leave empty to compare whole rows." },
      { name: "keep", type: "select", label: "Keep", options: [["first", "First occurrence"], ["last", "Last occurrence"], ["none", "None of them"]] },
    ],
  },
  {
    op: "convert_type",
    label: "Convert type",
    help: "Changes the column type. Values that can't be converted become missing, so check the score afterwards.",
    fields: [
      { name: "columns", type: "columns" },
      {
        name: "target",
        type: "select",
        label: "Convert to",
        options: [["number", "Number"], ["integer", "Whole number"], ["date", "Date"], ["boolean", "True / false"], ["text", "Text"]],
      },
      { name: "dayfirst", type: "checkbox", label: "Dates are day-first (31/12/2024)", showIf: (v) => v.target === "date" },
    ],
  },
  {
    op: "replace_values",
    label: "Replace a value",
    help: "Swaps one exact value for another. Leave the replacement empty to make it missing.",
    fields: [
      { name: "columns", type: "columns" },
      { name: "find", type: "text", label: "Find", placeholder: "Torontoo" },
      { name: "replace", type: "text", label: "Replace with", placeholder: "Toronto" },
    ],
  },
  {
    op: "handle_outliers",
    label: "Handle outliers",
    help: "Finds values outside the interquartile-range fences.",
    fields: [
      { name: "column", type: "column", kinds: NUMERIC },
      { name: "method", type: "select", label: "Do what", options: [["clip", "Clip to the fence"], ["missing", "Make them missing"], ["remove", "Drop those rows"]] },
      { name: "factor", type: "number", label: "IQR factor", value: "1.5", step: "0.1", min: "0.1", parse: Number },
    ],
  },
  {
    op: "rename_column",
    label: "Rename column",
    help: "Gives a column a new name.",
    fields: [
      { name: "column", type: "column" },
      { name: "new_name", type: "text", label: "New name" },
    ],
  },
  {
    op: "drop_columns",
    label: "Drop columns",
    help: "Removes columns you don't need.",
    fields: [{ name: "columns", type: "columns" }],
  },
];

function parseMapping(text) {
  const mapping = {};
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const at = line.indexOf("=>");
    if (at === -1) throw new Error(`Mapping lines need "=>": ${line.trim()}`);
    const from = line.slice(0, at).trim();
    if (from) mapping[from] = line.slice(at + 2).trim();
  }
  return mapping;
}

// ---------------------------------------------------------------------------
// Talking to the Python engine (a local server, or Pyodide in the browser)
// ---------------------------------------------------------------------------

let engine = null;

function api(method, path, options) {
  if (!engine) return Promise.reject(new Error("DataPatch is still starting."));
  return engine.request(method, path, options);
}

function query() {
  return { view: state.view, offset: state.offset, limit: PAGE_SIZE };
}

function workspacePath(suffix = "") {
  return `/api/workspaces/${state.ws.id}${suffix}`;
}

async function run(task) {
  if (state.busy) return;
  setBusy(true);
  try {
    await task();
  } catch (error) {
    if (error.status === 404 && state.ws) forgetWorkspace();
    toast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

function setBusy(busy) {
  state.busy = busy;
  document.body.classList.toggle("busy", busy);
  for (const button of document.querySelectorAll("#applyBtn, .fix-btn, #undoBtn, #redoBtn, #resetBtn")) {
    if (busy) button.disabled = true;
  }
  if (!busy && state.ws) renderControls();
}

function receive(data) {
  state.ws = data;
  state.view = data.view;
  state.offset = data.page.offset;
  if (engine?.persistent) {
    try {
      sessionStorage.setItem(STORAGE_KEY, data.id);
    } catch {
      // Storage can be blocked; the session just won't survive a reload.
    }
  }
  if (state.focus && !data.columns.some((c) => c.name === state.focus)) state.focus = null;
  render();
  if (data.message) toast(data.message);
}

function forgetWorkspace() {
  state.ws = null;
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
  showWelcome();
}

function saveFile({ filename, blob }) {
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: filename, hidden: true });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

function downloadFrom(suffix) {
  if (!state.ws) return;
  run(async () => saveFile(await engine.download(workspacePath(suffix))));
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

const MAX_UPLOAD_MB = 25;

function openFile(file) {
  if (!file) return;
  if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
    toast(`That file is too big. The limit is ${MAX_UPLOAD_MB} MB.`, "error");
    return;
  }
  run(async () => {
    state.view = "cleaned";
    state.offset = 0;
    state.focus = null;
    receive(await api("POST", "/api/upload", { query: query(), file }));
  });
}

function openSample() {
  run(async () => {
    state.view = "cleaned";
    state.offset = 0;
    state.focus = null;
    receive(await api("POST", "/api/sample", { query: query() }));
  });
}

function applyStep(op, params) {
  run(async () => {
    state.view = "cleaned";
    receive(await api("POST", workspacePath("/steps"), { query: query(), body: { op, params } }));
  });
}

async function applyRecipeFile(file) {
  if (!file || !state.ws) return;
  let recipe;
  try {
    recipe = JSON.parse(await file.text());
  } catch {
    toast("That file isn't valid JSON, so it can't be a recipe.", "error");
    return;
  }
  run(async () => {
    state.view = "cleaned";
    receive(await api("POST", workspacePath("/recipe"), { query: query(), body: recipe }));
  });
}

function historyAction(action) {
  run(async () => {
    state.view = "cleaned";
    receive(await api("POST", workspacePath(`/${action}`), { query: query() }));
  });
}

function reload() {
  run(async () => {
    const data = await api("GET", workspacePath(), { query: query() });
    data.message = null;
    receive(data);
  });
}

function focusColumn(name) {
  state.focus = state.focus === name ? null : name;
  renderProfiles();
  renderTable();
  renderOpFields({ preferFocus: true });
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function showWelcome() {
  $("#welcome").hidden = false;
  $("#workspace").hidden = true;
  $("#fileChip").hidden = true;
  $("#exportBtn").hidden = true;
  $("#newFileBtn").hidden = true;
}

function render() {
  const ws = state.ws;
  $("#welcome").hidden = true;
  $("#workspace").hidden = false;
  $("#newFileBtn").hidden = false;

  const chip = $("#fileChip");
  chip.hidden = false;
  chip.textContent = ws.filename;
  chip.title = ws.filename;

  const exportBtn = $("#exportBtn");
  exportBtn.hidden = false;
  exportBtn.disabled = false;

  renderScores();
  renderChangeSummary();
  renderProfiles();
  renderTable();
  renderHistory();
  renderOpFields();
  renderControls();
}

function scoreColor(score) {
  if (score >= 90) return "var(--good)";
  if (score >= 70) return "var(--warn)";
  return "var(--bad)";
}

function ring(label, score) {
  const radius = 38;
  const circumference = 2 * Math.PI * radius;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 92 92");
  svg.setAttribute("aria-hidden", "true");
  for (const cls of ["track", "value"]) {
    const circle = document.createElementNS(ns, "circle");
    circle.setAttribute("cx", "46");
    circle.setAttribute("cy", "46");
    circle.setAttribute("r", String(radius));
    circle.setAttribute("class", cls);
    if (cls === "value") {
      circle.style.stroke = scoreColor(score);
      circle.style.strokeDasharray = String(circumference);
      circle.style.strokeDashoffset = String(circumference * (1 - score / 100));
    }
    svg.append(circle);
  }
  return [
    el("span", { className: "score-label" }, label),
    el("div", { className: "ring", role: "img", "aria-label": `${label} quality score ${score} out of 100` }, svg, el("span", { className: "ring-text" }, score.toFixed(1))),
  ];
}

function renderScores() {
  const { original, cleaned } = state.ws.scores;
  $("#scoreOriginal").replaceChildren(...ring("Original", original.score));
  $("#scoreCleaned").replaceChildren(...ring("Cleaned", cleaned.score));

  const delta = Math.round((cleaned.score - original.score) * 10) / 10;
  const deltaEl = $("#scoreDelta");
  deltaEl.className = `score-delta ${delta > 0 ? "up" : delta < 0 ? "down" : ""}`;
  deltaEl.textContent = delta > 0 ? `+${delta.toFixed(1)}` : delta < 0 ? delta.toFixed(1) : "±0";
  deltaEl.title = "Change in quality score";

  const names = { completeness: "Completeness", uniqueness: "Uniqueness", consistency: "Consistency", validity: "Validity" };
  $("#components").replaceChildren(
    ...Object.entries(names).map(([key, label]) => {
      const before = original.components[key];
      const after = cleaned.components[key];
      return el(
        "div",
        { className: "component" },
        el("span", { className: "component-name" }, label),
        el(
          "div",
          { className: "bars" },
          el("div", { className: "bar before", title: `Original: ${before}%` }, el("span", { style: `width:${before}%` })),
          el("div", { className: "bar after", title: `Cleaned: ${after}%` }, el("span", { style: `width:${after}%` })),
        ),
        el("span", { className: "component-values" }, `${before} → `, el("b", {}, String(after))),
      );
    }),
  );

  const rows = [
    ["Rows", "rows", 0],
    ["Missing cells", "missing_cells", -1],
    ["Duplicate rows", "duplicate_rows", -1],
    ["Inconsistent text cells", "inconsistent_cells", -1],
    ["Columns with type problems", "type_issues", -1],
    ["Open issues", "issue_count", -1],
  ];
  const table = $("#metrics");
  table.replaceChildren(
    el("thead", {}, el("tr", {}, el("th", {}, "Metric"), el("th", {}, "Original"), el("th", {}, "Cleaned"), el("th", {}, "Change"))),
    el(
      "tbody",
      {},
      rows.map(([label, key, goodDirection]) => {
        const diff = cleaned[key] - original[key];
        let cls = "";
        if (diff && goodDirection) cls = Math.sign(diff) === goodDirection ? "change-good" : "change-bad";
        return el(
          "tr",
          {},
          el("td", {}, label),
          el("td", {}, fmt(original[key])),
          el("td", {}, fmt(cleaned[key])),
          el("td", { className: cls }, diff === 0 ? "–" : `${diff > 0 ? "+" : ""}${fmt(diff)}`),
        );
      }),
    ),
  );
}

function renderProfiles() {
  const ws = state.ws;
  const editable = ws.view === "cleaned";
  const totalIssues = ws.profiles.reduce((sum, p) => sum + p.issues.length, 0);
  $("#issueCount").textContent = totalIssues ? `${totalIssues} issue${totalIssues === 1 ? "" : "s"}` : "all clear";

  const cards = ws.profiles.map((profile) => {
    const issues = profile.issues;
    const header = el(
      "div",
      { className: "col-head", title: "Click to focus this column", onclick: () => focusColumn(profile.name) },
      el("span", { className: `kind kind-${profile.kind}` }, profile.kind),
      el("h3", { title: profile.name }, profile.name),
      issues.length
        ? el("span", { className: "issue-badge" }, `${issues.length} issue${issues.length === 1 ? "" : "s"}`)
        : el("span", { className: "clean-badge" }, "✓ clean"),
    );

    const stats = [`${profile.missing_pct}% missing`, `${fmt(profile.unique)} unique`];
    const edits = editable ? ws.changes.by_column[profile.name] : 0;
    if (edits) stats.push(`${fmt(edits)} changed`);
    if (profile.summary && profile.summary.min !== undefined) stats.push(`${profile.summary.min} – ${profile.summary.max}`);

    const list = el(
      "ul",
      { className: "issues" },
      issues.map((issue) =>
        el(
          "li",
          { className: `issue issue-${issue.kind}` },
          el("span", { className: "issue-text" }, issue.message),
          issue.fix
            ? el(
                "button",
                {
                  type: "button",
                  className: "fix-btn",
                  disabled: !editable || state.busy,
                  title: editable ? "Apply this fix as a new step" : "Switch to the cleaned view to apply fixes",
                  onclick: () => applyStep(issue.fix.op, issue.fix.params),
                },
                issue.fix.label,
              )
            : null,
          issue.examples.length ? el("span", { className: "issue-examples", title: issue.examples.join("   ") }, issue.examples.join("  ")) : null,
        ),
      ),
    );

    return el(
      "article",
      { className: `col-card${state.focus === profile.name ? " focused" : ""}` },
      header,
      el("div", { className: "missbar", title: `${profile.missing} missing` }, el("span", { style: `width:${profile.missing_pct}%` })),
      el("p", { className: "col-stats" }, stats.join(" · ")),
      issues.length ? list : null,
    );
  });
  $("#profileList").replaceChildren(...cards);
}

function renderCell(td, value, kind) {
  if (value === null) {
    td.className = "cell-missing";
    td.textContent = "missing";
    return;
  }
  if (typeof value !== "string") {
    td.textContent = String(value);
    if (kind === "number" || kind === "integer") td.classList.add("num");
    return;
  }

  td.title = value;
  if (PLACEHOLDERS.has(value.trim().toLowerCase())) td.classList.add("cell-placeholder");

  const tidy = value.replace(/\s+/g, " ").trim();
  if (tidy === value) {
    td.textContent = value;
    return;
  }
  td.classList.add("cell-space");
  const [, lead, middle, trail] = value.match(/^(\s*)([\s\S]*?)(\s*)$/);
  const inner = middle.replace(/\s{2,}/g, (run) => "·".repeat(run.length));
  td.append(
    lead ? el("span", { className: "ws" }, "·".repeat(lead.length)) : "",
    inner,
    trail ? el("span", { className: "ws" }, "·".repeat(trail.length)) : "",
  );
}

function describeValue(value) {
  if (value === null) return "missing";
  return typeof value === "string" ? JSON.stringify(value) : String(value);
}

function renderTable() {
  const ws = state.ws;
  const { columns, rows, page } = ws;

  for (const button of document.querySelectorAll(".segmented button")) {
    button.setAttribute("aria-pressed", String(button.dataset.view === ws.view));
  }

  const first = page.total ? page.offset + 1 : 0;
  const last = Math.min(page.offset + page.limit, page.total);
  $("#gridMeta").textContent = `Rows ${fmt(first)}–${fmt(last)} of ${fmt(page.total)} · ${columns.length} columns`;
  $("#prevPage").disabled = page.offset === 0;
  $("#nextPage").disabled = page.offset + page.limit >= page.total;

  const focusIndex = columns.findIndex((c) => c.name === state.focus);
  const headRow = el("tr", {}, el("th", { className: "rownum", title: "Row number in the original file" }, "row"));
  const highlight = ws.view === "cleaned" && state.showChanges;
  const changed = new Map(highlight ? ws.changed_cells.map(([r, c, was]) => [`${r}:${c}`, was]) : []);
  $("#changesToggle").hidden = ws.view !== "cleaned";
  columns.forEach((column, index) => {
    headRow.append(
      el(
        "th",
        {
          className: index === focusIndex ? "focus-col" : "",
          title: `${column.name} (${column.kind}). Click to focus.`,
          onclick: () => focusColumn(column.name),
        },
        column.name,
        el("span", { className: `kind kind-${column.kind}` }, column.kind),
      ),
    );
  });

  const body = el("tbody");
  rows.forEach((row, rowIndex) => {
    const tr = body.insertRow();
    const rowNumber = ws.row_numbers ? ws.row_numbers[rowIndex] : page.offset + rowIndex + 1;
    tr.append(el("td", { className: "rownum" }, String(rowNumber)));
    row.forEach((value, colIndex) => {
      const td = tr.insertCell();
      renderCell(td, value, columns[colIndex].kind);
      const key = `${rowIndex}:${colIndex}`;
      if (changed.has(key)) {
        td.classList.add("cell-changed");
        td.title = `Was: ${describeValue(changed.get(key))}`;
      }
      if (colIndex === focusIndex) td.classList.add("focus-col");
    });
  });
  if (!rows.length) {
    body.append(el("tr", { className: "empty-row" }, el("td", { colSpan: columns.length + 1 }, "No rows left.")));
  }

  $("#dataTable").replaceChildren(el("thead", {}, headRow), body);
}

function renderChangeSummary() {
  const ws = state.ws;
  const changes = ws.changes;
  const node = $("#changeSummary");
  if (!ws.history.length) {
    node.textContent = "No steps yet. Apply a fix and the cleaned score updates here.";
    return;
  }
  const parts = [];
  const add = (count, text) => parts.push(el("span", {}, el("b", {}, fmt(count)), ` ${text}`));
  add(changes.cells_changed, changes.cells_changed === 1 ? "cell changed" : "cells changed");
  if (changes.rows_removed) add(changes.rows_removed, changes.rows_removed === 1 ? "row removed" : "rows removed");
  const dropped = changes.columns_removed.length;
  if (dropped) add(dropped, dropped === 1 ? "column dropped" : "columns dropped");
  const renamed = Object.keys(changes.columns_renamed).length;
  if (renamed) add(renamed, renamed === 1 ? "column renamed" : "columns renamed");
  node.replaceChildren(...parts.flatMap((part, i) => (i ? [" · ", part] : [part])));
}

function renderHistory() {
  const ws = state.ws;
  const save = $("#saveRecipeBtn");
  save.disabled = !ws.history.length;
  const items = [el("li", { className: "origin" }, `Opened ${ws.filename}`)];
  for (const step of ws.history) items.push(el("li", {}, step.label));
  if (ws.redo_label) items.push(el("li", { className: "redo", title: "Next step Redo will bring back" }, ws.redo_label));
  $("#historyList").replaceChildren(...items);
}

function renderControls() {
  const ws = state.ws;
  $("#undoBtn").disabled = state.busy || !ws.can_undo;
  $("#redoBtn").disabled = state.busy || !ws.can_redo;
  $("#redoBtn").title = ws.redo_label ? `Redo: ${ws.redo_label} (Ctrl+Shift+Z)` : "Redo (Ctrl+Shift+Z)";
  $("#resetBtn").disabled = state.busy || !ws.can_undo;
  $("#applyBtn").disabled = state.busy;
  for (const button of document.querySelectorAll(".fix-btn")) {
    button.disabled = state.busy || ws.view !== "cleaned";
  }
}

// ---------------------------------------------------------------------------
// Transform form
// ---------------------------------------------------------------------------

function currentOperation() {
  return OPERATIONS.find((o) => o.op === state.opName) || OPERATIONS[0];
}

// The form always works on the cleaned data, whatever the preview shows.
function eligible(field) {
  const columns = state.ws ? state.ws.cleaned_columns : [];
  return field.kinds ? columns.filter((c) => field.kinds.includes(c.kind)) : columns;
}

function renderOpFields({ preferFocus = false } = {}) {
  const container = $("#opFields");
  if (!state.ws) return;
  const operation = currentOperation();
  $("#opHelp").textContent = operation.help;

  // Keep whatever the user already typed when we redraw. Clicking a column
  // overrides the column pickers so the focused column is preselected.
  const previous = collectRaw();
  if (preferFocus) {
    for (const field of operation.fields) {
      if (field.type === "columns" || field.type === "column") previous.delete(field.name);
    }
  }

  const nodes = operation.fields.map((field) => {
    const label = field.label || (field.type === "columns" ? "Columns" : "Column");
    const hint = field.hint ? el("small", {}, field.hint) : null;
    const wrapper = { dataset: { field: field.name } };

    if (field.type === "columns") {
      const options = eligible(field);
      const chosen = previous.has(field.name)
        ? new Set(previous.get(field.name))
        : new Set(state.focus && options.some((c) => c.name === state.focus) ? [state.focus] : []);
      const picker = el(
        "div",
        { className: "col-picker" },
        options.length
          ? options.map((column) =>
              el(
                "label",
                {},
                el("input", { type: "checkbox", value: column.name, checked: chosen.has(column.name) }),
                column.name,
                el("span", { className: `kind kind-${column.kind}` }, column.kind),
              ),
            )
          : el("div", { className: "picker-empty" }, "No suitable columns."),
      );
      const setAll = (checked) => picker.querySelectorAll("input").forEach((box) => (box.checked = checked));
      return el(
        "fieldset",
        { className: "field", ...wrapper },
        el("legend", {}, label + (field.optional ? " (optional)" : "")),
        el(
          "div",
          { className: "picker-links" },
          el("button", { type: "button", onclick: () => setAll(true) }, "Select all"),
          el("button", { type: "button", onclick: () => setAll(false) }, "Clear"),
        ),
        picker,
        hint,
      );
    }

    let control;
    if (field.type === "column") {
      const options = eligible(field);
      const current = previous.get(field.name) ?? (options.some((c) => c.name === state.focus) ? state.focus : options[0]?.name);
      control = el(
        "select",
        { name: field.name },
        options.length
          ? options.map((c) => el("option", { value: c.name, selected: c.name === current }, `${c.name} (${c.kind})`))
          : el("option", { value: "" }, "No suitable columns"),
      );
      if (options.length && options.some((c) => c.name === current)) control.value = current;
    } else if (field.type === "select") {
      control = el("select", { name: field.name }, field.options.map(([value, text]) => el("option", { value }, text)));
      if (previous.has(field.name)) control.value = previous.get(field.name);
      control.addEventListener("change", updateVisibility);
    } else if (field.type === "textarea") {
      control = el("textarea", { name: field.name, placeholder: field.placeholder || "", spellcheck: false });
      control.value = previous.get(field.name) ?? "";
    } else if (field.type === "checkbox") {
      const box = el("input", { type: "checkbox", name: field.name, checked: Boolean(previous.get(field.name)) });
      return el("label", { className: "checkline", ...wrapper }, box, field.label);
    } else {
      control = el("input", {
        type: field.type === "number" ? "number" : "text",
        name: field.name,
        placeholder: field.placeholder || "",
        step: field.step,
        min: field.min,
        autocomplete: "off",
      });
      control.value = previous.get(field.name) ?? field.value ?? "";
    }
    return el("label", { className: "field", ...wrapper }, el("span", {}, label), control, hint);
  });

  container.replaceChildren(...nodes);
  updateVisibility();
}

function collectRaw() {
  const raw = new Map();
  for (const node of document.querySelectorAll("#opFields [data-field]")) {
    const name = node.dataset.field;
    if (node.tagName === "FIELDSET") {
      raw.set(name, [...node.querySelectorAll("input:checked")].map((box) => box.value));
    } else {
      const input = node.querySelector("input, select, textarea");
      if (!input) continue;
      raw.set(name, input.type === "checkbox" ? input.checked : input.value);
    }
  }
  return raw;
}

function updateVisibility() {
  const values = Object.fromEntries(collectRaw());
  for (const field of currentOperation().fields) {
    const node = document.querySelector(`#opFields [data-field="${field.name}"]`);
    if (node && field.showIf) node.hidden = !field.showIf(values);
  }
}

function buildParams() {
  const operation = currentOperation();
  const raw = collectRaw();
  const values = Object.fromEntries(raw);
  const params = {};

  for (const field of operation.fields) {
    if (field.showIf && !field.showIf(values)) continue;
    let value = raw.get(field.name);

    if (field.type === "columns") {
      if (!value.length) {
        if (!field.optional) throw new Error("Pick at least one column.");
        value = null;
      }
    } else if (field.type === "column" && !value) {
      throw new Error("Pick a column.");
    } else if (field.parse && typeof value === "string") {
      value = field.parse(value);
    }

    if (value !== null && value !== undefined) params[field.name] = value;
  }
  return params;
}

// ---------------------------------------------------------------------------
// Toasts, drag and drop, keyboard
// ---------------------------------------------------------------------------

function toast(message, kind = "info") {
  const node = el("div", { className: `toast ${kind}`, role: kind === "error" ? "alert" : "status" }, message);
  const stack = $("#toasts");
  stack.append(node);
  while (stack.children.length > 3) stack.firstElementChild.remove();
  setTimeout(() => node.remove(), kind === "error" ? 6000 : 3500);
}

function setupDropZone() {
  const overlay = $("#dropOverlay");
  let depth = 0;
  const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");

  window.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth += 1;
    overlay.hidden = false;
  });
  window.addEventListener("dragover", (event) => {
    if (hasFiles(event)) event.preventDefault();
  });
  window.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) overlay.hidden = true;
  });
  window.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth = 0;
    overlay.hidden = true;
    openFile(event.dataTransfer.files[0]);
  });
}

function setupKeyboard() {
  window.addEventListener("keydown", (event) => {
    if (!state.ws || !(event.ctrlKey || event.metaKey)) return;
    if (event.target.closest("input, textarea, select")) return;
    const key = event.key.toLowerCase();
    if (key === "z" && !event.shiftKey && state.ws.can_undo) {
      event.preventDefault();
      historyAction("undo");
    } else if (((key === "z" && event.shiftKey) || key === "y") && state.ws.can_redo) {
      event.preventDefault();
      historyAction("redo");
    }
  });
}

// ---------------------------------------------------------------------------
// Start up
// ---------------------------------------------------------------------------

function init() {
  const select = $("#opSelect");
  select.replaceChildren(...OPERATIONS.map((o) => el("option", { value: o.op }, o.label)));
  select.value = state.opName;
  select.addEventListener("change", () => {
    state.opName = select.value;
    $("#opFields").replaceChildren();
    renderOpFields();
  });

  $("#opForm").addEventListener("submit", (event) => {
    event.preventDefault();
    let params;
    try {
      params = buildParams();
    } catch (error) {
      toast(error.message, "error");
      return;
    }
    applyStep(state.opName, params);
  });

  for (const input of [$("#fileInput"), $("#fileInputTop")]) {
    input.addEventListener("change", () => {
      openFile(input.files[0]);
      input.value = "";
    });
  }
  $("#sampleBtn").addEventListener("click", openSample);
  $("#recipeInput").addEventListener("change", (event) => {
    applyRecipeFile(event.target.files[0]);
    event.target.value = "";
  });
  $("#showChanges").addEventListener("change", (event) => {
    state.showChanges = event.target.checked;
    renderTable();
  });
  $("#undoBtn").addEventListener("click", () => historyAction("undo"));
  $("#redoBtn").addEventListener("click", () => historyAction("redo"));
  $("#resetBtn").addEventListener("click", () => {
    if (window.confirm("Undo every step and go back to the original data? You can still redo them.")) historyAction("reset");
  });
  $("#prevPage").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - PAGE_SIZE);
    reload();
  });
  $("#nextPage").addEventListener("click", () => {
    state.offset += PAGE_SIZE;
    reload();
  });
  for (const button of document.querySelectorAll(".segmented button")) {
    button.addEventListener("click", () => {
      if (state.view === button.dataset.view) return;
      state.view = button.dataset.view;
      state.offset = 0;
      reload();
    });
  }

  $("#exportBtn").addEventListener("click", () => downloadFrom("/export"));
  $("#saveRecipeBtn").addEventListener("click", () => downloadFrom("/recipe"));

  setupDropZone();
  setupKeyboard();
  showWelcome();
  startEngine();
}

function setEngineStatus(status, text) {
  const node = $("#engineStatus");
  node.dataset.state = status;
  node.textContent = text;
  const ready = status === "ready";
  $("#sampleBtn").disabled = !ready;
  $("#fileInput").disabled = !ready;
  $("#chooseFile").classList.toggle("disabled", !ready);
}

async function startEngine() {
  setEngineStatus("loading", "Starting DataPatch…");
  try {
    engine = await createEngine({ onProgress: (text) => setEngineStatus("loading", text) });
    if (engine.kind === "browser") setEngineStatus("loading", engine.label);
    await engine.ready;
  } catch (error) {
    engine = null;
    setEngineStatus(
      "failed",
      `The Python engine couldn't start (${error.message}). It needs a recent browser with WebAssembly ` +
        "and access to cdn.jsdelivr.net. You can also run DataPatch locally with python app.py.",
    );
    return;
  }

  setEngineStatus("ready", engine.label);
  const chip = $("#engineChip");
  chip.hidden = false;
  chip.textContent = engine.kind === "browser" ? "Python in your browser" : "Local server";
  chip.title = engine.label;

  if (!engine.persistent) return;
  let saved = null;
  try {
    saved = sessionStorage.getItem(STORAGE_KEY);
  } catch {
    saved = null;
  }
  if (!saved) return;
  try {
    const data = await api("GET", `/api/workspaces/${saved}`, { query: query() });
    data.message = null;
    receive(data);
  } catch {
    forgetWorkspace();
  }
}

init();
