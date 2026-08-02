"""
Sandboxed shell command execution tool.
"""

import os
import subprocess
from typing import List
import shlex

ALLOWED_COMMANDS = {
    "git", "python", "pytest", "mypy", "bandit", "pip", 
    "ls", "cat", "head", "tail", "wc", "find", "grep"
}

def execute_bash(command: str, project_root: str) -> str:
    """Execute a shell command with restrictions."""
    try:
        # Parse command safely
        parts = shlex.split(command)
        if not parts:
            return "Error: Empty command"
            
        base_cmd = parts[0]
        if base_cmd not in ALLOWED_COMMANDS:
            return f"Error: Command '{base_cmd}' is not allowed. Allowed commands: {', '.join(ALLOWED_COMMANDS)}"
            
        # Execute
        result = subprocess.run(
            parts,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        output = []
        if result.stdout:
            output.append("STDOUT:\n" + result.stdout)
        if result.stderr:
            output.append("STDERR:\n" + result.stderr)
            
        if not output:
            return f"Command executed successfully (exit code {result.returncode}) with no output."
            
        out_str = "\n".join(output)
        
        # Truncate if too long
        if len(out_str) > 5000:
            out_str = out_str[:5000] + "\n... (output truncated)"
            
        return f"Exit code {result.returncode}\n{out_str}"
        
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 30 seconds."
    except Exception as e:
        return f"Error executing command: {e}"


def register_bash_tool(registry, project_root: str):
    """Register bash tool."""
    from app.core.tool_registry import PermissionMode
    
    registry.register(
        name="bash",
        description="Run a shell command in the project root. Allowed: git, python, pytest, mypy, bandit, ls, grep, etc.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run"}
            },
            "required": ["command"]
        },
        required_permission=PermissionMode.EXECUTE,
        handler=lambda command: execute_bash(command, project_root),
        requires_approval=True
    )
