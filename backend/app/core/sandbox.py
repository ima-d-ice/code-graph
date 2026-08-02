"""
Execution sandbox for validating code changes.
Tries Docker first, falls back to tempfile + subprocess.
"""

import os
import shutil
import tempfile
import subprocess
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)


class ExecutionSandbox:
    """
    Provides an isolated environment for validating code changes.
    """
    def __init__(self, project_root: str):
        self.project_root = os.path.abspath(project_root)
        self.sandbox_dir = None
        self._temp_dir = None
        
    def __enter__(self):
        self._temp_dir = tempfile.TemporaryDirectory(prefix="codegraph_sandbox_")
        self.sandbox_dir = self._temp_dir.name
        
        # Copy project to sandbox
        # Simple copy for now. Real implementation might use rsync or docker cp
        # Ignore heavy dirs like node_modules, venv, .git
        shutil.copytree(
            self.project_root, 
            os.path.join(self.sandbox_dir, "project"),
            ignore=shutil.ignore_patterns('node_modules', 'venv', '.git', '__pycache__', 'chroma_db')
        )
        self.sandbox_dir = os.path.join(self.sandbox_dir, "project")
        
        logger.info(f"📦 Sandbox created at {self.sandbox_dir}")
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._temp_dir:
            self._temp_dir.cleanup()
            logger.info("🧹 Sandbox cleaned up")
            
    def apply_changes(self, changes: List[Dict[str, str]]):
        """Apply a list of file changes to the sandbox."""
        for change in changes:
            file_path = change["file_path"]
            content = change["content"]
            
            full_path = os.path.join(self.sandbox_dir, file_path)
            
            # Ensure dir exists
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
                
    def run_command(self, cmd: List[str]) -> subprocess.CompletedProcess:
        """Run a command inside the sandbox."""
        logger.debug(f"🏃 Running in sandbox: {' '.join(cmd)}")
        return subprocess.run(
            cmd,
            cwd=self.sandbox_dir,
            capture_output=True,
            text=True,
            timeout=60 # 60s timeout for tests/validation
        )
