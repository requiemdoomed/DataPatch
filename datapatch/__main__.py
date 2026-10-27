"""Command-line use of DataPatch, for scripts and pipelines.

python -m datapatch data.csv                       # profile only
python -m datapatch data.csv --recipe steps.json   # clean, writes data_cleaned.csv
python -m datapatch data.csv -r steps.json -o out.csv --quiet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .cleaning import CleaningError
from .files import read_csv, to_csv_bytes
from .profiling import profile_frame
from .recipe import apply_recipe


def _print_profile(title: str, report: dict) -> None:
    summary = report["summary"]
    parts = summary["components"]
    print(f"{title}: score {summary['score']} / 100")
    print(
        f"  {summary['rows']} rows, {summary['columns']} columns | "
        f"completeness {parts['completeness']}, uniqueness {parts['uniqueness']}, "
        f"consistency {parts['consistency']}, validity {parts['validity']}"
    )
    for column in report["columns"]:
        for issue in column["issues"]:
            print(f"  - {column['name']}: {issue['message']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m datapatch",
        description="Profile a CSV file and optionally clean it with a saved DataPatch recipe.",
    )
    parser.add_argument("input", type=Path, help="CSV file to read")
    parser.add_argument("-r", "--recipe", type=Path, help="recipe JSON saved from the DataPatch web app")
    parser.add_argument(
        "-o", "--output", type=Path, help="where to write the cleaned CSV (default: <input>_cleaned.csv)"
    )
    parser.add_argument(
        "--strict", action="store_true", help="fail on steps that have nothing to do instead of skipping them"
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    parser.add_argument("--version", action="version", version=f"DataPatch {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    say = (lambda *a, **k: None) if args.quiet else print

    try:
        frame = read_csv(args.input.read_bytes())
    except OSError as exc:
        print(f"error: can't read {args.input}: {exc.strerror}", file=sys.stderr)
        return 2
    except CleaningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        _print_profile("Original", profile_frame(frame))
    if args.recipe is None:
        return 0

    try:
        recipe = args.recipe.read_bytes()
        cleaned, report = apply_recipe(frame, recipe, skip_noops=not args.strict)
    except OSError as exc:
        print(f"error: can't read {args.recipe}: {exc.strerror}", file=sys.stderr)
        return 2
    except CleaningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    output = args.output or args.input.with_name(f"{args.input.stem}_cleaned.csv")
    output.write_bytes(to_csv_bytes(cleaned))

    say(f"\n{report.summary()}:")
    for label in report.applied:
        say(f"  + {label}")
    for label in report.skipped:
        say(f"  ~ {label} (nothing to do)")
    if not args.quiet:
        print()
        _print_profile("Cleaned", profile_frame(cleaned))
    say(f"\nWrote {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
