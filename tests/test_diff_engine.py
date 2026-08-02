"""Unit tests for the AST-aware DiffEngine (rename semantics, safe failure)."""
import ast

import pytest

from app.core.diff_engine import DiffEngine

SRC = '''class Calculator:
    def add(self, a, b):
        return self.subtract(a, b)

    def subtract(self, a, b):
        return a - b
'''


@pytest.fixture
def engine():
    return DiffEngine()


def test_rename_function(engine):
    new_source, diff = engine.apply_transform(
        SRC, "rename_symbol", {"old_name": "add", "new_name": "compute_sum"}
    )
    assert "def compute_sum" in new_source
    assert "def add" not in new_source
    assert diff  # unified diff produced


def test_rename_call_sites(engine):
    """Rename must propagate to every occurrence, not just the definition."""
    src = SRC + "\n\nresult = Calculator().add(1, 2)\n"
    new_source, _ = engine.apply_transform(
        src, "rename_symbol", {"old_name": "add", "new_name": "compute_sum"}
    )
    assert "Calculator().compute_sum(1, 2)" in new_source
    assert "add(1, 2)" not in new_source


def test_rename_keeps_unrelated_attributes(engine):
    """self.subtract must NOT be renamed when renaming 'add'."""
    new_source, _ = engine.apply_transform(
        SRC, "rename_symbol", {"old_name": "add", "new_name": "compute_sum"}
    )
    assert "self.subtract(a, b)" in new_source


def test_rename_does_not_touch_strings_or_comments(engine):
    src = '# add is documented here\ntext = "call add now"\ndef add():\n    pass\n'
    new_source, _ = engine.apply_transform(
        src, "rename_symbol", {"old_name": "add", "new_name": "compute_sum"}
    )
    assert 'text = "call add now"' in new_source
    assert "# add is documented here" in new_source
    assert "def compute_sum" in new_source


def test_rename_output_is_valid_python(engine):
    new_source, _ = engine.apply_transform(
        SRC, "rename_symbol", {"old_name": "add", "new_name": "compute_sum"}
    )
    ast.parse(new_source)  # raises on invalid syntax


def test_unknown_transform_raises(engine):
    with pytest.raises(ValueError):
        engine.apply_transform(SRC, "nonexistent_transform", {})


def test_missing_args_raise(engine):
    with pytest.raises(ValueError):
        engine.apply_transform(SRC, "rename_symbol", {})


def test_rename_nonexistent_symbol_is_noop(engine):
    new_source, diff = engine.apply_transform(
        SRC, "rename_symbol", {"old_name": "does_not_exist", "new_name": "x"}
    )
    assert new_source == SRC
    assert diff == ""
