"""Unit tests for the validation pipeline (offline: syntax/imports gates only)."""
import json
import os
import tempfile

import pytest

from app.tools.validation_tools import validate_changes


def _validate(contents):
    with tempfile.TemporaryDirectory() as proj:
        return json.loads(validate_changes(
            [{"file_path": "mod.py", "content": contents}], proj
        ))


def test_report_structure():
    report = _validate("def f():\n    return 1\n")
    assert set(report["gates"].keys()) == {
        "syntax", "imports", "types", "tests", "security"
    }
    assert report["overall"] in ("PASS", "FAIL")


def test_syntax_gate_rejects_bad_python():
    report = _validate("def broken(:\n    pass\n")
    assert report["overall"] == "FAIL"
    assert report["gates"]["syntax"]["status"] == "FAIL"


def test_syntax_gate_passes_good_python():
    report = _validate("def f():\n    return 1\n")
    assert report["gates"]["syntax"]["status"] == "PASS"


def test_empty_changes_handled():
    with tempfile.TemporaryDirectory() as proj:
        report = json.loads(validate_changes([], proj))
    assert report["overall"] in ("PASS", "FAIL", "ERROR")


def test_multiple_files_validated():
    changes = [
        {"file_path": "a.py", "content": "def fa():\n    return 1\n"},
        {"file_path": "b.py", "content": "def fb():\n    return 2\n"},
    ]
    with tempfile.TemporaryDirectory() as proj:
        report = json.loads(validate_changes(changes, proj))
    assert report["gates"]["syntax"]["status"] == "PASS"
