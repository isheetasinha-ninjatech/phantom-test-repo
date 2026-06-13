"""Tests for phantom.actions module."""

from unittest.mock import MagicMock, call, patch

import pytest

from phantom.actions import (
    _dismiss_overlay,
    _get_selector_candidates,
    _is_transient_error,
    _resolve_selector,
    _selector_cache,
    clear_selector_cache,
    execute_action,
    set_elements,
)


@pytest.fixture(autouse=True)
def clear_elements():
    """Clear stored elements and selector cache before each test."""
    set_elements([])
    clear_selector_cache()
    yield
    set_elements([])
    clear_selector_cache()


@pytest.fixture
def mock_browser():
    """Create a mock BrowserInterface."""
    browser = MagicMock()
    browser.url = "https://example.com"
    browser.title = "Example"
    browser.page = MagicMock()
    browser.goto.return_value = {
        "url": "https://example.com",
        "title": "Example",
        "status": 200,
    }
    browser.text.return_value = "Hello World"
    browser.html.return_value = "<div>Hello</div>"
    browser.attribute.return_value = "https://example.com"
    browser.query_all.return_value = 0
    return browser


class TestResolveSelector:
    """Tests for _resolve_selector."""

    def test_regular_selector(self):
        assert _resolve_selector("#submit-btn") == "#submit-btn"

    def test_css_selector(self):
        assert _resolve_selector("button.primary") == "button.primary"

    def test_index_selector_with_elements(self):
        set_elements(
            [
                {"index": 0, "selector": "#real-btn", "selectors": ["#real-btn"]},
                {
                    "index": 1,
                    "selector": "input[name='q']",
                    "selectors": ["input[name='q']"],
                },
            ]
        )
        assert _resolve_selector("[0]") == "#real-btn"
        assert _resolve_selector("[1]") == "input[name='q']"

    def test_index_selector_not_found(self):
        set_elements([{"index": 0, "selector": "#btn"}])
        result = _resolve_selector("[5]")
        assert ":nth-match" in result  # fallback

    def test_whitespace_stripped(self):
        assert _resolve_selector("  #btn  ") == "#btn"

    def test_text_selector(self):
        assert _resolve_selector("text=Submit") == "text=Submit"


class TestGetSelectorCandidates:
    """Tests for _get_selector_candidates."""

    def test_basic_selector(self):
        candidates = _get_selector_candidates("#btn")
        assert "#btn" in candidates
        assert len(candidates) >= 1

    def test_index_selector_with_alternatives(self):
        set_elements(
            [
                {
                    "index": 0,
                    "selector": "#submit",
                    "selectors": ["#submit", "button[name='submit']", "text=Submit"],
                    "id": "submit",
                },
            ]
        )
        candidates = _get_selector_candidates("[0]")
        assert "#submit" in candidates
        assert "button[name='submit']" in candidates
        assert "text=Submit" in candidates

    def test_matching_element_by_id(self):
        set_elements(
            [
                {
                    "index": 0,
                    "selector": "#search",
                    "selectors": ["#search", "input[name='q']"],
                    "id": "search",
                },
            ]
        )
        candidates = _get_selector_candidates("#search")
        assert "#search" in candidates
        assert "input[name='q']" in candidates

    def test_deduplication(self):
        set_elements(
            [
                {
                    "index": 0,
                    "selector": "#btn",
                    "selectors": ["#btn", "#btn", "text=Click"],
                    "id": "btn",
                },
            ]
        )
        candidates = _get_selector_candidates("#btn")
        assert candidates.count("#btn") == 1  # no duplicates


class TestExecuteAction:
    """Tests for execute_action."""

    def test_goto(self, mock_browser):
        result = execute_action(mock_browser, "goto", {"url": "https://example.com"})
        assert "Navigated" in result
        mock_browser.goto.assert_called_once()

    def test_goto_adds_https(self, mock_browser):
        execute_action(mock_browser, "goto", {"url": "example.com"})
        mock_browser.goto.assert_called_with("https://example.com", wait_until="load")

    def test_click(self, mock_browser):
        result = execute_action(mock_browser, "click", {"selector": "#btn"})
        assert "Clicked" in result
        mock_browser.click.assert_called_once()

    def test_fill(self, mock_browser):
        result = execute_action(
            mock_browser, "fill", {"selector": "#input", "value": "hello"}
        )
        assert "Filled" in result
        mock_browser.fill.assert_called_once()

    def test_type_text(self, mock_browser):
        result = execute_action(
            mock_browser, "type_text", {"selector": "#input", "text": "hello"}
        )
        assert "Typed" in result
        mock_browser.type_text.assert_called_once()

    def test_press_key(self, mock_browser):
        result = execute_action(mock_browser, "press", {"key": "Enter"})
        assert "Pressed Enter" in result
        mock_browser.page.keyboard.press.assert_called_with("Enter")

    def test_press_with_selector(self, mock_browser):
        result = execute_action(
            mock_browser, "press", {"key": "Enter", "selector": "#input"}
        )
        assert "Pressed Enter" in result
        mock_browser.press.assert_called_once()

    def test_scroll_down(self, mock_browser):
        result = execute_action(mock_browser, "scroll_down", {"px": 300})
        assert "Scrolled down 300px" in result
        mock_browser.scroll_down.assert_called_with(px=300)

    def test_scroll_up(self, mock_browser):
        result = execute_action(mock_browser, "scroll_up", {})
        assert "Scrolled up 500px" in result  # default
        mock_browser.scroll_up.assert_called_with(px=500)

    def test_extract_text(self, mock_browser):
        result = execute_action(mock_browser, "extract_text", {"selector": "body"})
        assert "Hello World" in result

    def test_extract_html(self, mock_browser):
        result = execute_action(mock_browser, "extract_html", {"selector": "div"})
        assert "<div>" in result

    def test_extract_attribute(self, mock_browser):
        result = execute_action(
            mock_browser, "extract_attribute", {"selector": "a", "attribute": "href"}
        )
        assert "https://example.com" in result

    def test_go_back(self, mock_browser):
        result = execute_action(mock_browser, "go_back", {})
        assert "back" in result.lower()

    def test_go_forward(self, mock_browser):
        result = execute_action(mock_browser, "go_forward", {})
        assert "forward" in result.lower()

    def test_reload(self, mock_browser):
        result = execute_action(mock_browser, "reload", {})
        assert "Reloaded" in result

    def test_wait(self, mock_browser):
        result = execute_action(mock_browser, "wait", {"seconds": 1})
        assert "Waited 1s" in result
        mock_browser.sleep.assert_called_with(1)

    def test_done(self, mock_browser):
        result = execute_action(mock_browser, "done", {"result": "Task complete"})
        assert "DONE" in result
        assert "Task complete" in result

    def test_fail(self, mock_browser):
        result = execute_action(mock_browser, "fail", {"reason": "Can't find element"})
        assert "FAIL" in result

    def test_need_human(self, mock_browser):
        result = execute_action(
            mock_browser, "need_human", {"reason": "CAPTCHA detected"}
        )
        assert "NEED_HUMAN" in result

    def test_unknown_action(self, mock_browser):
        result = execute_action(mock_browser, "nonexistent_action", {})
        assert "Unknown action" in result

    def test_error_handling(self, mock_browser):
        mock_browser.click.side_effect = Exception("Element not found")
        result = execute_action(mock_browser, "click", {"selector": "#missing"})
        assert "ERROR" in result
        assert "Element not found" in result

    def test_action_case_insensitive(self, mock_browser):
        result = execute_action(mock_browser, "GOTO", {"url": "https://example.com"})
        assert "Navigated" in result

    def test_hover(self, mock_browser):
        result = execute_action(mock_browser, "hover", {"selector": "#menu"})
        assert "Hovered" in result

    def test_check(self, mock_browser):
        result = execute_action(mock_browser, "check", {"selector": "#checkbox"})
        assert "Checked" in result

    def test_select_option_by_value(self, mock_browser):
        result = execute_action(
            mock_browser, "select_option", {"selector": "#dropdown", "value": "opt1"}
        )
        assert "Selected" in result

    def test_select_option_by_label(self, mock_browser):
        result = execute_action(
            mock_browser,
            "select_option",
            {"selector": "#dropdown", "label": "Option 1"},
        )
        assert "Selected" in result
        assert "Option 1" in result

    def test_scroll_to(self, mock_browser):
        result = execute_action(mock_browser, "scroll_to", {"selector": "#footer"})
        assert "Scrolled to" in result

    def test_screenshot(self, mock_browser):
        result = execute_action(mock_browser, "screenshot", {"filename": "test.png"})
        assert "Screenshot saved" in result
        mock_browser.screenshot.assert_called_once()


class TestDismissOverlay:
    """Tests for _dismiss_overlay."""

    def test_no_overlay(self, mock_browser):
        mock_browser.query_all.return_value = 0
        result = _dismiss_overlay(mock_browser)
        # Should try Escape as fallback
        mock_browser.page.keyboard.press.assert_called_with("Escape")

    def test_overlay_found(self, mock_browser):
        # First selector returns a match
        mock_browser.query_all.side_effect = [1]  # first selector matches
        result = _dismiss_overlay(mock_browser)
        assert "Dismissed" in result
        mock_browser.click.assert_called_once()

    def test_overlay_click_fails_tries_next(self, mock_browser):
        # First returns 1 but click fails, second returns 1 and succeeds
        mock_browser.query_all.side_effect = [1, 1]
        mock_browser.click.side_effect = [Exception("click failed"), None]
        result = _dismiss_overlay(mock_browser)
        assert "Dismissed" in result


class TestSelfHealing:
    """Tests for self-healing selector resolution."""

    def test_click_with_healing_primary_succeeds(self, mock_browser):
        result = execute_action(mock_browser, "click", {"selector": "#btn"})
        assert "Clicked" in result
        assert mock_browser.click.call_count == 1

    def test_click_with_healing_fallback(self, mock_browser):
        set_elements(
            [
                {
                    "index": 0,
                    "selector": "#btn",
                    "selectors": ["#btn", "button[name='submit']", "text=Submit"],
                    "id": "btn",
                },
            ]
        )
        # Primary fails, second alternative succeeds
        mock_browser.click.side_effect = [
            Exception("not found"),  # #btn fails
            None,  # button[name='submit'] succeeds
        ]
        result = execute_action(mock_browser, "click", {"selector": "#btn"})
        assert "Clicked" in result
        assert mock_browser.click.call_count == 2

    def test_fill_with_healing_fallback(self, mock_browser):
        set_elements(
            [
                {
                    "index": 0,
                    "selector": "#email",
                    "selectors": ["#email", "input[name='email']"],
                    "id": "email",
                },
            ]
        )
        mock_browser.fill.side_effect = [
            Exception("not found"),
            None,
        ]
        result = execute_action(
            mock_browser, "fill", {"selector": "#email", "value": "test@test.com"}
        )
        assert "Filled" in result


class TestSelectorCache:
    """Tests for the selector cache (cross-step memory)."""

    def test_cache_records_successful_fallback(self, mock_browser):
        """When a fallback selector succeeds, it's cached for next time."""
        set_elements(
            [
                {
                    "index": 0,
                    "selector": "#btn",
                    "selectors": ["#btn", "button[name='submit']"],
                    "id": "btn",
                },
            ]
        )
        # Primary fails, fallback succeeds
        mock_browser.click.side_effect = [
            Exception("not found"),  # #btn fails
            None,  # button[name='submit'] succeeds
        ]
        execute_action(mock_browser, "click", {"selector": "#btn"})

        # Cache should map #btn -> button[name='submit']
        from phantom.actions import _selector_cache

        assert _selector_cache.get("#btn") == "button[name='submit']"

    def test_cached_selector_tried_first(self, mock_browser):
        """Cached selector should be the first candidate on retry."""
        set_elements(
            [
                {
                    "index": 0,
                    "selector": "#btn",
                    "selectors": ["#btn", "button[name='submit']", "text=Submit"],
                    "id": "btn",
                },
            ]
        )
        # Simulate a cached resolution
        import phantom.actions
        from phantom.actions import _cache_url, _selector_cache

        phantom.actions._selector_cache["#btn"] = "button[name='submit']"
        phantom.actions._cache_url = "https://example.com"

        candidates = _get_selector_candidates("#btn")
        # Cached selector should be first
        assert candidates[0] == "button[name='submit']"

    def test_cache_cleared_on_navigation(self, mock_browser):
        """Cache should be invalidated when navigating to a new page."""
        import phantom.actions

        phantom.actions._selector_cache["#btn"] = "button[name='submit']"
        phantom.actions._cache_url = "https://example.com"

        execute_action(mock_browser, "goto", {"url": "https://other.com"})

        assert len(phantom.actions._selector_cache) == 0

    def test_cache_cleared_on_go_back(self, mock_browser):
        """Cache should be invalidated on go_back."""
        import phantom.actions

        phantom.actions._selector_cache["#btn"] = "fallback"
        phantom.actions._cache_url = "https://example.com"

        execute_action(mock_browser, "go_back", {})

        assert len(phantom.actions._selector_cache) == 0

    def test_cache_cleared_on_reload(self, mock_browser):
        """Cache should be invalidated on reload."""
        import phantom.actions

        phantom.actions._selector_cache["#btn"] = "fallback"
        phantom.actions._cache_url = "https://example.com"

        execute_action(mock_browser, "reload", {})

        assert len(phantom.actions._selector_cache) == 0

    def test_cache_invalidated_on_url_change(self, mock_browser):
        """Cache should be invalidated if browser URL changes between actions."""
        import phantom.actions

        phantom.actions._selector_cache["#btn"] = "fallback"
        phantom.actions._cache_url = "https://old-page.com"

        # Browser URL is now different
        mock_browser.url = "https://new-page.com"
        execute_action(mock_browser, "scroll_down", {"px": 500})

        assert len(phantom.actions._selector_cache) == 0

    def test_same_selector_not_cached(self, mock_browser):
        """If primary selector succeeds, it's not redundantly cached."""
        execute_action(mock_browser, "click", {"selector": "#btn"})

        from phantom.actions import _selector_cache

        assert "#btn" not in _selector_cache


class TestTransientErrorRetry:
    """Tests for smart retry on transient Playwright errors."""

    def test_is_transient_detached(self):
        assert _is_transient_error(Exception("Element is detached from DOM"))

    def test_is_transient_context_destroyed(self):
        assert _is_transient_error(Exception("Execution context was destroyed"))

    def test_is_transient_intercepted(self):
        assert _is_transient_error(
            Exception("Element click intercepted by another element")
        )

    def test_is_not_transient_not_found(self):
        assert not _is_transient_error(Exception("Element not found"))

    def test_is_not_transient_timeout(self):
        assert not _is_transient_error(Exception("Timeout 5000ms exceeded"))

    def test_click_retries_on_transient(self, mock_browser):
        """Click should retry once on transient errors before moving to fallback."""
        mock_browser.click.side_effect = [
            Exception("Element is detached"),  # first try
            None,  # retry succeeds
        ]
        result = execute_action(mock_browser, "click", {"selector": "#btn"})
        assert "Clicked" in result
        assert mock_browser.click.call_count == 2

    def test_fill_retries_on_transient(self, mock_browser):
        """Fill should retry once on transient errors."""
        mock_browser.fill.side_effect = [
            Exception("Execution context was destroyed"),  # first try
            None,  # retry succeeds
        ]
        result = execute_action(
            mock_browser, "fill", {"selector": "#input", "value": "test"}
        )
        assert "Filled" in result
        assert mock_browser.fill.call_count == 2


class TestEnsureVisible:
    """Tests for element visibility pre-check."""

    def test_offscreen_element_scrolled_into_view(self, mock_browser):
        """Element below viewport should be scrolled into view before click."""
        mock_browser.evaluate.return_value = True  # element is offscreen
        execute_action(mock_browser, "click", {"selector": "#footer-btn"})
        # Should have called scroll_to before click
        mock_browser.scroll_to.assert_called_once()
        mock_browser.click.assert_called()

    def test_visible_element_not_scrolled(self, mock_browser):
        """Element already visible should not trigger scroll."""
        mock_browser.evaluate.return_value = False  # element is visible
        execute_action(mock_browser, "click", {"selector": "#btn"})
        mock_browser.scroll_to.assert_not_called()
        mock_browser.click.assert_called()

    def test_evaluate_error_doesnt_block(self, mock_browser):
        """If visibility check fails, action should still proceed."""
        mock_browser.evaluate.side_effect = Exception("evaluate failed")
        result = execute_action(mock_browser, "click", {"selector": "#btn"})
        assert "Clicked" in result


class TestNavigationAwareClick:
    """Tests for smart post-click navigation detection."""

    def test_click_detects_navigation(self, mock_browser):
        """When URL changes after click, should wait for page load and clear cache."""
        import phantom.actions

        phantom.actions._cache_url = "https://example.com"

        # URL changes after click: first call returns original, subsequent returns new
        url_calls = iter(
            ["https://example.com", "https://example.com", "https://example.com/page2"]
        )
        type(mock_browser).url = property(
            lambda self: next(url_calls, "https://example.com/page2")
        )

        execute_action(mock_browser, "click", {"selector": "#link"})
        # Cache should be cleared due to navigation and load state waited on
        mock_browser.page.wait_for_load_state.assert_called()

    def test_click_same_page_no_extra_wait(self, mock_browser):
        """When URL stays the same after click, should not wait for load."""
        # URL stays at https://example.com (mock default)
        mock_browser.evaluate.return_value = False
        execute_action(mock_browser, "click", {"selector": "#btn"})
        # Should not have called wait_for_load_state for navigation
        # (it may be called by _maybe_invalidate_cache at start, but not for nav)
        mock_browser.click.assert_called()


class TestExtendedActions:
    """Tests for extended actions (v0.8)."""

    def test_save_pdf(self, mock_browser):
        result = execute_action(mock_browser, "save_pdf", {"filename": "test.pdf"})
        assert "PDF saved" in result
        mock_browser.pdf.assert_called_once()

    def test_scroll_to_top(self, mock_browser):
        result = execute_action(mock_browser, "scroll_to_top", {})
        assert "top" in result.lower()
        mock_browser.scroll_to_top.assert_called_once()

    def test_scroll_to_bottom(self, mock_browser):
        result = execute_action(mock_browser, "scroll_to_bottom", {})
        assert "bottom" in result.lower()
        mock_browser.scroll_to_bottom.assert_called_once()

    def test_wait_for_element(self, mock_browser):
        result = execute_action(
            mock_browser, "wait_for_element", {"selector": "#loaded"}
        )
        assert "appeared" in result
        mock_browser.wait_for.assert_called_once()

    def test_wait_for_element_timeout(self, mock_browser):
        result = execute_action(
            mock_browser, "wait_for_element", {"selector": "#loaded", "timeout": 5000}
        )
        mock_browser.wait_for.assert_called_with("#loaded", timeout=5000)

    def test_extract_table(self, mock_browser):
        mock_browser.evaluate.return_value = [
            ["Name", "Age"],
            ["Alice", "30"],
            ["Bob", "25"],
        ]
        result = execute_action(
            mock_browser, "extract_table", {"selector": "table#users"}
        )
        assert "3 rows" in result
        assert "Alice" in result
        assert "Name | Age" in result

    def test_extract_table_not_found(self, mock_browser):
        mock_browser.evaluate.return_value = None
        result = execute_action(
            mock_browser, "extract_table", {"selector": "table.missing"}
        )
        assert "No table found" in result

    def test_extract_links(self, mock_browser):
        mock_browser.evaluate.return_value = [
            {"text": "Example", "href": "https://example.com"},
            {"text": "Google", "href": "https://google.com"},
        ]
        result = execute_action(mock_browser, "extract_links", {"selector": "nav"})
        assert "2" in result
        assert "Example" in result
        assert "https://example.com" in result

    def test_extract_links_none(self, mock_browser):
        mock_browser.evaluate.return_value = []
        result = execute_action(mock_browser, "extract_links", {"selector": "body"})
        assert "No links found" in result

    def test_execute_js(self, mock_browser):
        mock_browser.evaluate.return_value = 42
        result = execute_action(mock_browser, "execute_js", {"script": "1 + 1"})
        assert "42" in result

    def test_execute_js_undefined(self, mock_browser):
        mock_browser.evaluate.return_value = None
        result = execute_action(mock_browser, "execute_js", {"script": "void 0"})
        assert "undefined" in result

    def test_get_cookies(self, mock_browser):
        mock_browser.cookies.return_value = [
            {"name": "session", "value": "abc123"},
            {"name": "theme", "value": "dark"},
        ]
        result = execute_action(mock_browser, "get_cookies", {})
        assert "session" in result
        assert "theme" in result
        assert "2" in result

    def test_clear_cookies(self, mock_browser):
        result = execute_action(mock_browser, "clear_cookies", {})
        assert "cleared" in result.lower()
        mock_browser.clear_cookies.assert_called_once()
