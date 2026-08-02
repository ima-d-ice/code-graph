"""Tests for deterministic dead-code removal (gardener's executor fallback)."""
import os

from app.core.rename_propagation import (
    parse_remove_objective,
    apply_objective_removal,
    defined_symbols,
)

CALLER = '''"""Caller module."""

from utils import compute_sum


def check_threshold_1(value):
    if value > 100:
        return "high"
    return "low"


def use_sum(x, y):
    return compute_sum(x, y) + 1
'''


def test_parse_remove_objective_variants():
    assert parse_remove_objective(
        "Remove the dead function check_threshold_1 from callers/caller_001.py"
    ) == "check_threshold_1"
    assert parse_remove_objective("Remove dead code compute_legacy") == "compute_legacy"
    assert parse_remove_objective("Remove unused function old_helper") == "old_helper"
    assert parse_remove_objective("Refactor the naming of compute_sum") == ""


def test_removal_of_orphan(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "caller.py").write_text(CALLER)

    changes = apply_objective_removal(
        "Remove the dead function check_threshold_1 from caller.py",
        "caller.py", str(project), {},
    )
    assert len(changes) == 1
    new_content = changes[0]["content"]
    assert "check_threshold_1" not in new_content
    assert "def use_sum" in new_content
    assert "compute_sum" in new_content  # other symbols untouched
    assert "check_threshold_1" not in defined_symbols(new_content)


def test_removal_blocked_when_referenced(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "caller.py").write_text(CALLER)
    other = '''"""Other module."""


def wrapper(x):
    return check_threshold_1(x)
'''
    (project / "other.py").write_text(other)

    changes = apply_objective_removal(
        "Remove the dead function check_threshold_1 from caller.py",
        "caller.py", str(project), {"other.py": other},
    )
    assert changes == []


def test_removal_blocked_when_symbol_used_in_same_file(tmp_path):
    used = CALLER.replace(
        "def use_sum(x, y):\n    return compute_sum(x, y) + 1",
        "def use_sum(x, y):\n    return compute_sum(x, y) + check_threshold_1(5)",
    )
    project = tmp_path / "repo"
    project.mkdir()
    (project / "caller.py").write_text(used)

    changes = apply_objective_removal(
        "Remove the dead function check_threshold_1 from caller.py",
        "caller.py", str(project), {},
    )
    assert changes == []


def test_removal_requires_valid_syntax_result(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "caller.py").write_text(CALLER)

    changes = apply_objective_removal(
        "Remove the dead function check_threshold_1 from caller.py",
        "caller.py", str(project), {},
    )
    import ast
    ast.parse(changes[0]["content"])  # must not raise


def test_removal_returns_empty_for_unknown_symbol(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "caller.py").write_text(CALLER)

    changes = apply_objective_removal(
        "Remove the dead function no_such_fn from caller.py",
        "caller.py", str(project), {},
    )
    assert changes == []


def test_removal_of_only_def_in_file(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()

    only = '''"""Module with a single orphan."""


def lonely_orphan():
    return 42
'''
    (project / "solo.py").write_text(only)
    changes = apply_objective_removal(
        "Remove the dead function lonely_orphan from solo.py",
        "solo.py", str(project), {},
    )
    assert len(changes) == 1
    assert "lonely_orphan" not in changes[0]["content"]
    assert changes[0]["content"].strip() != ""
    import ast
    ast.parse(changes[0]["content"])
