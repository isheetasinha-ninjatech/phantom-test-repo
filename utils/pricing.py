"""
Unified model pricing for Claude API usage tracking.

All prices are USD per million tokens. This is the single source of truth
used by the dashboard (app.py), standalone monitor (claude_monitor.py),
and CLI log analyzer (log_analyzer.py).
"""

# Pricing per million tokens (USD)
# Patterns are matched against the model name string (case-insensitive, substring match).
# More specific patterns must come before catch-alls (e.g. "opus-4-6" before "opus-4").
MODEL_PRICING = {
    # Opus 4.8 — same rate card as 4.7/4.6.
    "opus-4-8": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.50,
    },
    "opus-4.8": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.50,
    },
    # Opus 4.7 (Apr 2026) — same rate card as 4.6, but new tokenizer produces
    # up to 35% more tokens on the same text (per Anthropic migration guide).
    "opus-4-7": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.50,
    },
    "opus-4.7": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.50,
    },
    # Opus 4.6
    "opus-4-6": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.50,
    },
    "opus-4.6": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.50,
    },
    # Opus 4.5
    "opus-4-5": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.50,
    },
    "opus-4.5": {
        "input": 5.0,
        "output": 25.0,
        "cache_write_5m": 6.25,
        "cache_write_1h": 10.0,
        "cache_read": 0.50,
    },
    # Opus 4.1
    "opus-4-1": {
        "input": 15.0,
        "output": 75.0,
        "cache_write_5m": 18.75,
        "cache_write_1h": 30.0,
        "cache_read": 1.50,
    },
    "opus-4.1": {
        "input": 15.0,
        "output": 75.0,
        "cache_write_5m": 18.75,
        "cache_write_1h": 30.0,
        "cache_read": 1.50,
    },
    # Opus 4 (catch-all for older opus-4 models)
    "opus-4": {
        "input": 15.0,
        "output": 75.0,
        "cache_write_5m": 18.75,
        "cache_write_1h": 30.0,
        "cache_read": 1.50,
    },
    # Opus 3
    "opus-3": {
        "input": 15.0,
        "output": 75.0,
        "cache_write_5m": 18.75,
        "cache_write_1h": 30.0,
        "cache_read": 1.50,
    },
    # Sonnet 4.6
    "sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
    },
    "sonnet-4.6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
    },
    # Sonnet 4.5
    "sonnet-4-5": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
    },
    "sonnet-4.5": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
    },
    # Sonnet 4 / 3.7
    "sonnet-4": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
    },
    "sonnet-3.7": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.0,
        "cache_read": 0.30,
    },
    # Haiku 4.5
    "haiku-4-5": {
        "input": 1.0,
        "output": 5.0,
        "cache_write_5m": 1.25,
        "cache_write_1h": 2.0,
        "cache_read": 0.10,
    },
    "haiku-4.5": {
        "input": 1.0,
        "output": 5.0,
        "cache_write_5m": 1.25,
        "cache_write_1h": 2.0,
        "cache_read": 0.10,
    },
    # Haiku 3.5
    "haiku-3.5": {
        "input": 0.80,
        "output": 4.0,
        "cache_write_5m": 1.0,
        "cache_write_1h": 1.6,
        "cache_read": 0.08,
    },
    # Haiku 3
    "haiku-3": {
        "input": 0.25,
        "output": 1.25,
        "cache_write_5m": 0.30,
        "cache_write_1h": 0.50,
        "cache_read": 0.03,
    },
}

# Default fallback Opus as we use it most often
DEFAULT_PRICING = MODEL_PRICING["opus-4-8"]


def get_pricing(model: str) -> dict:
    """Get pricing for a model by substring match, with fallback to Sonnet defaults."""
    model_lower = (model or "").lower()
    for pattern, pricing in MODEL_PRICING.items():
        if pattern in model_lower:
            return pricing
    return DEFAULT_PRICING
