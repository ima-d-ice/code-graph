"""
Central registry for agent tools.
Handles permission gating and tool discovery.
"""

from typing import Dict, List, Any, Callable, Awaitable, Optional
from dataclasses import dataclass, field
from enum import Enum
import logging
import json

logger = logging.getLogger(__name__)


class PermissionMode(str, Enum):
    PLAN = "plan"         # Read-only exploration (Read, Grep, Glob, Graph, Impact)
    EXECUTE = "execute"   # Full access, prompts for bash commands
    AUTO = "auto"         # Autonomous execution, safety limits


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]
    required_permission: str  # "plan", "execute", "auto"
    handler: Callable[..., Any]
    requires_approval: bool = False


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        required_permission: str,
        handler: Callable[..., Any],
        requires_approval: bool = False
    ):
        """Register a new tool."""
        self._tools[name] = ToolSpec(
            name=name,
            description=description,
            input_schema=input_schema,
            required_permission=required_permission,
            handler=handler,
            requires_approval=requires_approval
        )
        logger.debug(f"🛠️ Registered tool: {name}")

    def get_available(self, permission_mode: str) -> List[Dict[str, Any]]:
        """Get LangChain-compatible tool definitions allowed in the given mode."""
        available = []
        for name, spec in self._tools.items():
            if self._is_permitted(spec.required_permission, permission_mode):
                available.append({
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.input_schema
                    }
                })
        return available

    def _is_permitted(self, tool_perm: str, current_mode: str) -> bool:
        """Check if a tool is permitted in the current mode."""
        if current_mode == PermissionMode.AUTO:
            return True
        if current_mode == PermissionMode.EXECUTE:
            return True
        if current_mode == PermissionMode.PLAN:
            return tool_perm == PermissionMode.PLAN
        return False

    async def execute(self, name: str, arguments: Dict[str, Any], current_mode: str) -> str:
        """Execute a tool by name with the given arguments."""
        if name not in self._tools:
            return f"Error: Tool '{name}' not found."

        spec = self._tools[name]

        if not self._is_permitted(spec.required_permission, current_mode):
            return f"Error: Tool '{name}' is not permitted in {current_mode} mode."

        try:
            # Handle async vs sync handlers
            import inspect
            if inspect.iscoroutinefunction(spec.handler):
                result = await spec.handler(**arguments)
            else:
                result = spec.handler(**arguments)

            if not isinstance(result, str):
                result = json.dumps(result, indent=2)
                
            return result
        except Exception as e:
            logger.error(f"Error executing tool {name}: {e}")
            return f"Error: {str(e)}"
