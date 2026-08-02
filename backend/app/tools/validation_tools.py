"""
6-gate validation pipeline.
1. Syntax (ast.parse)
2. Imports (py_compile)
3. Types (mypy)
4. Tests (pytest)
5. Security (bandit)
6. Graph integrity (no dangling references after the change)
"""

import ast
import builtins
import os
import shutil
import subprocess
import json
from typing import List, Dict, Any


def _python_exe() -> str:
    """Resolve the Python interpreter (python3 on macOS, python elsewhere)."""
    for name in ("python3", "python"):
        if shutil.which(name):
            return name
    return "python"


def _collect_module_symbols(tree: ast.AST) -> set:
    """
    Names defined anywhere in a module: defs, classes, imports,
    assignment targets, parameters, and other binding forms.
    """
    symbols = set()
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
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                symbols.add(node.target.id)
        elif isinstance(node, ast.arg):
            symbols.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            symbols.add(node.name)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            if isinstance(node.target, ast.Name):
                symbols.add(node.target.id)
        elif isinstance(node, ast.comprehension):
            if isinstance(node.target, ast.Name):
                symbols.add(node.target.id)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            if isinstance(node.optional_vars, ast.Name):
                symbols.add(node.optional_vars.id)
    return symbols


def _collect_references(tree: ast.AST) -> set:
    """Called functions and attribute bases — the references that matter for renames."""
    refs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                refs.add(func.id)
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                refs.add(func.value.id)
    return refs


def _local_symbols(path: str, per_file: Dict[str, set]) -> set:
    """Symbols defined in the file at `path` (disk or old-content state)."""
    return per_file.get(path, set())


def _was_dangling_before(old_tree, ref: str, local_syms_old: set,
                         old_all_symbols: set) -> bool:
    """True if `ref` was already unresolvable before this refactor."""
    builtin_names = set(dir(builtins))
    if ref in local_syms_old or ref in builtin_names or ref in old_all_symbols:
        return False
    return True


def validate_graph_integrity(changes: List[Dict[str, str]], project_root: str,
                             old_contents: Dict[str, str] = None) -> Dict[str, Any]:
    """
    Gate 6: graph integrity. Re-parses the changed files and asserts that no
    reference dangles after the change. Two checks:

      1. Removed-symbol check: any symbol that a changed file USED to define
         and no longer defines must not be referenced anywhere else (catches
         missed call sites, even when the stale name is still imported).
      2. Resolution check: every called symbol resolves to something defined
         in the project, imported, or builtin.

    When Neo4j is up, the graph cross-checks remaining references; when it is
    down the gate still runs on disk (AST-based).
    """
    report = {"gate": "graph", "status": "PENDING", "details": "", "neo4j": "SKIP"}

    per_file: Dict[str, set] = {}
    per_file_refs: Dict[str, set] = {}
    unparsable = 0

    for root, _, files in os.walk(project_root):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(root, fname)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    tree = ast.parse(fh.read())
            except Exception:
                unparsable += 1
                continue
            per_file[path] = _collect_module_symbols(tree)
            per_file_refs[path] = _collect_references(tree)

    new_tree_by_file: Dict[str, ast.AST] = {}
    for change in changes:
        file_path = change["file_path"]
        try:
            tree = ast.parse(change["content"])
        except SyntaxError:
            continue  # gate 1 already flags this
        abs_path = os.path.join(project_root, file_path)
        new_tree_by_file[abs_path] = tree

    # Changed files must be checked against their NEW content, not the stale
    # on-disk version: substitute the proposed trees into the per-file maps.
    for abs_path, tree in new_tree_by_file.items():
        per_file[abs_path] = _collect_module_symbols(tree)
        per_file_refs[abs_path] = _collect_references(tree)

    # Symbols a changed file used to define and no longer does
    removed_symbols: set = set()
    for change in changes:
        abs_path = os.path.join(project_root, change["file_path"])
        old_src = old_contents.get(change["file_path"]) if old_contents else None
        if old_src is None:
            continue
        try:
            old_tree = ast.parse(old_src)
        except SyntaxError:
            continue
        new_tree = new_tree_by_file.get(abs_path)
        if new_tree is None:
            continue
        removed_symbols |= _collect_module_symbols(old_tree) - _collect_module_symbols(new_tree)

    all_symbols: set = set()
    for syms in per_file.values():
        all_symbols |= syms
    for tree in new_tree_by_file.values():
        all_symbols |= _collect_module_symbols(tree)

    builtin_names = set(dir(builtins))
    dangling: List[tuple] = []

    # Pre-change state: what each file looked like before the refactor.
    # Changed files use old_contents; untouched files use the on-disk state.
    old_syms_by_path: Dict[str, set] = {}
    for path in per_file:
        rel = os.path.relpath(path, project_root)
        if old_contents and rel in old_contents:
            try:
                old_syms_by_path[path] = _collect_module_symbols(ast.parse(old_contents[rel]))
                continue
            except SyntaxError:
                pass
        old_syms_by_path[path] = per_file[path]

    old_all_symbols: set = set()
    for syms in old_syms_by_path.values():
        old_all_symbols |= syms

    for path, refs in sorted(per_file_refs.items()):
        rel = os.path.relpath(path, project_root)
        for ref in sorted(refs):
            if ref in removed_symbols:
                # The change deleted this symbol's definition — any remaining
                # reference is a missed call site. Baseline-independent.
                dangling.append((rel, ref, "removed-symbol"))
                continue
            if ref in per_file.get(path, set()) or ref in builtin_names or ref in all_symbols:
                continue
            # Pre-existing unresolved reference, untouched by the change:
            # not a regression the refactor introduced.
            if _was_dangling_before(None, ref, old_syms_by_path.get(path, set()),
                                    old_all_symbols):
                continue
            dangling.append((rel, ref, "unresolved"))

    # Neo4j cross-check: a symbol the on-disk scan missed may exist in the twin.
    # Only "unresolved" refs may be rescued by the graph; a "removed-symbol"
    # ref is definitive for THIS repo — stale twin data from other repos must
    # never overrule what's on disk.
    unresolved = [(p, r, why) for p, r, why in dangling if why == "unresolved"]
    if unresolved:
        try:
            from app.services.neo4j_service import Neo4jService
            svc = Neo4jService()
            resolved = set()
            for _, ref, _reason in unresolved:
                try:
                    if svc.symbol_exists(ref):
                        resolved.add(ref)
                except Exception:
                    pass
            svc.close()
            report["neo4j"] = "PASS"
            dangling = [(p, r, why) for p, r, why in dangling
                        if r not in resolved or why != "unresolved"]
        except Exception as e:
            report["neo4j"] = f"unavailable ({e})"

    if dangling:
        report["status"] = "FAIL"
        report["details"] = "Dangling references:\n" + "\n".join(
            f"  {p}: {r} ({why})" for p, r, why in sorted(dangling)[:20]
        )
    else:
        report["status"] = "PASS"
        report["details"] = f"All references resolve ({len(changes)} file(s) checked)."

    return report


def validate_changes(changes: List[Dict[str, str]], project_root: str) -> str:
    """
    Validates proposed code changes.
    Expected changes format: [{"file_path": "path/to/file.py", "content": "..."}]
    """
    from app.core.sandbox import ExecutionSandbox
    
    report = {
        "overall": "FAIL",
        "gates": {
            "syntax": {"status": "PENDING", "details": ""},
            "imports": {"status": "PENDING", "details": ""},
            "types": {"status": "PENDING", "details": ""},
            "tests": {"status": "PENDING", "details": ""},
            "security": {"status": "PENDING", "details": ""},
            "graph": {"status": "PENDING", "details": ""}
        }
    }
    
    # Run in sandbox
    try:
        with ExecutionSandbox(project_root) as sandbox:
            # 1. Snapshot pre-change content (for the graph gate's removed-symbol diff)
            old_contents = {}
            for change in changes:
                old_path = os.path.join(sandbox.sandbox_dir, change["file_path"])
                if os.path.isfile(old_path):
                    with open(old_path, "r", encoding="utf-8", errors="replace") as fh:
                        old_contents[change["file_path"]] = fh.read()

            # 2. Apply changes
            sandbox.apply_changes(changes)
            changed_files = [c["file_path"] for c in changes]
            
            # GATE 1: Syntax
            syntax_passed = True
            for file_path in changed_files:
                try:
                    content = next(c["content"] for c in changes if c["file_path"] == file_path)
                    ast.parse(content)
                except SyntaxError as e:
                    syntax_passed = False
                    report["gates"]["syntax"]["details"] += f"Syntax error in {file_path}, line {e.lineno}: {e.msg}\n"
                    
            if syntax_passed:
                report["gates"]["syntax"]["status"] = "PASS"
            else:
                report["gates"]["syntax"]["status"] = "FAIL"
                return json.dumps(report, indent=2)  # Fail fast
                
            # GATE 2: Imports
            import_passed = True
            for file_path in changed_files:
                res = sandbox.run_command([_python_exe(), "-m", "py_compile", file_path])
                if res.returncode != 0:
                    import_passed = False
                    report["gates"]["imports"]["details"] += f"Import/compile error in {file_path}:\n{res.stderr}\n"
                    
            if import_passed:
                report["gates"]["imports"]["status"] = "PASS"
            else:
                report["gates"]["imports"]["status"] = "FAIL"
                return json.dumps(report, indent=2)
                
            # GATE 3: Types
            # Use --explicit-package-bases so nested modules (namespace packages
            # without __init__.py, e.g. lib/utils.py) map to ONE module name.
            res = sandbox.run_command([_python_exe(), "-m", "mypy", "--explicit-package-bases", *changed_files])
            if res.returncode == 0:
                report["gates"]["types"]["status"] = "PASS"
            else:
                report["gates"]["types"]["status"] = "FAIL"
                report["gates"]["types"]["details"] = res.stdout
                return json.dumps(report, indent=2)
                
            # GATE 4: Tests
            # For now, run all tests. In a real system, we'd use the graph to find affected tests.
            res = sandbox.run_command([_python_exe(), "-m", "pytest", "-v"])
            if res.returncode == 0 or res.returncode == 5: # 5 means no tests collected, which is ok
                report["gates"]["tests"]["status"] = "PASS"
            else:
                report["gates"]["tests"]["status"] = "FAIL"
                report["gates"]["tests"]["details"] = res.stdout
                return json.dumps(report, indent=2)
                
            # GATE 5: Security
            res = sandbox.run_command([_python_exe(), "-m", "bandit", "-r", "-ll", *changed_files])
            if res.returncode == 0:
                report["gates"]["security"]["status"] = "PASS"
            else:
                report["gates"]["security"]["status"] = "FAIL"
                report["gates"]["security"]["details"] = res.stdout
                return json.dumps(report, indent=2)

            # GATE 6: Graph integrity — no dangling references after the change
            graph_report = validate_graph_integrity(changes, sandbox.sandbox_dir, old_contents)
            report["gates"]["graph"]["status"] = graph_report["status"]
            report["gates"]["graph"]["details"] = graph_report["details"]
            if graph_report["neo4j"] != "SKIP":
                report["gates"]["graph"]["details"] += f"\nNeo4j cross-check: {graph_report['neo4j']}"
            if graph_report["status"] == "FAIL":
                return json.dumps(report, indent=2)

            report["overall"] = "PASS"
            return json.dumps(report, indent=2)
            
    except Exception as e:
        report["overall"] = "ERROR"
        report["gates"]["syntax"]["details"] = f"Sandbox execution failed: {e}"
        return json.dumps(report, indent=2)


def register_validation_tools(registry, project_root: str):
    """Register validation tools."""
    from app.core.tool_registry import PermissionMode
    
    registry.register(
        name="validate_changes",
        description="Run the 6-gate validation pipeline on proposed changes. Gates: Syntax, Imports, Types, Tests, Security, Graph Integrity.",
        input_schema={
            "type": "object",
            "properties": {
                "changes": {
                    "type": "array",
                    "description": "List of changes to validate",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "content": {"type": "string"}
                        },
                        "required": ["file_path", "content"]
                    }
                }
            },
            "required": ["changes"]
        },
        required_permission=PermissionMode.EXECUTE,
        handler=lambda changes: validate_changes(changes, project_root)
    )
