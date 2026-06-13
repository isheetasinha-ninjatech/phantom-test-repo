"""
Phantom configuration.

Reads settings from environment variables, phantom/config.json, or defaults.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PHANTOM_DIR = Path(__file__).parent
PROJECT_ROOT = PHANTOM_DIR.parent
BROWSER_DATA_DIR = PHANTOM_DIR / "browser_data"
SCREENSHOTS_DIR = PHANTOM_DIR / "screenshots"

# Ensure dirs exist
BROWSER_DATA_DIR.mkdir(exist_ok=True)
SCREENSHOTS_DIR.mkdir(exist_ok=True)


@dataclass
class PhantomConfig:
    # LLM settings
    model: str = "claude-opus-4-8"
    max_tokens: int = 4096
    temperature: float = 0.0

    # Browser settings
    headless: bool = False
    viewport_width: int = 1600
    viewport_height: int = 900
    timeout: int = 30000
    slow_mo: int = 0
    user_data_dir: str = str(BROWSER_DATA_DIR)

    # Proxy settings
    proxy: Optional[str] = None  # e.g. "http://proxy:8080"

    # Agent settings
    max_steps: int = 30
    screenshot_on_step: bool = True
    verbose: bool = False

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "PhantomConfig":
        """Load config from JSON file, env vars, then defaults."""
        data = {}

        # 1. Load from file
        path = Path(config_path) if config_path else PHANTOM_DIR / "config.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)

        # 2. Override with env vars
        env_map = {
            "PHANTOM_MODEL": "model",
            "PHANTOM_MAX_STEPS": ("max_steps", int),
            "PHANTOM_HEADLESS": ("headless", lambda v: v.lower() in ("1", "true")),
            "PHANTOM_PROXY": "proxy",
            "PHANTOM_VERBOSE": ("verbose", lambda v: v.lower() in ("1", "true")),
            "PHANTOM_TIMEOUT": ("timeout", int),
        }
        for env_key, mapping in env_map.items():
            val = os.environ.get(env_key)
            if val is not None:
                if isinstance(mapping, str):
                    data[mapping] = val
                else:
                    field_name, converter = mapping
                    data[field_name] = converter(val)

        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
