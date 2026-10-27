"""DataPatch - a small workbench for profiling and cleaning CSV files."""

from .cleaning import OPERATIONS, CleaningError, NothingToDo, apply_operation
from .profiling import profile_frame, quality_score
from .recipe import apply_recipe, make_recipe, parse_recipe
from .workspace import Workspace

__version__ = "1.2.0"
__author__ = "Swayam Shakya"

__all__ = [
    "OPERATIONS",
    "CleaningError",
    "NothingToDo",
    "Workspace",
    "apply_operation",
    "apply_recipe",
    "make_recipe",
    "parse_recipe",
    "profile_frame",
    "quality_score",
]
