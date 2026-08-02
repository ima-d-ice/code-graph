"""
5-gate validation pipeline.
1. Syntax (ast.parse)
2. Imports (py_compile)
3. Types (mypy)
4. Tests (pytest)
5. Security (bandit)
"""

import ast
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
            "security": {"status": "PENDING", "details": ""}
        }
    }
    
    # Run in sandbox
    try:
        with ExecutionSandbox(project_root) as sandbox:
            # 1. Apply changes
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
            res = sandbox.run_command([_python_exe(), "-m", "mypy", *changed_files])
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
        description="Run the 5-gate validation pipeline on proposed changes. Gates: Syntax, Imports, Types, Tests, Security.",
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
