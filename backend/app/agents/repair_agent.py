import json
import logging
from typing import Dict, Any, List


from app.core.agent_loop import AgentLoop
from app.core.tool_registry import ToolRegistry, PermissionMode
from app.core.llm_router import LLMRouter
from app.tools.file_tools import register_file_tools
from app.tools.graph_tools import register_graph_tools


logger = logging.getLogger(__name__)


class RepairAgent:
    """
    Takes validation failure feedback and attempts to fix the code.
    Write access (EXECUTE permission).
    """

    def __init__(self, project_root: str):
        self.project_root = project_root

        self.router = LLMRouter()
        self.registry = ToolRegistry()

        register_file_tools(self.registry, project_root)
        register_graph_tools(self.registry)

        self.loop = AgentLoop(self.router, self.registry, max_turns=15)

    async def run(self, proposed_changes: List[Dict[str, str]],
                  validation_report: Dict[str, Any],
                  affected_files: Dict[str, str]) -> List[Dict[str, str]]:
        """Attempt to fix errors based on validation report."""

        # Build context
        context_str = "PREVIOUS PROPOSED CHANGES:\n"
        for change in proposed_changes:
            context_str += f"--- {change['file_path']} ---\n{change['content']}\n\n"

        context_str += f"VALIDATION ERRORS:\n{json.dumps(validation_report, indent=2)}\n\n"

        context_str += "ORIGINAL AFFECTED FILES:\n"
        for path, content in affected_files.items():
            context_str += f"--- {path} ---\n{content}\n\n"

        system_prompt = """You are an expert repair agent.
The previous code changes failed validation. You must fix the errors based on the validation report.
You can use the edit_file tool, or just return the fixed file contents in the XML-like format:

<changes>
  <file path="path/to/file.py">
    <![CDATA[
    ... full fixed file content ...
    ]]>
  </file>
</changes>

Fix ONLY what is necessary to pass validation. Do not introduce new features.
"""
        user_prompt = context_str

        result = await self.loop.run(system_prompt, user_prompt, permission_mode=PermissionMode.EXECUTE)

        # Use same parser as ExecutorAgent
        from app.agents.executor_agent import ExecutorAgent
        executor = ExecutorAgent(self.project_root)
        return executor._parse_changes(result.get("result", ""))
