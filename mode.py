"""Phantom runtime mode resolver.

Selects between ``slack`` (the default existing Phantom path) and
``whatsapp`` (the WhatsApp Ninja runtime). Resolution order:

1. ``PHANTOM_MODE`` environment variable. ``.env`` is loaded
   automatically by the orchestrator's existing dotenv pickup so this
   covers both shell and ``.env`` configuration.
2. ``mode`` key in ``~/.agent_settings.json`` (written by
   ``scripts/phantom-install.sh --mode whatsapp``).
3. Default: ``slack``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

PhantomMode = Literal["slack", "whatsapp"]

VALID_MODES: tuple[PhantomMode, ...] = ("slack", "whatsapp")
DEFAULT_MODE: PhantomMode = "slack"

SETTINGS_PATH = Path.home() / ".agent_settings.json"


def _normalize(value: str | None) -> PhantomMode | None:
    if not value:
        return None
    v = value.strip().lower()
    if v in VALID_MODES:
        return v  # type: ignore[return-value]
    return None


def _from_env() -> PhantomMode | None:
    return _normalize(os.environ.get("PHANTOM_MODE"))


def _from_settings() -> PhantomMode | None:
    if not SETTINGS_PATH.exists():
        return None
    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    # Top-level "mode" is the canonical key; we also accept
    # "whatsapp.mode" for forward compatibility but prefer the simpler shape.
    top = _normalize(data.get("mode"))
    if top:
        return top
    wa = data.get("whatsapp")
    if isinstance(wa, dict):
        return _normalize(wa.get("mode"))
    return None


def get_phantom_mode() -> PhantomMode:
    """Return the active Phantom runtime mode."""
    return _from_env() or _from_settings() or DEFAULT_MODE


if __name__ == "__main__":
    # Tiny CLI: `python -m phantom.mode` prints the resolved mode.
    print(get_phantom_mode())
