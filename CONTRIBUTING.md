# Contributing to DataPatch

Thanks for taking the time to help. Bug reports, ideas and pull requests are all welcome.

## Getting set up

```bash
git clone https://github.com/<your-username>/datapatch.git
cd datapatch
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest
python app.py                    # http://127.0.0.1:5000
```

To try the GitHub Pages version locally:

```bash
python tools/build_site.py
python -m http.server 8000 -d _site    # http://localhost:8000
```

or open the Flask server with `?engine=browser`. Both run the Python package in the browser
with Pyodide (an internet connection is needed to download it).

## Before you open a pull request

- `pytest` and `node --test tests/js/*.test.mjs` pass.
- `ruff check .` and `ruff format --check .` are clean (run `ruff format .` to fix formatting).
- New behaviour has tests. Bug fixes come with a test that fails without the fix.
- User-facing changes are noted under "Unreleased" in `CHANGELOG.md`, and the README is updated
  if the feature or API changed.
- If you touched the front end, try it in a browser and check the console for errors, both with
  `python app.py` and with the in-browser engine.

Keep pull requests focused on one change. A short description of what changed and why is enough.

## Adding a cleaning operation

1. Write the function in `datapatch/cleaning.py`. It takes the DataFrame first and its
   parameters as keyword arguments, returns a new DataFrame and must not modify its input. Keep
   the index of surviving rows (don't call `reset_index`) so change tracking keeps working.
2. Raise `CleaningError` with a message a user can act on for bad parameters, and `NothingToDo`
   when the data has nothing for the operation to change.
3. Register it in `OPERATIONS` with a label and a `describe` function for the history panel.
4. Add its form to the `OPERATIONS` list at the top of `static/js/app.js`.
5. Add tests in `tests/test_cleaning.py`, and an issue fix in `profiling.py` if a detected
   problem should offer it as a one-click fix.
6. Document its parameters in the README's operations table.

If you add a new module that `datapatch/api.py` imports, also add it to `PACKAGE_FILES` in
`static/js/engine-worker.js` and `BROWSER_MODULES` in `tools/build_site.py`, or the in-browser
engine won't find it. The site build fails if the two lists disagree. Keep that code free of
Flask imports and anything Pyodide can't run (starting threads or subprocesses, native
extensions other than numpy and pandas).

## Reporting bugs

Open an issue using the bug report template. A small CSV that shows the problem (with any
private data removed) makes bugs much faster to fix. For security problems, see
[SECURITY.md](SECURITY.md) instead.
