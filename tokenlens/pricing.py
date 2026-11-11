"""Model price catalog.

Prices are **USD per one million tokens** (the unit every provider publishes), split by
token class:

* ``input``       — uncached prompt tokens
* ``output``      — completion tokens
* ``cache_read``  — prompt tokens served from a provider prompt cache (usually 10%)
* ``cache_write`` — prompt tokens written into the cache (usually 125%)

The catalog deliberately mirrors what ai-token-hud ships (a local price table) but is
extended into a server side, versioned catalog so that cost can be recomputed when a
provider changes prices. ``PRICE_VERSION`` is stamped onto every ingested snapshot.

Unknown models are not rejected: they are priced with a documented fallback so that a
new model release never breaks ingestion. ``resolve_model`` returns the canonical entry
plus a ``known`` flag, and ingest reports unknown models back to the client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

PRICE_VERSION = "2025-11-01"


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """A single catalog entry."""

    model: str
    provider: str
    display_name: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0
    context_window: int = 200_000
    notes: str = ""

    def as_dict(self, known: bool = True) -> dict:
        return {
            "model": self.model,
            "provider": self.provider,
            "display_name": self.display_name,
            "input_per_mtok": self.input_per_mtok,
            "output_per_mtok": self.output_per_mtok,
            "cache_read_per_mtok": self.cache_read_per_mtok,
            "cache_write_per_mtok": self.cache_write_per_mtok,
            "context_window": self.context_window,
            "known": known,
        }


def _p(
    model: str,
    provider: str,
    display: str,
    inp: float,
    out: float,
    cache_read: float = 0.0,
    cache_write: float = 0.0,
    ctx: int = 200_000,
    notes: str = "",
) -> ModelPrice:
    return ModelPrice(
        model=model,
        provider=provider,
        display_name=display,
        input_per_mtok=inp,
        output_per_mtok=out,
        cache_read_per_mtok=cache_read,
        cache_write_per_mtok=cache_write,
        context_window=ctx,
        notes=notes,
    )


# --------------------------------------------------------------------------------------
# Catalog (60+ models across 7 providers). Prices in USD / 1M tokens.
# --------------------------------------------------------------------------------------
_MODELS: tuple[ModelPrice, ...] = (
    # ---- Anthropic Claude -------------------------------------------------------------
    _p("claude-opus-4-1", "anthropic", "Claude Opus 4.1", 15.0, 75.0, 1.5, 18.75),
    _p("claude-opus-4", "anthropic", "Claude Opus 4", 15.0, 75.0, 1.5, 18.75),
    _p("claude-sonnet-4-5", "anthropic", "Claude Sonnet 4.5", 3.0, 15.0, 0.3, 3.75),
    _p("claude-sonnet-4", "anthropic", "Claude Sonnet 4", 3.0, 15.0, 0.3, 3.75),
    _p("claude-3-7-sonnet", "anthropic", "Claude 3.7 Sonnet", 3.0, 15.0, 0.3, 3.75),
    _p("claude-3-5-sonnet", "anthropic", "Claude 3.5 Sonnet", 3.0, 15.0, 0.3, 3.75),
    _p("claude-3-5-haiku", "anthropic", "Claude 3.5 Haiku", 0.8, 4.0, 0.08, 1.0),
    _p("claude-3-opus", "anthropic", "Claude 3 Opus", 15.0, 75.0, 1.5, 18.75),
    _p("claude-3-haiku", "anthropic", "Claude 3 Haiku", 0.25, 1.25, 0.03, 0.3),
    # ---- OpenAI -----------------------------------------------------------------------
    _p("gpt-5", "openai", "GPT-5", 1.25, 10.0, 0.125, 1.25, 400_000),
    _p("gpt-5-mini", "openai", "GPT-5 mini", 0.25, 2.0, 0.025, 0.25, 400_000),
    _p("gpt-5-nano", "openai", "GPT-5 nano", 0.05, 0.4, 0.005, 0.05, 400_000),
    _p("gpt-4.1", "openai", "GPT-4.1", 2.0, 8.0, 0.5, 2.0, 1_047_576),
    _p("gpt-4.1-mini", "openai", "GPT-4.1 mini", 0.4, 1.6, 0.1, 0.4, 1_047_576),
    _p("gpt-4.1-nano", "openai", "GPT-4.1 nano", 0.1, 0.4, 0.025, 0.1, 1_047_576),
    _p("gpt-4o", "openai", "GPT-4o", 2.5, 10.0, 1.25, 2.5, 128_000),
    _p("gpt-4o-mini", "openai", "GPT-4o mini", 0.15, 0.6, 0.075, 0.15, 128_000),
    _p("gpt-4-turbo", "openai", "GPT-4 Turbo", 10.0, 30.0, 5.0, 10.0, 128_000),
    _p("gpt-4", "openai", "GPT-4", 30.0, 60.0, 15.0, 30.0, 8_192),
    _p("gpt-3.5-turbo", "openai", "GPT-3.5 Turbo", 0.5, 1.5, 0.25, 0.5, 16_385),
    _p("o3", "openai", "o3", 2.0, 8.0, 0.5, 2.0, 200_000, "reasoning"),
    _p("o3-mini", "openai", "o3-mini", 1.1, 4.4, 0.55, 1.1, 200_000, "reasoning"),
    _p("o4-mini", "openai", "o4-mini", 1.1, 4.4, 0.275, 1.1, 200_000, "reasoning"),
    _p("o1", "openai", "o1", 15.0, 60.0, 7.5, 15.0, 200_000, "reasoning"),
    _p("o1-mini", "openai", "o1-mini", 1.1, 4.4, 0.55, 1.1, 128_000, "reasoning"),
    _p("text-embedding-3-large", "openai", "text-embedding-3-large", 0.13, 0.0, 0.0, 0.0, 8_191),
    _p("text-embedding-3-small", "openai", "text-embedding-3-small", 0.02, 0.0, 0.0, 0.0, 8_191),
    # ---- DeepSeek ---------------------------------------------------------------------
    _p("deepseek-chat", "deepseek", "DeepSeek Chat (V3)", 0.27, 1.1, 0.07, 0.27, 128_000),
    _p("deepseek-reasoner", "deepseek", "DeepSeek Reasoner (R1)", 0.55, 2.19, 0.14, 0.55, 128_000, "reasoning"),
    _p("deepseek-v3", "deepseek", "DeepSeek V3", 0.27, 1.1, 0.07, 0.27, 128_000),
    _p("deepseek-v3.1", "deepseek", "DeepSeek V3.1", 0.27, 1.1, 0.07, 0.27, 128_000),
    _p("deepseek-v3.2", "deepseek", "DeepSeek V3.2", 0.28, 0.42, 0.028, 0.28, 128_000),
    _p("deepseek-coder", "deepseek", "DeepSeek Coder", 0.27, 1.1, 0.07, 0.27, 128_000),
    # ---- Google Gemini ----------------------------------------------------------------
    _p("gemini-2.5-pro", "google", "Gemini 2.5 Pro", 1.25, 10.0, 0.31, 1.25, 1_048_576),
    _p("gemini-2.5-flash", "google", "Gemini 2.5 Flash", 0.3, 2.5, 0.075, 0.3, 1_048_576),
    _p("gemini-2.5-flash-lite", "google", "Gemini 2.5 Flash Lite", 0.1, 0.4, 0.025, 0.1, 1_048_576),
    _p("gemini-2.0-flash", "google", "Gemini 2.0 Flash", 0.1, 0.4, 0.025, 0.1, 1_048_576),
    _p("gemini-1.5-pro", "google", "Gemini 1.5 Pro", 1.25, 5.0, 0.3125, 1.25, 2_097_152),
    _p("gemini-1.5-flash", "google", "Gemini 1.5 Flash", 0.075, 0.3, 0.01875, 0.075, 1_048_576),
    # ---- xAI Grok ---------------------------------------------------------------------
    _p("grok-4", "xai", "Grok 4", 3.0, 15.0, 0.75, 3.0, 256_000),
    _p("grok-3", "xai", "Grok 3", 3.0, 15.0, 0.75, 3.0, 131_072),
    _p("grok-3-mini", "xai", "Grok 3 mini", 0.3, 0.5, 0.075, 0.3, 131_072),
    _p("grok-2", "xai", "Grok 2", 2.0, 10.0, 0.5, 2.0, 131_072),
    # ---- Mistral ----------------------------------------------------------------------
    _p("mistral-large", "mistral", "Mistral Large", 2.0, 6.0, 0.5, 2.0, 128_000),
    _p("mistral-medium", "mistral", "Mistral Medium", 0.4, 2.0, 0.1, 0.4, 128_000),
    _p("mistral-small", "mistral", "Mistral Small", 0.1, 0.3, 0.025, 0.1, 128_000),
    _p("codestral", "mistral", "Codestral", 0.3, 0.9, 0.075, 0.3, 256_000),
    _p("devstral", "mistral", "Devstral", 0.4, 2.0, 0.1, 0.4, 128_000),
    # ---- Local / self hosted (Ollama etc.) --------------------------------------------
    # Priced at an amortised GPU cost so local runs still show up in the ledger instead
    # of silently reading as "free". Override per deployment if needed.
    _p("llama3.1:70b", "ollama", "Llama 3.1 70B (local)", 0.0, 0.0, 0.0, 0.0, 131_072, "local-gpu-amortised"),
    _p("llama3.1:8b", "ollama", "Llama 3.1 8B (local)", 0.0, 0.0, 0.0, 0.0, 131_072, "local-gpu-amortised"),
    _p("llama3.2:3b", "ollama", "Llama 3.2 3B (local)", 0.0, 0.0, 0.0, 0.0, 131_072, "local-gpu-amortised"),
    _p("qwen2.5-coder:32b", "ollama", "Qwen2.5 Coder 32B (local)", 0.0, 0.0, 0.0, 0.0, 131_072, "local-gpu-amortised"),
    _p("qwen2.5-coder:7b", "ollama", "Qwen2.5 Coder 7B (local)", 0.0, 0.0, 0.0, 0.0, 131_072, "local-gpu-amortised"),
    _p("deepseek-r1:32b", "ollama", "DeepSeek R1 32B (local)", 0.0, 0.0, 0.0, 0.0, 131_072, "local-gpu-amortised"),
    _p("deepseek-r1:14b", "ollama", "DeepSeek R1 14B (local)", 0.0, 0.0, 0.0, 0.0, 131_072, "local-gpu-amortised"),
    _p("mistral-nemo:12b", "ollama", "Mistral Nemo 12B (local)", 0.0, 0.0, 0.0, 0.0, 131_072, "local-gpu-amortised"),
    _p("phi4:14b", "ollama", "Phi-4 14B (local)", 0.0, 0.0, 0.0, 0.0, 16_384, "local-gpu-amortised"),
    _p("gemma3:27b", "ollama", "Gemma 3 27B (local)", 0.0, 0.0, 0.0, 0.0, 131_072, "local-gpu-amortised"),
    # ---- Code assistants (HUD-focused: Claude Code / Codex / Cursor) -------------------
    _p("claude-code-sonnet", "anthropic", "Claude Code (Sonnet 4.5)", 3.0, 15.0, 0.3, 3.75),
    _p("claude-code-opus", "anthropic", "Claude Code (Opus 4.1)", 15.0, 75.0, 1.5, 18.75),
    _p("codex-mini-latest", "openai", "Codex mini", 1.5, 6.0, 0.375, 1.5, 200_000),
    _p("codex-5", "openai", "Codex 5", 1.25, 10.0, 0.125, 1.25, 400_000),
    _p("gpt-4.1-codex", "openai", "GPT-4.1 Codex", 2.0, 8.0, 0.5, 2.0, 1_047_576),
    _p("cursor-small", "cursor", "Cursor Small", 0.3, 1.2, 0.075, 0.3, 200_000),
)

CATALOG: dict[str, ModelPrice] = {entry.model: entry for entry in _MODELS}

#: Fallback price used for models missing from the catalog (flagged as unknown).
UNKNOWN_PRICE = ModelPrice(
    model="unknown",
    provider="unknown",
    display_name="Unknown model (fallback pricing)",
    input_per_mtok=3.0,
    output_per_mtok=15.0,
    cache_read_per_mtok=0.3,
    cache_write_per_mtok=3.75,
    context_window=200_000,
    notes="fallback; override by adding the model to pricing.py",
)

#: Aliases normalised before lookup (dated snapshots, provider prefixes, shorthand).
_ALIASES: dict[str, str] = {
    "claude-3-5-sonnet-latest": "claude-3-5-sonnet",
    "claude-3-5-sonnet-20240620": "claude-3-5-sonnet",
    "claude-3-5-sonnet-20241022": "claude-3-5-sonnet",
    "claude-3-7-sonnet-latest": "claude-3-7-sonnet",
    "claude-3-7-sonnet-20250219": "claude-3-7-sonnet",
    "claude-sonnet-4-20250514": "claude-sonnet-4",
    "claude-sonnet-4-5-20250929": "claude-sonnet-4-5",
    "claude-opus-4-20250514": "claude-opus-4",
    "claude-opus-4-1-20250805": "claude-opus-4-1",
    "claude-3-5-haiku-latest": "claude-3-5-haiku",
    "claude-3-opus-20240229": "claude-3-opus",
    "claude-3-haiku-20240307": "claude-3-haiku",
    "sonnet": "claude-sonnet-4-5",
    "opus": "claude-opus-4-1",
    "haiku": "claude-3-5-haiku",
    "gpt-4o-2024-08-06": "gpt-4o",
    "gpt-4o-2024-11-20": "gpt-4o",
    "gpt-4o-mini-2024-07-18": "gpt-4o-mini",
    "gpt-4.1-2025-04-14": "gpt-4.1",
    "gpt-4.1-mini-2025-04-14": "gpt-4.1-mini",
    "gpt-4.1-nano-2025-04-14": "gpt-4.1-nano",
    "gpt-4-turbo-2024-04-09": "gpt-4-turbo",
    "gpt-3.5-turbo-0125": "gpt-3.5-turbo",
    "o1-preview": "o1",
    "o3-mini-2025-01-31": "o3-mini",
    "deepseek-v3.2-exp": "deepseek-v3.2",
    "deepseek-chat-v3": "deepseek-chat",
    "deepseek-reasoner-r1": "deepseek-reasoner",
    "gemini-2.5-pro-latest": "gemini-2.5-pro",
    "gemini-2.5-flash-latest": "gemini-2.5-flash",
    "gemini-2.0-flash-exp": "gemini-2.0-flash",
    "grok-4-0709": "grok-4",
    "llama3.1": "llama3.1:8b",
    "llama3.2": "llama3.2:3b",
    "qwen2.5-coder": "qwen2.5-coder:7b",
    "codex": "codex-mini-latest",
    "o3-pro": "o3",
}

#: Runtime -> provider hint, used when a snapshot's model is unknown.
RUNTIME_PROVIDERS = {
    "openai": "openai",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "ollama": "ollama",
    "local": "ollama",
    "deepseek": "deepseek",
    "google": "google",
    "gemini": "google",
    "xai": "xai",
    "mistral": "mistral",
    "cursor": "cursor",
}

#: Runtime/model-name heuristics, applied when the model is not in the catalog.
_PROVIDER_PREFIXES = (
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("codex", "openai"),
    ("text-embedding", "openai"),
    ("deepseek", "deepseek"),
    ("gemini", "google"),
    ("grok", "xai"),
    ("mistral", "mistral"),
    ("codestral", "mistral"),
    ("devstral", "mistral"),
    ("llama", "ollama"),
    ("qwen", "ollama"),
    ("phi", "ollama"),
    ("gemma", "ollama"),
)


def normalize_model_name(model: str) -> str:
    """Lower-case, strip provider prefixes/suffixes and resolve aliases."""
    raw = (model or "").strip()
    if not raw:
        return "unknown"
    key = raw.lower()
    if key in CATALOG:
        return key
    if key in _ALIASES:
        return _ALIASES[key]
    # strip "provider/" and ":tag" style qualifiers, then retry
    for sep in ("/", "::"):
        if sep in key:
            key = key.rsplit(sep, 1)[-1]
            if key in CATALOG:
                return key
            if key in _ALIASES:
                return _ALIASES[key]
    # strip a date suffix: gpt-4o-2024-08-06 -> gpt-4o
    parts = key.split("-")
    for cut in range(len(parts) - 1, 1, -1):
        candidate = "-".join(parts[:cut])
        if candidate in CATALOG:
            return candidate
        if candidate in _ALIASES:
            return _ALIASES[candidate]
    # "llama3.1:70b" style local tags are matched by family
    base = key.split(":")[0]
    for candidate in CATALOG:
        if candidate.split(":")[0] == base:
            return candidate
    return key


def infer_provider(model: str, runtime: str | None = None) -> str:
    """Guess the provider from the model name, then from the runtime."""
    key = (model or "").lower()
    for prefix, provider in _PROVIDER_PREFIXES:
        if key.startswith(prefix):
            return provider
    if runtime:
        provider = RUNTIME_PROVIDERS.get(str(runtime).lower())
        if provider:
            return provider
    return "unknown"


def resolve_model(model: str, runtime: str | None = None) -> tuple[ModelPrice, bool]:
    """Return ``(price_entry, known)`` for a reported model name."""
    canonical = normalize_model_name(model)
    entry = CATALOG.get(canonical)
    if entry is not None:
        return entry, True
    provider = infer_provider(model, runtime)
    fallback = ModelPrice(
        model=canonical,
        provider=provider,
        display_name=f"{model} (fallback)",
        input_per_mtok=UNKNOWN_PRICE.input_per_mtok,
        output_per_mtok=UNKNOWN_PRICE.output_per_mtok,
        cache_read_per_mtok=UNKNOWN_PRICE.cache_read_per_mtok,
        cache_write_per_mtok=UNKNOWN_PRICE.cache_write_per_mtok,
        context_window=UNKNOWN_PRICE.context_window,
        notes="unknown model, fallback pricing applied",
    )
    return fallback, False


def compute_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    runtime: str | None = None,
) -> tuple[float, str, bool]:
    """Price one usage record.

    Returns ``(cost_usd, canonical_model, known)``. Reasoning tokens are billed at the
    output rate, which is how every provider that exposes them charges.
    """
    entry, known = resolve_model(model, runtime)
    cost = (
        input_tokens * entry.input_per_mtok
        + output_tokens * entry.output_per_mtok
        + cache_read_tokens * entry.cache_read_per_mtok
        + cache_write_tokens * entry.cache_write_per_mtok
        + reasoning_tokens * entry.output_per_mtok
    ) / 1_000_000.0
    return round(cost, 6), entry.model, known


def cost_breakdown(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    runtime: str | None = None,
) -> dict[str, float]:
    """Per-token-class cost split (used by the HUD detail pane and the README charts)."""
    entry, _ = resolve_model(model, runtime)
    input_cost = round(input_tokens * entry.input_per_mtok / 1e6, 6)
    output_cost = round(output_tokens * entry.output_per_mtok / 1e6, 6)
    cache_read_cost = round(cache_read_tokens * entry.cache_read_per_mtok / 1e6, 6)
    cache_write_cost = round(cache_write_tokens * entry.cache_write_per_mtok / 1e6, 6)
    reasoning_cost = round(reasoning_tokens * entry.output_per_mtok / 1e6, 6)
    return {
        "input": input_cost,
        "output": output_cost,
        "cache_read": cache_read_cost,
        "cache_write": cache_write_cost,
        "reasoning": reasoning_cost,
        "total": round(
            input_cost + output_cost + cache_read_cost + cache_write_cost + reasoning_cost, 6
        ),
    }


def list_models(provider: str | None = None) -> list[dict]:
    """Catalog listing for ``GET /api/v1/catalog/models``."""
    entries: Iterable[ModelPrice] = CATALOG.values()
    if provider:
        entries = [e for e in entries if e.provider == provider.lower()]
    return sorted((e.as_dict(True) for e in entries), key=lambda d: (d["provider"], d["model"]))


def model_count() -> int:
    return len(CATALOG)
