"""Test/demo data factories.

Kept inside the package (not under ``tests/``) on purpose: the global site-packages of
this machine ships a top-level ``tests`` package which shadows a local ``tests`` module,
so helpers must be importable as ``tokenlens.testing``.

These factories are used by the unit tests, the integration tests and
``tools/simulate.py`` so that the synthetic workload always matches the ingest schema.
"""

from __future__ import annotations

import random
from typing import Any, Iterable

from . import timeutil
from .schemas import SnapshotIn

AGENTS: list[tuple[str, str]] = [
    ("claude-code", "anthropic"),
    ("codex-cli", "openai"),
    ("openai-proxy", "openai"),
    ("ollama-local", "ollama"),
    ("deepseek-agent", "deepseek"),
]

PROJECTS = ["payments-api", "web-dashboard", "ml-pipeline", "infra-tools", "docs-site"]

MODEL_MIX: list[tuple[str, float]] = [
    ("claude-sonnet-4-5", 0.30),
    ("claude-opus-4-1", 0.05),
    ("gpt-5", 0.15),
    ("gpt-5-mini", 0.12),
    ("deepseek-chat", 0.18),
    ("deepseek-reasoner", 0.05),
    ("qwen2.5-coder:32b", 0.10),
    ("o3", 0.05),
]


def make_snapshot(
    *,
    ts: int | None = None,
    agent: str = "claude-code",
    project: str = "payments-api",
    model: str = "claude-sonnet-4-5",
    session_id: str = "sess-1",
    runtime: str = "anthropic",
    input_tokens: int = 1200,
    output_tokens: int = 400,
    cache_read_tokens: int = 3000,
    cache_write_tokens: int = 200,
    reasoning_tokens: int = 0,
    requests: int = 1,
    retries: int = 0,
    tool_calls: int = 1,
    context_window: int | None = 200_000,
    tool_result_bytes: int = 1000,
    event_id: str | None = None,
    tags: Any = None,
    meta: Any = None,
    **kwargs: Any,
) -> SnapshotIn:
    """Build a valid :class:`SnapshotIn` with realistic defaults."""
    return SnapshotIn(
        event_id=event_id,
        ts=ts if ts is not None else timeutil.now_ms(),
        agent=agent,
        project=project,
        model=model,
        session_id=session_id,
        runtime=runtime,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        requests=requests,
        retries=retries,
        tool_calls=tool_calls,
        context_window=context_window,
        tool_result_bytes=tool_result_bytes,
        tags=tags,
        meta=meta,
        **kwargs,
    )


def pick_model(rng: random.Random) -> str:
    """Weighted random model choice from :data:`MODEL_MIX`."""
    threshold = rng.random()
    cumulative = 0.0
    for model, weight in MODEL_MIX:
        cumulative += weight
        if threshold <= cumulative:
            return model
    return MODEL_MIX[-1][0]


def runtime_for(model: str) -> str:
    """Provider/runtime string for a model name."""
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    if model.startswith("deepseek"):
        return "deepseek"
    if ":" in model:
        return "ollama"
    return "custom"


def generate_workload(
    *,
    count: int = 500,
    end_ms: int | None = None,
    span_minutes: float = 60.0,
    seed: int = 42,
    agents: Iterable[tuple[str, str]] | None = None,
    projects: Iterable[str] | None = None,
) -> list[SnapshotIn]:
    """Generate ``count`` snapshots spread over the trailing ``span_minutes``.

    Used by tests and by ``tools/simulate.py --dry-run``; deterministic for a given seed.
    """
    rng = random.Random(seed)
    end_ms = end_ms if end_ms is not None else timeutil.now_ms()
    start_ms = int(end_ms - span_minutes * 60_000)
    agent_pool = list(agents or AGENTS)
    project_pool = list(projects or PROJECTS)
    sessions = [f"sess-{i:03d}" for i in range(max(4, count // 20))]

    snapshots: list[SnapshotIn] = []
    for index in range(count):
        agent, runtime = agent_pool[rng.randrange(len(agent_pool))]
        project = project_pool[rng.randrange(len(project_pool))]
        model = pick_model(rng)
        session_id = sessions[rng.randrange(len(sessions))]
        ts = start_ms + int((end_ms - start_ms) * (index / max(1, count - 1)))
        ts += rng.randint(-3000, 3000)
        prompt = rng.randint(400, 9000)
        cache_read = int(prompt * rng.uniform(0.2, 2.4))
        output = rng.randint(80, 2200)
        reasoning = rng.randint(0, 900) if "reasoner" in model or model.startswith("o") else 0
        requests = rng.choice([1, 1, 1, 2, 3])
        snapshots.append(
            make_snapshot(
                ts=ts,
                agent=agent,
                project=project,
                model=model,
                session_id=session_id,
                runtime=runtime_for(model) if runtime else runtime_for(model),
                input_tokens=prompt,
                output_tokens=output,
                cache_read_tokens=cache_read,
                cache_write_tokens=rng.randint(0, 900),
                reasoning_tokens=reasoning,
                requests=requests,
                retries=rng.choice([0, 0, 0, 0, 1]),
                tool_calls=rng.randint(0, 6),
                context_window=rng.choice([128_000, 200_000, 200_000, 400_000, 1_000_000]),
                tool_result_bytes=rng.randint(0, 120_000),
                event_id=f"gen-{seed}-{index}",
            )
        )
    return snapshots
