import json
import logging
import re
from typing import Dict, Any, List


from app.core.agent_loop import AgentLoop
from app.core.tool_registry import ToolRegistry, PermissionMode
from app.core.llm_router import LLMRouter
from app.tools.file_tools import register_file_tools
from app.tools.graph_tools import register_graph_tools


logger = logging.getLogger(__name__)


class ExecutorAgent:
    """
    Generates code changes based on the refactoring plan.
    Write access (EXECUTE permission).
    """

    def __init__(self, project_root: str):
        self.project_root = project_root

        self.router = LLMRouter()
        self.registry = ToolRegistry()

        register_file_tools(self.registry, project_root, write_access=False)
        register_graph_tools(self.registry, self.project_root)

        self.loop = AgentLoop(self.router, self.registry, max_turns=15)

    async def run(self, objective: str, plan: Dict[str, Any],
                  affected_files: Dict[str, str],
                  graph_context: Dict[str, Any]) -> List[Dict[str, str]]:
        """Generate code changes based on the plan."""

        # Build context (bounded: cap both per-file content AND the number of
        # files so the prompt fits the smallest tool-capable model's budget).
        MAX_FILES = 8
        MAX_CHARS = 30000
        context_str = f"OBJECTIVE:\n{objective}\n\nPLAN:\n{json.dumps(plan, indent=2)}\n\nAFFECTED FILES:\n"
        budget = MAX_CHARS
        shown = 0
        for path, content in affected_files.items():
            if shown >= MAX_FILES or budget <= 0:
                break
            if len(content) > 4000:
                content = content[:4000] + "\n... (truncated)"
            context_str += f"--- {path} ---\n{content}\n\n"
            shown += 1
            budget -= len(content) + 40
        if len(affected_files) > shown:
            context_str += f"\n... and {len(affected_files) - shown} more affected file(s) (truncated).\n"

        system_prompt = """You are an expert refactoring engine.
You have the plan and the contents of all affected files.
You can use the edit_file tool to apply semantic AST-aware edits, OR you can just return the fully rewritten file contents in your final answer if it's simpler.

Since this is an automated pipeline, your final answer MUST contain the proposed changes in the following XML-like format, and NOTHING ELSE:

<changes>
  <file path="path/to/file1.py">
    <![CDATA[
    ... full new file content ...
    ]]>
  </file>
  <file path="path/to/file2.py">
    <![CDATA[
    ... full new file content ...
    ]]>
  </file>
</changes>

Ensure the code you provide is fully complete and valid Python.
"""
        user_prompt = context_str

        result = await self.loop.run(system_prompt, user_prompt, permission_mode=PermissionMode.EXECUTE)

        return self._parse_changes(result.get("result", ""))

    def _parse_changes(self, text: str) -> List[Dict[str, str]]:
        """Parse the XML-like changes output with multiple fallback strategies."""
        changes = []

        # Strategy 1: XML <file path="..."> tags
        file_pattern = r'<file\s+path=["\']([^"\']+)["\'](.*?)<\/file>'
        for match in re.finditer(file_pattern, text, re.DOTALL):
            path = match.group(1)
            content = match.group(2).strip()

            # Remove CDATA wrapper wherever it appears (Groq sometimes
            # prefixes stray characters like '>' before <![CDATA[)
            if "<![CDATA[" in content:
                content = content.split("<![CDATA[", 1)[1]
            if "]]>" in content:
                content = content.split("]]>", 1)[0]

            # Drop junk lines (e.g. a lone '>' produced by the model)
            content = "\n".join(
                ln for ln in content.splitlines()
                if ln.strip() not in (">",)
            ).strip()

            if content:
                changes.append({"file_path": path, "content": content})

        if changes:
            return changes

        # Strategy 2: Markdown code blocks with file paths in comments
        md_pattern = r'```python\s*\n#\s*File:\s*([^\n]+)\n(.*?)```'
        for match in re.finditer(md_pattern, text, re.DOTALL):
            path = match.group(1).strip()
            content = match.group(2).strip()
            if content:
                changes.append({"file_path": path, "content": content})

        if changes:
            return changes

        # Strategy 3: Fallback — if text looks like Python code, wrap it
        stripped = text.strip()
        if stripped.startswith("def ") or stripped.startswith("class ") or stripped.startswith("import "):
            logger.warning("ExecutorAgent: No structured format found, treating entire response as single file content")
            # We don't know the file path here — caller should handle
            return []

        logger.warning(f"ExecutorAgent: Could not parse any changes from response. Raw length: {len(text)}")
        return []
