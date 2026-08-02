"""
Objective-driven rename propagation.

Deterministic, graph-native guarantee layer: when the LLM executor or repair
agent fails to produce usable changes, the rename is performed mechanically
with the semantic DiffEngine across the blast radius found during DISCOVER.
"""

import ast
import difflib
import logging
import os
import re
from typing import Dict, Any, Tuple, List

logger = logging.getLogger(__name__)


def defined_symbols(source: str) -> set:
    """Names defined in a module: defs, classes, imports, assignments, params."""
    symbols = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return symbols
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                symbols.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    symbols.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols.add(target.id)
        elif isinstance(node, ast.arg):
            symbols.add(node.arg)
    return symbols


def parse_rename_objective(objective: str) -> Tuple[str, str]:
    """
    Deterministically extract a rename mapping from an objective like
    "Rename compute_sum to calculate_total ..." or
    "Rename the function compute_sum to calculate_total ...".
    Returns (old, new) or ("", "").
    """
    match = re.search(
        r"rename\s+(?:the\s+)?(?:function|method|class|symbol|variable)?\s*"
        r"([A-Za-z_][A-Za-z0-9_]*)\s+to\s+([A-Za-z_][A-Za-z0-9_]*)",
        objective, re.IGNORECASE)
    if match:
        return match.group(1), match.group(2)
    return "", ""


def apply_objective_rename(objective: str, trigger_file: str, project_root: str,
                           affected_files: Dict[str, str]) -> List[Dict[str, str]]:
    """
    Deterministic rename fallback: when the executor fails to produce valid
    changes, perform the rename mechanically with the DiffEngine.

    Returns a complete changes list (trigger file + every affected file that
    references the old symbol), or [] if the objective is not a rename.
    """
    from app.core.diff_engine import DiffEngine

    old_name, new_name = parse_rename_objective(objective)
    if not old_name or not new_name:
        return []

    engine = DiffEngine()
    changes = []

    # 1. Rename the trigger file itself (read from disk)
    try:
        with open(os.path.join(project_root, trigger_file), "r", encoding="utf-8", errors="replace") as fh:
            trigger_src = fh.read()
    except OSError:
        return []

    try:
        new_src, _ = engine.apply_transform(
            trigger_src, "rename_symbol",
            {"old_name": old_name, "new_name": new_name},
        )
    except Exception as e:
        logger.warning(f"Objective rename failed on trigger file: {e}")
        return []

    if new_src != trigger_src:
        changes.append({"file_path": trigger_file, "content": new_src})

    # 2. Propagate to every affected file that still references the old symbol
    for path, content in affected_files.items():
        if path == trigger_file:
            continue
        try:
            new_content, _ = engine.apply_transform(
                content, "rename_symbol",
                {"old_name": old_name, "new_name": new_name},
            )
        except Exception as e:
            logger.warning(f"Objective rename failed in {path}: {e}")
            continue
        if new_content != content:
            changes.append({"file_path": path, "content": new_content})

    return changes


def propagate_renames(changes: List[Dict[str, str]], project_root: str,
                      affected_files: Dict[str, str]) -> List[Dict[str, str]]:
    """
    Graph-native guarantee: deterministically apply every symbol rename to all
    affected files the LLM missed. Rename pairs are inferred from the changed
    files (removed symbol -> most-similar added symbol), then applied to every
    other affected file via the semantic DiffEngine.

    Returns the original changes plus any propagation-driven additions.
    """
    from app.core.diff_engine import DiffEngine

    engine = DiffEngine()
    already = {c["file_path"] for c in changes}

    # 1. Infer rename pairs from changed files (old content on disk vs new)
    pairs = []
    for change in changes:
        old_path = os.path.join(project_root, change["file_path"])
        if not os.path.isfile(old_path):
            continue
        try:
            with open(old_path, "r", encoding="utf-8", errors="replace") as fh:
                old_src = fh.read()
        except OSError:
            continue
        old_syms = defined_symbols(old_src)
        new_syms = defined_symbols(change["content"])
        removed = old_syms - new_syms
        added = new_syms - old_syms
        for old in sorted(removed):
            best, best_score = None, -1.0
            for new in sorted(added):
                score = difflib.SequenceMatcher(None, old, new).ratio()
                if score > best_score:
                    best, best_score = new, score
            if best and best_score > 0.3:
                pairs.append((old, best))

    if not pairs:
        return changes

    logger.info(f"Propagating renames across affected files: {pairs}")

    # 2. Apply each pair to every affected file not already changed
    extra = []
    for path, content in affected_files.items():
        if path in already:
            continue
        new_content = content
        for old, new in pairs:
            try:
                new_content, _ = engine.apply_transform(
                    new_content, "rename_symbol",
                    {"old_name": old, "new_name": new},
                )
            except Exception as e:
                logger.warning(f"Propagation rename failed in {path} ({old}->{new}): {e}")
        if new_content != content:
            extra.append({"file_path": path, "content": new_content})

    return changes + extra
