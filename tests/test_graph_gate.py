"""Behavioral tests for Gate 6 (graph integrity).

Offline: fixtures are fully self-resolving, so the Neo4j cross-check is
never exercised (no unresolved refs remain when the tests pass/fail as
expected).
"""
import os

from app.tools.validation_tools import validate_graph_integrity

UTILS_DISK = '''"""Core arithmetic utilities."""

TWO = 2


def compute_sum(a, b):
    return a + b


def calculate_product(a, b):
    return a * b
'''

CALLER_DISK = '''"""Caller module."""

from utils import compute_sum, TWO


def use_sum(x, y):
    return compute_sum(x, y) + TWO
'''

UTILS_RENAMED = '''"""Core arithmetic utilities."""

TWO = 2


def calculate_total(a, b):
    return a + b


def calculate_product(a, b):
    return a * b
'''

CALLER_RENAMED = '''"""Caller module."""

from utils import calculate_total, TWO


def use_sum(x, y):
    return calculate_total(x, y) + TWO
'''


def _write(project, files):
    for rel, content in files.items():
        path = os.path.join(project, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


def _old_contents(files):
    return dict(files)


def test_complete_rename_passes(tmp_path):
    """Rename applied everywhere (def + all call sites) -> PASS."""
    project = str(tmp_path / "proj")
    _write(project, {"utils.py": UTILS_DISK, "caller.py": CALLER_DISK})
    changes = [
        {"file_path": "utils.py", "content": UTILS_RENAMED},
        {"file_path": "caller.py", "content": CALLER_RENAMED},
    ]
    report = validate_graph_integrity(
        changes, project, _old_contents({"utils.py": UTILS_DISK, "caller.py": CALLER_DISK})
    )
    assert report["status"] == "PASS", report["details"]


def test_missed_call_site_fails(tmp_path):
    """Rename applied to the def but one caller still uses the old name -> FAIL."""
    project = str(tmp_path / "proj")
    _write(project, {"utils.py": UTILS_DISK, "caller.py": CALLER_DISK})
    changes = [{"file_path": "utils.py", "content": UTILS_RENAMED}]
    report = validate_graph_integrity(
        changes, project, _old_contents({"utils.py": UTILS_DISK})
    )
    assert report["status"] == "FAIL"
    assert "removed-symbol" in report["details"]
    assert "compute_sum" in report["details"]


def test_missed_import_site_fails(tmp_path):
    """Two callers; one was updated, the other wasn't -> FAIL on the stale one."""
    project = str(tmp_path / "proj")
    caller2_stale = CALLER_DISK.replace("use_sum", "use_sum2")
    _write(project, {"utils.py": UTILS_DISK, "caller.py": CALLER_DISK, "caller2.py": caller2_stale})
    changes = [
        {"file_path": "utils.py", "content": UTILS_RENAMED},
        {"file_path": "caller.py", "content": CALLER_RENAMED},
    ]
    report = validate_graph_integrity(
        changes, project,
        _old_contents({"utils.py": UTILS_DISK, "caller.py": CALLER_DISK}),
    )
    assert report["status"] == "FAIL"
    assert "removed-symbol" in report["details"]


def test_preexisting_unresolved_ref_not_flagged(tmp_path):
    """A dangling ref that existed before the change must not fail the gate."""
    project = str(tmp_path / "proj")
    unrelated = '''"""Untouched module with a pre-existing with-as binding."""

def load():
    with open("data.txt") as fh:
        return fh.read()
'''
    _write(project, {
        "utils.py": UTILS_DISK,
        "caller.py": CALLER_DISK,
        "unrelated.py": unrelated,
    })
    changes = [
        {"file_path": "utils.py", "content": UTILS_RENAMED},
        {"file_path": "caller.py", "content": CALLER_RENAMED},
    ]
    report = validate_graph_integrity(
        changes, project,
        _old_contents({"utils.py": UTILS_DISK, "caller.py": CALLER_DISK}),
    )
    assert report["status"] == "PASS", report["details"]


def test_with_as_binding_is_local_symbol(tmp_path):
    """`with open(...) as fh:` defines fh locally — no false unresolved."""
    project = str(tmp_path / "proj")
    module = '''"""Uses a with-as binding."""

def load():
    with open("data.txt") as fh:
        return fh.read()
'''
    _write(project, {"module.py": module})
    report = validate_graph_integrity(
        [{"file_path": "module.py", "content": module}], project,
        _old_contents({"module.py": module}),
    )
    assert report["status"] == "PASS", report["details"]


def test_no_changes_report(tmp_path):
    """Empty changes list is a valid no-op (nothing to check)."""
    project = str(tmp_path / "proj")
    _write(project, {"utils.py": UTILS_DISK})
    report = validate_graph_integrity([], project)
    assert report["status"] in ("PASS", "FAIL")
