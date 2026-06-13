"""Tests for phantom.presets module."""

import pytest

from phantom.presets import PRESETS, get_preset_task, list_presets


class TestGetPresetTask:
    """Tests for get_preset_task."""

    def test_screenshot_preset(self):
        task = get_preset_task("screenshot", url="https://example.com")
        assert "example.com" in task
        assert "screenshot" in task.lower()

    def test_extract_preset(self):
        task = get_preset_task("extract", url="https://example.com")
        assert "example.com" in task
        assert "extract" in task.lower()

    def test_search_preset(self):
        task = get_preset_task("search", query="AI news 2026")
        assert "AI news 2026" in task
        assert "bing" in task.lower()

    def test_extract_links_preset(self):
        task = get_preset_task("extract_links", url="https://example.com")
        assert "example.com" in task
        assert "links" in task.lower()

    def test_monitor_preset(self):
        task = get_preset_task("monitor", url="https://example.com")
        assert "example.com" in task

    def test_case_insensitive(self):
        task = get_preset_task("SCREENSHOT", url="https://example.com")
        assert "example.com" in task

    def test_unknown_preset_raises(self):
        with pytest.raises(ValueError, match="Unknown preset"):
            get_preset_task("nonexistent")

    def test_missing_url_raises(self):
        with pytest.raises(ValueError, match="requires a --url"):
            get_preset_task("screenshot")

    def test_search_no_url_needed(self):
        # search preset should work without URL
        task = get_preset_task("search", query="test")
        assert "test" in task

    def test_whitespace_stripped(self):
        task = get_preset_task("  screenshot  ", url="https://example.com")
        assert "example.com" in task


class TestListPresets:
    """Tests for list_presets."""

    def test_returns_string(self):
        result = list_presets()
        assert isinstance(result, str)

    def test_lists_all_presets(self):
        result = list_presets()
        for name in PRESETS:
            assert name in result

    def test_shows_descriptions(self):
        result = list_presets()
        assert "screenshot" in result.lower() or "Screenshot" in result
