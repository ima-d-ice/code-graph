"""
Groq-only multi-key LLM router.

Routes requests across 12 Groq API keys with:
- Per-key rate limit tracking (RPM/RPD/TPM)
- Circuit breaker pattern (3 consecutive errors = 60s cooldown)
- Task-based model routing (planning→70b, codegen→70b, quick→8b)
- Least-loaded key selection within model tier
- Fallback chains across model tiers
- Session cost tracking
"""

import os
import time
import logging
import json
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from collections import deque
from datetime import datetime, timedelta
import threading
from enum import Enum

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Task Types
# ─────────────────────────────────────────────

class TaskType(str, Enum):
    PLANNING = "planning"
    CODE_GENERATION = "code_generation"
    CRITIQUE = "critique"
    QUICK_SEARCH = "quick_search"
    SUMMARIZE = "summarize"
    INTENT_EXTRACTION = "intent_extraction"
    SAFETY = "safety"


# ─────────────────────────────────────────────
# Provider Profile
# ─────────────────────────────────────────────

@dataclass
class ProviderProfile:
    """Tracks rate limits and health for a single key+model combination."""
    key_id: str
    model: str
    api_key: str
    rpm: int = 30        # requests per minute
    rpd: int = 1000      # requests per day
    tpm: int = 12000     # tokens per minute
    tpd: Optional[int] = None  # tokens per day (None = unlimited)

    # Runtime state
    request_times: deque = field(default_factory=lambda: deque(maxlen=5000))
    daily_count: int = 0
    daily_tokens_used: int = 0
    daily_reset: datetime = field(default_factory=datetime.now)
    consecutive_errors: int = 0
    circuit_open: bool = False
    circuit_until: float = 0.0
    total_tokens_used: int = 0
    total_requests: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def is_available(self) -> bool:
        """Check if this key+model is available for a request."""
        now = datetime.now()

        # Circuit breaker
        if self.circuit_open:
            if time.time() < self.circuit_until:
                return False
            else:
                # Half-open: allow one request to test
                with self._lock:
                    self.circuit_open = False
                    self.consecutive_errors = 0

        # Daily reset
        if now - self.daily_reset > timedelta(days=1):
            with self._lock:
                self.daily_count = 0
                self.daily_tokens_used = 0
                self.daily_reset = now

        # RPM check
        cutoff = now - timedelta(minutes=1)
        recent = sum(1 for t in self.request_times if t > cutoff)
        if recent >= self.rpm:
            return False

        # RPD check
        if self.daily_count >= self.rpd:
            return False

        # TPD check (daily token budget)
        if self.tpd is not None and self.daily_tokens_used >= self.tpd:
            return False

        return True

    def record_use(self, tokens: int):
        """Record a successful request."""
        with self._lock:
            self.request_times.append(datetime.now())
            self.daily_count += 1
            self.daily_tokens_used += tokens
            self.total_tokens_used += tokens
            self.total_requests += 1
            self.consecutive_errors = 0
            if self.circuit_open:
                self.circuit_open = False

    def record_error(self, is_rate_limit: bool = False):
        """Record a failed request."""
        with self._lock:
            self.consecutive_errors += 1
            if self.consecutive_errors >= 3 or is_rate_limit:
                self.circuit_open = True
                cooldown = 120 if is_rate_limit else 60
                self.circuit_until = time.time() + cooldown
                logger.warning(
                    f"🔴 Circuit breaker OPEN for {self.key_id}/{self.model} "
                    f"({cooldown}s cooldown)"
                )

    @property
    def load(self) -> float:
        """Current load factor (0.0 = idle, 1.0 = at capacity)."""
        cutoff = datetime.now() - timedelta(minutes=1)
        recent = sum(1 for t in self.request_times if t > cutoff)
        return recent / max(self.rpm, 1)


# ─────────────────────────────────────────────
# LLM Router
# ─────────────────────────────────────────────

# Groq model specs: (model_id, rpm, rpd, tpm, tpd)
GROQ_MODELS = [
    ("groq/compound",                      30, 250,   70000, None),
    ("groq/compound-mini",                 30, 250,   70000, None),
    ("openai/gpt-oss-120b",                30, 1000,  8000,  200000),
    ("openai/gpt-oss-20b",                 30, 1000,  8000,  200000),
    ("qwen/qwen3.6-27b",                   30, 1000,  8000,  200000),
    ("llama-3.3-70b-versatile",            30, 1000,  12000, 100000),
    ("llama-3.1-8b-instant",               30, 14400, 6000,  500000),
    ("meta-llama/llama-prompt-guard-2-86m", 30, 14400, 15000, 500000),
]

# Which models to use for each task type (ordered by preference)
# NOTE: groq/compound and compound-mini do NOT support tool calling —
# only use them for no-tool tasks (RAG synthesis, quick search).
NO_TOOL_CALLING_MODELS = {"groq/compound", "groq/compound-mini"}

TASK_MODEL_MAP: Dict[str, List[str]] = {
    TaskType.PLANNING: [
        "openai/gpt-oss-120b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
    ],
    TaskType.CODE_GENERATION: [
        "openai/gpt-oss-120b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
    ],
    TaskType.CRITIQUE: [
        "openai/gpt-oss-120b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
    ],
    TaskType.INTENT_EXTRACTION: [
        "llama-3.1-8b-instant", "groq/compound-mini",
    ],
    TaskType.QUICK_SEARCH: [
        "llama-3.1-8b-instant", "groq/compound-mini",
    ],
    TaskType.SUMMARIZE: [
        "llama-3.1-8b-instant", "groq/compound", "groq/compound-mini",
    ],
    TaskType.SAFETY: [
        "meta-llama/llama-prompt-guard-2-86m",
    ],
}


class LLMRouter:
    """
    Production-grade Groq-only LLM router.

    Features:
    - 12 API keys × N models = pool of provider profiles
    - Circuit breaker per key+model
    - Task-based routing with fallback chains
    - Least-loaded selection within model tier
    - Session cost tracking
    - Thread-safe for concurrent requests
    """

    def __init__(self):
        self.providers: List[ProviderProfile] = []
        self.session_tokens: int = 0
        self.session_requests: int = 0
        self._load_keys()

    def _load_keys(self):
        """Load all GROQ_API_KEY_N (or GROQ_KEY_N) from environment."""
        key_count = 0
        for i in range(1, 13):
            key = os.getenv(f"GROQ_API_KEY_{i}") or os.getenv(f"GROQ_KEY_{i}")
            if not key:
                continue
            key_count += 1

            for model, rpm, rpd, tpm, tpd in GROQ_MODELS:
                self.providers.append(ProviderProfile(
                    key_id=f"groq-{i}",
                    model=model,
                    api_key=key,
                    rpm=rpm,
                    rpd=rpd,
                    tpm=tpm,
                    tpd=tpd,
                ))

        logger.info(
            f"🔑 Loaded {len(self.providers)} provider profiles "
            f"from {key_count} Groq API keys"
        )

        if not self.providers:
            logger.error("❌ No Groq API keys found! Set GROQ_KEY_1 through GROQ_KEY_12")

    def _build_client(self, profile: ProviderProfile):
        """Build a LangChain ChatGroq client for the given profile."""
        from langchain_groq import ChatGroq
        return ChatGroq(
            model=profile.model,
            api_key=profile.api_key,
            temperature=0,
            max_retries=0,  # We handle retries ourselves
        )

    def _estimate_tokens(self, prompt: str, response: str) -> int:
        """Rough token estimate: ~4 chars per token."""
        return (len(prompt) + len(response)) // 4

    def _get_token_count(self, response, prompt: str, response_text: str) -> int:
        """
        Prefer the model-reported token usage; fall back to the char estimate.
        LangChain AIMessage exposes `usage_metadata` (input/output/total tokens).
        """
        usage = getattr(response, "usage_metadata", None) or {}
        total = usage.get("total_tokens")
        if total:
            return int(total)
        return self._estimate_tokens(prompt, response_text)

    def _select_provider(self, task_type: str,
                         preferred_model: Optional[str] = None) -> Optional[ProviderProfile]:
        """
        Select the best available provider for a task.
        
        Strategy:
        1. Get preferred model list for the task
        2. For each model tier, find available providers
        3. Pick the least-loaded provider
        4. If no preferred models available, fall back to any available
        """
        # Get model preference order
        models = TASK_MODEL_MAP.get(task_type, ["llama-3.3-70b-versatile"])
        if preferred_model:
            models = [preferred_model] + [m for m in models if m != preferred_model]

        # Try each model tier in order
        for model in models:
            candidates = [
                p for p in self.providers
                if p.model == model and p.is_available()
            ]
            if candidates:
                # Pick least loaded
                return min(candidates, key=lambda p: p.load)

        # Fallback: any available provider
        available = [p for p in self.providers if p.is_available()]
        if available:
            return min(available, key=lambda p: p.load)

        return None

    def select_provider(self, task_type: str,
                        preferred_model: Optional[str] = None) -> Optional[ProviderProfile]:
        """Public wrapper for _select_provider."""
        return self._select_provider(task_type, preferred_model)

    def available_providers(self, task_type: str,
                            preferred_model: Optional[str] = None,
                            require_tool_calling: bool = False) -> List[ProviderProfile]:
        """
        All available providers for a task, in preference order (best tier first,
        least-loaded within each tier). Used for tool-calling retry loops.
        """
        models = TASK_MODEL_MAP.get(task_type, ["llama-3.3-70b-versatile"])
        if preferred_model:
            models = [preferred_model] + [m for m in models if m != preferred_model]

        def supports_tools(p: ProviderProfile) -> bool:
            return not require_tool_calling or p.model not in NO_TOOL_CALLING_MODELS

        ordered: List[ProviderProfile] = []
        seen = set()
        for model in models:
            candidates = sorted(
                (p for p in self.providers if p.model == model and p.is_available() and supports_tools(p)),
                key=lambda p: p.load,
            )
            for p in candidates:
                ordered.append(p)
                seen.add(id(p))

        fallback = sorted(
            (p for p in self.providers
             if id(p) not in seen and p.is_available() and supports_tools(p)),
            key=lambda p: p.load,
        )
        return ordered + fallback

    async def route(self, task_type: str, prompt: str,
                    preferred_model: Optional[str] = None,
                    require_json: bool = False,
                    max_retries: int = 3) -> str:
        """
        Route a request to the best available provider.

        Args:
            task_type: One of TaskType values
            prompt: The prompt to send
            preferred_model: Override model selection
            require_json: Whether to request JSON output
            max_retries: Max retry attempts across providers

        Returns:
            The model's response text

        Raises:
            RuntimeError: If all providers exhausted
        """
        last_error = None

        for attempt in range(max_retries):
            provider = self._select_provider(task_type, preferred_model)

            if not provider:
                # All providers busy — wait and retry
                wait_time = min(15 * (attempt + 1), 60)
                logger.warning(
                    f"⏳ All providers busy. Waiting {wait_time}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                import asyncio
                await asyncio.sleep(wait_time)
                continue

            try:
                logger.info(
                    f"🔀 Routing to {provider.key_id}/{provider.model} "
                    f"(load: {provider.load:.1%})"
                )

                client = self._build_client(provider)

                # Build messages
                messages = [{"role": "user", "content": prompt}]

                if require_json:
                    messages[0]["content"] += "\n\nRespond with valid JSON only. No markdown."

                response = client.invoke(messages)
                response_text = response.content

                # Record success (real token count when available)
                tokens = self._get_token_count(response, prompt, response_text)
                provider.record_use(tokens)
                self.session_tokens += tokens
                self.session_requests += 1

                return response_text

            except Exception as e:
                error_msg = str(e)
                last_error = e
                logger.warning(f"⚠️ {provider.key_id}/{provider.model} failed: {error_msg}")

                is_rate_limit = "429" in error_msg or "rate" in error_msg.lower()
                provider.record_error(is_rate_limit=is_rate_limit)

        raise RuntimeError(
            f"All LLM providers exhausted after {max_retries} attempts. "
            f"Last error: {last_error}"
        )

    def route_sync(self, task_type: str, prompt: str,
                   preferred_model: Optional[str] = None,
                   require_json: bool = False,
                   max_retries: int = 3) -> str:
        """
        Synchronous version of route() for non-async contexts.
        """
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # We're in an async context — create a new thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    self.route(task_type, prompt, preferred_model,
                              require_json, max_retries)
                )
                return future.result()
        else:
            return asyncio.run(
                self.route(task_type, prompt, preferred_model,
                           require_json, max_retries)
            )

    async def guard(self, prompt: str) -> Optional[str]:
        """
        Classify a prompt with llama-prompt-guard-2-86m (prompt injection gate).

        The guard model has a small context window, so the prompt is truncated
        to a bounded window before classification (fail-open on any error).
        """
        provider = self._select_provider(TaskType.SAFETY)
        if not provider:
            return None

        # Guard only needs a bounded window to detect an injection
        if len(prompt) > 4000:
            prompt = prompt[:4000]

        try:
            client = self._build_client(provider)
            messages = [{"role": "user", "content": prompt}]
            response = await client.ainvoke(messages)
            text = (response.content or "").strip()

            tokens = self._get_token_count(response, prompt, text)
            provider.record_use(tokens)
            self.session_tokens += tokens
            self.session_requests += 1

            return text.lower()
        except Exception as e:
            logger.warning(f"⚠️ Prompt guard failed: {e}")
            return None

    def get_session_cost(self) -> Dict[str, Any]:
        """Return session usage statistics."""
        return {
            "total_requests": self.session_requests,
            "total_tokens": self.session_tokens,
            "estimated_cost_usd": 0.0,  # Free tier
            "providers_available": sum(1 for p in self.providers if p.is_available()),
            "providers_total": len(self.providers),
            "providers_circuit_open": sum(1 for p in self.providers if p.circuit_open),
        }

    def get_provider_status(self) -> List[Dict]:
        """Return detailed status of all providers."""
        return [
            {
                "key_id": p.key_id,
                "model": p.model,
                "available": p.is_available(),
                "load": f"{p.load:.1%}",
                "circuit_open": p.circuit_open,
                "total_requests": p.total_requests,
                "total_tokens": p.total_tokens_used,
                "daily_tokens_used": p.daily_tokens_used,
                "daily_token_budget": p.tpd,
                "consecutive_errors": p.consecutive_errors,
            }
            for p in self.providers
        ]
