"""Tests for ninja.config module."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from ninja.config import BROWSER_DATA_DIR, NINJA_DIR, SCREENSHOTS_DIR, NinjaConfig


class TestNinjaConfig:
    """Tests for NinjaConfig dataclass."""

    def test_defaults(self):
        config = NinjaConfig()
        assert config.model == "claude-opus-4-8"
        assert config.max_tokens == 4096
        assert config.temperature == 0.0
        assert config.headless is False
        assert config.viewport_width == 1600
        assert config.viewport_height == 900
        assert config.timeout == 30000
        assert config.slow_mo == 0
        assert config.proxy is None
        assert config.max_steps == 30
        assert config.screenshot_on_step is True
        assert config.verbose is False

    def test_custom_values(self):
        config = NinjaConfig(
            model="gpt-4o",
            max_steps=10,
            headless=True,
            proxy="http://proxy:8080",
        )
        assert config.model == "gpt-4o"
        assert config.max_steps == 10
        assert config.headless is True
        assert config.proxy == "http://proxy:8080"

    def test_load_defaults(self):
        config = NinjaConfig.load()
        assert config.model == "claude-opus-4-8"
        assert config.max_steps == 30

    def test_load_from_json_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"model": "test-model", "max_steps": 5}, f)
            f.flush()
            try:
                config = NinjaConfig.load(f.name)
                assert config.model == "test-model"
                assert config.max_steps == 5
            finally:
                os.unlink(f.name)

    def test_load_from_env_vars(self):
        env_vars = {
            "NINJA_MODEL": "env-model",
            "NINJA_MAX_STEPS": "15",
            "NINJA_HEADLESS": "true",
            "NINJA_PROXY": "http://env-proxy:9090",
            "NINJA_VERBOSE": "1",
            "NINJA_TIMEOUT": "60000",
        }
        old_values = {}
        for k, v in env_vars.items():
            old_values[k] = os.environ.get(k)
            os.environ[k] = v
        try:
            config = NinjaConfig.load()
            assert config.model == "env-model"
            assert config.max_steps == 15
            assert config.headless is True
            assert config.proxy == "http://env-proxy:9090"
            assert config.verbose is True
            assert config.timeout == 60000
        finally:
            for k, v in old_values.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_env_overrides_json(self):
        """Env vars should override JSON file values."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"model": "file-model", "max_steps": 5}, f)
            f.flush()
            old_val = os.environ.get("NINJA_MODEL")
            os.environ["NINJA_MODEL"] = "env-wins"
            try:
                config = NinjaConfig.load(f.name)
                assert config.model == "env-wins"
                assert config.max_steps == 5  # from file
            finally:
                if old_val is None:
                    os.environ.pop("NINJA_MODEL", None)
                else:
                    os.environ["NINJA_MODEL"] = old_val
                os.unlink(f.name)

    def test_directories_exist(self):
        assert SCREENSHOTS_DIR.exists()
        assert BROWSER_DATA_DIR.exists()
        assert NINJA_DIR.exists()

    def test_unknown_fields_ignored(self):
        """Unknown keys in JSON should not cause errors."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"model": "test", "unknown_field": "ignored"}, f)
            f.flush()
            try:
                config = NinjaConfig.load(f.name)
                assert config.model == "test"
            finally:
                os.unlink(f.name)
