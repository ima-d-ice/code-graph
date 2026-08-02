"""
AST-aware Diff Engine.

Applies semantic transformations to Python code, preserving comments and formatting.

Priority:
1. libcst (if installed) — true round-trip AST preserving all formatting
2. ast + tokenize (fallback) — safer than naive string replacement
3. Naive string replacement (last resort) — with safety checks

Supported transforms:
- rename_symbol: Rename a function/class/variable across a file
- extract_method: Extract a code block into a new method (placeholder)
- inline_variable: Replace a variable with its value (placeholder)
"""

import ast
import difflib
import logging
import tokenize
import io
from typing import Dict, Any, Tuple, List

logger = logging.getLogger(__name__)

# Try to import libcst for production-quality transforms
_LIBCST_AVAILABLE = False
try:
    import libcst as cst
    _LIBCST_AVAILABLE = True
    logger.info("✅ libcst loaded — using production-grade AST transforms")
except ImportError:
    logger.info("⚠️ libcst not installed — using ast+tokenize fallback for AST-aware transforms")


class DiffEngine:
    """
    Applies structural transformations to code.
    """

    def apply_transform(self, source: str, transform: str, args: Dict[str, Any]) -> Tuple[str, str]:
        """
        Apply a transform and return (new_source, unified_diff).

        Args:
            source: Original Python source code
            transform: Transform type ("rename_symbol", "extract_method", etc.)
            args: Transform arguments

        Returns:
            (new_source, unified_diff_string)
        """
        if _LIBCST_AVAILABLE:
            return self._apply_with_libcst(source, transform, args)
        else:
            return self._apply_with_ast_tokenize(source, transform, args)

    # ─────────────────────────────────────────
    # libcst-based transforms (production)
    # ─────────────────────────────────────────

    def _apply_with_libcst(self, source: str, transform: str, args: Dict[str, Any]) -> Tuple[str, str]:
        """Use libcst for true round-trip AST transforms."""
        if transform == "rename_symbol":
            return self._libcst_rename(source, args.get("old_name"), args.get("new_name"))
        elif transform == "extract_method":
            raise NotImplementedError("extract_method requires line range selection — use LLM generation instead")
        else:
            raise ValueError(f"Unknown transform: {transform}")

    def _libcst_rename(self, source: str, old_name: str, new_name: str) -> Tuple[str, str]:
        """Rename a symbol using libcst."""
        if not old_name or not new_name:
            raise ValueError("old_name and new_name are required")

        module = cst.parse_module(source)

        class RenameTransformer(cst.CSTTransformer):
            def __init__(self, old: str, new: str):
                self.old = old
                self.new = new

            def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.BaseExpression:
                if updated_node.value == self.old:
                    return updated_node.with_changes(value=self.new)
                return updated_node

            def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.BaseStatement:
                if updated_node.name.value == self.old:
                    return updated_node.with_changes(
                        name=updated_node.name.with_changes(value=self.new)
                    )
                return updated_node

            def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.BaseStatement:
                if updated_node.name.value == self.old:
                    return updated_node.with_changes(
                        name=updated_node.name.with_changes(value=self.new)
                    )
                return updated_node

            def leave_Attribute(self, original_node: cst.Attribute, updated_node: cst.Attribute) -> cst.BaseExpression:
                if updated_node.attr.value == self.old:
                    return updated_node.with_changes(
                        attr=updated_node.attr.with_changes(value=self.new)
                    )
                return updated_node

        transformer = RenameTransformer(old_name, new_name)
        new_module = module.visit(transformer)
        new_source = new_module.code

        # Verify round-trip
        try:
            ast.parse(new_source)
        except SyntaxError as e:
            raise RuntimeError(f"libcst rename produced invalid syntax: {e}")

        diff = self._make_diff(source, new_source)
        return new_source, diff

    # ─────────────────────────────────────────
    # ast + tokenize fallback (safe, no libcst)
    # ─────────────────────────────────────────

    def _apply_with_ast_tokenize(self, source: str, transform: str, args: Dict[str, Any]) -> Tuple[str, str]:
        """Use ast + tokenize for safer-than-naive transforms."""
        if transform == "rename_symbol":
            return self._tokenize_rename(source, args.get("old_name"), args.get("new_name"))
        elif transform == "extract_method":
            logger.warning("extract_method not implemented in fallback diff engine")
            return source, ""
        else:
            raise ValueError(f"Unknown transform: {transform}")

    def _tokenize_rename(self, source: str, old_name: str, new_name: str) -> Tuple[str, str]:
        """
        Rename a symbol using Python's tokenize module.
        Only replaces NAME tokens that match old_name, avoiding string literals and comments.
        """
        if not old_name or not new_name:
            raise ValueError("old_name and new_name are required")

        # First, parse AST to find all legitimate occurrences
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            raise RuntimeError(f"Cannot parse source: {e}")

        # Collect all line numbers where old_name appears as a real identifier
        # We use tokenize to be precise about which tokens to replace
        lines = source.split("\n")
        new_lines = list(lines)

        # Track which (line, col) positions contain the old_name as an identifier
        rename_positions: List[Tuple[int, int, int]] = []  # (line_idx, start_col, end_col)

        try:
            tokens = tokenize.generate_tokens(io.StringIO(source).readline)
            for tok in tokens:
                if tok.type == tokenize.NAME and tok.string == old_name:
                    # line_idx is 0-based, tok.start[0] is 1-based
                    line_idx = tok.start[0] - 1
                    start_col = tok.start[1]
                    end_col = tok.end[1]
                    rename_positions.append((line_idx, start_col, end_col))
        except tokenize.TokenizeError as e:
            logger.warning(f"Tokenize error during rename: {e}")
            # Fall through to naive approach
            return self._naive_rename(source, old_name, new_name)

        # Apply replacements from bottom-right to top-left to preserve positions
        rename_positions.sort(key=lambda x: (x[0], x[1]), reverse=True)

        for line_idx, start_col, end_col in rename_positions:
            line = new_lines[line_idx]
            # Verify the substring matches
            if line[start_col:end_col] == old_name:
                new_lines[line_idx] = line[:start_col] + new_name + line[end_col:]

        new_source = "\n".join(new_lines)

        # Verify round-trip
        try:
            ast.parse(new_source)
        except SyntaxError as e:
            raise RuntimeError(f"Tokenize rename produced invalid syntax: {e}")

        diff = self._make_diff(source, new_source)
        return new_source, diff

    def _naive_rename(self, source: str, old_name: str, new_name: str) -> Tuple[str, str]:
        """Last resort: naive string replacement with safety checks."""
        logger.warning(f"Using naive rename for {old_name} -> {new_name}")

        lines = source.split("\n")
        new_lines = []

        for line in lines:
            # Only replace standalone identifiers, not substrings inside other words
            import re
            # Use word boundary regex
            new_line = re.sub(rf'\b{re.escape(old_name)}\b', new_name, line)
            new_lines.append(new_line)

        new_source = "\n".join(new_lines)

        try:
            ast.parse(new_source)
        except SyntaxError as e:
            raise RuntimeError(f"Naive rename produced invalid syntax: {e}")

        diff = self._make_diff(source, new_source)
        return new_source, diff

    # ─────────────────────────────────────────
    # Utilities
    # ─────────────────────────────────────────

    def _make_diff(self, old: str, new: str, filename: str = "file.py") -> str:
        """Generate a unified diff."""
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)

        # Ensure both end with newline for clean diff
        if old_lines and not old_lines[-1].endswith("\n"):
            old_lines[-1] += "\n"
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"

        diff = list(difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            lineterm=""
        ))

        return "".join(diff)
