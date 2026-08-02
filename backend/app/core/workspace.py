import json
import os

WORKSPACE_FILE = "workspace.json"

def get_project_root() -> str:
    """Read the workspace project root from workspace.json."""
    if not os.path.exists(WORKSPACE_FILE):
        raise RuntimeError("workspace.json not found. Run ingest.py first.")

    with open(WORKSPACE_FILE, "r") as f:
        data = json.load(f)

    project_root = data.get("project_root")

    if not project_root:
        raise RuntimeError("project_root missing in workspace.json. Re-run ingest.py")

    project_root = os.path.abspath(project_root)

    if not os.path.exists(project_root):
        raise RuntimeError(f"Project root does not exist: {project_root}")

    return project_root
