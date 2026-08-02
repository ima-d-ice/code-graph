"""
The core tool-using agent execution cycle.
"""

import json
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
import uuid

from app.core.tool_registry import ToolRegistry, PermissionMode
from app.core.llm_router import LLMRouter

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    output: str
    is_error: bool = False


@dataclass
class Turn:
    assistant_message: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_results: List[ToolResult] = field(default_factory=list)


class AgentLoop:
    """
    Executes a LangGraph-style agent loop.
    Receive → Plan → Execute Tools → Repeat → Return
    """
    
    def __init__(self, llm_router: LLMRouter, tool_registry: ToolRegistry, max_turns: int = 30):
        self.llm = llm_router
        self.tools = tool_registry
        self.max_turns = max_turns
        self.history: List[Turn] = []
        
    async def run(self, system_prompt: str, user_prompt: str, permission_mode: str = PermissionMode.PLAN) -> Dict[str, Any]:
        """Run the agent loop until it returns a final answer or hits max_turns."""
        turn_count = 0
        
        while turn_count < self.max_turns:
            logger.info(f"🔄 Agent Turn {turn_count + 1}/{self.max_turns}")
            
            # Assemble the conversation history
            messages = self._build_messages(system_prompt, user_prompt)
            
            # Get available tools
            available_tools = self.tools.get_available(permission_mode)
            
            # Ask LLM
            response = await self._call_llm_with_tools(messages, available_tools, task_type="planning")
            
            if not response.get("tool_calls"):
                # Final answer
                logger.info("✅ Agent provided final answer")
                return {
                    "result": response.get("content", ""),
                    "turns": turn_count + 1,
                    "history": self._serialize_history(),
                }
            
            # Execute tools
            current_turn = Turn(
                assistant_message=response.get("content", ""),
                tool_calls=[]
            )
            
            for tc_data in response["tool_calls"]:
                tc = ToolCall(
                    id=tc_data.get("id", str(uuid.uuid4())),
                    name=tc_data["function"]["name"],
                    arguments=json.loads(tc_data["function"]["arguments"])
                )
                current_turn.tool_calls.append(tc)
                
                logger.info(f"🛠️ Executing tool: {tc.name}")
                
                try:
                    output = await self.tools.execute(tc.name, tc.arguments, permission_mode)
                    result = ToolResult(tc.id, output)
                except Exception as e:
                    logger.error(f"❌ Tool {tc.name} failed: {e}")
                    result = ToolResult(tc.id, f"Error: {e}", is_error=True)
                    
                current_turn.tool_results.append(result)
            
            self.history.append(current_turn)
            turn_count += 1
            
        return {
            "error": "max_turns_exceeded", 
            "turns": turn_count,
            "history": self._serialize_history()
        }
        
    def _build_messages(self, system_prompt: str, user_prompt: str) -> List[Dict[str, Any]]:
        """Construct the message array including history."""
        # Add strong instruction to prevent Groq tool_use_failed (XML tag generation)
        safe_system_prompt = system_prompt + "\n\nCRITICAL: When calling tools, you MUST use the native JSON tool calling mechanism. NEVER use XML tags like <function=...>. If you are done, just output your final answer as raw text/JSON without any tool call."
        
        messages = [
            {"role": "system", "content": safe_system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        for turn in self.history:
            # Assistant's message + tool calls
            msg = {"role": "assistant", "content": turn.assistant_message}
            if turn.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)
                        }
                    } for tc in turn.tool_calls
                ]
            messages.append(msg)
            
            # Tool results (bounded: a single tool result — e.g. impact_analysis
            # over a large repo — must not blow the message window)
            for res in turn.tool_results:
                output = res.output
                if len(output) > 3000:
                    output = output[:3000] + "\n... (truncated)"
                messages.append({
                    "role": "tool",
                    "tool_call_id": res.tool_call_id,
                    "name": next((tc.name for tc in turn.tool_calls if tc.id == res.tool_call_id), "unknown"),
                    "content": output
                })
                
        return messages

    async def _call_llm_with_tools(self, messages: List[Dict], tools: List[Dict], task_type: str) -> Dict:
        """Call the LLM with tool definitions. Uses the router to select provider."""

        # Prompt-injection guard (llama-prompt-guard-2-86m) on the latest user message
        for m in reversed(messages):
            if m["role"] == "user":
                verdict = await self.llm.guard(m["content"])
                if verdict and "prompt_attack" in verdict:
                    logger.warning(f"🚫 Prompt guard blocked message: {verdict}")
                    return {
                        "content": f"Blocked by prompt guard: {verdict}",
                        "tool_calls": [],
                    }
                break

        # Walk the provider chain in preference order (best tier first).
        # Some models (e.g. gpt-oss-120b) can emit malformed tool-call JSON —
        # on failure we record it against that provider and fall to the next tier.
        # groq/compound* cannot do tool calling, so exclude them when tools are bound.
        providers = self.llm.available_providers(task_type, require_tool_calling=bool(tools))
        if not providers:
            raise RuntimeError("No available LLM providers")

        last_error = None
        for provider in providers:
            client = self.llm._build_client(provider)

            try:
                # Convert dict messages to LangChain BaseMessages
                from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

                lc_messages = []
                for m in messages:
                    if m["role"] == "system":
                        lc_messages.append(SystemMessage(content=m["content"]))
                    elif m["role"] == "user":
                        lc_messages.append(HumanMessage(content=m["content"]))
                    elif m["role"] == "assistant":
                        kwargs = {"content": m["content"] or ""}
                        if "tool_calls" in m:
                            kwargs["tool_calls"] = [
                                {
                                    "name": tc["function"]["name"],
                                    "args": json.loads(tc["function"]["arguments"]),
                                    "id": tc["id"]
                                } for tc in m["tool_calls"]
                            ]
                        lc_messages.append(AIMessage(**kwargs))
                    elif m["role"] == "tool":
                        lc_messages.append(ToolMessage(
                            content=m["content"],
                            tool_call_id=m["tool_call_id"],
                            name=m.get("name", "tool")
                        ))

                if tools:
                    client = client.bind_tools(tools)

                response = await client.ainvoke(lc_messages)

                # Record usage (real token count from model response when available)
                prompt_str = str(messages)
                tokens = self.llm._get_token_count(response, prompt_str, response.content or "")
                provider.record_use(tokens)
                self.llm.session_tokens += tokens
                self.llm.session_requests += 1

                # Convert response back to dict format
                result = {
                    "content": response.content,
                    "tool_calls": []
                }

                if hasattr(response, "tool_calls") and response.tool_calls:
                    for tc in response.tool_calls:
                        result["tool_calls"].append({
                            "id": tc.get("id"),
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["args"])
                            }
                        })

                return result

            except Exception as e:
                last_error = e
                error_msg = str(e)
                logger.warning(f"⚠️ {provider.key_id}/{provider.model} failed: {error_msg[:200]}")
                provider.record_error(is_rate_limit=("429" in error_msg or "rate" in error_msg.lower()))
                continue

        raise RuntimeError(
            f"All LLM providers exhausted. Last error: {last_error}"
        )
            
    def _serialize_history(self) -> List[Dict]:
        """Convert history to JSON-serializable format."""
        out = []
        for turn in self.history:
            out.append({
                "assistant_message": turn.assistant_message,
                "tool_calls": [{"name": tc.name, "args": tc.arguments} for tc in turn.tool_calls],
                "tool_results": [{"id": tr.tool_call_id, "output": tr.output[:500] + ("..." if len(tr.output) > 500 else "")} for tr in turn.tool_results]
            })
        return out
