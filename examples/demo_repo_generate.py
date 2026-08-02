"""
Synthetic demo repo generator for the rename workflow demo.

Creates a repo of the form:

    demo_repo/
      lib/utils.py          compute_sum (target) + calculate_product + constants
      lib/utils_v2.py       legacy_add (decoy — must NOT be touched)
      callers/caller_N.py   import & call compute_sum (blast radius)
      decoys/decoy_N.py     use calculate_product/legacy_add only (must NOT change)

N callers are generated; the rest of the file budget is split into decoys.
"""

import os


def _utils_source() -> str:
    return '''"""Core arithmetic utilities for the demo repo."""

TWO = 2
THRESHOLD = 100


def compute_sum(a, b):
    """Add two numbers together."""
    return a + b


def calculate_product(a, b):
    """Multiply two numbers together."""
    return a * b
'''


def _utils_v2_source() -> str:
    return '''"""Legacy utilities kept around for back-compat (decoy)."""


def legacy_add(a, b):
    return a + b
'''


def _caller_source(n: int, n_decoys: int = 0) -> str:
    """Caller module {n}. For the first n_decoys callers, also reference a
    decoy function so decoys are NOT dead code (keeps the gardener demo's
    'decoys untouched' invariant meaningful)."""
    decoy_import = ""
    decoy_call = ""
    if n <= n_decoys:
        decoy_import = f"\nfrom decoys.decoy_{n:03d} import use_product_{n}"
        decoy_call = f"\n    extra = use_product_{n}(x, y)"
    return f'''"""Caller module {n} - uses compute_sum."""

from lib.utils import compute_sum, TWO, THRESHOLD{decoy_import}


def use_sum_{n}(x, y):
    total = compute_sum(x, y)
    extra = 0{decoy_call}
    return total + TWO + extra


def check_threshold_{n}(value):
    return value > THRESHOLD
'''


def _decoy_source(n: int) -> str:
    return f'''"""Decoy module {n} - must NOT be touched by the rename."""

from lib.utils import calculate_product
from lib.utils_v2 import legacy_add


def use_product_{n}(a, b):
    return calculate_product(a, b) + legacy_add(a, b)
'''


def generate(project_root: str, files: int = 10, callers_ratio: float = 0.8) -> None:
    """
    (Re)generate the demo repo at project_root with `files` py files total
    (excluding lib/, which is fixed): callers vs decoys split by callers_ratio.
    """
    import shutil
    if os.path.isdir(project_root):
        shutil.rmtree(project_root)

    lib_dir = os.path.join(project_root, "lib")
    callers_dir = os.path.join(project_root, "callers")
    decoys_dir = os.path.join(project_root, "decoys")
    os.makedirs(lib_dir, exist_ok=True)
    os.makedirs(callers_dir, exist_ok=True)
    os.makedirs(decoys_dir, exist_ok=True)

    with open(os.path.join(lib_dir, "utils.py"), "w") as f:
        f.write(_utils_source())
    with open(os.path.join(lib_dir, "utils_v2.py"), "w") as f:
        f.write(_utils_v2_source())

    n_callers = max(1, int(files * callers_ratio))
    n_decoys = max(1, files - n_callers)
    for i in range(1, n_callers + 1):
        with open(os.path.join(callers_dir, f"caller_{i:03d}.py"), "w") as f:
            f.write(_caller_source(i, n_decoys))

    for i in range(1, n_decoys + 1):
        with open(os.path.join(decoys_dir, f"decoy_{i:03d}.py"), "w") as f:
            f.write(_decoy_source(i))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--files", type=int, default=10)
    args = parser.parse_args()
    generate(args.root, args.files)
