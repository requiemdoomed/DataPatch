# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.0] - 2026-10-26

### Added
- Runs on GitHub Pages. With no server available, `index.html` runs DataPatch's own Python
  package in the browser with Pyodide 314.0.7 (Python 3.14, pandas 3.0) in a Web Worker. Files
  never leave the visitor's device.
- A deploy workflow (`.github/workflows/pages.yml`) that tests, builds the site with
  `tools/build_site.py` and publishes it, plus `.nojekyll` for branch-based deploys.
- An engine status line on the start screen and a badge showing where Python is running.
- `?engine=browser` to try the in-browser engine with the local server, and `?pyodide=` to
  serve Pyodide from the same site.

### Changed
- `index.html` moved to the repository root and uses relative paths, so it works both at `/`
  and under a Pages project path such as `/datapatch/`.
- Request handling moved from `server.py` into a framework-free `datapatch/api.py`, which the
  Flask server and the browser engine both call.
- Export and Save recipe are buttons that work with either engine.
- CI checks every JavaScript file and test-builds the Pages site.

## [1.1.0] - 2026-10-26

### Added
- Change tracking: changed cells are marked in the cleaned view with their original value on
  hover, the row column shows each row's number in the original file, and the scoreboard and
  column cards show how many cells changed and how many rows and columns were removed.
- Recipes: save the history as JSON and apply it to another file. Applying is all or nothing,
  and steps with nothing to change are skipped and reported.
- Command-line interface: `python -m datapatch data.csv --recipe recipe.json`.
- A loading bar for requests that take more than a moment.
- `pyproject.toml` with project metadata and a `datapatch` console script.
- Contributor docs, architecture notes, a security policy, issue and pull request templates.
- CI now lints Python, checks the JavaScript syntax, tests against the oldest supported pandas
  and runs the CLI.

### Changed
- Profiling analyses each distinct value once and caches column profiles, so a step on a 20 MB
  file takes about a second instead of ten.
- A single undo is instant; it no longer replays the whole history.
- Rows dropped by a step keep their original row labels instead of being renumbered.
- The minimum pandas version is now 2.2, the oldest version the test suite runs against.

### Fixed
- A CSV row with more fields than the header made pandas use the first column as the index,
  silently shifting every value one column left. Such files are now rejected with the line
  number.
- Infinite values in numeric columns no longer produce warnings or `NaN` statistics.
- Applying a recipe outside the browser no longer counts steps that change nothing as applied.
- On medium-width screens the column list no longer slides over the Transform panel when scrolling.

## [1.0.0] - 2026-10-26

### Added
- Upload and preview CSV, TSV and text files with encoding and delimiter detection.
- Column profiles with missing values, placeholders, whitespace, case variants, numbers, dates
  and booleans stored as text, outliers and rare values.
- Eleven cleaning operations with one-click fixes, undo, redo and reset.
- Original vs cleaned quality score with four components.
- CSV export, a messy sample dataset, a walkthrough notebook and a pytest suite.

[Unreleased]: https://github.com/<your-username>/datapatch/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/<your-username>/datapatch/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/<your-username>/datapatch/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/<your-username>/datapatch/releases/tag/v1.0.0
