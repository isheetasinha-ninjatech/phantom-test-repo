"""
Core LiteLLM Client Configuration
==================================

Reads API credentials from /root/.claude/settings.json and provides
shared configuration for all utility modules.

Environment variables (set automatically from settings.json):
    LITELLM_API_KEY  - API key for the gateway
    LITELLM_BASE_URL - Base URL of the LiteLLM gateway

You can also override by setting these env vars before importing.
"""

import json
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Settings discovery
# ---------------------------------------------------------------------------

SETTINGS_PATHS = [
    Path("/root/.claude/settings.json"),
    Path(__file__).resolve().parent.parent / "settings.json",
]

_config_cache = None


def _load_settings() -> dict:
    """Load settings from the first available settings file."""
    for path in SETTINGS_PATHS:
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            env = data.get("env", {})
            return {
                "api_key": env.get("ANTHROPIC_AUTH_TOKEN", ""),
                "base_url": env.get("ANTHROPIC_BASE_URL", ""),
                "default_model": env.get("ANTHROPIC_MODEL", "claude-opus-4-8"),
                "source": str(path),
            }
    return {}


def get_config() -> dict:
    """
    Get the gateway configuration.

    Returns a dict with keys: api_key, base_url, default_model, source.
    Values can be overridden with environment variables:
        LITELLM_API_KEY, LITELLM_BASE_URL
    """
    global _config_cache
    if _config_cache is None:
        _config_cache = _load_settings()

    return {
        "api_key": os.environ.get("LITELLM_API_KEY", _config_cache.get("api_key", "")),
        "base_url": os.environ.get(
            "LITELLM_BASE_URL", _config_cache.get("base_url", "")
        ),
        "default_model": _config_cache.get("default_model", "claude-opus-4-8"),
        "source": _config_cache.get("source", "env"),
    }


def get_headers(extra: dict | None = None) -> dict:
    """Return standard Authorization + Content-Type headers."""
    cfg = get_config()
    h = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def api_url(path: str) -> str:
    """Build a full API URL from a relative path like '/v1/chat/completions'."""
    cfg = get_config()
    base = cfg["base_url"].rstrip("/")
    return f"{base}{path}"


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

MODELS = {
    # Chat / Text models
    "claude-opus": "claude-opus-4-8",  # Default: latest Opus
    "claude-opus-4-8": "claude-opus-4-8",  # Explicit alias for the latest
    "claude-opus-4-7": "claude-opus-4-7",  # Previous generation (kept for migration)
    "claude-opus-4-6": "claude-opus-4-6",  # Previous generation (still fully supported)
    "claude-sonnet": "claude-sonnet-4-6",  # Was: claude-sonnet-4-5-20250929 (retired)
    "claude-sonnet-4-6": "claude-sonnet-4-6",  # Explicit alias
    "claude-haiku": "claude-haiku-4-5-20251001",
    "gpt-5": "openai/openai/gpt-5.5",  # Was: gpt-5.2 (retired); 5.5 is current
    "gpt-5.5": "openai/openai/gpt-5.5",  # Explicit alias
    "gpt-5.4": "openai/openai/gpt-5.4",  # Explicit alias (still available)
    "gemini-pro": "google/gemini/gemini-3-pro-preview",
    "ninja-fast": "ninja-cline-fast",
    "ninja-standard": "ninja-cline-standard",
    "ninja-complex": "ninja-cline-complex",
    # Image models
    "gpt-image": "alias/openai/gpt-image-2.0",  # Default (new): state-of-the-art, up to 2K, 16 reference images
    "gpt-image-2": "alias/openai/gpt-image-2.0",  # Explicit alias for the latest
    "gpt-image-1.5": "openai/openai/gpt-image-1.5",  # Legacy — kept for backward compatibility
    "gemini-image": "google/gemini/gemini-3-pro-image-preview",
    # Video models
    "sora": "openai/openai/sora-2",
    "sora-pro": "openai/openai/sora-2-pro",
    # Embedding models
    "embed-small": "openai/openai/text-embedding-3-small",
    "embed-large": "openai/openai/text-embedding-3-large",
}


def resolve_model(name: str) -> str:
    """
    Resolve a short model alias to its full gateway model ID.

    Examples:
        resolve_model("claude-sonnet")  -> "claude-sonnet-4-5-20250929"
        resolve_model("gpt-5")         -> "openai/openai/gpt-5.2"
        resolve_model("sora")          -> "openai/openai/sora-2"

    If the name is not a known alias, it is returned as-is (assumed to be
    a full model ID already).
    """
    return MODELS.get(name, name)


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg = get_config()
    print(f"Source:  {cfg['source']}")
    print(f"Base:    {cfg['base_url']}")
    print(f"Key:     {cfg['api_key'][:10]}...{cfg['api_key'][-4:]}")
    print(f"Default: {cfg['default_model']}")
    print(f"\nModel aliases:")
    for alias, full in MODELS.items():
        print(f"  {alias:20s} -> {full}")
