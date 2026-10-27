"""Assemble the static GitHub Pages site.

    python tools/build_site.py            # writes _site/
    python tools/build_site.py out/dir

The site is index.html plus the files it loads: the front end in static/, the
datapatch package's .py files (run in the browser by Pyodide) and the sample
CSV. The Flask server, CLI and tests aren't needed there.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BROWSER_MODULES = ["__init__.py", "cleaning.py", "files.py", "profiling.py", "recipe.py", "workspace.py", "api.py"]


def build(target: Path) -> list[Path]:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    shutil.copy2(ROOT / "index.html", target / "index.html")
    shutil.copytree(ROOT / "static", target / "static")
    (target / "datapatch").mkdir()
    for name in BROWSER_MODULES:
        shutil.copy2(ROOT / "datapatch" / name, target / "datapatch" / name)
    (target / "sample_data").mkdir()
    for csv in (ROOT / "sample_data").glob("*.csv"):
        if not csv.stem.endswith("_cleaned"):
            shutil.copy2(csv, target / "sample_data" / csv.name)
    shutil.copy2(ROOT / "LICENSE", target / "LICENSE")
    # Without this, GitHub Pages runs Jekyll, which skips files starting with "_"
    # such as datapatch/__init__.py.
    (target / ".nojekyll").touch()
    return sorted(p for p in target.rglob("*") if p.is_file())


def check_worker_list() -> None:
    """The worker downloads a fixed list of modules; make sure it matches this one."""
    worker = (ROOT / "static" / "js" / "engine-worker.js").read_text(encoding="utf-8")
    missing = [name for name in BROWSER_MODULES if f'"{name}"' not in worker]
    if missing:
        raise SystemExit(f"engine-worker.js doesn't load: {', '.join(missing)}")


if __name__ == "__main__":
    check_worker_list()
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "_site"
    files = build(out)
    print(f"Built {len(files)} files into {out}")
