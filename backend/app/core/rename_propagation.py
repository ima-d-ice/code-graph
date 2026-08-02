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


def parse_remove_objective(objective: str) -> str:
    """
    Deterministically extract a dead-code removal target from an objective
    like "Remove the dead function check_threshold_1 from callers/caller_001.py"
    or "Remove dead code compute_legacy".

    Returns the symbol name or "".
    """
    match = re.search(
        r"remove\s+(?:the\s+)?(?:dead\s+|unused\s+|unreferenced\s+|orphaned\s+)?"
        r"(?:code\s*:?\s*|function|method|class|symbol|variable)?\s*"
        r"([A-Za-z_][A-Za-z0-9_]*)",
        objective, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def _symbol_referenced(source: str, symbol: str,
                       ignore_def_lines: set = None) -> bool:
    """True if `symbol` is used anywhere outside its own definition block."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return True  # be conservative: can't verify, don't remove
    if ignore_def_lines is None:
        ignore_def_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == symbol:
            if node.lineno not in ignore_def_lines:
                return True
    return False


def apply_objective_removal(objective: str, trigger_file: str, project_root: str,
                            affected_files: Dict[str, str]) -> List[Dict[str, str]]:
    """
    Deterministic dead-code removal fallback: when the executor fails to
    produce usable changes, remove the orphaned symbol mechanically via AST.

    Conservative by design:
      - the symbol must be defined in the trigger file,
      - nothing else in the trigger file may reference it,
      - no affected file may reference it (the graph gate is the backstop).

    Returns a complete changes list or [] if the objective is not a removal.
    """
    symbol = parse_remove_objective(objective)
    if not symbol:
        return []

    try:
        with open(os.path.join(project_root, trigger_file), "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError:
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    target = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
            target = node
            break
    if target is None:
        return []

    if _symbol_referenced(
        source, symbol, ignore_def_lines=set(range(target.lineno, target.end_lineno + 1))
    ):
        logger.warning(f"Objective removal blocked: {symbol} still referenced in {trigger_file}")
        return []

    for path, content in affected_files.items():
        if path == trigger_file:
            continue
        if re.search(rf"\b{re.escape(symbol)}\b", content):
            logger.warning(f"Objective removal blocked: {symbol} referenced in {path}")
            return []

    # Slice out the def block (decorators included: lineno starts at them)
    lines = source.splitlines(keepends=True)
    del lines[target.lineno - 1:target.end_lineno]

    # Consume the blank lines that separated the block from its neighbors
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines and lines[-1].strip() == "":
        lines.pop()

    new_src = "".join(lines)

    # Collapse runs of 3+ blank lines down to 2 (PEP8 top-level spacing)
    new_src = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n\n", new_src)

    if new_src.strip() == "":
        new_src = "\n"

    try:
        verify = ast.parse(new_src)
    except SyntaxError as e:
        logger.warning(f"Objective removal produced invalid source: {e}")
        return []

    if symbol in defined_symbols(new_src):
        logger.warning(f"Objective removal failed: {symbol} still defined after edit")
        return []

    return [{"file_path": trigger_file, "content": new_src}]


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
