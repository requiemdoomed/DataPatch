# Architecture

DataPatch is one page (`index.html`) with a plain HTML/CSS/JavaScript front end. All data
handling happens in Python with pandas, in `datapatch/api.py` and the modules below it. The page
only renders what that API sends back.

## Two engines, one API

```
index.html + static/js/app.js
   │  engine.request(method, path, {query, body, file})      static/js/engine.js
   ├──────────────────────────────┬─────────────────────────────────────────────
   ▼ ServerEngine: fetch()         ▼ BrowserEngine: postMessage()
 server.py (Flask)               engine-worker.js (module Web Worker)
   │                               │  Pyodide + pandas, datapatch/*.py copied
   │                               │  into Pyodide's virtual file system
   ▼                               ▼
 DataPatchAPI.dispatch()  ◄──────  DataPatchAPI.handle_json()
```

`DataPatchAPI` maps a method and a path such as `POST /api/workspaces/<id>/steps` to a handler
and returns a `Reply` (a status plus either a JSON body or a file to download). It doesn't know
about Flask or browsers.

- **Local server.** `server.py` forwards `/api/...` requests to `dispatch` and turns the reply
  into a Flask response. When it serves `index.html` it sets
  `<meta name="datapatch-engine" content="server">`, which tells `engine.js` to use `fetch`.
- **In the browser.** A static host serves `index.html` unchanged (`content="browser"`), so
  `engine.js` starts `engine-worker.js`. The worker imports Pyodide from jsDelivr, loads pandas,
  downloads the package's `.py` files and the sample CSV from the site, and creates a
  `DataPatchAPI`. Each request is passed to `handle_json` as JSON text; uploads are written to
  Pyodide's file system first, and downloads come back base64-encoded. Running in a worker keeps
  the page responsive while pandas works.

`tools/build_site.py` assembles the static site and checks that the worker's list of modules
matches the one the build copies.

```
server.py / engine-worker.js
   │
   ▼
api.py ──► WorkspaceStore ──► Workspace
                                   ├── original DataFrame (never modified)
                                   ├── steps: [Step(op, params, label)]
                                   ├── current DataFrame = original + steps
                                   └── ColumnCache (column profiles)
                                         │
                cleaning.py  ◄───────────┤  operations
                profiling.py ◄───────────┘  profiles and score
```

## Operations are pure functions

Every operation in `cleaning.py` takes a DataFrame and keyword parameters and returns a new
DataFrame. It never modifies its input. Operations are registered in `OPERATIONS`, which gives
each one a label and a function that describes a step in words for the history panel.

Errors a user can cause raise `CleaningError` and reach the browser as a 400 with a readable
message. `NothingToDo`, a subclass, means the step is valid but would change nothing (no
duplicates left, no outliers). The workspace refuses such steps so the history only holds steps
that did something, and recipes skip them.

## Undo is replay

The cleaned data is always `original + steps`. Undo pops the last step and rebuilds the frame by
replaying the remaining steps from the original, so the result is exact by construction. The
frame from just before the last step is kept as well, which makes the common single undo
instant. Redo re-applies the popped step. Reset moves every step onto the redo stack.

## Tracing cells back to the original

When the workspace is created, the original gets row labels `0..n-1`. Operations that drop rows
keep the labels of the rows that survive, so every cleaned row knows which original row it came
from. Renames and dropped columns are followed by walking the step list.

With rows and columns lined up, `changed_cells` compares a cleaned column with its original. Two
cells count as the same when both are missing or when they're equal and both or neither are text.
`"42"` and `42` are different (a conversion happened); `1.0` and `1` are the same. This feeds the
change summary, the per-column change counts and the highlighted cells in the grid.

## Profiling and caching

`profile_frame` builds a profile for each column and combines the column statistics into the
quality score (the README describes the formula). Text columns are analysed per distinct value
weighted by its count, rather than per cell, and the type checks look at a sample first and skip
the full pass when the sample is clearly far from the threshold.

Profiling every column after every step would still be the slowest part of a request on large
files, so the workspace keeps a `ColumnCache`. Before profiling a column it checks for a cached
column with identical values (a fast vectorised comparison) and reuses that profile. A step that
changes one column only re-profiles that column.

## Recipes

`recipe.py` turns the step list into JSON and back. `parse_recipe` validates the structure and
the operation names before anything runs. `Workspace.apply_recipe` applies the steps through the
normal `apply` path and restores the previous state if any step fails, so a recipe is all or
nothing. The command line (`python -m datapatch`) uses `apply_recipe` directly on a DataFrame.

## Front end

`static/js/app.js` keeps a small state object (the last API response, the current view, page
offset and focused column) and redraws from it after every response. Operation forms are
generated from the `OPERATIONS` list at the top of the file. Every value from the data is inserted
with `textContent` or attributes, never as HTML.
