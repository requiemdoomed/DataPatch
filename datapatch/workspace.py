"""A cleaning session: the original upload, the steps applied to it and undo/redo."""

from __future__ import annotations

import copy
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .cleaning import CleaningError, NothingToDo, apply_operation, apply_step, describe_operation
from .files import json_value
from .profiling import ColumnCache, profile_frame
from .recipe import RecipeReport, parse_recipe, step_failed, step_label


class WorkspaceNotFound(LookupError):
    pass


@dataclass
class Step:
    op: str
    params: dict[str, Any]
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "params": self.params, "label": self.label}


def _same_value(a: Any, b: Any) -> bool:
    # "42" and 42 are different cells as far as cleaning goes; 42 and 42.0 aren't.
    if isinstance(a, str) != isinstance(b, str):
        return False
    try:
        return bool(a == b)
    except (TypeError, ValueError):
        return False


def changed_cells(current: pd.Series, original: pd.Series) -> np.ndarray:
    """Boolean mask of positions where two equally long columns hold different values."""
    cur = current.reset_index(drop=True)
    org = original.reset_index(drop=True)
    if cur.dtype == org.dtype and cur.equals(org):
        return np.zeros(len(cur), dtype=bool)

    a = cur.astype(object).to_numpy()
    b = org.astype(object).to_numpy()
    a_missing = pd.isna(a)
    b_missing = pd.isna(b)
    both = ~a_missing & ~b_missing
    same = a_missing & b_missing
    if both.any():
        same[both] = [_same_value(x, y) for x, y in zip(a[both], b[both], strict=True)]
    return ~same


@dataclass(eq=False)
class Workspace:
    original: pd.DataFrame
    filename: str = "data.csv"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        # Row labels 0..n-1 let every cleaned row point back at its original row.
        self.original = self.original.reset_index(drop=True)
        self.current = self.original
        self.steps: list[Step] = []
        self._redo: list[Step] = []
        self._before_last: pd.DataFrame | None = None
        self._columns = ColumnCache()
        self._original_profile: dict | None = None
        self._current_profile: dict | None = None
        self._changes: dict | None = None
        self.lock = threading.RLock()
        self.touch()

    def touch(self) -> None:
        self.last_used = time.monotonic()

    # -- profiles -----------------------------------------------------------

    @property
    def original_profile(self) -> dict:
        if self._original_profile is None:
            self._original_profile = profile_frame(self.original, self._columns)
        return self._original_profile

    @property
    def current_profile(self) -> dict:
        if not self.steps:
            return self.original_profile
        if self._current_profile is None:
            self._current_profile = profile_frame(self.current, self._columns)
        return self._current_profile

    # -- history ------------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return bool(self.steps)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def next_redo(self) -> Step | None:
        return self._redo[-1] if self._redo else None

    def apply(self, op: str, params: dict | None = None) -> Step:
        with self.lock:
            params = copy.deepcopy(params or {})
            result = apply_step(self.current, op, params)
            step = Step(op, params, describe_operation(op, params))
            before = self.current
            self.steps.append(step)
            self._redo.clear()
            self._set_current(result, before)
            return step

    def undo(self) -> Step:
        with self.lock:
            if not self.steps:
                raise CleaningError("Nothing to undo.")
            step = self.steps.pop()
            self._redo.append(step)
            previous = self._before_last if self._before_last is not None else self._replay()
            self._set_current(previous, None)
            return step

    def redo(self) -> Step:
        with self.lock:
            if not self._redo:
                raise CleaningError("Nothing to redo.")
            step = self._redo[-1]
            result = apply_operation(self.current, step.op, step.params)
            self._redo.pop()
            before = self.current
            self.steps.append(step)
            self._set_current(result, before)
            return step

    def reset(self) -> int:
        """Go back to the original. The undone steps stay on the redo stack."""
        with self.lock:
            if not self.steps:
                raise CleaningError("Already at the original data.")
            count = len(self.steps)
            self._redo.extend(reversed(self.steps))
            self.steps.clear()
            self._set_current(self.original, None)
            return count

    def apply_recipe(self, recipe: Any) -> RecipeReport:
        """Apply every step of a recipe, all or nothing.

        Steps that have nothing to change on this data are skipped. If any other
        step fails, the workspace is left exactly as it was.
        """
        plan = parse_recipe(recipe)
        with self.lock:
            saved = (list(self.steps), list(self._redo), self.current, self._before_last)
            report = RecipeReport()
            try:
                for number, (op, params) in enumerate(plan, start=1):
                    try:
                        report.applied.append(self.apply(op, params).label)
                    except NothingToDo:
                        report.skipped.append(step_label(op, params))
                    except CleaningError as exc:
                        raise step_failed(number, op, params, exc) from None
            except CleaningError:
                self.steps, self._redo, current, self._before_last = saved
                self._set_current(current, self._before_last)
                raise
            return report

    def _replay(self) -> pd.DataFrame:
        frame = self.original
        for step in self.steps:
            frame = apply_operation(frame, step.op, step.params)
        return frame

    def _set_current(self, frame: pd.DataFrame, before: pd.DataFrame | None) -> None:
        # Keeping the frame from before the last step makes a single undo instant;
        # undoing further back replays from the original.
        self.current = frame
        self._before_last = before
        self._current_profile = None
        self._changes = None
        self.touch()

    # -- comparing with the original -----------------------------------------

    def origin_columns(self) -> dict[Any, Any]:
        """Map each current column to the original column it came from."""
        origin = {c: c for c in self.original.columns}
        for step in self.steps:
            if step.op == "rename_column":
                old, new = step.params.get("column"), str(step.params.get("new_name", "")).strip()
                if old in origin:
                    origin[new] = origin.pop(old)
            elif step.op == "drop_columns":
                dropped = step.params.get("columns") or []
                for name in [dropped] if isinstance(dropped, str) else dropped:
                    origin.pop(name, None)
        return {c: origin[c] for c in self.current.columns if c in origin}

    def _positions(self) -> np.ndarray | None:
        index = self.current.index
        if not pd.api.types.is_integer_dtype(index.dtype) or not index.is_unique:
            return None
        positions = index.to_numpy()
        if len(positions) and (positions.min() < 0 or positions.max() >= len(self.original)):
            return None
        return positions

    @property
    def changes(self) -> dict[str, Any]:
        """How the cleaned data differs from the original, in numbers."""
        if self._changes is None:
            self._changes = self._compute_changes()
        return self._changes

    def _compute_changes(self) -> dict[str, Any]:
        origin = self.origin_columns()
        kept = set(origin.values())
        summary = {
            "cells_changed": 0,
            "by_column": {},
            "rows_removed": len(self.original) - len(self.current),
            "columns_removed": [str(c) for c in self.original.columns if c not in kept],
            "columns_renamed": {str(src): str(col) for col, src in origin.items() if col != src},
        }
        positions = self._positions()
        if positions is None or not self.steps:
            return summary
        for column, source in origin.items():
            count = int(changed_cells(self.current[column], self.original[source].iloc[positions]).sum())
            if count:
                summary["by_column"][str(column)] = count
                summary["cells_changed"] += count
        return summary

    def page_changes(self, offset: int, limit: int) -> dict[str, Any]:
        """Original row numbers for a page of cleaned rows, plus the cells that changed."""
        positions = self._positions()
        if positions is None:
            return {"row_numbers": None, "cells": []}
        page = positions[offset : offset + limit]
        cells: list[list[Any]] = []
        if self.steps:
            origin = self.origin_columns()
            for col_index, column in enumerate(self.current.columns):
                source = origin.get(column)
                if source is None:
                    continue
                current = self.current[column].iloc[offset : offset + limit]
                original = self.original[source].iloc[page]
                for row in np.flatnonzero(changed_cells(current, original)):
                    cells.append([int(row), col_index, json_value(original.iloc[row])])
        return {"row_numbers": (page + 1).tolist(), "cells": cells}


class WorkspaceStore:
    """Keeps sessions in memory and drops the stale ones."""

    def __init__(self, max_items: int = 32, ttl_seconds: float = 3 * 60 * 60) -> None:
        self.max_items = max_items
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, Workspace] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._items)

    def create(self, frame: pd.DataFrame, filename: str) -> Workspace:
        workspace = Workspace(frame, filename)
        with self._lock:
            self._evict()
            self._items[workspace.id] = workspace
        return workspace

    def get(self, workspace_id: str) -> Workspace:
        with self._lock:
            workspace = self._items.get(workspace_id)
        if workspace is None:
            raise WorkspaceNotFound(workspace_id)
        workspace.touch()
        return workspace

    def _evict(self) -> None:
        now = time.monotonic()
        for key, item in list(self._items.items()):
            if now - item.last_used > self.ttl_seconds:
                del self._items[key]
        while len(self._items) >= self.max_items:
            oldest = min(self._items.values(), key=lambda w: w.last_used)
            del self._items[oldest.id]
