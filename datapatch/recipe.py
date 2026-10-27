"""Cleaning recipes: a saved list of steps that can be replayed on another file.

A recipe is plain JSON, so it can be kept next to a project, reviewed in a pull
request or run from the command line::

    {
      "datapatch_recipe": 1,
      "source": "customers.csv",
      "created": "2026-10-26T21:40:00+00:00",
      "steps": [
        {"op": "trim_whitespace", "params": {}, "label": "Trim whitespace in all columns"},
        {"op": "convert_type", "params": {"columns": ["age"], "target": "integer"}}
      ]
    }

``label`` is optional and only there for people reading the file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd

from .cleaning import OPERATIONS, CleaningError, NothingToDo, apply_step, describe_operation

RECIPE_VERSION = 1
MAX_STEPS = 500


@dataclass
class RecipeReport:
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"Applied {len(self.applied)} step{'s' if len(self.applied) != 1 else ''}"
        if self.skipped:
            text += f", skipped {len(self.skipped)} with nothing to do"
        return text


def make_recipe(steps: Iterable[Any], source: str | None = None) -> dict[str, Any]:
    """Build a recipe from workspace steps (anything with ``op``, ``params`` and ``label``)."""
    return {
        "datapatch_recipe": RECIPE_VERSION,
        "source": source,
        "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "steps": [{"op": s.op, "params": s.params, "label": s.label} for s in steps],
    }


def parse_recipe(data: Any) -> list[tuple[str, dict[str, Any]]]:
    """Validate a recipe (dict, JSON text or bytes) and return its ``(op, params)`` pairs."""
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8-sig")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise CleaningError(f"That recipe isn't valid JSON (line {exc.lineno}).") from None

    if not isinstance(data, dict) or "steps" not in data:
        raise CleaningError("That doesn't look like a DataPatch recipe.")
    version = data.get("datapatch_recipe", RECIPE_VERSION)
    if version != RECIPE_VERSION:
        raise CleaningError(f"This recipe is version {version}; DataPatch reads version {RECIPE_VERSION}.")

    steps = data["steps"]
    if not isinstance(steps, list) or not steps:
        raise CleaningError("The recipe has no steps.")
    if len(steps) > MAX_STEPS:
        raise CleaningError(f"Recipes are limited to {MAX_STEPS} steps.")

    parsed = []
    for number, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or not isinstance(step.get("op"), str):
            raise CleaningError(f"Step {number} of the recipe is missing its 'op'.")
        if step["op"] not in OPERATIONS:
            raise CleaningError(f"Step {number} uses an unknown operation: {step['op']!r}.")
        params = step.get("params") or {}
        if not isinstance(params, dict):
            raise CleaningError(f"Step {number} has parameters that aren't an object.")
        parsed.append((step["op"], params))
    return parsed


def step_label(op: str, params: dict) -> str:
    try:
        return describe_operation(op, params)
    except Exception:  # a hand-written recipe can have params the describer doesn't expect
        return OPERATIONS[op].label


def step_failed(number: int, op: str, params: dict, error: Exception) -> CleaningError:
    return CleaningError(f"Recipe step {number} ({step_label(op, params)}) failed: {error}")


def apply_recipe(frame: pd.DataFrame, recipe: Any, *, skip_noops: bool = True) -> tuple[pd.DataFrame, RecipeReport]:
    """Run every step of a recipe on ``frame``.

    Steps with nothing to change (no duplicates left, say) are skipped when
    ``skip_noops`` is true. Any other failure stops the run with a CleaningError
    naming the step.
    """
    report = RecipeReport()
    for number, (op, params) in enumerate(parse_recipe(recipe), start=1):
        label = step_label(op, params)
        try:
            frame = apply_step(frame, op, params)
        except NothingToDo:
            if not skip_noops:
                raise
            report.skipped.append(label)
            continue
        except CleaningError as exc:
            raise step_failed(number, op, params, exc) from None
        report.applied.append(label)
    return frame, report
