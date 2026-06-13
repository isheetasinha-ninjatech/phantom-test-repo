"""
Phantom — Browser Automation Agent

Usage:
    # Via orchestrator (primary entry point)
    python -m phantom                              # Default work loop
    python -m phantom "Go to google.com and search for AI news"

    # Python API (for direct use in scripts)
    from phantom.observer import observe
    from phantom.actions import execute_action, set_elements
    from browser_interface import BrowserInterface

    # Connect to persistent browser (preferred — tabs survive between tasks)
    browser = BrowserInterface.connect_cdp()
    obs = observe(browser, step=0)
    set_elements(obs["interactive_elements"])
    result = execute_action(browser, "click", {"selector": "#submit"})
    browser.stop()  # Disconnects only — browser keeps running

    # Browser server management
    from phantom.browser_server import ensure_running
    ensure_running()  # Starts browser if not already running

    # Presets
    from phantom.presets import get_preset_task
    task = get_preset_task("screenshot", url="https://example.com")
"""

from phantom.config import PhantomConfig
from phantom.presets import get_preset_task, list_presets

__all__ = ["PhantomConfig", "get_preset_task", "list_presets"]
