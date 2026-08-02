import json
import logging
from typing import Dict, Any, List

from app.core.agent_loop import AgentLoop
from app.core.tool_registry import ToolRegistry, PermissionMode
from app.core.llm_router import LLMRouter
from app.tools.file_tools import register_file_tools
from app.tools.graph_tools import register_graph_tools

logger = logging.getLogger(__name__)

class PlannerAgent:
    """
    Explores the codebase and generates refactoring plans.
    Read-only access (PLAN permission).
    """
    def __init__(self, project_root: str):
        self.project_root = project_root
        
        self.router = LLMRouter()
        self.registry = ToolRegistry()
        
        # Register read-only tools
        register_file_tools(self.registry, project_root, write_access=False)
        register_graph_tools(self.registry, self.project_root)
        
        self.loop = AgentLoop(self.router, self.registry, max_turns=10)

    async def run(self, objective: str, file_name: str, function_name: str) -> Dict[str, Any]:
        """Generate a refactoring plan."""
        system_prompt = """You are an expert software architect planning a codebase refactoring.
You have read-only tools to explore the codebase (read_file, glob, grep, graph_query, semantic_search, impact_analysis).

Explore the codebase to understand the implications of the user's objective.
Once you have explored sufficiently, return a JSON object with your plan:
{
  "plan": {
    "steps": ["Step 1...", "Step 2..."],
    "files_to_modify": ["path/to/file1.py", "path/to/file2.py"],
    "risk_assessment": "High/Medium/Low",
    "rationale": "..."
  }
}
"""
        user_prompt = f"Objective: {objective}\nStarting File: {file_name}\nTarget Function/Symbol: {function_name}"
        
        result = await self.loop.run(system_prompt, user_prompt, permission_mode=PermissionMode.PLAN)
        
        # Extract JSON plan from result text
        text = result.get("result", "")
        try:
            # Find JSON block
            import re
            match = re.search(r'```(?:json)?(.*?)```', text, re.DOTALL)
            if match:
                text = match.group(1).strip()
            
            # Simple parse
            # If the LLM returned extra text, this might fail without more robust parsing,
            # but we assume the system prompt's strict instruction worked.
            if "{" in text:
                text = text[text.find("{"):text.rfind("}")+1]
                return json.loads(text)
        except Exception as e:
            logger.error(f"Failed to parse plan JSON: {e}\nRaw output: {text}")
            
        return {"plan": {"steps": [text], "files_to_modify": [file_name]}}

    def analyze_impact(self, symbol: str) -> Dict[str, Any]:
        """Convenience method to directly run impact analysis (graph-first)."""
        try:
            from app.tools.graph_tools import impact_analysis
            res = impact_analysis(symbol, self.project_root)
            return json.loads(res)
        except Exception as e:
            # Graph-first, grep-second: fall back to on-disk discovery
            logger.warning(f"Impact analysis failed ({e}); using grep fallback")
            try:
                from app.tools.graph_tools import grep_fallback_impact
                return json.loads(grep_fallback_impact(symbol, self.project_root))
            except Exception as e2:
                logger.error(f"Grep fallback failed: {e2}")
                return {"affected_files": []}
