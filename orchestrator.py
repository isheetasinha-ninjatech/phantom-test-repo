"""
Phantom Orchestrator — Browser Automation Agent

Runs Claude Code as the Phantom browser automation agent.
Agent identity is read from ~/.agent_settings.json config file.
Agent behavior is defined by agent-docs/PHANTOM_SPEC.md.

Usage:
    python orchestrator.py                    # Run default work loop
    python orchestrator.py --task "Do X"      # Run single task
    python orchestrator.py --list             # List available agents
    python orchestrator.py --test             # Run capability tests
"""

import argparse
import json
import logging
import re
import shutil
import string
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Import centralized agent configuration
from agents_config import AGENTS

from mode import get_phantom_mode

REPO_ROOT = Path(__file__).parent
CONFIG_PATH = Path.home() / ".agent_settings.json"
LOCK_FILE = REPO_ROOT / ".orchestrator.lock"
LOG_DIR = Path("/workspace/logs")
MCP_TOKEN_FILE = Path("/dev/shm/mcp-token")
SETTINGS_FILE = REPO_ROOT / "settings.json"
CLAUDE_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"

# Settings template - variables filled from /root/.claude/settings.json
SETTINGS_TEMPLATE = string.Template(
    """{
    "env": {
        "ANTHROPIC_AUTH_TOKEN": "$auth_token",
        "ANTHROPIC_BASE_URL": "$base_url",
        "ANTHROPIC_MODEL": "$model"
    },
    "permissions": {
        "allow": [
            "Edit(**)","Bash"
        ]
    },
    "attribution": {
        "commit": ""
    }
}
"""
)

# Ensure log directory exists
LOG_DIR.mkdir(exist_ok=True)


# Single canonical logger name. Filename is always ``phantom_YYYY-MM-DD.log``.
# We deliberately ignore caller-supplied agent names — historically several
# callers passed ``"orchestrator"`` early in startup and then ``"phantom"``
# later, which created empty ``orchestrator_*.log`` orphan files daily.
_LOGGER_NAME = "phantom"


def setup_logging(agent_name: str = "orchestrator") -> logging.Logger:
    """Return the canonical phantom logger.

    Idempotent: repeated calls return the same logger without re-creating
    handlers. The file is opened lazily (``delay=True``) so a logger that
    is set up but never written to does not create an empty file.

    The ``agent_name`` argument is accepted for backward compatibility
    but ignored — see ``_LOGGER_NAME``.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # don't bubble to root → no duplicate stdout

    file_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_filename = LOG_DIR / f"phantom_{datetime.now().strftime('%Y-%m-%d')}.log"
    file_handler = logging.FileHandler(
        log_filename,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # No StreamHandler: systemd captures stdout/stderr into the journal
    # (StandardOutput=journal in phantom.service). Adding a stdout handler
    # here would duplicate every logger.info() into journalctl.

    return logger


def log_and_print(
    msg: str, level: str = "info", logger: logging.Logger = None, file=None
):
    """Print a message and also log it. Works before or after logger is set up."""
    # Always print to console (or specified file)
    print(msg, file=file)
    # Also log if logger is available
    if logger:
        clean_msg = msg.strip()
        if clean_msg:
            getattr(logger, level, logger.info)(clean_msg)


SANDBOX_METADATA_FILE = Path("/dev/shm/sandbox_metadata.json")
DEFAULT_MODEL = "claude-opus-4-8"


def get_selected_model(logger: logging.Logger = None) -> str:
    """
    Read litellm_selected_model from /dev/shm/sandbox_metadata.json if present.
    Falls back to DEFAULT_MODEL ('claude-opus-4-8') if the file
    doesn't exist, is unreadable, or doesn't contain litellm_selected_model.

    Returns:
        Model name string
    """
    _logger = logger or setup_logging("orchestrator")

    if not SANDBOX_METADATA_FILE.exists():
        _logger.debug(
            f"sandbox_metadata not found at {SANDBOX_METADATA_FILE}, using default model: {DEFAULT_MODEL}"
        )
        return DEFAULT_MODEL

    try:
        with open(SANDBOX_METADATA_FILE, "r") as f:
            meta = json.load(f)

        model = meta.get("litellm_selected_model", "").strip()
        if model:
            _logger.info(f"🎯 Model from sandbox_metadata: {model}")
            return model
        else:
            _logger.debug(
                f"litellm_selected_model not set in sandbox_metadata, using default: {DEFAULT_MODEL}"
            )
            return DEFAULT_MODEL
    except (json.JSONDecodeError, IOError, KeyError) as e:
        _logger.warning(
            f"⚠️  Failed to read sandbox_metadata: {e}, using default: {DEFAULT_MODEL}"
        )
        return DEFAULT_MODEL


def upgrade_claude_cli(logger: logging.Logger = None, timeout: int = 60) -> None:
    """
    Upgrade the Claude Code CLI binary to the latest release by shelling
    out to ``claude update``.

    Behaviour
    ---------
    * If ``claude`` isn't on ``PATH`` we log and return cleanly — this
      function must never block sandbox startup on an absent binary.
    * ``claude update`` is idempotent: 3–5 s the first time (real
      download), <1 s afterwards ("Claude Code is up to date"). Exit
      code is 0 in both cases, so we don't have to parse output.
    * Any failure (non-zero exit, timeout, FileNotFoundError) is logged
      at WARNING level and swallowed. Phantom continues with whatever
      version is currently installed.

    Args:
        logger:  Optional pre-configured logger. Falls back to the
                 orchestrator logger.
        timeout: Seconds to wait for ``claude update`` before giving
                 up. 60s is generous — in practice the upgrade finishes
                 in <5s over a normal connection.
    """
    _logger = logger or setup_logging("orchestrator")

    if not shutil.which("claude"):
        _logger.debug("claude CLI not found on PATH — skipping upgrade check")
        return

    # Capture the before-version so we can log a clean "X → Y" line when
    # an actual upgrade happened. Any failure here is harmless; we just
    # proceed without the before number.
    before = ""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            # Output is like "2.1.131 (Claude Code)"; take the first token.
            before = result.stdout.strip().split()[0] if result.stdout.strip() else ""
    except (subprocess.SubprocessError, OSError):
        pass

    try:
        # stdin=DEVNULL so the CLI never prompts interactively; the
        # --help output confirms the command takes no args.
        result = subprocess.run(
            ["claude", "update"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _logger.warning(
            f"⚠️ claude update timed out after {timeout}s — "
            f"continuing with installed version"
        )
        return
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.warning(
            f"⚠️ claude update failed to start ({exc}) — "
            f"continuing with installed version"
        )
        return

    if result.returncode != 0:
        _logger.warning(
            f"⚠️ claude update exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:200]}"
        )
        return

    # Re-read the version so we can report whether this run actually
    # pulled a new binary.
    after = ""
    try:
        v = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if v.returncode == 0 and v.stdout.strip():
            after = v.stdout.strip().split()[0]
    except (subprocess.SubprocessError, OSError):
        pass

    if before and after and before != after:
        _logger.info(f"⬆️  Upgraded Claude CLI {before} → {after}")
    elif after:
        _logger.info(f"✓ Claude CLI is up to date ({after})")
    else:
        _logger.info("✓ Claude CLI update check completed")


def ensure_settings_file(logger: logging.Logger = None) -> bool:
    """
    Ensure settings.json exists in the project directory and that
    /root/.claude/settings.json uses the correct model. Also upgrades
    the Claude Code CLI binary to the latest release (once per start).

    Model selection priority:
      1. litellm_selected_model from /dev/shm/sandbox_metadata.json (if present)
      2. Default: claude-opus-4-8

    Always regenerates settings.json to pick up model changes.
    Also updates /root/.claude/settings.json with the selected model.

    Returns:
        True if settings.json exists or was created, False otherwise
    """
    _logger = logger or setup_logging("orchestrator")

    # Upgrade the Claude CLI binary before we touch settings. This runs
    # exactly once per sandbox start because ensure_settings_file() is
    # itself called once from main(). `claude update` is idempotent, so
    # subsequent starts on an already-current binary are a <1 s no-op.
    upgrade_claude_cli(_logger)

    # Determine model
    model = get_selected_model(_logger)

    if not CLAUDE_SETTINGS_FILE.exists():
        _logger.error(f"❌ Source settings not found: {CLAUDE_SETTINGS_FILE}")
        _logger.error("Cannot auto-generate settings.json. Please create it manually.")
        return False

    try:
        with open(CLAUDE_SETTINGS_FILE, "r") as f:
            claude_settings = json.load(f)

        env = claude_settings.get("env", {})
        auth_token = env.get("ANTHROPIC_AUTH_TOKEN", "")
        base_url = env.get("ANTHROPIC_BASE_URL", "")

        if not auth_token or not base_url:
            _logger.error(
                "❌ Missing required fields in source settings (auth_token or base_url)"
            )
            return False

        # --- Update /root/.claude/settings.json with selected model ---
        current_model = env.get("ANTHROPIC_MODEL", "")
        if current_model != model:
            claude_settings["env"]["ANTHROPIC_MODEL"] = model
            with open(CLAUDE_SETTINGS_FILE, "w") as f:
                json.dump(claude_settings, f, indent=4)
            _logger.info(
                f"🔄 Updated {CLAUDE_SETTINGS_FILE} model: {current_model} → {model}"
            )

        # --- Generate project settings.json (always regenerate) ---
        settings_content = SETTINGS_TEMPLATE.substitute(
            auth_token=auth_token,
            base_url=base_url,
            model=model,
        )

        with open(SETTINGS_FILE, "w") as f:
            f.write(settings_content)

        _logger.info(f"✅ Generated {SETTINGS_FILE}")
        _logger.info(f"   Model: {model}")
        _logger.info(f"   Base URL: {base_url}")
        return True

    except (json.JSONDecodeError, IOError, KeyError) as e:
        _logger.error(f"❌ Failed to generate settings.json: {e}")
        return False


def get_github_token() -> str | None:
    """Read GitHub token from /dev/shm/mcp-token file."""
    if not MCP_TOKEN_FILE.exists():
        return None

    try:
        content = MCP_TOKEN_FILE.read_text()
        # Parse Github={"access_token": "..."} format
        for line in content.strip().split("\n"):
            if line.startswith("Github="):
                json_str = line[7:]  # Remove 'Github=' prefix
                data = json.loads(json_str)
                return data.get("access_token")
    except (json.JSONDecodeError, IOError, KeyError) as e:
        return None

    return None


def login_github_cli(logger: logging.Logger) -> bool:
    """Login to GitHub CLI using token from /dev/shm/mcp-token."""
    token = get_github_token()

    if not token:
        logger.warning("⚠️  No GitHub token found in /dev/shm/mcp-token")
        return False

    # Check if gh is installed
    if not shutil.which("gh"):
        logger.warning("⚠️  GitHub CLI (gh) not installed")
        return False

    try:
        # Login using the token via stdin
        logger.info("🔐 Logging into GitHub CLI...")
        result = subprocess.run(
            ["gh", "auth", "login", "--with-token"],
            input=token,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0:
            # Verify login
            verify = subprocess.run(
                ["gh", "auth", "status"], capture_output=True, text=True, timeout=10
            )
            if verify.returncode == 0:
                # Extract username from status output
                logger.info("✅ GitHub CLI authenticated successfully")
                logger.debug(f"GitHub status: {verify.stdout.strip()}")
                return True
            else:
                logger.warning(f"⚠️  GitHub auth verification failed: {verify.stderr}")
                return False
        else:
            logger.warning(f"⚠️  GitHub login failed: {result.stderr}")
            return False

    except subprocess.TimeoutExpired:
        logger.error("❌ GitHub login timed out")
        return False
    except Exception as e:
        logger.error(f"❌ GitHub login error: {e}")
        return False


def check_single_instance():
    """
    Ensure only one instance of the orchestrator is running.
    Uses a lock file with PID to detect and prevent duplicate instances.

    Raises:
        SystemExit if another instance is already running
    """
    import os

    current_pid = os.getpid()

    if LOCK_FILE.exists():
        try:
            with open(LOCK_FILE, "r") as f:
                lock_data = json.load(f)

            old_pid = lock_data.get("pid")
            old_agent = lock_data.get("agent", "unknown")
            old_started = lock_data.get("started", "unknown")
            old_heartbeat = lock_data.get("heartbeat", old_started)

            # Check if the old process is still running
            if old_pid:
                process_exists = False
                is_orchestrator = False

                try:
                    # Send signal 0 to check if process exists
                    os.kill(old_pid, 0)
                    process_exists = True

                    # Verify it's actually an orchestrator process (not PID reuse)
                    try:
                        with open(f"/proc/{old_pid}/cmdline", "r") as f:
                            cmdline = f.read()
                            is_orchestrator = "orchestrator.py" in cmdline
                    except (IOError, FileNotFoundError):
                        # Can't read cmdline (maybe not Linux), assume it's orchestrator
                        is_orchestrator = True

                except OSError:
                    # Process doesn't exist
                    process_exists = False

                # Also check heartbeat staleness (if no heartbeat for 10+ minutes, consider stale)
                heartbeat_stale = False
                try:
                    heartbeat_time = datetime.fromisoformat(old_heartbeat)
                    if (
                        datetime.now() - heartbeat_time
                    ).total_seconds() > 600:  # 10 minutes
                        heartbeat_stale = True
                except (ValueError, TypeError):
                    pass  # Can't parse heartbeat, ignore

                if process_exists and is_orchestrator and not heartbeat_stale:
                    # Process exists and is orchestrator - another instance is running
                    _early_logger = setup_logging("orchestrator")
                    _early_logger.error("=" * 70)
                    _early_logger.error(
                        "ERROR: Another orchestrator instance is already running!"
                    )
                    _early_logger.error("=" * 70)
                    _early_logger.error(f"   Existing instance:")
                    _early_logger.error(f"   - PID: {old_pid}")
                    _early_logger.error(f"   - Agent: {old_agent}")
                    _early_logger.error(f"   - Started: {old_started}")
                    _early_logger.error(f"   - Last heartbeat: {old_heartbeat}")
                    _early_logger.error(f"   To stop the existing instance:")
                    _early_logger.error(f"   - kill {old_pid}")
                    _early_logger.error("   - Or: pkill -f 'orchestrator.py'")
                    _early_logger.error(
                        f"   To force remove the lock (if process is stuck):"
                    )
                    _early_logger.error(f"   - rm {LOCK_FILE}")
                    _early_logger.error("=" * 70)
                    sys.exit(1)
                else:
                    # Stale lock - process doesn't exist, wrong process, or heartbeat stale
                    reason = []
                    if not process_exists:
                        reason.append(f"PID {old_pid} no longer running")
                    elif not is_orchestrator:
                        reason.append(f"PID {old_pid} is not orchestrator (PID reuse)")
                    elif heartbeat_stale:
                        reason.append(f"heartbeat stale since {old_heartbeat}")
                    _early_logger = setup_logging("orchestrator")
                    _early_logger.info(
                        f"Removing stale lock file ({', '.join(reason)})"
                    )
        except (json.JSONDecodeError, IOError, KeyError):
            # Corrupted lock file, remove it
            _early_logger = setup_logging("orchestrator")
            _early_logger.warning("Removing corrupted lock file")

    # Create/update lock file with current process info
    lock_data = {
        "pid": current_pid,
        "agent": None,  # Will be updated after agent is determined
        "started": datetime.now().isoformat(),
        "heartbeat": datetime.now().isoformat(),
    }

    try:
        with open(LOCK_FILE, "w") as f:
            json.dump(lock_data, f)
    except IOError as e:
        _early_logger = setup_logging("orchestrator")
        _early_logger.warning(f"Could not create lock file: {e}")


def update_lock_file(agent_name: str = None):
    """Update the lock file with the agent name and refresh heartbeat."""
    if LOCK_FILE.exists():
        try:
            with open(LOCK_FILE, "r") as f:
                lock_data = json.load(f)
            if agent_name:
                lock_data["agent"] = agent_name
            lock_data["heartbeat"] = datetime.now().isoformat()
            with open(LOCK_FILE, "w") as f:
                json.dump(lock_data, f)
        except (json.JSONDecodeError, IOError):
            pass


def update_heartbeat():
    """Update just the heartbeat timestamp in the lock file."""
    update_lock_file(agent_name=None)


def remove_lock_file():
    """Remove the lock file when orchestrator exits."""
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except IOError:
        pass


def load_config() -> dict:
    """Load agent configuration from ~/.agent_settings.json"""
    if not CONFIG_PATH.exists():
        return {}

    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        _early_logger = setup_logging("orchestrator")
        _early_logger.warning(f"Could not read config: {e}")
        return {}


def get_agent_from_config() -> dict:
    """
    Get the agent configuration from the config file.

    Returns:
        Agent dict with name, role, emoji, spec

    Raises:
        SystemExit if no agent is configured
    """
    config = load_config()
    agent_id = config.get("default_agent", "").lower()

    _early_logger = setup_logging("orchestrator")

    if not agent_id:
        _early_logger.error("❌ ERROR: No agent configured!")
        _early_logger.error("")
        _early_logger.error(
            "The orchestrator requires an agent identity to be set in the config file."
        )
        _early_logger.error(f"Config file: {CONFIG_PATH}")
        _early_logger.error("")
        _early_logger.error("💡 To configure your agent, run:")
        _early_logger.error("   python slack_interface.py config --set-agent nova")
        _early_logger.error("")
        _early_logger.error(f"🤖 Available agents: {', '.join(AGENTS.keys())}")
        sys.exit(1)

    if agent_id not in AGENTS:
        _early_logger.error(f"❌ ERROR: Invalid agent '{agent_id}' in config!")
        _early_logger.error("")
        _early_logger.error(f"💡 Valid agents: {', '.join(AGENTS.keys())}")
        _early_logger.error("")
        _early_logger.error("💡 To fix, run:")
        _early_logger.error("   python slack_interface.py config --set-agent nova")
        sys.exit(1)

    return AGENTS[agent_id]


def read_file(path: Path) -> str:
    """Read file content or return empty string."""
    return path.read_text() if path.exists() else ""


def build_prompt(agent: dict, task: str = "") -> str:
    """Build the prompt for the Phantom browser automation agent.

    Args:
        agent: Agent configuration dict
        task: Optional specific task
    """

    # Get default channel from config
    config = load_config()
    mode = get_phantom_mode()

    memory = read_file(REPO_ROOT / "memory" / f"{agent['name'].lower()}_memory.md")

    if mode == "whatsapp":
        wa = config.get("whatsapp") if isinstance(config.get("whatsapp"), dict) else {}
        channel = wa.get("channel_label") or wa.get("bound_chat_jid") or "(unbound)"
        default_task = (
            f"Check WhatsApp ({channel}) for new requests via "
            "`python3 -m phantom.whatsapp_interface read --json`, do your work, "
            "reply with `python3 -m phantom.whatsapp_interface say \"<text>\" "
            "--group-jid <jid>` (or `--to <jid>` for DMs), then update your memory "
            "file and reflect per agent-docs/ORCHESTRATOR.md."
        )
        docs_block = """1. **Your Specification:** `cat agent-docs/PHANTOM_SPEC.md`
2. **Agent Protocol:** `cat agent-docs/AGENT_PROTOCOL.md`
3. **WhatsApp Interface Docs:** `cat agent-docs/NINJA.md`
4. **Workflow Docs:** `cat agent-docs/ORCHESTRATOR.md`
5. **Pipedream Integrations:** `cat agent-docs/PIPEDREAM_CONNECT.md` — connected app discovery, OAuth dashboard, and `tools/pdx.py` (`pdx`) CLI tools"""
    else:
        channel = config.get(
            "default_channel_name", config.get("default_channel", "#your-channel")
        )
        default_task = f"Check Slack {channel} for new requests, do your work, update your memory file and reflect and improve your toolkit as per agent-docs/ORCHESTRATOR.md."
        docs_block = """1. **Your Specification:** `cat agent-docs/PHANTOM_SPEC.md`
2. **Agent Protocol:** `cat agent-docs/AGENT_PROTOCOL.md`
3. **Slack Interface Docs:** `cat agent-docs/SLACK_INTERFACE.md`
4. **Workflow Docs:** `cat agent-docs/ORCHESTRATOR.md`
5. **Pipedream Integrations:** `cat agent-docs/PIPEDREAM_CONNECT.md` — connected app discovery, OAuth dashboard, and `tools/pdx.py` (`pdx`) CLI tools"""

    return f"""# You are {agent['name']} {agent['emoji']}

## Your Identity
- **Name:** {agent['name']}
- **Role:** {agent['role']}
- **Emoji:** {agent['emoji']}
- **Runtime mode:** {mode}

---

## Documentation Files (READ THESE FIRST)

You are currently running as the orchestrator agent. Before starting work, read these files for full context:

{docs_block}

---

## Your Memory

{memory if memory else "No previous memory. This is your first session."}

---

## Current Task

{task if task else default_task}
"""


def run_agent(agent: dict, task: str = "") -> None:
    """Run Claude Code for a single agent in headless autonomous mode."""
    # Setup logger for this subprocess
    agent_logger = setup_logging(agent["name"].lower())

    agent_logger.info(f"\n{'='*60}")
    agent_logger.info(f"{agent['emoji']} Starting {agent['name']} ({agent['role']})")
    agent_logger.info(f"{'='*60}\n")

    prompt = build_prompt(agent, task)

    # Run Claude Code CLI
    # -p: Print mode (non-interactive)
    # Permissions are configured in ~/.claude/settings.json
    # Timeout: 15 minutes (900 seconds) to allow for complex tasks
    try:
        result = subprocess.run(
            [str(REPO_ROOT / "claude-wrapper.sh"), "-c", "-p", prompt],
            cwd=str(REPO_ROOT),
            timeout=900,  # 15 minutes
            capture_output=True,
            text=True,
        )
        if result.stdout:
            agent_logger.info(f"Claude output:\n{result.stdout}")
        if result.stderr:
            agent_logger.warning(f"Claude stderr:\n{result.stderr}")
    except subprocess.TimeoutExpired:
        agent_logger.warning("⏰ Claude CLI timed out after 15 minutes")
    except FileNotFoundError:
        agent_logger.error("❌ Claude CLI not found!")
        agent_logger.error("Claude CLI is REQUIRED to run agents.")
        agent_logger.error("Please install Claude Code CLI first.")
        sys.exit(1)

    agent_logger.info(f"\n✅ {agent['name']} completed\n")


def run_capability_tests() -> bool:
    """
    Run all capability tests and report results.

    Returns:
        True if all tests pass, False otherwise
    """
    test_logger = setup_logging("orchestrator")

    test_logger.info("\n" + "=" * 60)
    test_logger.info("🧪 CAPABILITY TESTS")
    test_logger.info("=" * 60)

    results = {}
    all_passed = True

    # Test 1: Config file
    test_logger.info("\n📋 Test 1: Configuration File")
    config = load_config()
    if config.get("default_agent"):
        test_logger.info(f"   ✅ Agent configured: {config.get('default_agent')}")
        results["config"] = True
    else:
        test_logger.error("   ❌ No agent configured")
        results["config"] = False
        all_passed = False

    if config.get("default_channel"):
        test_logger.info(f"   ✅ Channel configured: {config.get('default_channel')}")
    else:
        test_logger.warning("   ⚠️  No default channel configured")

    # Test 2: Browser Server
    test_logger.info("\n📋 Test 2: Browser Server")
    try:
        import urllib.request

        resp = urllib.request.urlopen("http://localhost:9222/json/version", timeout=3)
        if resp.status == 200:
            test_logger.info("   ✅ Browser server running on port 9222")
            results["browser"] = True
        else:
            test_logger.error("   ❌ Browser server not responding")
            results["browser"] = False
            all_passed = False
    except Exception:
        test_logger.warning(
            "   ⚠️  Browser server not running (start with: python phantom/browser_server.py start)"
        )
        results["browser"] = False

    # Test 3: Claude CLI (MANDATORY)
    test_logger.info("\n📋 Test 3: Claude CLI (REQUIRED)")
    if shutil.which("claude"):
        test_logger.info("   ✅ Claude CLI installed")
        results["claude"] = True
    else:
        test_logger.error("   ❌ Claude CLI not installed")
        test_logger.warning("   ⚠️  Claude CLI is REQUIRED to run agents")
        results["claude"] = False
        all_passed = False

    # Test 4: Project Files
    test_logger.info("\n📋 Test 4: Project Files")
    required_files = [
        "slack_interface.py",
        "browser_interface.py",
        "phantom/browser_server.py",
        "phantom/observer.py",
        "phantom/actions.py",
        "agent-docs/PHANTOM_SPEC.md",
        "agent-docs/AGENT_PROTOCOL.md",
        "agent-docs/SLACK_INTERFACE.md",
        "agent-docs/PIPEDREAM_CONNECT.md",
        "memory",
    ]
    files_ok = True
    for f in required_files:
        path = REPO_ROOT / f
        if path.exists():
            test_logger.info(f"   ✅ {f}")
        else:
            test_logger.error(f"   ❌ {f} missing")
            files_ok = False
            all_passed = False
    results["files"] = files_ok

    # Summary
    test_logger.info("\n" + "=" * 60)
    test_logger.info("📊 TEST SUMMARY")
    test_logger.info("=" * 60)

    for test, passed in results.items():
        if passed is True:
            status = "✅ PASS"
        elif passed is False:
            status = "❌ FAIL"
        else:
            status = "⚠️  SKIP"
        test_logger.info(f"   {test:12} {status}")

    test_logger.info("")
    if all_passed:
        test_logger.info("🎉 All tests passed! Agent is ready to work.")
    else:
        test_logger.warning(
            "⚠️  Some tests failed. Please fix issues before running agent."
        )
    test_logger.info("=" * 60 + "\n")

    return all_passed


def main():
    parser = argparse.ArgumentParser(
        description="Phantom Orchestrator — Browser Automation Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python orchestrator.py                    Run default work loop
  python orchestrator.py --task "Do X"      Run with specific task
  python orchestrator.py --test             Run capability tests

Configuration:
  Agent identity is read from ~/.agent_settings.json
  Set with: python slack_interface.py config --set-agent phantom
        """,
    )
    parser.add_argument("--task", "-t", default="", help="Specific task for the agent")
    parser.add_argument(
        "--list", "-l", action="store_true", help="List all available agents"
    )
    parser.add_argument("--test", action="store_true", help="Run capability tests")

    args = parser.parse_args()

    if args.test:
        success = run_capability_tests()
        sys.exit(0 if success else 1)

    if args.list:
        list_logger = setup_logging("orchestrator")
        list_logger.info("\n📋 Available Agents:\n")
        for agent_id, agent in AGENTS.items():
            list_logger.info(f"  {agent['emoji']} {agent['name']:8} - {agent['role']}")
        list_logger.info("")

        # Show current config
        config = load_config()
        current = config.get("default_agent", "")
        if current:
            list_logger.info(f"📌 Currently configured: {current}")
        else:
            list_logger.warning(
                "⚠️  No agent configured. Run: python slack_interface.py config --set-agent <name>"
            )
        list_logger.info("")
        return

    # Check for existing instance BEFORE doing anything else
    check_single_instance()

    # Get agent from config first (needed for logging setup)
    agent = get_agent_from_config()

    # Setup logging
    logger = setup_logging(agent["name"].lower())
    logger.info("=" * 60)
    logger.info(f"Orchestrator starting for {agent['name']}")
    logger.info("=" * 60)

    # Register cleanup handler to remove lock file on exit
    import atexit
    import signal

    atexit.register(remove_lock_file)

    # Start heartbeat thread to keep lock file fresh
    import threading

    heartbeat_stop = threading.Event()

    def heartbeat_loop():
        """Update lock file heartbeat every 60 seconds."""
        while not heartbeat_stop.wait(60):  # Wait 60 seconds or until stopped
            update_heartbeat()
            logger.debug("Heartbeat updated")

    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    # Also handle SIGTERM and SIGINT to clean up lock file
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        heartbeat_stop.set()  # Stop heartbeat thread
        remove_lock_file()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Ensure settings.json exists (auto-generate from /root/.claude/settings.json if missing)
    if not ensure_settings_file(logger):
        logger.error("❌ Cannot start without settings.json. Exiting.")
        sys.exit(1)

    # Login to GitHub CLI
    login_github_cli(logger)

    # Update lock file with agent name
    update_lock_file(agent["name"])

    # Show which agent we're running
    config = load_config()
    mode = get_phantom_mode()
    logger.info(f"Config: {CONFIG_PATH}")
    logger.info(f"Agent: {agent['name']} ({agent['role']})")
    logger.info(f"Mode: {mode}")
    if mode == "whatsapp":
        wa = config.get("whatsapp") if isinstance(config.get("whatsapp"), dict) else {}
        if wa.get("bound_chat_jid"):
            logger.info(f"WhatsApp chat: {wa.get('bound_chat_jid')}")
        else:
            logger.info("WhatsApp chat: (unbound — pair via dashboard or CLI)")
    elif config.get("default_channel"):
        logger.info(f"Channel: {config.get('default_channel')}")
    log_file = (
        LOG_DIR / f"{agent['name'].lower()}_{datetime.now().strftime('%Y-%m-%d')}.log"
    )
    logger.info(f"Log file: {log_file}")

    # Run the agent — single work cycle.
    # The monitor (monitor.py) runs as a separate process managed independently
    # by phantom-monitor.service (systemd) or by the operator directly.
    # This process exits when the work cycle completes; systemd Restart=on-failure
    # will re-invoke it for the next cycle.
    if args.task:
        work_task = args.task
    elif mode == "whatsapp":
        work_task = (
            "Check WhatsApp for new requests via `python3 -m "
            "phantom.whatsapp_interface read --json`, do your work, "
            "update your memory file."
        )
    else:
        work_task = "Check Slack for new requests, do your work, update your memory file."
    logger.info(f"🚀 Running work cycle: {work_task}")
    run_agent(agent, work_task)


if __name__ == "__main__":
    main()
