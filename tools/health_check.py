#!/usr/bin/env python3
"""
Health Check — Unified system diagnostics for Ninja.

Checks all critical subsystems in one command:
  - Browser server (port 9222)
  - Slack configuration and connectivity
  - GitHub CLI authentication
  - Settings file validity
  - Model configuration
  - Browser stealth status

Usage:
    python tools/health_check.py              # Human-readable output
    python tools/health_check.py --json       # JSON output for scripting
    python tools/health_check.py --fix        # Attempt auto-fix for common issues

Python API:
    from tools.health_check import run_health_check
    results = run_health_check()
    print(results["browser"]["status"])  # "ok" or "error"
"""

import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path.home() / ".agent_settings.json"
SETTINGS_FILE = REPO_ROOT / "settings.json"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
MCP_TOKEN_FILE = Path("/dev/shm/mcp-token")
SANDBOX_METADATA = Path("/dev/shm/sandbox_metadata.json")


def check_browser() -> dict:
    """Check if browser server is running on port 9222."""
    try:
        resp = urllib.request.urlopen("http://localhost:9222/json/version", timeout=3)
        if resp.status == 200:
            data = json.loads(resp.read().decode())
            return {
                "status": "ok",
                "message": "Browser server running",
                "browser": data.get("Browser", "unknown"),
                "ws_url": data.get("webSocketDebuggerUrl", ""),
            }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Browser server not responding: {e}",
            "fix": "python ninja/browser_server.py start",
        }


def check_slack() -> dict:
    """Check Slack configuration."""
    if not CONFIG_PATH.exists():
        return {
            "status": "error",
            "message": f"Config file not found: {CONFIG_PATH}",
            "fix": "python slack_interface.py config --set-agent ninja",
        }

    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)

        agent = config.get("default_agent", "")
        channel = config.get("default_channel", "")
        bot_token = config.get("bot_token", "")

        issues = []
        if not agent:
            issues.append("No default_agent configured")
        if not channel:
            issues.append("No default_channel configured")
        if not bot_token:
            issues.append("No bot_token configured")

        if issues:
            return {
                "status": "warning",
                "message": "; ".join(issues),
                "agent": agent,
                "channel": channel,
            }

        return {
            "status": "ok",
            "message": "Slack configured",
            "agent": agent,
            "channel": channel,
        }
    except (json.JSONDecodeError, IOError) as e:
        return {"status": "error", "message": f"Cannot read config: {e}"}


def check_github() -> dict:
    """Check GitHub CLI authentication."""
    if not shutil.which("gh"):
        return {"status": "error", "message": "GitHub CLI (gh) not installed"}

    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            # Extract account info from output
            output = result.stdout + result.stderr
            return {
                "status": "ok",
                "message": "GitHub authenticated",
                "details": output.strip()[:200],
            }
        else:
            has_token = MCP_TOKEN_FILE.exists()
            return {
                "status": "error",
                "message": "GitHub not authenticated",
                "token_available": has_token,
                "fix": "cat /dev/shm/mcp-token | python -c \"import sys,json; print(json.loads(sys.stdin.read().split('=',1)[1])['access_token'])\" | gh auth login --with-token"
                if has_token
                else "No token found at /dev/shm/mcp-token",
            }
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "GitHub auth check timed out"}


def check_settings() -> dict:
    """Check settings.json and model configuration."""
    issues = []
    info = {}

    # Check project settings.json
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                settings = json.load(f)
            env = settings.get("env", {})
            info["model"] = env.get("ANTHROPIC_MODEL", "not set")
            info["base_url"] = env.get("ANTHROPIC_BASE_URL", "not set")[:50]
            if not env.get("ANTHROPIC_AUTH_TOKEN"):
                issues.append("No auth token in settings.json")
        except (json.JSONDecodeError, IOError) as e:
            issues.append(f"Cannot read settings.json: {e}")
    else:
        issues.append("settings.json not found (will be auto-generated on start)")

    # Check sandbox metadata for model override
    if SANDBOX_METADATA.exists():
        try:
            with open(SANDBOX_METADATA) as f:
                meta = json.load(f)
            selected = meta.get("litellm_selected_model", "")
            if selected:
                info["sandbox_model"] = selected
        except Exception:
            pass

    # Check Claude settings
    if CLAUDE_SETTINGS.exists():
        try:
            with open(CLAUDE_SETTINGS) as f:
                cs = json.load(f)
            claude_model = cs.get("env", {}).get("ANTHROPIC_MODEL", "")
            if claude_model:
                info["claude_model"] = claude_model
        except Exception:
            pass

    if issues:
        return {"status": "warning", "message": "; ".join(issues), **info}
    return {"status": "ok", "message": "Settings valid", **info}


def check_files() -> dict:
    """Check that required project files exist."""
    required = [
        "orchestrator.py",
        "slack_interface.py",
        "browser_interface.py",
        "ninja/browser_server.py",
        "ninja/observer.py",
        "ninja/actions.py",
        "ninja/stealth.py",
        "agent-docs/NINJA_SPEC.md",
        "agent-docs/AGENT_PROTOCOL.md",
        "agent-docs/SLACK_INTERFACE.md",
        "agent-docs/PIPEDREAM_CONNECT.md",
    ]
    missing = [f for f in required if not (REPO_ROOT / f).exists()]

    if missing:
        return {
            "status": "error",
            "message": f"Missing files: {', '.join(missing)}",
            "missing": missing,
        }
    return {"status": "ok", "message": f"All {len(required)} required files present"}


def check_claude_cli() -> dict:
    """Check if Claude CLI is installed."""
    if shutil.which("claude"):
        return {"status": "ok", "message": "Claude CLI installed"}

    # Check common install locations
    home_path = Path.home() / ".local" / "bin" / "claude"
    if home_path.exists():
        return {"status": "ok", "message": f"Claude CLI found at {home_path}"}

    return {
        "status": "error",
        "message": "Claude CLI not found",
        "fix": "Install Claude Code CLI: https://docs.anthropic.com/en/docs/claude-code",
    }


def run_health_check(auto_fix: bool = False) -> dict:
    """
    Run all health checks and return structured results.

    Args:
        auto_fix: If True, attempt to fix common issues automatically.

    Returns:
        Dict mapping check name to result dict with "status", "message", etc.
    """
    results = {
        "browser": check_browser(),
        "slack": check_slack(),
        "github": check_github(),
        "settings": check_settings(),
        "files": check_files(),
        "claude_cli": check_claude_cli(),
    }

    # Auto-fix if requested
    if auto_fix:
        # Fix 1: Start browser if not running
        if results["browser"]["status"] == "error":
            try:
                subprocess.run(
                    ["python", "ninja/browser_server.py", "start"],
                    cwd=str(REPO_ROOT),
                    capture_output=True,
                    timeout=30,
                )
                results["browser"] = check_browser()
                if results["browser"]["status"] == "ok":
                    results["browser"]["fixed"] = True
            except Exception:
                pass

        # Fix 2: GitHub login if token available
        if results["github"]["status"] == "error" and MCP_TOKEN_FILE.exists():
            try:
                # Read token
                content = MCP_TOKEN_FILE.read_text()
                for line in content.strip().split("\n"):
                    if line.startswith("Github="):
                        token = json.loads(line[7:]).get("access_token", "")
                        if token:
                            subprocess.run(
                                ["gh", "auth", "login", "--with-token"],
                                input=token,
                                capture_output=True,
                                text=True,
                                timeout=15,
                            )
                            results["github"] = check_github()
                            if results["github"]["status"] == "ok":
                                results["github"]["fixed"] = True
            except Exception:
                pass

        # Fix 3: Regenerate settings.json
        if results["settings"]["status"] != "ok":
            try:
                sys.path.insert(0, str(REPO_ROOT))
                from orchestrator import ensure_settings_file, setup_logging

                logger = setup_logging("health_check")
                if ensure_settings_file(logger):
                    results["settings"] = check_settings()
                    if results["settings"]["status"] == "ok":
                        results["settings"]["fixed"] = True
            except Exception:
                pass

    # Overall status
    statuses = [r["status"] for r in results.values()]
    if all(s == "ok" for s in statuses):
        results["overall"] = "healthy"
    elif any(s == "error" for s in statuses):
        results["overall"] = "unhealthy"
    else:
        results["overall"] = "degraded"

    return results


def print_results(results: dict):
    """Pretty-print health check results."""
    icons = {"ok": "✅", "warning": "⚠️", "error": "❌"}
    overall_icons = {"healthy": "🟢", "degraded": "🟡", "unhealthy": "🔴"}

    print("\n" + "=" * 60)
    print("🏥 NINJA HEALTH CHECK")
    print("=" * 60)

    for name, result in results.items():
        if name == "overall":
            continue
        status = result.get("status", "unknown")
        icon = icons.get(status, "❓")
        msg = result.get("message", "")
        print(f"\n  {icon} {name:12s} — {msg}")

        # Show fix hint on error
        if status in ("error", "warning") and "fix" in result:
            print(f"     💡 Fix: {result['fix']}")

        # Show extra details
        for key in ("model", "agent", "channel", "browser"):
            if key in result:
                print(f"     {key}: {result[key]}")

        if result.get("fixed"):
            print("     🔧 Auto-fixed!")

    overall = results.get("overall", "unknown")
    icon = overall_icons.get(overall, "❓")
    print(f"\n{'=' * 60}")
    print(f"  {icon} Overall: {overall.upper()}")
    print(f"{'=' * 60}\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Ninja Health Check — unified system diagnostics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/health_check.py            Human-readable output
  python tools/health_check.py --json     JSON output for scripting
  python tools/health_check.py --fix      Auto-fix common issues
        """,
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--fix", action="store_true", help="Attempt auto-fix for common issues"
    )

    args = parser.parse_args()
    results = run_health_check(auto_fix=args.fix)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_results(results)

    # Exit code: 0 if healthy, 1 if not
    sys.exit(0 if results.get("overall") == "healthy" else 1)


if __name__ == "__main__":
    main()
