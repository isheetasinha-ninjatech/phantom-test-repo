"""Tests for phantom.observer module."""

from unittest.mock import MagicMock, mock_open, patch

import pytest

from phantom.observer import (
    _build_text_summary,
    _detect_overlay,
    _extract_interactive_elements,
    _format_a11y_node,
    _inject_som_labels,
    _remove_som_labels,
)


class TestFormatA11yNode:
    """Tests for _format_a11y_node."""

    def test_simple_node(self):
        node = {"role": "button", "name": "Submit"}
        result = _format_a11y_node(node)
        assert '[button] "Submit"' in result

    def test_node_with_value(self):
        node = {"role": "textbox", "name": "Email", "value": "user@test.com"}
        result = _format_a11y_node(node)
        assert "[textbox]" in result
        assert '"Email"' in result
        assert 'value="user@test.com"' in result

    def test_focused_node(self):
        node = {"role": "textbox", "name": "Search", "focused": True}
        result = _format_a11y_node(node)
        assert "(focused)" in result

    def test_node_with_description(self):
        node = {"role": "img", "name": "Logo", "description": "Company logo"}
        result = _format_a11y_node(node)
        assert 'desc="Company logo"' in result

    def test_nested_children(self):
        node = {
            "role": "navigation",
            "name": "Main",
            "children": [
                {"role": "link", "name": "Home"},
                {"role": "link", "name": "About"},
            ],
        }
        result = _format_a11y_node(node)
        assert "[navigation]" in result
        assert '[link] "Home"' in result
        assert '[link] "About"' in result

    def test_max_depth_respected(self):
        node = {
            "role": "document",
            "name": "root",
            "children": [
                {
                    "role": "div",
                    "name": "level1",
                    "children": [
                        {
                            "role": "div",
                            "name": "level2",
                        }
                    ],
                }
            ],
        }
        result = _format_a11y_node(node, max_depth=1)
        assert "root" in result
        assert "level1" in result
        assert "level2" not in result

    def test_generic_nodes_skipped(self):
        node = {
            "role": "generic",
            "name": "",
            "children": [{"role": "button", "name": "OK"}],
        }
        result = _format_a11y_node(node)
        assert "[generic]" not in result
        assert '[button] "OK"' in result

    def test_none_role_skipped(self):
        node = {
            "role": "none",
            "name": "",
            "children": [{"role": "link", "name": "Click"}],
        }
        result = _format_a11y_node(node)
        assert "[none]" not in result
        assert '[link] "Click"' in result

    def test_long_name_truncated(self):
        node = {"role": "heading", "name": "A" * 200}
        result = _format_a11y_node(node)
        # Name should be truncated to 80 chars
        assert len(result) < 200

    def test_empty_node(self):
        result = _format_a11y_node({})
        # Empty dict has role="" (not in skip list), renders as "[]"
        assert result == "[]"

    def test_indentation(self):
        node = {
            "role": "document",
            "name": "root",
            "children": [{"role": "heading", "name": "Title"}],
        }
        result = _format_a11y_node(node)
        lines = result.split("\n")
        assert lines[0].startswith("[document]")
        assert lines[1].startswith("  [heading]")  # indented


class TestBuildTextSummary:
    """Tests for _build_text_summary."""

    def test_normal_page(self):
        browser = MagicMock()
        browser.text.return_value = "Hello World, this is a test page."
        result = _build_text_summary(browser)
        assert "Hello World" in result
        assert result.startswith("Page text:")

    def test_empty_page(self):
        browser = MagicMock()
        browser.text.return_value = ""
        result = _build_text_summary(browser)
        assert result == "(empty page)"

    def test_error_handling(self):
        browser = MagicMock()
        browser.text.side_effect = Exception("page crashed")
        result = _build_text_summary(browser)
        assert result == "(empty page)"

    def test_long_text_truncated(self):
        browser = MagicMock()
        browser.text.return_value = "x" * 5000
        result = _build_text_summary(browser)
        assert len(result) <= 2100  # "Page text: " + 2000 chars max


class TestDetectOverlay:
    """Tests for _detect_overlay."""

    def test_no_overlay(self):
        browser = MagicMock()
        browser.evaluate.return_value = False
        assert _detect_overlay(browser) is False

    def test_overlay_detected(self):
        browser = MagicMock()
        browser.evaluate.return_value = True
        assert _detect_overlay(browser) is True

    def test_error_returns_false(self):
        browser = MagicMock()
        browser.evaluate.side_effect = Exception("JS error")
        assert _detect_overlay(browser) is False


class TestExtractInteractiveElements:
    """Tests for _extract_interactive_elements."""

    def test_returns_list(self):
        browser = MagicMock()
        browser.evaluate.return_value = [
            {
                "index": 0,
                "tag": "button",
                "text": "Click",
                "selector": "#btn",
                "visible": True,
            }
        ]
        result = _extract_interactive_elements(browser)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["tag"] == "button"

    def test_empty_page(self):
        browser = MagicMock()
        browser.evaluate.return_value = []
        result = _extract_interactive_elements(browser)
        assert result == []

    def test_none_returns_empty(self):
        browser = MagicMock()
        browser.evaluate.return_value = None
        result = _extract_interactive_elements(browser)
        assert result == []

    def test_error_returns_empty(self):
        browser = MagicMock()
        browser.evaluate.side_effect = Exception("page gone")
        result = _extract_interactive_elements(browser)
        assert result == []


class TestSomLabels:
    """Tests for Set-of-Mark label injection/removal."""

    def test_inject_with_elements(self):
        browser = MagicMock()
        elements = [
            {"index": 0, "selector": "#btn", "selectors": ["#btn"], "visible": True},
            {"index": 1, "selector": "input", "selectors": ["input"], "visible": True},
        ]
        _inject_som_labels(browser, elements)
        browser.evaluate.assert_called_once()
        # Check that label data was passed
        call_args = browser.evaluate.call_args
        label_data = call_args[0][1]
        assert len(label_data) == 2
        assert label_data[0]["index"] == 0
        assert label_data[1]["index"] == 1

    def test_inject_empty_elements(self):
        browser = MagicMock()
        _inject_som_labels(browser, [])
        browser.evaluate.assert_not_called()

    def test_inject_no_visible_elements(self):
        browser = MagicMock()
        elements = [
            {"index": 0, "selector": "#btn", "selectors": ["#btn"], "visible": False},
        ]
        _inject_som_labels(browser, elements)
        browser.evaluate.assert_not_called()

    def test_inject_caps_at_50(self):
        browser = MagicMock()
        elements = [
            {
                "index": i,
                "selector": f"#btn{i}",
                "selectors": [f"#btn{i}"],
                "visible": True,
            }
            for i in range(100)
        ]
        _inject_som_labels(browser, elements)
        call_args = browser.evaluate.call_args
        label_data = call_args[0][1]
        assert len(label_data) == 50

    def test_inject_error_handled(self):
        browser = MagicMock()
        browser.evaluate.side_effect = Exception("JS error")
        elements = [
            {"index": 0, "selector": "#btn", "selectors": ["#btn"], "visible": True}
        ]
        # Should not raise
        _inject_som_labels(browser, elements)

    def test_remove_labels(self):
        browser = MagicMock()
        _remove_som_labels(browser)
        browser.evaluate.assert_called_once()

    def test_remove_labels_error_handled(self):
        browser = MagicMock()
        browser.evaluate.side_effect = Exception("JS error")
        # Should not raise
        _remove_som_labels(browser)
