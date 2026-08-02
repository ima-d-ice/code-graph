"""
File operation tools for the agent.
Includes reading, globbing, grepping, and writing.
Editing is handled by the AST-aware diff engine, but exposed here as a tool.
"""

import os
import glob
import re
from typing import Dict, Any, List
import logging

logger = logging.getLogger(__name__)


def read_file(file_path: str, project_root: str) -> str:
    """Read file content with line numbers."""
    full_path = os.path.abspath(os.path.join(project_root, file_path))
    if not full_path.startswith(os.path.abspath(project_root)):
        return "Error: Access denied. Cannot read outside project root."
        
    if not os.path.exists(full_path):
        return f"Error: File {file_path} does not exist."
        
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        result = []
        for i, line in enumerate(lines, 1):
            if i > 200:
                result.append(f"... ({len(lines) - 200} more lines truncated)")
                break
            result.append(f"{i:4d} | {line.rstrip('\n')}")

        return "\n".join(result)
    except Exception as e:
        return f"Error reading file: {e}"


def write_file(file_path: str, content: str, project_root: str) -> str:
    """Create a new file with the given content."""
    full_path = os.path.abspath(os.path.join(project_root, file_path))
    if not full_path.startswith(os.path.abspath(project_root)):
        return "Error: Access denied. Cannot write outside project root."
        
    # Create directories if they don't exist
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
        
    try:
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} characters to {file_path}"
    except Exception as e:
        return f"Error writing file: {e}"


def glob_search(pattern: str, project_root: str) -> str:
    """Find files matching a glob pattern."""
    full_pattern = os.path.join(project_root, pattern)
    try:
        # Using glob with recursive=True
        matches = glob.glob(full_pattern, recursive=True)
        
        # Convert to relative paths
        rel_matches = [os.path.relpath(m, project_root) for m in matches if os.path.isfile(m)]
        
        if not rel_matches:
            return "No files found matching pattern."
            
        return "\n".join(sorted(rel_matches))
    except Exception as e:
        return f"Error in glob search: {e}"


def grep_search(pattern: str, path: str, project_root: str) -> str:
    """Search for text in files (simple grep)."""
    search_path = os.path.abspath(os.path.join(project_root, path))
    if not search_path.startswith(os.path.abspath(project_root)):
        return "Error: Access denied. Cannot search outside project root."
        
    try:
        regex = re.compile(pattern)
        results = []
        
        if os.path.isfile(search_path):
            files_to_check = [search_path]
        else:
            files_to_check = []
            for root, _, files in os.walk(search_path):
                for f in files:
                    # Skip hidden dirs and common ignores
                    if ".git" in root or "__pycache__" in root or "node_modules" in root:
                        continue
                    files_to_check.append(os.path.join(root, f))
                    
        for filepath in files_to_check:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if regex.search(line):
                            rel_path = os.path.relpath(filepath, project_root)
                            results.append(f"{rel_path}:{i}: {line.strip()}")
            except UnicodeDecodeError:
                # Skip binary files
                pass
                
        if not results:
            return "No matches found."
            
        # Limit results if too many
        if len(results) > 100:
            return "\n".join(results[:100]) + f"\n... and {len(results)-100} more matches."
            
        return "\n".join(results)
    except Exception as e:
        return f"Error in grep search: {e}"


def edit_file(file_path: str, transform: str, args: Dict[str, Any], project_root: str) -> str:
    """
    Apply an AST-aware transformation to a file.
    This delegates to the diff engine (Phase 3).
    """
    from app.core.diff_engine import DiffEngine
    
    full_path = os.path.abspath(os.path.join(project_root, file_path))
    if not full_path.startswith(os.path.abspath(project_root)):
        return "Error: Access denied. Cannot edit outside project root."
        
    if not os.path.exists(full_path):
        return f"Error: File {file_path} does not exist."
        
    engine = DiffEngine()
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            source = f.read()
            
        new_source, diff = engine.apply_transform(source, transform, args)
        
        # In a real environment, we'd write back or return the diff to the sandbox.
        # For the agent's memory, we return the unified diff.
        return f"Successfully applied {transform} to {file_path}.\n\nDiff:\n{diff}"
    except Exception as e:
        return f"Error applying transform '{transform}': {e}"


def register_file_tools(registry, project_root: str, write_access: bool = True):
    """Register all file tools with the tool registry.

    write_access=False registers only read-only tools (read_file, glob, grep)
    so agents in the VALIDATE-before-COMMIT pipeline cannot mutate the real
    project mid-loop — writes happen only in the COMMIT node.
    """
    from app.core.tool_registry import PermissionMode
    
    registry.register(
        name="read_file",
        description="Read the contents of a file. Lines are numbered.",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path relative to project root"}
            },
            "required": ["file_path"]
        },
        required_permission=PermissionMode.PLAN,
        handler=lambda file_path: read_file(file_path, project_root)
    )
    
    registry.register(
        name="write_file",
        description="Create a new file with the given content.",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path relative to project root"},
                "content": {"type": "string", "description": "Full file content"}
            },
            "required": ["file_path", "content"]
        },
        required_permission=PermissionMode.EXECUTE,
        handler=lambda file_path, content: write_file(file_path, content, project_root)
        if write_access
        else "write_file is disabled in the validation pipeline — return changes to COMMIT instead."
    )
    
    registry.register(
        name="glob",
        description="Find files matching a glob pattern (e.g. '**/*.py').",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"}
            },
            "required": ["pattern"]
        },
        required_permission=PermissionMode.PLAN,
        handler=lambda pattern: glob_search(pattern, project_root)
    )
    
    registry.register(
        name="grep",
        description="Search for a regex pattern in files.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern"},
                "path": {"type": "string", "description": "Directory or file to search in. Use '.' for project root"}
            },
            "required": ["pattern", "path"]
        },
        required_permission=PermissionMode.PLAN,
        handler=lambda pattern, path: grep_search(pattern, path, project_root)
    )
    
    registry.register(
        name="edit_file",
        description="Apply an AST-aware semantic edit to a file. Transforms: rename_symbol, extract_method.",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path relative to project root"},
                "transform": {"type": "string", "description": "Type of transform (e.g., 'rename_symbol')"},
                "args": {"type": "object", "description": "Arguments for the transform"}
            },
            "required": ["file_path", "transform", "args"]
        },
        required_permission=PermissionMode.EXECUTE,
        handler=lambda file_path, transform, args: edit_file(file_path, transform, args, project_root)
        if write_access
        else "edit_file is disabled in the validation pipeline — return changes to COMMIT instead."
    )
