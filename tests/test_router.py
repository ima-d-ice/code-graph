"""Unit tests for the LLM router: rate limits, circuit breaker, task routing.

All tests are offline — they exercise ProviderProfile logic and pool
configuration without making any network calls.
"""
from datetime import datetime, timedelta
import time

import pytest

from app.core.llm_router import (
    ProviderProfile, LLMRouter, TaskType, TASK_MODEL_MAP, GROQ_MODELS,
)


def make_profile(**overrides):
    defaults = dict(key_id="k1", model="m", api_key="x")
    defaults.update(overrides)
    return ProviderProfile(**defaults)


# ── ProviderProfile: rate limiting ──

def test_rpm_limit_blocks_after_threshold():
    p = make_profile(rpm=2)
    assert p.is_available()
    p.record_use(10)
    assert p.is_available()
    p.record_use(10)
    assert not p.is_available()


def test_tpd_limit_blocks_after_daily_budget():
    p = make_profile(tpd=100)
    p.record_use(60)
    assert p.is_available()
    p.record_use(60)  # total 120 > 100
    assert not p.is_available()


def test_tpd_none_means_unlimited():
    p = make_profile(tpd=None, rpm=10_000)
    for _ in range(100):
        p.record_use(10_000)
    assert p.is_available()


def test_rpd_limit():
    p = make_profile(rpd=3)
    for _ in range(3):
        p.record_use(1)
    assert not p.is_available()


def test_daily_rollover_resets_counters():
    p = make_profile(rpd=2, tpd=100)
    p.record_use(50)
    p.record_use(50)
    assert not p.is_available()
    p.daily_reset = datetime.now() - timedelta(days=2)  # force rollover
    assert p.is_available()


# ── ProviderProfile: circuit breaker ──

def test_circuit_breaker_opens_after_3_errors():
    p = make_profile()
    p.record_error()
    p.record_error()
    assert p.is_available()  # < 3 errors
    p.record_error()
    assert p.circuit_open
    assert not p.is_available()


def test_circuit_breaker_half_open_recovery():
    p = make_profile()
    for _ in range(3):
        p.record_error()
    assert not p.is_available()
    p.circuit_until = time.time() - 1  # cooldown expired
    assert p.is_available()  # half-open probe allowed
    assert not p.circuit_open


def test_rate_limit_error_opens_circuit_immediately():
    p = make_profile()
    p.record_error(is_rate_limit=True)
    assert p.circuit_open


def test_success_resets_breaker():
    p = make_profile()
    for _ in range(3):
        p.record_error()
    p.circuit_until = time.time() - 1
    p.record_use(10)  # half-open probe succeeds
    assert not p.circuit_open
    assert p.consecutive_errors == 0


def test_load_factor_scales_with_rpm():
    p = make_profile(rpm=100)
    for _ in range(50):
        p.record_use(1)
    assert 0.0 < p.load <= 1.0


# ── Pool configuration ──

def test_eight_models_in_catalog():
    assert len(GROQ_MODELS) == 8
    ids = [m[0] for m in GROQ_MODELS]
    assert "openai/gpt-oss-120b" in ids
    assert "meta-llama/llama-prompt-guard-2-86m" in ids


def test_catalog_entries_have_tpd_field():
    for model, rpm, rpd, tpm, tpd in GROQ_MODELS:
        assert model
        assert rpm > 0
        assert rpd > 0
        assert tpm > 0


def test_safety_task_uses_only_prompt_guard():
    assert TASK_MODEL_MAP[TaskType.SAFETY] == ["meta-llama/llama-prompt-guard-2-86m"]


def test_agent_tiers_are_tool_calling_capable():
    """compound/compound-mini don't support tool calling — must never be first
    choice for agent tasks (PLANNING/CODE_GENERATION/CRITIQUE)."""
    for task in (TaskType.PLANNING, TaskType.CODE_GENERATION, TaskType.CRITIQUE):
        tier = TASK_MODEL_MAP[task]
        assert "groq/compound" not in tier[0]
        assert "groq/compound-mini" not in tier[0]


def test_no_tool_tiers_prefer_compound():
    for task in (TaskType.SUMMARIZE, TaskType.QUICK_SEARCH):
        tier = TASK_MODEL_MAP[task]
        assert "groq/compound" in tier or "groq/compound-mini" in tier


def test_router_providers_match_env_or_empty():
    """Router must construct without error even with no keys (empty pool)."""
    router = LLMRouter()
    assert len(router.providers) % len(GROQ_MODELS) == 0
    router = None


def test_available_providers_respects_preference_order():
    """available_providers must return best-tier models first."""
    router = LLMRouter()
    if not router.providers:
        pytest.skip("no GROQ keys in environment")
    ordered = router.available_providers(TaskType.PLANNING)
    assert ordered
    assert ordered[0].model == TASK_MODEL_MAP[TaskType.PLANNING][0]


def test_guard_fails_open_without_provider():
    """guard() must return None (fail-open) when no providers exist."""
    router = LLMRouter()
    router.providers = []
    import asyncio
    result = asyncio.run(router.guard("test prompt"))
    assert result is None
