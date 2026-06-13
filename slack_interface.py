#!/usr/bin/env python3
"""
Slack Interface CLI

A command-line tool and Python API for interacting with Slack workspaces.
Supports agent-based messaging with custom avatars, file uploads, and more.

Token Sources (in priority order):
    1. Cached config (~/.agent_settings.json) - persisted from first connection
    2. /dev/shm/mcp-token - Auto-populated when you click 'Connect' in chat
    3. Environment variable: SLACK_BOT_TOKEN

Token Type:
    - Bot Token (xoxb-*) ONLY — user tokens (xoxp-*) are NOT supported

Required Scopes:
    - channels:read        - List channels
    - channels:history     - Read channel messages
    - chat:write           - Send messages
    - users:read           - List users
    - files:write          - Upload files (for file uploads)
    - files:read           - Read file info / permalinks
    - chat:write.customize - Post messages as a specific agent identity
                             (custom username + icon_url). Required for
                             agent-authored file uploads.
    - links:read           - Allow Slack to unfurl its own file permalinks
                             (required for agent-authored file previews).

First-Time Setup:
    1. Set your default channel:
        python slack_interface.py config --set-channel "#your-channel"

    2. Set your default agent:
        python slack_interface.py config --set-agent nova

Usage:
    python slack_interface.py --help
    python slack_interface.py config                    # Show/set configuration
    python slack_interface.py agents                    # List all available agents
    python slack_interface.py channels                  # List all channels
    python slack_interface.py users                     # List all users
    python slack_interface.py say "message"             # Send as default agent
    python slack_interface.py read                      # Read from default channel
    python slack_interface.py upload file.png           # Upload file to default channel

Configuration:
    The tool uses a config file at ~/.agent_settings.json:

    {
        "default_channel": "#logo-creator",
        "default_channel_id": "C0AAAAMBR1R",
        "default_agent": "nova",
        "workspace": "RenovateAI"
    }

    Set default channel:
        python slack_interface.py config --set-channel "#logo-creator"

    Set default agent:
        python slack_interface.py config --set-agent nova

Agents:
    nova  - Product Manager (🌟 purple)
    pixel - UX Designer (🎨 pink)
    bolt  - Full-Stack Developer (⚡ yellow)
    scout - QA Engineer (🔍 green)

Examples:
    # First-time setup
    python slack_interface.py config --set-channel "#logo-creator"
    python slack_interface.py config --set-agent pixel

    # Send message as configured agent
    python slack_interface.py say "Sprint planning at 2pm!"

    # Upload file with comment
    python slack_interface.py upload designs/mockup.png -m "New design ready!"

    # Read recent messages
    python slack_interface.py read -l 20
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

import requests

# ============================================================================
# Logging — writes to the same logs/<agent>_<date>.log as orchestrator
# ============================================================================

REPO_ROOT = Path(__file__).parent
LOG_DIR = REPO_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Module-level logger — configured lazily on first use
_logger: Optional[logging.Logger] = None
_logger_initialized = False


def _get_logger() -> logging.Logger:
    """
    Get or create the slack_interface logger.

    Writes to logs/<agent>_<date>.log (same location as orchestrator).
    Agent name is read from ~/.agent_settings.json config.
    Falls back to 'slack' if no agent is configured.
    """
    global _logger, _logger_initialized

    if _logger_initialized and _logger is not None:
        return _logger

    _logger_initialized = True
    _logger = logging.getLogger("slack_interface")
    _logger.setLevel(logging.DEBUG)

    # Don't add handlers if they already exist (avoid duplicates)
    if _logger.handlers:
        return _logger

    # Determine agent name from config for log filename
    agent_name = "slack"
    try:
        config_path = os.path.expanduser("~/.agent_settings.json")
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                data = json.load(f)
            agent_name = data.get("default_agent", "slack").lower()
    except Exception:
        pass

    # File handler — same format and location as orchestrator
    log_filename = LOG_DIR / f"{agent_name}_{datetime.now().strftime('%Y-%m-%d')}.log"
    try:
        file_handler = logging.FileHandler(log_filename, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | [slack] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        _logger.addHandler(file_handler)
    except Exception:
        pass  # If we can't write logs, don't crash

    # No console handler — slack_interface already prints to stdout/stderr
    # Adding a console handler would duplicate output

    return _logger


# Markdown to Slack mrkdwn conversion (REQUIRED)
try:
    from slackify_markdown import slackify_markdown
except ImportError:
    print("=" * 70, file=sys.stderr)
    print("❌ MISSING REQUIRED DEPENDENCY: slackify-markdown", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "The 'slackify-markdown' package is required for Slack message formatting.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print("💡 To install, run:", file=sys.stderr)
    print("   pip install -r requirements.txt", file=sys.stderr)
    print("", file=sys.stderr)
    print("   Or install directly:", file=sys.stderr)
    print("   pip install slackify-markdown", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    sys.exit(1)

# Native Block Kit conversion (markdown tables -> markdown blocks). Optional:
# if the module is missing for any reason, fall back to the plain text path.
try:
    from slack_md_blocks import md_to_slack_blocks
except ImportError:
    md_to_slack_blocks = None

# Agent Event Cache client (optional — pydantic may not be installed during
# initial install.sh config calls that happen before pip install).
try:
    from clients.agent_event_cache_client import (
        AgentEventCacheClient,
        GetMessagesRequest,
        is_event_cache_enabled,
    )
except ImportError:
    AgentEventCacheClient = None
    GetMessagesRequest = None
    is_event_cache_enabled = None


# ============================================================================
# Retry Logic with Exponential Backoff
# ============================================================================


def retry_with_backoff(
    max_retries: int = 5, base_delay: float = 1.0, max_delay: float = 60.0
):
    """
    Decorator that retries a function with exponential backoff on rate limiting or transient errors.

    Args:
        max_retries: Maximum number of retry attempts (default: 5)
        base_delay: Initial delay in seconds (default: 1.0)
        max_delay: Maximum delay in seconds (default: 60.0)

    Handles:
        - HTTP 429 (Too Many Requests / Rate Limited)
        - HTTP 500, 502, 503, 504 (Server errors)
        - Slack API rate_limited errors
        - Connection errors
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    result = func(*args, **kwargs)

                    # Check if result is a dict with Slack API error
                    if isinstance(result, dict):
                        if (
                            result.get("error") == "ratelimited"
                            or result.get("error") == "rate_limited"
                        ):
                            retry_after = result.get(
                                "retry_after", base_delay * (2**attempt)
                            )
                            if attempt < max_retries:
                                delay = min(float(retry_after), max_delay)
                                print(
                                    f"[Rate Limited] Retry {attempt + 1}/{max_retries} after {delay:.1f}s...",
                                    file=sys.stderr,
                                )
                                time.sleep(delay)
                                continue

                    return result

                except requests.exceptions.HTTPError as e:
                    last_exception = e
                    status_code = (
                        e.response.status_code if e.response is not None else 0
                    )

                    # Rate limited
                    if status_code == 429:
                        retry_after = e.response.headers.get(
                            "Retry-After", base_delay * (2**attempt)
                        )
                        if attempt < max_retries:
                            delay = min(float(retry_after), max_delay)
                            print(
                                f"[HTTP 429 Rate Limited] Retry {attempt + 1}/{max_retries} after {delay:.1f}s...",
                                file=sys.stderr,
                            )
                            time.sleep(delay)
                            continue

                    # Server errors (retriable)
                    elif status_code in (500, 502, 503, 504):
                        if attempt < max_retries:
                            delay = min(base_delay * (2**attempt), max_delay)
                            print(
                                f"[HTTP {status_code}] Retry {attempt + 1}/{max_retries} after {delay:.1f}s...",
                                file=sys.stderr,
                            )
                            time.sleep(delay)
                            continue

                    # Non-retriable HTTP error
                    raise

                except (
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                ) as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = min(base_delay * (2**attempt), max_delay)
                        print(
                            f"[Connection Error] Retry {attempt + 1}/{max_retries} after {delay:.1f}s...",
                            file=sys.stderr,
                        )
                        time.sleep(delay)
                        continue
                    raise

                except requests.exceptions.RequestException as e:
                    # Other request exceptions - don't retry
                    raise

            # If we've exhausted all retries, raise the last exception
            if last_exception:
                raise last_exception
            return result

        return wrapper

    return decorator


# ============================================================================
# Sandbox URL Conversion
# ============================================================================
# Converts 0.0.0.0:<port> references in messages to public sandbox URLs.
# Reads sandbox_id and stage from /dev/shm/sandbox_metadata.json.
#
# Pattern: 0.0.0.0:<port> → <port>-<sandbox_id>.app.super.<stage>myninja.ai
# Example: 0.0.0.0:8080 → 8080-134212d3-8907-4593-8090-b21ec7365c33.app.super.betamyninja.ai

SANDBOX_METADATA_FILE = "/dev/shm/sandbox_metadata.json"

# Regex to match 0.0.0.0:<port> (port = 1-5 digit number)
_PORT_URL_PATTERN = re.compile(r"0\.0\.0\.0:(\d{1,5})")

# Cache for sandbox metadata (read once)
_sandbox_metadata_cache: Optional[Dict[str, str]] = None


def _load_sandbox_metadata() -> Optional[Dict[str, str]]:
    """
    Load sandbox metadata from /dev/shm/sandbox_metadata.json.
    Results are cached after first successful read.

    Returns:
        Dict with 'environment' and 'thread_id' keys, or None if unavailable.
    """
    global _sandbox_metadata_cache

    if _sandbox_metadata_cache is not None:
        return _sandbox_metadata_cache

    try:
        with open(SANDBOX_METADATA_FILE, "r") as f:
            data = json.load(f)

        environment = data.get("environment", "")
        thread_id = data.get("thread_id", "")

        if environment and thread_id:
            _sandbox_metadata_cache = {
                "environment": environment,
                "thread_id": thread_id,
            }
            return _sandbox_metadata_cache
        else:
            print(
                f"⚠️ Sandbox metadata missing environment or thread_id", file=sys.stderr
            )
            return None
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, IOError) as e:
        print(f"⚠️ Error reading sandbox metadata: {e}", file=sys.stderr)
        return None


def convert_sandbox_urls(text: str) -> str:
    """
    Convert 0.0.0.0:<port> patterns in text to public sandbox URLs.

    In a cloud sandbox (LOCAL_DEVELOPMENT_MODE not set):
        0.0.0.0:<port> → https://<port>-<sandbox_id>.app.super.<stage>myninja.ai

    When LOCAL_DEVELOPMENT_MODE=True (local docker-compose):
        0.0.0.0:<port> → http://localhost:<port>


    Args:
        text: Message text that may contain 0.0.0.0:<port> references

    Returns:
        Text with all 0.0.0.0:<port> replaced with public or local URLs.
    """
    local_mode = os.environ.get("LOCAL_DEVELOPMENT_MODE", "").lower() in (
        "true",
        "1",
        "yes",
    )

    if local_mode:
        # Local / docker-compose — use localhost
        def _replace_port(match):
            port = match.group(1)
            return f"http://localhost:{port}"

        return _PORT_URL_PATTERN.sub(_replace_port, text)

    # Cloud sandbox — build the full public URL from sandbox metadata
    metadata = _load_sandbox_metadata()
    if not metadata:
        return text

    sandbox_id = metadata["thread_id"]
    stage = metadata["environment"]
    prefix = f"{stage}" if stage and stage != "prod" else ""

    def _replace_port(match):
        port = match.group(1)
        return f"https://{port}-{sandbox_id}.app.super.{prefix}myninja.ai"

    return _PORT_URL_PATTERN.sub(_replace_port, text)


# ============================================================================
# Shared Cache (channels, users) — S3-backed
# ============================================================================
# S3-based cache shared across all agents. Eliminates local disk dependency
# and enables cross-environment cache sharing. Uses UTC timestamps for TTL.
# Reduces Slack API calls by ~70-80%.
#
# S3 layout:  s3://<bucket>/<cache_prefix>/<name>.json
# Config:     s3_config.json at repo root (gitignored)

from datetime import timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

# ---------------------------------------------------------------------------
# S3 client initialisation (lazy, singleton)
# ---------------------------------------------------------------------------
_s3_client = None
_s3_bucket = None
_s3_cache_prefix = "slack-channel"

# Candidate locations for s3_config.json
_S3_CONFIG_LOCATIONS = [
    "/root/s3_config.json",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "s3_config.json"),
    os.path.join(os.getcwd(), "s3_config.json"),
    os.path.expanduser("~/ninja-squad/s3_config.json"),
    "/workspace/ninja-squad/s3_config.json",
]


def _get_s3_config() -> dict:
    """Load S3 configuration from s3_config.json at repo root."""
    for path in _S3_CONFIG_LOCATIONS:
        if os.path.isfile(path):
            with open(path) as f:
                return json.load(f)
    raise FileNotFoundError(
        "❌ s3_config.json not found!\n"
        "   The S3 cache config is REQUIRED for slack_interface to function.\n"
        "   Expected locations:\n"
        + "".join(f"     - {p}\n" for p in _S3_CONFIG_LOCATIONS)
        + "\n   Create s3_config.json with: aws_access_key_id, aws_secret_access_key, bucket_name, region"
    )


# --- Validate s3_config.json exists at import time ---
if not any(os.path.isfile(p) for p in _S3_CONFIG_LOCATIONS):
    raise FileNotFoundError(
        "❌ s3_config.json not found!\n"
        "   The S3 cache config is REQUIRED for slack_interface to function.\n"
        "   Expected locations:\n"
        + "".join(f"     - {p}\n" for p in _S3_CONFIG_LOCATIONS)
        + "\n   Create s3_config.json with: aws_access_key_id, aws_secret_access_key, bucket_name, region"
    )


def _init_s3():
    """Initialise the S3 client singleton from s3_config.json."""
    global _s3_client, _s3_bucket, _s3_cache_prefix
    if _s3_client is not None:
        return
    cfg = _get_s3_config()
    boto_kwargs = dict(region_name=cfg.get("region", "us-west-2"))
    local_mode = os.environ.get("LOCAL_DEVELOPMENT_MODE", "").lower() in ("true", "1")
    if not local_mode:
        boto_kwargs["aws_access_key_id"] = cfg["aws_access_key_id"]
        boto_kwargs["aws_secret_access_key"] = cfg["aws_secret_access_key"]
    _s3_client = boto3.client("s3", **boto_kwargs)
    _s3_bucket = cfg["bucket_name"]
    _s3_cache_prefix = cfg.get("cache_prefix", "slack-channel")


def _s3_key(name: str) -> str:
    """Return the S3 object key for a given cache name."""
    return f"{_s3_cache_prefix}/{name}.json"


# ---------------------------------------------------------------------------
# Pipedream credentials bootstrap (S3 → ~/.agent_settings.json)
# ---------------------------------------------------------------------------
#
# Pipedream Connect requires four secrets (client_id, client_secret,
# project_id, environment). We keep them out of git by storing a JSON
# file in the same S3 bucket we already use for the Slack cache and
# fetching it on first token load. The downloaded object is merged into
# ~/.agent_settings.json under a top-level "pipedream" key so every
# downstream component (MCP client, integrations dashboard, utils
# wrapper) reads a single canonical source.
#
# S3 location:  s3://<bucket>/pipedream-client/pipedream_credentials.json
# Schema:       {"client_id": "...", "client_secret": "...",
#                "project_id": "proj_...", "environment": "development"}
# Keys prefixed with "_" (e.g. "_comment", "_field_docs") are treated as
# helper docs and stripped during install.

_PIPEDREAM_S3_KEY = "pipedream-client/pipedream_credentials.json"
_PIPEDREAM_REQUIRED_FIELDS = ("client_id", "client_secret", "project_id", "environment")
_PIPEDREAM_PROJECT_ID_RE = re.compile(r"^proj_[a-zA-Z0-9]+$")
_PIPEDREAM_ENVIRONMENTS = ("development", "production")


def _fetch_pipedream_credentials_from_s3() -> Optional[Dict[str, str]]:
    """
    Download ``pipedream-client/pipedream_credentials.json`` from the
    shared S3 bucket, validate it, and return the cleaned dict.

    Returns None on any non-fatal failure (missing object, malformed
    JSON, missing fields, S3 unreachable). Callers treat None as "no
    creds yet" and move on — phantom must never refuse to boot just
    because the Pipedream creds haven't been uploaded.
    """
    try:
        _init_s3()
        resp = _s3_client.get_object(Bucket=_s3_bucket, Key=_PIPEDREAM_S3_KEY)
        body = resp["Body"].read().decode("utf-8")
        raw = json.loads(body)
    except (ClientError, BotoCoreError, NoCredentialsError) as exc:
        # 404 NoSuchKey is common on a fresh sandbox before creds are
        # uploaded — don't spam the log with it.
        code = (
            getattr(getattr(exc, "response", None), "get", lambda *_: {})(
                "Error", {}
            ).get("Code", "")
            if isinstance(exc, ClientError)
            else ""
        )
        if code != "NoSuchKey":
            print(f"⚠️  Pipedream creds: S3 fetch failed ({exc})", file=sys.stderr)
        return None
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"⚠️  Pipedream creds: could not parse S3 object ({exc})", file=sys.stderr
        )
        return None

    if not isinstance(raw, dict):
        print("⚠️  Pipedream creds: S3 object is not a JSON object", file=sys.stderr)
        return None

    # Strip helper / doc keys (anything starting with "_").
    clean: Dict[str, str] = {
        k: v for k, v in raw.items() if not k.startswith("_") and v not in (None, "")
    }

    # Required fields check.
    missing = [f for f in _PIPEDREAM_REQUIRED_FIELDS if not clean.get(f)]
    if missing:
        print(f"⚠️  Pipedream creds: missing fields {missing}", file=sys.stderr)
        return None

    # Shape checks — cheap and catches obvious mistakes like
    # accidentally pasting a display name into project_id.
    if not _PIPEDREAM_PROJECT_ID_RE.match(str(clean["project_id"])):
        print(
            f"⚠️  Pipedream creds: project_id {clean['project_id']!r} "
            f"does not match 'proj_<alnum>'",
            file=sys.stderr,
        )
        return None
    if clean["environment"] not in _PIPEDREAM_ENVIRONMENTS:
        print(
            f"⚠️  Pipedream creds: environment {clean['environment']!r} "
            f"must be one of {_PIPEDREAM_ENVIRONMENTS}",
            file=sys.stderr,
        )
        return None

    # Only keep the 4 required + any recognised optional fields (e.g.
    # a custom MCP base URL). Everything else (including forgotten
    # _fields) is dropped.
    allowed = set(_PIPEDREAM_REQUIRED_FIELDS) | {"remote_mcp_base_url", "api_base_url"}
    return {k: str(v) for k, v in clean.items() if k in allowed}


def _install_pipedream_credentials(config_file: str) -> bool:
    """
    Fetch Pipedream credentials from S3 and merge them into the given
    agent_settings.json under a top-level ``pipedream`` key.

    This is idempotent: re-uploading a new file to S3 rotates the
    credentials on the next phantom start; if nothing has changed the
    on-disk file is rewritten identically. If the S3 object is missing
    or invalid the existing ``pipedream`` block (if any) is preserved.

    Returns:
        True if the file now has a valid ``pipedream`` block (freshly
        installed OR already present), False otherwise.
    """
    creds = _fetch_pipedream_credentials_from_s3()
    if not creds:
        return False

    # Read the existing settings file raw (not via SlackConfig.load,
    # which would strip any nested blocks it doesn't know about).
    try:
        with open(config_file, "r") as f:
            existing = json.load(f)
    except (OSError, json.JSONDecodeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}

    before = existing.get("pipedream")
    existing["pipedream"] = creds

    if before == creds:
        return True  # No-op; already up to date.

    try:
        with open(config_file, "w") as f:
            json.dump(existing, f, indent=2)
    except OSError as exc:
        print(
            f"⚠️  Pipedream creds: could not write {config_file} ({exc})",
            file=sys.stderr,
        )
        return False

    action = "installed" if before is None else "rotated"
    print(
        f"🔐 Pipedream credentials {action} in {config_file} "
        f"(project {creds['project_id']}, env {creds['environment']})",
        file=sys.stderr,
    )
    return True


# ---------------------------------------------------------------------------
# Cache read / write / invalidate
# ---------------------------------------------------------------------------


def _read_cache(name: str, ttl_seconds: int = 120) -> Optional[Any]:
    """Read from S3 cache if fresh (UTC). Returns None if stale or missing."""
    try:
        _init_s3()
        resp = _s3_client.get_object(Bucket=_s3_bucket, Key=_s3_key(name))
        payload = json.loads(resp["Body"].read().decode("utf-8"))
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        # Ensure fetched_at is UTC-aware
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        age = (now_utc - fetched_at).total_seconds()
        if age < ttl_seconds:
            return payload.get("data")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchKey":
            logging.debug(f"S3 cache read error for '{name}': {e}")
    except (NoCredentialsError, BotoCoreError, FileNotFoundError) as e:
        logging.debug(f"S3 cache unavailable for read '{name}': {e}")
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logging.debug(f"S3 cache parse error for '{name}': {e}")
    return None


def _write_cache(name: str, data: Any, ttl_seconds: int = 120) -> None:
    """Write data to S3 cache with UTC timestamp."""
    try:
        _init_s3()
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": ttl_seconds,
            "data": data,
        }
        _s3_client.put_object(
            Bucket=_s3_bucket,
            Key=_s3_key(name),
            Body=json.dumps(payload).encode("utf-8"),
            ContentType="application/json",
        )
    except (ClientError, NoCredentialsError, BotoCoreError, FileNotFoundError) as e:
        logging.debug(f"S3 cache write error for '{name}': {e}")
        pass  # Cache write failure is non-fatal


# Cache TTLs (seconds) — 2 minutes
CHANNEL_CACHE_TTL = 120  # 2 minutes
USER_CACHE_TTL = 120  # 2 minutes


# ============================================================================
# Channel Mirror Cache — full channel contents in S3
# ============================================================================
# Stores the complete message history for each channel. On read, if the cache
# is fresh (< 2 min), returns directly. Otherwise fetches only the delta from
# Slack, merges into the mirror, and writes back.
#
# S3 layout: s3://<bucket>/slack-channel/messages_<channel_id>.json
# Payload:   {"fetched_at": "<UTC ISO>", "messages": [...]}


def _read_channel_mirror(cache_key: str) -> Optional[List[Dict]]:
    """
    Read channel mirror from S3. Returns data regardless of age (stale OK).
    Returns None only if the key doesn't exist or S3 is unreachable.

    Args:
        cache_key: S3 cache key
    """
    try:
        _init_s3()
        resp = _s3_client.get_object(Bucket=_s3_bucket, Key=_s3_key(cache_key))
        payload = json.loads(resp["Body"].read().decode("utf-8"))
        return payload.get("messages", [])
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return None  # Cache doesn't exist yet
        logging.debug(f"S3 mirror read error for '{cache_key}': {e}")
    except (NoCredentialsError, BotoCoreError, FileNotFoundError) as e:
        logging.debug(f"S3 mirror unavailable for '{cache_key}': {e}")
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logging.debug(f"S3 mirror parse error for '{cache_key}': {e}")
    return None


# ============================================================================
# Agent Configuration
# ============================================================================
# Each agent has a unique identity with custom avatar for Slack messages.
# Avatars are hosted on a public URL and displayed in Slack when sending messages.

AVATAR_BASE_URL = (
    "https://sites.super.betamyninja.ai/44664728-914e-4c05-bdf2-d171ad4edcb3/82c331aa"
)

AGENT_AVATARS = {
    "nova": {
        "name": "Nova",
        "role": "Product Manager",
        "emoji": "🌟",
        "color": "purple",
        "icon_url": f"{AVATAR_BASE_URL}/nova.png",
        "icon_emoji": ":star:",  # Fallback if icon_url not supported
    },
    "pixel": {
        "name": "Pixel",
        "role": "UX Designer",
        "emoji": "🎨",
        "color": "pink",
        "icon_url": f"{AVATAR_BASE_URL}/pixel.png",
        "icon_emoji": ":art:",
    },
    "bolt": {
        "name": "Bolt",
        "role": "Full-Stack Developer",
        "emoji": "⚡",
        "color": "yellow",
        "icon_url": f"{AVATAR_BASE_URL}/bolt.png",
        "icon_emoji": ":zap:",
    },
    "scout": {
        "name": "Scout",
        "role": "QA Engineer",
        "emoji": "🔍",
        "color": "green",
        "icon_url": f"{AVATAR_BASE_URL}/scout.png",
        "icon_emoji": ":mag:",
    },
    "phantom": {
        "name": os.environ.get("PHANTOM_AGENT_NAME", "Phantom"),
        "role": "Browser Automation Agent",
        "emoji": os.environ.get("PHANTOM_AGENT_EMOJI", "👻"),
        "color": "gray",
        "icon_url": f"{AVATAR_BASE_URL}/phantom.png",
        "icon_emoji": ":ghost:",
    },
}


def get_agent_avatar(agent_name: str) -> Optional[Dict[str, str]]:
    """
    Get avatar configuration for an agent by name.

    Args:
        agent_name: Agent identifier (nova, pixel, bolt, scout)

    Returns:
        Dict with agent info (name, role, emoji, color, icon_url, icon_emoji)
        or None if agent not found
    """
    return AGENT_AVATARS.get(agent_name.lower())


# ============================================================================
# Configuration Management
# ============================================================================
# Configuration is persisted to ~/.agent_settings.json and includes:
# - default_channel: Channel name (e.g., "#logo-creator")
# - default_channel_id: Channel ID (e.g., "C0AAAAMBR1R") - preferred for API calls
# - default_agent: Default agent for 'say' command
# - workspace: Workspace name (informational)

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.agent_settings.json")


@dataclass
class SlackConfig:
    """
    Configuration container for Slack Interface.

    Attributes:
        default_channel: Channel name (e.g., "#logo-creator")
        default_channel_id: Channel ID for API calls (e.g., "C0AAAAMBR1R")
        default_team_id: Slack team/workspace ID (e.g., "T0A9Q27KD1T") —
            resolved from auth.test on first token load. Together with
            default_channel_id this forms the stable
            "<team_id>.<channel_id>" identifier used by downstream
            integrations such as Pipedream's external_user_id.
        default_team_name: Team display name (e.g., "RenovateAI")
        default_team_domain: Slack subdomain (e.g., "renovateai-hq")
        default_agent: Default agent for say command (nova, pixel, bolt, scout)
        workspace: Workspace name (informational only, mirrors default_team_name)
        bot_token: Cached bot token (xoxb-*)
    """

    default_channel: Optional[str] = None
    default_channel_id: Optional[str] = None
    default_team_id: Optional[str] = None
    default_team_name: Optional[str] = None
    default_team_domain: Optional[str] = None
    default_agent: Optional[str] = None
    workspace: Optional[str] = None
    bot_token: Optional[str] = None

    @classmethod
    def load(cls, filepath: str = DEFAULT_CONFIG_PATH) -> "SlackConfig":
        """
        Load configuration from JSON file.

        Args:
            filepath: Path to config file (default: ~/.agent_settings.json)

        Returns:
            SlackConfig instance with loaded values (or defaults if file missing)
        """
        config = cls()
        try:
            if os.path.exists(filepath):
                with open(filepath, "r") as f:
                    data = json.load(f)
                config.default_channel = data.get("default_channel")
                config.default_channel_id = data.get("default_channel_id")
                config.default_team_id = data.get("default_team_id")
                config.default_team_name = data.get("default_team_name")
                config.default_team_domain = data.get("default_team_domain")
                config.default_agent = data.get("default_agent")
                config.workspace = data.get("workspace")
                config.bot_token = data.get("bot_token")
        except Exception as e:
            print(f"⚠️ Warning: Could not load config: {e}", file=sys.stderr)
        return config

    def save(self, filepath: str = DEFAULT_CONFIG_PATH, quiet: bool = False) -> None:
        """
        Save configuration to JSON file.

        Args:
            filepath: Path to save config (default: ~/.agent_settings.json)
            quiet: If True, suppress success message
        """
        data = {
            "default_channel": self.default_channel,
            "default_channel_id": self.default_channel_id,
            "default_team_id": self.default_team_id,
            "default_team_name": self.default_team_name,
            "default_team_domain": self.default_team_domain,
            "default_agent": self.default_agent,
            "workspace": self.workspace,
            "bot_token": self.bot_token,
        }
        # Remove None values for cleaner JSON
        data = {k: v for k, v in data.items() if v is not None}

        # Preserve any top-level keys we don't manage here (e.g. the
        # "pipedream" block installed from S3, or future per-integration
        # configuration blocks). Without this merge, save() would silently
        # drop whatever other components have added to the file.
        if os.path.exists(filepath):
            try:
                with open(filepath, "r") as f:
                    preexisting = json.load(f) or {}
                if isinstance(preexisting, dict):
                    for k, v in preexisting.items():
                        # Only bring forward keys we don't actively manage,
                        # so explicit None-assignment (= "unset") still works.
                        if k not in data and k not in {
                            "default_channel",
                            "default_channel_id",
                            "default_team_id",
                            "default_team_name",
                            "default_team_domain",
                            "default_agent",
                            "workspace",
                            "bot_token",
                        }:
                            data[k] = v
            except (OSError, json.JSONDecodeError):
                pass  # Best effort — fall through and write our fields only.

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        if not quiet:
            print(f"✅ Configuration saved to {filepath}")

    def has_tokens(self) -> bool:
        """Check if bot token is cached in config."""
        return bool(self.bot_token)

    def get_default_channel(self) -> Optional[str]:
        """
        Get the default channel identifier for API calls.
        Prefers channel ID over name since IDs are more reliable.

        Returns:
            Channel ID if available, otherwise channel name, or None
        """
        return self.default_channel_id or self.default_channel


# ============================================================================
# Token Management
# ============================================================================
# This interface ONLY supports Bot Tokens (xoxb-*).
# User tokens (xoxp-*) are NOT supported and will be rejected.
#
# Bot Token (xoxb-*):
#   - Acts as the bot/app itself
#   - Can only access channels where bot is invited
#   - Supports custom username/icon for automated messaging
#   - Scopes are configured in app settings


@dataclass
class SlackTokens:
    """
    Container for Slack authentication tokens.

    Only bot tokens (xoxb-*) are supported. User tokens (xoxp-*) will
    be rejected with an error.

    Attributes:
        bot_token: Bot token (xoxb-*) - acts as the bot/app
    """

    bot_token: Optional[str] = None  # xoxb-* (bot token)


def parse_mcp_tokens(filepath: str = "/dev/shm/mcp-token") -> Dict[str, Any]:
    """
    Parse all tokens from the MCP token file.

    The MCP token file contains credentials for various services in the format:
        ServiceName=value
    or for JSON values:
        ServiceName={"key": "value"}

    Args:
        filepath: Path to MCP token file (default: /dev/shm/mcp-token)

    Returns:
        Dict mapping service names to their token values
    """
    tokens = {}

    try:
        with open(filepath, "r") as f:
            content = f.read()

        for line in content.strip().split("\n"):
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                # Try to parse JSON values (e.g., Slack tokens)
                if value.startswith("{"):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        pass  # Keep as string if not valid JSON

                tokens[key] = value

        return tokens

    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"⚠️ Error parsing tokens: {e}", file=sys.stderr)
        return {}


def get_slack_tokens(
    filepath: str = "/dev/shm/mcp-token", config_file: str = DEFAULT_CONFIG_PATH
) -> SlackTokens:
    """
    Extract Slack bot token from cached config, MCP token file, or environment.

    ONLY bot tokens (xoxb-*) are supported. If a user token (xoxp-*) is
    provided without a bot token, an error is raised.

    Token sources (in priority order):
        1. Cached bot token in config file (~/.agent_settings.json)
        2. MCP token file (/dev/shm/mcp-token) - auto-populated by Connect button
        3. Environment variables: SLACK_BOT_TOKEN or SLACK_MCP_XOXB_TOKEN

    Args:
        filepath: Path to MCP token file
        config_file: Path to config file for caching tokens

    Returns:
        SlackTokens instance with bot token

    Raises:
        SystemExit: If only a user token (xoxp-*) is found with no bot token
    """
    tokens = SlackTokens()
    config = SlackConfig.load(config_file)

    # 1. Try to get from cached config first
    if config.has_tokens():
        tokens.bot_token = config.bot_token

    # 2. Try to get from MCP token file (and update cache if found)
    if not tokens.bot_token:
        all_tokens = parse_mcp_tokens(filepath)
        slack_data = all_tokens.get("Slack", {})

        # Read workspace ID from mcp-token if not already set in config.
        # SLACK_WORKSPACE_ID takes precedence over auth.test resolution but
        # not over a value already persisted by install.sh --workspace-id.
        workspace_id_from_token = all_tokens.get("SLACK_WORKSPACE_ID")
        if workspace_id_from_token and not config.default_team_id:
            config.default_team_id = workspace_id_from_token

        if isinstance(slack_data, dict):
            tokens.bot_token = slack_data.get("bot_token")
            user_token = slack_data.get("access_token")

            # Reject user-only token
            if not tokens.bot_token and user_token:
                print("❌ ERROR: Only a user token (xoxp-*) was found.", file=sys.stderr)
                print(
                    "   This interface requires a bot token (xoxb-*).", file=sys.stderr
                )
                print(
                    "   Please configure a bot token in your Slack app.",
                    file=sys.stderr,
                )
                sys.exit(1)

            # Cache bot token to config file for future use
            if tokens.bot_token:
                config.bot_token = tokens.bot_token

                # Resolve full workspace identity via auth.test.
                # We capture team_id (+ team_domain) so downstream
                # integrations (e.g. Pipedream, whose external_user_id
                # is "<team_id>.<channel_id>") can read a single
                # canonical source of truth.
                try:
                    response = requests.post(
                        "https://slack.com/api/auth.test",
                        headers={"Authorization": f"Bearer {tokens.bot_token}"},
                        timeout=10,
                    ).json()
                    if response.get("ok"):
                        config.workspace = response.get("team")
                        # Only overwrite team_id if not already set from
                        # mcp-token SLACK_WORKSPACE_ID or install.sh --workspace-id.
                        if not config.default_team_id:
                            config.default_team_id = response.get("team_id")
                        config.default_team_name = response.get("team")
                        # Slack returns url like "https://renovateai-hq.slack.com/";
                        # the leading subdomain is the team_domain.
                        url = response.get("url") or ""
                        if url:
                            host = urlparse(url).hostname or ""
                            config.default_team_domain = (
                                host.split(".", 1)[0] if host else None
                            )
                except Exception:
                    pass  # Ignore errors when getting workspace name

                config.save(config_file, quiet=True)
                print(f"🔐 Slack bot token cached to {config_file}", file=sys.stderr)

                # Install Pipedream Connect credentials from S3 into the
                # same agent_settings.json, under a top-level "pipedream"
                # block. Runs once per sandbox start alongside the Slack
                # identity resolution; idempotent and non-fatal on failure
                # (sandboxes without uploaded creds boot normally).
                _install_pipedream_credentials(config_file)

    # 3. Fall back to environment variables
    if not tokens.bot_token:
        tokens.bot_token = os.environ.get("SLACK_BOT_TOKEN") or os.environ.get(
            "SLACK_MCP_XOXB_TOKEN"
        )

        # Check if user token was given via env var (reject it)
        if not tokens.bot_token:
            user_env = os.environ.get("SLACK_TOKEN") or os.environ.get(
                "SLACK_MCP_XOXP_TOKEN"
            )
            if user_env:
                print(
                    "❌ ERROR: Only a user token (xoxp-*) was found in environment.",
                    file=sys.stderr,
                )
                print(
                    "   This interface requires a bot token (xoxb-*).", file=sys.stderr
                )
                print("   Set SLACK_BOT_TOKEN instead of SLACK_TOKEN.", file=sys.stderr)
                sys.exit(1)

    return tokens


# ============================================================================
# Slack API Client
# ============================================================================
# Low-level client for Slack Web API calls.
# See https://api.slack.com/methods for full API documentation.
#
# Audio/Voice Message Support:
#   Slack messages may contain audio/voice attachments (voice clips, audio files).
#   These appear in the message's 'files' array with mimetype starting with
#   'audio/' (e.g., audio/webm, audio/mp4, audio/ogg) or subtype 'voice_message'.
#
#   When processing messages from get_channel_history() or get_thread_replies(),
#   check for audio attachments and transcribe them using the utils transcript API:
#
#       from utils.litellm_client import get_config, api_url
#       cfg = get_config()
#       # 1. Download audio: GET file['url_private_download'] with bot token auth
#       # 2. Transcribe:     POST api_url("/v1/audio/transcriptions")
#       #                    with files={"file": (name, bytes, mimetype)}
#       #                    and data={"model": "whisper-1"}
#       # 3. Use resp.json()["text"] as the message content
#
#   See AGENT_PROTOCOL.md Section 5 for the full audio handling protocol.


class SlackClient:
    """
    Low-level Slack API client with automatic token handling.

    This client provides direct access to Slack Web API methods.
    For higher-level operations, use the SlackInterface class instead.

    Audio/Voice Messages:
        Messages retrieved via get_channel_history() or get_thread_replies()
        may contain audio/voice attachments. Check msg['files'] for entries
        where mimetype starts with 'audio/' or subtype is 'voice_message'.
        Transcribe these using the utils transcript API (LiteLLM gateway's
        /v1/audio/transcriptions endpoint). See AGENT_PROTOCOL.md Section 5.

    Attributes:
        tokens: SlackTokens instance with available tokens

    Example:
        tokens = get_slack_tokens()
        client = SlackClient(tokens)
        result = client.send_message(tokens.bot_token, "#general", "Hello!")
    """

    BASE_URL = "https://slack.com/api"

    def __init__(self, tokens: SlackTokens):
        """
        Initialize Slack client with tokens.

        Args:
            tokens: SlackTokens instance containing available tokens
        """
        self.tokens = tokens
        self._scopes_cache: Dict[str, List[str]] = {}

    def _get_headers(self, token: str) -> Dict[str, str]:
        """Get HTTP headers for API request with Bearer token auth."""
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _get_headers_multipart(self, token: str) -> Dict[str, str]:
        """Get HTTP headers for multipart/form-data requests (file uploads)."""
        return {
            "Authorization": f"Bearer {token}"
            # Note: Don't set Content-Type for multipart - requests handles it
        }

    def _refresh_token(self, old_token: str) -> Optional[str]:
        """
        Attempt to refresh bot token from /dev/shm/mcp-token when current token is expired.

        This method:
        1. Re-reads bot token from /dev/shm/mcp-token
        2. Updates the cached config file with new token
        3. Updates self.tokens with new value
        4. Returns the new bot token

        Args:
            old_token: The expired token that needs refreshing

        Returns:
            New bot token if refresh successful, None otherwise
        """
        try:
            # Re-read tokens from MCP token file
            all_tokens = parse_mcp_tokens("/dev/shm/mcp-token")
            slack_data = all_tokens.get("Slack", {})

            if not isinstance(slack_data, dict):
                return None

            new_bot_token = slack_data.get("bot_token")

            if not new_bot_token:
                return None

            # Update self.tokens
            self.tokens.bot_token = new_bot_token

            # Update cached config file
            config = SlackConfig.load(DEFAULT_CONFIG_PATH)
            config.bot_token = new_bot_token

            # Re-resolve identity if the config was missing team_id or
            # was populated before this field existed. This keeps the
            # Pipedream external_user_id derivation stable across
            # sandbox restarts and token rotations.
            if not config.default_team_id:
                try:
                    response = requests.post(
                        "https://slack.com/api/auth.test",
                        headers={"Authorization": f"Bearer {new_bot_token}"},
                        timeout=10,
                    ).json()
                    if response.get("ok"):
                        config.workspace = config.workspace or response.get("team")
                        config.default_team_id = response.get("team_id")
                        config.default_team_name = response.get("team")
                        url = response.get("url") or ""
                        if url:
                            host = urlparse(url).hostname or ""
                            config.default_team_domain = (
                                host.split(".", 1)[0] if host else None
                            )
                except Exception:
                    pass  # Best-effort identity refresh — never block token refresh on it.

            config.save(DEFAULT_CONFIG_PATH, quiet=True)
            print(
                f"🔄 Slack bot token refreshed and cached to {DEFAULT_CONFIG_PATH}",
                file=sys.stderr,
            )

            # Opportunistically refresh Pipedream credentials too — if
            # ops rotated them in S3 we'll pick them up on this next
            # token cycle without a dedicated redeploy.
            _install_pipedream_credentials(DEFAULT_CONFIG_PATH)

            return new_bot_token

        except Exception as e:
            print(f"[Token Refresh Error] {str(e)}", file=sys.stderr)
            return None

    def _api_call(
        self,
        method: str,
        token: str,
        params: Optional[Dict] = None,
        max_retries: int = 5,
        base_delay: float = 1.0,
    ) -> Dict:
        """
        Make a Slack API call with automatic retry on rate limiting.

        Args:
            method: API method name (e.g., "chat.postMessage")
            token: Authentication token to use
            params: Optional parameters for the API call
            max_retries: Maximum number of retry attempts (default: 5)
            base_delay: Initial delay in seconds for exponential backoff (default: 1.0)

        Returns:
            API response as dict (always contains 'ok' boolean)

        Retry Behavior:
            - Retries on HTTP 429 (rate limited) with Retry-After header
            - Retries on HTTP 500, 502, 503, 504 (server errors)
            - Retries on Slack API 'ratelimited' error response
            - Retries on connection errors and timeouts
            - Uses exponential backoff: delay = base_delay * (2 ^ attempt)
            - Maximum delay capped at 60 seconds
        """
        url = f"{self.BASE_URL}/{method}"
        headers = self._get_headers(token)
        max_delay = 60.0
        last_exception = None

        for attempt in range(max_retries + 1):
            try:
                if params:
                    response = requests.post(
                        url, headers=headers, json=params, timeout=30
                    )
                else:
                    response = requests.get(url, headers=headers, timeout=30)

                # Check for HTTP 429 rate limiting
                if response.status_code == 429:
                    if attempt < max_retries:
                        retry_after = response.headers.get(
                            "Retry-After", base_delay * (2**attempt)
                        )
                        delay = min(float(retry_after), max_delay)
                        _get_logger().warning(
                            f"API {method}: Rate limited (HTTP 429), retry {attempt + 1}/{max_retries} after {delay:.1f}s"
                        )
                        print(
                            f"[Slack API Rate Limited] {method}: Retry {attempt + 1}/{max_retries} after {delay:.1f}s...",
                            file=sys.stderr,
                        )
                        time.sleep(delay)
                        continue

                # Check for server errors
                if response.status_code in (500, 502, 503, 504):
                    if attempt < max_retries:
                        delay = min(base_delay * (2**attempt), max_delay)
                        print(
                            f"[Slack API Error {response.status_code}] {method}: Retry {attempt + 1}/{max_retries} after {delay:.1f}s...",
                            file=sys.stderr,
                        )
                        time.sleep(delay)
                        continue

                result = response.json()

                # Check for Slack API rate limit error in response body
                if not result.get("ok") and result.get("error") in (
                    "ratelimited",
                    "rate_limited",
                ):
                    if attempt < max_retries:
                        retry_after = result.get(
                            "retry_after", base_delay * (2**attempt)
                        )
                        delay = min(float(retry_after), max_delay)
                        _get_logger().warning(
                            f"API {method}: Rate limited (API response), retry {attempt + 1}/{max_retries} after {delay:.1f}s"
                        )
                        print(
                            f"[Slack API Rate Limited] {method}: Retry {attempt + 1}/{max_retries} after {delay:.1f}s...",
                            file=sys.stderr,
                        )
                        time.sleep(delay)
                        continue

                # Check for token expiration/invalid errors
                if not result.get("ok") and result.get("error") in (
                    "token_expired",
                    "invalid_auth",
                    "token_revoked",
                    "not_authed",
                ):
                    _get_logger().warning(
                        f"API {method}: Token error ({result.get('error')}), attempting refresh"
                    )
                    print(
                        f"[Slack API Token Error] {method}: {result.get('error')} - attempting to refresh tokens...",
                        file=sys.stderr,
                    )
                    refreshed_token = self._refresh_token(token)
                    if refreshed_token and refreshed_token != token:
                        print(
                            f"[Slack API] Tokens refreshed from /dev/shm/mcp-token, retrying...",
                            file=sys.stderr,
                        )
                        # Update headers with new token and retry
                        token = refreshed_token
                        headers = self._get_headers(token)
                        continue
                    else:
                        print(
                            f"[Slack API] Could not refresh tokens. Please reconnect Slack.",
                            file=sys.stderr,
                        )

                return result

            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as e:
                last_exception = e
                if attempt < max_retries:
                    delay = min(base_delay * (2**attempt), max_delay)
                    print(
                        f"[Connection Error] {method}: Retry {attempt + 1}/{max_retries} after {delay:.1f}s...",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                _get_logger().error(
                    f"API {method}: Connection error after {max_retries} retries: {str(e)}"
                )
                return {
                    "ok": False,
                    "error": f"Connection error after {max_retries} retries: {str(e)}",
                }

            except requests.RequestException as e:
                _get_logger().error(f"API {method}: Request error: {str(e)}")
                return {"ok": False, "error": str(e)}

        # If we've exhausted all retries
        if last_exception:
            _get_logger().error(
                f"API {method}: Failed after {max_retries} retries: {str(last_exception)}"
            )
            return {
                "ok": False,
                "error": f"Failed after {max_retries} retries: {str(last_exception)}",
            }
        return {"ok": False, "error": f"Failed after {max_retries} retries"}

    def test_auth(self, token: str) -> Dict:
        """
        Test authentication and get token info.

        API Method: auth.test
        Required Scopes: None (works with any valid token)

        Args:
            token: Token to test

        Returns:
            Dict with 'ok', 'user', 'team', 'url' on success
        """
        return self._api_call("auth.test", token)

    def get_scopes(self, token: str) -> List[str]:
        """
        Get available OAuth scopes for a token.

        Scopes are returned in the x-oauth-scopes response header.
        Results are cached to avoid repeated API calls.

        Args:
            token: Token to check scopes for

        Returns:
            List of scope strings (e.g., ["chat:write", "channels:read"])
        """
        if token in self._scopes_cache:
            return self._scopes_cache[token]

        url = f"{self.BASE_URL}/auth.test"
        headers = self._get_headers(token)

        try:
            response = requests.get(url, headers=headers, timeout=30)
            scopes_header = response.headers.get("x-oauth-scopes", "")
            scopes = [s.strip() for s in scopes_header.split(",") if s.strip()]
            self._scopes_cache[token] = scopes
            return scopes
        except:
            return []

    def list_channels(
        self,
        token: str,
        types: str = "public_channel,private_channel",
        limit: int = 200,
        use_cache: bool = True,
    ) -> List[Dict]:
        """
        List all channels in the workspace.

        API Method: conversations.list
        Required Scopes: channels:read, groups:read (for private channels)

        Uses S3-backed cache with 2-min UTC TTL to avoid redundant API calls
        across agents.

        Args:
            token: Authentication token
            types: Comma-separated channel types (public_channel, private_channel, mpim, im)
            limit: Max channels per page (max 200, handles pagination automatically)
            use_cache: If True, check cache first (default: True)

        Returns:
            List of channel dicts with 'id', 'name', 'num_members', etc.
        """
        # Check cache first
        cache_key = f"channels_{types.replace(',', '_')}"
        if use_cache:
            cached = _read_cache(cache_key, CHANNEL_CACHE_TTL)
            if cached is not None:
                return cached

        all_channels = []
        cursor = None

        while True:
            params = {
                "types": types,
                "limit": min(limit, 200),
                "exclude_archived": False,
            }
            if cursor:
                params["cursor"] = cursor

            result = self._api_call("conversations.list", token, params)

            if not result.get("ok"):
                print(
                    f"❌ Error: {result.get('error', 'Unknown error')}", file=sys.stderr
                )
                break

            channels = result.get("channels", [])
            all_channels.extend(channels)

            # Handle pagination
            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        # Write to cache
        if all_channels:
            _write_cache(cache_key, all_channels, CHANNEL_CACHE_TTL)

        return all_channels

    def list_users(
        self, token: str, limit: int = 200, use_cache: bool = True
    ) -> List[Dict]:
        """
        List all users in the workspace.

        API Method: users.list
        Required Scopes: users:read

        Uses S3-backed cache with 2-min UTC TTL.

        Args:
            token: Authentication token
            limit: Max users per page (handles pagination automatically)
            use_cache: If True, check cache first (default: True)

        Returns:
            List of user dicts with 'id', 'name', 'real_name', 'profile', etc.
        """
        # Check cache first
        if use_cache:
            cached = _read_cache("users", USER_CACHE_TTL)
            if cached is not None:
                return cached

        all_users = []
        cursor = None

        while True:
            params = {"limit": min(limit, 200)}
            if cursor:
                params["cursor"] = cursor

            result = self._api_call("users.list", token, params)

            if not result.get("ok"):
                print(
                    f"❌ Error: {result.get('error', 'Unknown error')}", file=sys.stderr
                )
                break

            users = result.get("members", [])
            all_users.extend(users)

            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        # Write to cache
        if all_users:
            _write_cache("users", all_users, USER_CACHE_TTL)

        return all_users

    def get_channel_history(
        self, token: str, channel: str, limit: int = 50
    ) -> List[Dict]:
        """
        Get message history from a channel via S3 cache (read-only).

        Cache is populated by a separate process. This method never calls
        the Slack API directly. Returns stale data if available, or empty
        list if cache has no data for this channel yet.

        Args:
            token: Authentication token (unused — kept for API compatibility)
            channel: Channel ID (e.g., "C0AAAAMBR1R")
            limit: Number of messages to return (from the full mirror)

        Returns:
            List of message dicts with 'text', 'user', 'ts', etc.
            Messages are in reverse chronological order (newest first)

        Audio/Voice Message Handling:
            Messages may contain audio/voice attachments in the 'files' array.
            Look for entries where mimetype starts with 'audio/' or subtype
            is 'voice_message'. When detected, download the audio file using
            the file's 'url_private_download' (with bot token auth) and
            transcribe it using the utils transcript API:

                from utils.litellm_client import get_config, api_url
                cfg = get_config()
                resp = requests.post(
                    api_url("/v1/audio/transcriptions"),
                    headers={"Authorization": f"Bearer {cfg['api_key']}"},
                    files={"file": (name, audio_bytes, mimetype)},
                    data={"model": "whisper-1"}
                )
                transcript = resp.json().get("text", "")

            See AGENT_PROTOCOL.md Section 5 for the full audio handling protocol.
        """
        cache_key = f"messages_{channel}"
        cached = _read_channel_mirror(cache_key)
        if cached is not None:
            return cached[:limit]
        return []

    def get_thread_replies(
        self, token: str, channel: str, thread_ts: str, limit: int = 50
    ) -> List[Dict]:
        """
        Get replies to a thread via S3 cache (read-only).

        Cache is populated by a separate process. This method never calls
        the Slack API directly. Returns stale data if available, or empty
        list if cache has no data for this thread yet.

        Args:
            token: Authentication token (unused — kept for API compatibility)
            channel: Channel ID (e.g., "C0AAAAMBR1R")
            thread_ts: Timestamp of the parent message
            limit: Number of replies to retrieve

        Returns:
            List of message dicts including parent and all replies.
            First message is the parent, rest are replies in chronological order.

        Audio/Voice Message Handling:
            Thread replies may also contain audio/voice attachments.
            Apply the same transcription protocol as get_channel_history():
            check msg['files'] for audio mimetypes or 'voice_message' subtype,
            then transcribe via utils transcript API at /v1/audio/transcriptions.
            See AGENT_PROTOCOL.md Section 5 for details.
        """
        safe_ts = thread_ts.replace(".", "_")
        cache_key = f"thread_{channel}_{safe_ts}"
        cached = _read_channel_mirror(cache_key)
        if cached is not None:
            return cached[:limit]
        return []

    def add_reaction(
        self, token: str, channel: str, timestamp: str, emoji: str
    ) -> bool:
        """
        Add an emoji reaction to a message via the Slack reactions.add API.

        Args:
            token: Bot token with reactions:write scope
            channel: Channel ID containing the message
            timestamp: Message timestamp (ts) to react to
            emoji: Emoji name without colons (e.g. "ghost", "eyes", "+1")

        Returns:
            True if the reaction was added successfully, False otherwise
            (already_reacted is treated as success — idempotent).
        """
        url = f"{self.BASE_URL}/reactions.add"
        payload = {
            "channel": channel,
            "timestamp": timestamp,
            "name": emoji,
        }
        try:
            resp = requests.post(
                url, headers=self._get_headers(token), json=payload, timeout=10
            )
            data = resp.json()
            if data.get("ok"):
                return True
            # already_reacted is fine — we just want the reaction there
            if data.get("error") == "already_reacted":
                return True
            return False
        except Exception:
            return False

    def send_message(
        self,
        token: str,
        channel: str,
        text: str,
        thread_ts: Optional[str] = None,
        username: Optional[str] = None,
        icon_emoji: Optional[str] = None,
        icon_url: Optional[str] = None,
        convert_markdown: bool = True,
        blocks: Optional[List[Dict]] = None,
        unfurl_links: Optional[bool] = None,
        unfurl_media: Optional[bool] = None,
        auto_blocks_for_tables: bool = True,
    ) -> Dict:
        """
        Send a message to a channel.

        API Method: chat.postMessage
        Required Scopes: chat:write

        Note: username, icon_emoji, and icon_url only work with bot tokens
        and require chat:write.customize scope for full customization.

        Args:
            token: Authentication token (bot token preferred for custom identity)
            channel: Channel ID or name
            text: Message text (supports Markdown - auto-converted to Slack mrkdwn)
                  Also serves as fallback text when blocks are provided.
            thread_ts: Thread timestamp for replies (optional)
            username: Custom bot username (optional, bot token only)
            icon_emoji: Custom emoji icon like ":robot_face:" (optional)
            icon_url: Custom icon image URL (optional, overrides icon_emoji)
            convert_markdown: If True, convert standard Markdown to Slack mrkdwn (default: True)
            blocks: List of Block Kit block dicts for rich layouts (optional)
                    See: https://api.slack.com/block-kit
            auto_blocks_for_tables: If True (default) and no explicit `blocks`
                    are supplied, Markdown containing tables/headers/dividers/
                    fenced code is promoted to Block Kit blocks so Slack renders
                    tables natively. Set False to force the plain mrkdwn path.

        Returns:
            API response with 'ok', 'ts' (timestamp), 'channel' on success

        Note:
            Markdown conversion handles:
            - **bold** -> *bold*
            - *italic* -> _italic_
            - [text](url) -> <url|text>
            - # Heading -> *Heading* (bold)
            - - item -> bullet item

            Tables in Markdown are auto-promoted to a Block Kit `markdown`
            block (Slack renders them as real tables). Disable with
            auto_blocks_for_tables=False to fall back to raw passthrough.

        Block Kit Examples:
            Radio buttons, checkboxes, buttons, select menus can be sent via blocks.
            See send_poll() for a convenient wrapper for multiple choice questions.
        """
        # Convert standard Markdown to Slack mrkdwn format
        # Convert 0.0.0.0:<port> to public sandbox URLs
        text = convert_sandbox_urls(text)

        if convert_markdown:
            # If the caller did not supply blocks, auto-conversion is enabled,
            # and the text contains a markdown table/header/divider/code-block,
            # promote to Block Kit (markdown blocks render tables natively).
            # Otherwise fall back to the plain slackify_markdown path.
            if (
                blocks is None
                and auto_blocks_for_tables
                and md_to_slack_blocks is not None
            ):
                auto_blocks, fallback = md_to_slack_blocks(text)
                if auto_blocks:
                    blocks = auto_blocks
                    text = fallback  # notification preview only
                else:
                    text = slackify_markdown(text)
            else:
                text = slackify_markdown(text)

        params = {"channel": channel, "text": text}
        if thread_ts:
            params["thread_ts"] = thread_ts

        # Custom bot appearance (only works with bot tokens)
        if username:
            params["username"] = username
        if icon_emoji:
            params["icon_emoji"] = icon_emoji
        if icon_url:
            params["icon_url"] = icon_url

        # Block Kit blocks for rich layouts
        if blocks:
            params["blocks"] = blocks

        # Unfurl controls — required for the permalink-as-attachment flow
        # used by upload_file(). Slack's default for bot messages is to
        # suppress unfurls unless explicitly enabled.
        if unfurl_links is not None:
            params["unfurl_links"] = bool(unfurl_links)
        if unfurl_media is not None:
            params["unfurl_media"] = bool(unfurl_media)

        # Log intent before the API call
        logger = _get_logger()
        sender = username or "bot"
        preview = text[:200] + ("..." if len(text) > 200 else "")
        logger.info(f"MSG SENDING [{sender} → {channel}]: {preview}")

        result = self._api_call("chat.postMessage", token, params)

        # Log outcome
        if result.get("ok"):
            logger.info(f"MSG SENT [{sender} → {channel}] ts={result.get('ts')}")
        else:
            logger.error(
                f"MSG FAIL [{sender} → {channel}]: {result.get('error', 'unknown')}"
            )

        return result

    def upload_file_v2(
        self,
        token: str,
        channel: Optional[str] = None,
        file_path: Optional[str] = None,
        content: Optional[str] = None,
        filename: Optional[str] = None,
        title: Optional[str] = None,
        initial_comment: Optional[str] = None,
        thread_ts: Optional[str] = None,
        snippet_type: Optional[str] = None,
    ) -> Dict:
        """
        Upload a file to Slack using the newer files.uploadV2 API.

        This is the recommended method for file uploads as files.upload is deprecated.
        The V2 API uses a three-step process:
        1. Get an upload URL from Slack (files.getUploadURLExternal)
        2. Upload the file content to that URL
        3. Complete the upload (files.completeUploadExternal). When
           `channel` is provided the file is shared into that channel on
           completion; when `channel` is None the file is stored in Slack
           but not posted anywhere — the caller is then free to share it
           later (e.g. by posting its permalink from `get_file_info`).

        API Methods: files.getUploadURLExternal, files.completeUploadExternal
        Required Scopes: files:write

        Retry Behavior:
            - Retries on HTTP 429 (rate limited) with exponential backoff
            - Retries on connection errors and timeouts
            - Maximum 5 retries per step with up to 60s delay

        Args:
            token: Authentication token with files:write scope
            channel: Channel ID to share file to (must be ID, not name).
                     Pass None to upload WITHOUT sharing to any channel —
                     useful when the caller wants to post the file's
                     permalink itself (e.g. as a specific agent identity).
            file_path: Path to file on disk (optional if content provided)
            content: File content as string/bytes (optional if file_path provided)
            filename: Filename to display in Slack (required if content provided)
            title: Title for the file (optional, defaults to filename)
            initial_comment: Message to post with the file (optional)
            thread_ts: Thread timestamp to post file as reply (optional)
            snippet_type: For text content, the syntax highlighting type (optional)

        Returns:
            API response with 'ok', 'files' array on success

        Example:
            # Upload and share to a channel
            result = client.upload_file_v2(token, "C123456", file_path="report.pdf")

            # Upload without sharing (caller shares the permalink later)
            result = client.upload_file_v2(token, channel=None, file_path="report.pdf")

            # Upload text content
            result = client.upload_file_v2(token, "C123456",
                                           content="print('hello')",
                                           filename="script.py",
                                           snippet_type="python")
        """
        max_retries = 5
        base_delay = 1.0
        max_delay = 60.0

        def _request_with_retry(
            method: str, url: str, step_name: str, **kwargs
        ) -> requests.Response:
            """Helper to make requests with retry logic."""
            for attempt in range(max_retries + 1):
                try:
                    if method == "post":
                        response = requests.post(url, **kwargs)
                    else:
                        response = requests.get(url, **kwargs)

                    # Check for rate limiting
                    if response.status_code == 429:
                        if attempt < max_retries:
                            retry_after = response.headers.get(
                                "Retry-After", base_delay * (2**attempt)
                            )
                            delay = min(float(retry_after), max_delay)
                            print(
                                f"[Rate Limited] {step_name}: Retry {attempt + 1}/{max_retries} after {delay:.1f}s...",
                                file=sys.stderr,
                            )
                            time.sleep(delay)
                            continue

                    # Check for server errors
                    if response.status_code in (500, 502, 503, 504):
                        if attempt < max_retries:
                            delay = min(base_delay * (2**attempt), max_delay)
                            print(
                                f"[Server Error {response.status_code}] {step_name}: Retry {attempt + 1}/{max_retries} after {delay:.1f}s...",
                                file=sys.stderr,
                            )
                            time.sleep(delay)
                            continue

                    return response

                except (
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                ) as e:
                    if attempt < max_retries:
                        delay = min(base_delay * (2**attempt), max_delay)
                        print(
                            f"[Connection Error] {step_name}: Retry {attempt + 1}/{max_retries} after {delay:.1f}s...",
                            file=sys.stderr,
                        )
                        time.sleep(delay)
                        continue
                    raise

            return response

        try:
            # Determine file content and metadata
            if file_path:
                path = Path(file_path)
                if not path.exists():
                    return {"ok": False, "error": f"File not found: {file_path}"}

                file_content = path.read_bytes()
                file_size = len(file_content)
                actual_filename = filename or path.name
            elif content:
                if isinstance(content, str):
                    file_content = content.encode("utf-8")
                else:
                    file_content = content
                file_size = len(file_content)
                actual_filename = filename or "untitled"
            else:
                return {
                    "ok": False,
                    "error": "Either file_path or content must be provided",
                }

            actual_title = title or actual_filename

            # Step 1: Get upload URL (uses form data, not JSON)
            get_url_data = {"filename": actual_filename, "length": file_size}
            if snippet_type:
                get_url_data["snippet_type"] = snippet_type

            headers = {"Authorization": f"Bearer {token}"}
            url_response = _request_with_retry(
                "post",
                f"{self.BASE_URL}/files.getUploadURLExternal",
                "files.getUploadURLExternal",
                headers=headers,
                data=get_url_data,
                timeout=30,
            )

            url_response_json = url_response.json()

            # Check for rate limit in response body
            if not url_response_json.get("ok"):
                if url_response_json.get("error") in ("ratelimited", "rate_limited"):
                    # Already retried in _request_with_retry, return error
                    pass
                return url_response_json

            upload_url = url_response_json.get("upload_url")
            file_id = url_response_json.get("file_id")

            if not upload_url or not file_id:
                return {"ok": False, "error": "Failed to get upload URL from Slack"}

            # Step 2: Upload file content to the URL
            upload_response = _request_with_retry(
                "post",
                upload_url,
                "file upload",
                data=file_content,
                headers={"Content-Type": "application/octet-stream"},
                timeout=120,
            )

            if upload_response.status_code != 200:
                return {
                    "ok": False,
                    "error": f"Upload failed with status {upload_response.status_code}",
                }

            # Step 3: Complete the upload and optionally share to channel (uses form data)
            complete_data = {
                "files": json.dumps([{"id": file_id, "title": actual_title}])
            }

            if channel:
                complete_data["channel_id"] = channel
            if initial_comment:
                complete_data["initial_comment"] = initial_comment
            if thread_ts:
                complete_data["thread_ts"] = thread_ts

            complete_response = _request_with_retry(
                "post",
                f"{self.BASE_URL}/files.completeUploadExternal",
                "files.completeUploadExternal",
                headers=headers,
                data=complete_data,
                timeout=30,
            )

            return complete_response.json()

        except requests.RequestException as e:
            return {"ok": False, "error": f"Request failed: {str(e)}"}
        except Exception as e:
            return {"ok": False, "error": f"Upload failed: {str(e)}"}

    def get_file_info(self, token: str, file_id: str) -> Dict:
        """
        Get information about an uploaded file.

        API Method: files.info
        Required Scopes: files:read

        Args:
            token: Authentication token
            file_id: The file ID to get info for

        Returns:
            API response with 'ok' and 'file' dict containing permalink, url_private, etc.
        """
        return self._api_call("files.info", token, {"file": file_id})

    def get_channel_info(self, token: str, channel: str) -> Dict:
        """
        Get information about a channel.

        API Method: conversations.info (GET with query params)
        Required Scopes: channels:read (public), groups:read (private)

        Args:
            token: Authentication token
            channel: Channel ID

        Returns:
            API response with 'ok', 'channel' object on success
        """
        url = f"{self.BASE_URL}/conversations.info"
        headers = self._get_headers(token)
        params = {"channel": channel}

        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            return response.json()
        except requests.RequestException as e:
            print(f"❌ Error: {e}", file=sys.stderr)
            return {"ok": False, "error": str(e)}

    def join_channel(self, token: str, channel: str) -> Dict:
        """
        Join a channel.

        API Method: conversations.join
        Required Scopes: channels:join

        Note: Bots can only join public channels. For private channels,
        the bot must be invited by a channel member.

        Args:
            token: Authentication token
            channel: Channel ID to join

        Returns:
            API response with 'ok', 'channel' object on success
        """
        params = {"channel": channel}
        return self._api_call("conversations.join", token, params)

    def create_channel(self, token: str, name: str, is_private: bool = False) -> Dict:
        """
        Create a new channel.

        API Method: conversations.create
        Required Scopes: channels:manage (public), groups:write (private)

        Args:
            token: Authentication token
            name: Channel name (lowercase, no spaces, max 80 chars)
            is_private: Create as private channel (default: False)

        Returns:
            API response with 'ok', 'channel' object on success
        """
        params = {"name": name, "is_private": is_private}
        return self._api_call("conversations.create", token, params)


# ============================================================================
# CLI Commands
# ============================================================================
# Each cmd_* function implements a CLI subcommand.
# Functions receive the client, tokens, and parsed args.


def cmd_agents(client: SlackClient, tokens: SlackTokens, args) -> None:
    """List all available agents with their avatars."""
    print("\n" + "=" * 60)
    print("🤖 AVAILABLE AGENTS")
    print("=" * 60)

    for agent_id, info in AGENT_AVATARS.items():
        print(f"\n{info['emoji']} {info['name']} ({agent_id})")
        print(f"   Role: {info['role']}")
        print(f"   Color: {info['color']}")
        print(f"   Avatar: {info['icon_url']}")

    print("\n" + "-" * 60)
    print("💡 Usage:")
    print("   python slack_interface.py say -a nova 'Hello from Nova!'")
    print("   python slack_interface.py say -a pixel 'Design ready!'")
    print("   python slack_interface.py say -a bolt 'Code deployed!'")
    print("   python slack_interface.py say -a scout 'Tests passed!'")
    print("=" * 60 + "\n")


def cmd_config(client: SlackClient, tokens: SlackTokens, args) -> None:
    """Show or set configuration."""
    config = SlackConfig.load(args.config_file)

    # Set default channel
    if hasattr(args, "set_channel") and args.set_channel:
        channel = args.set_channel

        # If channel ID was provided directly, skip the API lookup entirely
        explicit_id = getattr(args, "set_channel_id", None)
        if explicit_id:
            config.default_channel = channel
            config.default_channel_id = explicit_id
            print(f"✅ Channel configured: {channel} (ID: {explicit_id})")
        elif channel.startswith("#"):
            # Try to resolve channel ID via API
            # Unreliable as this runs into rate limits
            token = tokens.bot_token
            if token:
                channels = client.list_channels(token)
                channel_name = channel[1:]  # Remove #
                for ch in channels:
                    if ch.get("name") == channel_name:
                        config.default_channel = channel
                        config.default_channel_id = ch.get("id")
                        print(
                            f"✅ Found channel: {channel} (ID: {config.default_channel_id})"
                        )
                        break
                else:
                    print(f"⚠️ Channel {channel} not found, saving name only")
                    config.default_channel = channel
                    config.default_channel_id = None
            else:
                config.default_channel = channel
        else:
            # Assume it's a channel ID
            config.default_channel_id = channel

        config.save(args.config_file)
        return

    # Set workspace ID
    if hasattr(args, "set_workspace_id") and args.set_workspace_id:
        config.default_team_id = args.set_workspace_id
        config.save(args.config_file)
        print(f"✅ Workspace ID set to: {args.set_workspace_id}")
        return

    # Set default agent
    if hasattr(args, "set_agent") and args.set_agent:
        agent = args.set_agent.lower()
        if agent not in AGENT_AVATARS:
            print(f"❌ Invalid agent: {agent}", file=sys.stderr)
            print(
                f"   Valid agents: {', '.join(AGENT_AVATARS.keys())}", file=sys.stderr
            )
            sys.exit(1)

        config.default_agent = agent
        agent_info = AGENT_AVATARS[agent]
        print(f"✅ Default agent set to: {agent_info['name']} ({agent_info['role']})")
        config.save(args.config_file)
        return

    # Show current configuration
    print("\n" + "=" * 60)
    print("⚙️  SLACK INTERFACE CONFIGURATION")
    print("=" * 60)
    print(f"\n📁 Config file: {args.config_file}")
    print(f"\n📋 Current Settings:")
    print(f"   Default Channel: {config.default_channel or '(not set)'}")
    print(f"   Default Channel ID: {config.default_channel_id or '(not set)'}")
    if config.default_agent:
        agent_info = AGENT_AVATARS.get(config.default_agent, {})
        print(
            f"   Default Agent: {config.default_agent} ({agent_info.get('name', '')} - {agent_info.get('role', '')})"
        )
    else:
        print(f"   Default Agent: (not set)")
    print(f"   Workspace: {config.workspace or '(not set)'}")

    print(f"\n💡 Configuration Commands:")
    print(f"   python slack_interface.py config --set-channel '#channel-name'")
    print(f"   python slack_interface.py config --set-agent nova")
    print(f"\n🤖 Available Agents: {', '.join(AGENT_AVATARS.keys())}")
    print("=" * 60 + "\n")


def cmd_say(client: SlackClient, tokens: SlackTokens, args) -> None:
    """Send a message to the default channel as the configured agent."""
    config = SlackConfig.load(args.config_file)

    # Use -a flag if provided, otherwise fall back to config default
    agent = (
        args.agent.lower()
        if hasattr(args, "agent") and args.agent
        else config.default_agent.lower()
        if config.default_agent
        else None
    )

    if not agent:
        print("❌ No default agent configured", file=sys.stderr)
        print("\n🤖 The 'say' command requires an agent identity.", file=sys.stderr)
        print("\n💡 First, set your default agent:", file=sys.stderr)
        print("   python slack_interface.py config --set-agent nova", file=sys.stderr)
        print(
            f"\n🤖 Available agents: {', '.join(AGENT_AVATARS.keys())}", file=sys.stderr
        )
        sys.exit(1)

    # Validate agent
    if agent not in AGENT_AVATARS:
        print(f"❌ Invalid agent in config: {agent}", file=sys.stderr)
        print(f"\n💡 Set a valid agent:", file=sys.stderr)
        print(f"   python slack_interface.py config --set-agent nova", file=sys.stderr)
        print(f"\n🤖 Valid agents: {', '.join(AGENT_AVATARS.keys())}", file=sys.stderr)
        sys.exit(1)

    # Use channel from config (REQUIRED - must be set first)
    channel = config.get_default_channel()

    if not channel:
        print("❌ No default channel configured", file=sys.stderr)
        print("\n💡 First, set your default channel:", file=sys.stderr)
        print(
            "   python slack_interface.py config --set-channel '#channel-name'",
            file=sys.stderr,
        )
        sys.exit(1)

    # Use bot token for sending messages (supports custom username/icon)
    token = tokens.bot_token
    if not token:
        print("❌ No valid token available", file=sys.stderr)
        sys.exit(1)

    message = args.message
    thread = args.thread if hasattr(args, "thread") else None

    # Optional explicit Block Kit blocks / auto-conversion toggle.
    blocks = None
    if getattr(args, "blocks", None):
        try:
            blocks = json.loads(args.blocks)
        except json.JSONDecodeError as e:
            print(f"❌ Invalid --blocks JSON: {e}", file=sys.stderr)
            sys.exit(1)
    auto_blocks = not getattr(args, "no_auto_blocks", False)

    # Get agent avatar info
    agent_info = get_agent_avatar(agent)
    username = agent_info["name"]
    icon_url = agent_info["icon_url"]
    icon_emoji = None  # Don't use emoji when we have custom avatar URL

    # Show which channel we're sending to
    channel_display = channel if channel.startswith("#") else f"ID:{channel}"
    print(f"\n📤 Sending to {channel_display}...")
    print(f"   As: {username} ({agent_info['role']})")
    print(f"   Avatar: {agent_info['emoji']} Custom image")

    result = client.send_message(
        token,
        channel,
        message,
        thread,
        username=username,
        icon_emoji=icon_emoji,
        icon_url=icon_url,
        blocks=blocks,
        auto_blocks_for_tables=auto_blocks,
    )

    logger = _get_logger()
    if result.get("ok"):
        print(f"✅ Message sent successfully!")
        print(f"   Channel: {result.get('channel')}")
        print(f"   Timestamp: {result.get('ts')}")
        # Log the sent message
        preview = message[:200] + ("..." if len(message) > 200 else "")
        logger.info(f"MSG SENT as {username} to {channel_display}: {preview}")
    else:
        error = result.get("error", "Unknown error")
        print(f"❌ Failed to send: {error}")
        logger.error(f"MSG FAILED as {username} to {channel_display}: {error}")
        sys.exit(1)


def cmd_read(client: SlackClient, tokens: SlackTokens, args) -> None:
    """Read messages from the default channel."""
    config = SlackConfig.load(args.config_file)

    # Determine channel: CLI arg > config default
    channel = None
    if hasattr(args, "channel") and args.channel:
        channel = args.channel
    else:
        channel = config.get_default_channel()

    if not channel:
        print(
            "❌ No channel specified and no default channel configured", file=sys.stderr
        )
        print("\n💡 To set a default channel:", file=sys.stderr)
        print(
            "   python slack_interface.py config --set-channel '#channel-name'",
            file=sys.stderr,
        )
        print("\n   Or specify channel with -c:", file=sys.stderr)
        print("   python slack_interface.py read -c '#channel'", file=sys.stderr)
        sys.exit(1)

    # Use bot token for reading (has channels:history scope)
    token = tokens.bot_token
    if not token:
        print("❌ No valid token available", file=sys.stderr)
        sys.exit(1)

    limit = args.limit if hasattr(args, "limit") else 50

    # Show which channel we're reading from
    channel_display = channel if channel.startswith("#") else f"ID:{channel}"
    print(f"\n📖 Reading messages from {channel_display}...")

    messages = client.get_channel_history(token, channel, limit)

    if not messages:
        print("📭 No messages found or channel is empty")
        print("\n💡 Troubleshooting:")
        print(
            "   • 'missing_scope' error: Add 'channels:history' scope to your Slack app"
        )
        print("   • 'not_in_channel' error: Invite the bot to the channel first:")
        print("     → Go to the channel in Slack and type: /invite @superninja")
        print(
            "   • Or add 'channels:join' scope to allow the bot to join automatically"
        )
        return

    print(f"\n💬 Last {len(messages)} messages:\n")
    print("=" * 80)

    # Build user cache from message data only (no extra API call)
    # This avoids the expensive users.list API call which can be rate limited
    # and makes multiple paginated requests for large workspaces
    users_cache = {}
    for msg in messages:
        user_id = msg.get("user")
        if user_id and user_id not in users_cache:
            # Try to get username from message metadata if available
            if msg.get("user_profile"):
                profile = msg.get("user_profile")
                users_cache[user_id] = (
                    profile.get("real_name")
                    or profile.get("display_name")
                    or profile.get("name")
                    or user_id
                )
            elif msg.get("username"):
                users_cache[user_id] = msg.get("username")

    for msg in reversed(messages):
        user_id = msg.get("user", "unknown")
        user_name = users_cache.get(user_id, user_id)
        text = msg.get("text", "")
        ts = msg.get("ts", "")

        # Check for bot messages with custom username
        if msg.get("bot_id") and msg.get("username"):
            user_name = msg.get("username")

        # Convert timestamp
        try:
            dt = datetime.fromtimestamp(float(ts))
            time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except:
            time_str = ts

        # ----------------------------------------------------------------
        # Audio/Voice Message Detection
        # ----------------------------------------------------------------
        # Slack voice messages and audio files appear in the 'files' array
        # with mimetype starting with 'audio/' (e.g., audio/webm, audio/mp4,
        # audio/ogg, audio/wav, audio/mpeg) or subtype 'voice_message'.
        #
        # When an audio/voice attachment is detected:
        #   1. Download the file using file['url_private_download'] with bot token
        #   2. Transcribe it via the utils transcript API:
        #        from utils.litellm_client import get_config, api_url
        #        resp = requests.post(
        #            api_url("/v1/audio/transcriptions"),
        #            headers={"Authorization": f"Bearer {cfg['api_key']}"},
        #            files={"file": (name, audio_bytes, mimetype)},
        #            data={"model": "whisper-1"}
        #        )
        #        transcript = resp.json().get("text", "")
        #   3. Use the transcript text as the message content for processing
        #
        # See AGENT_PROTOCOL.md Section 5 for the full audio handling protocol.
        # ----------------------------------------------------------------
        has_audio = False
        files = msg.get("files", [])
        for f in files:
            mimetype = f.get("mimetype", "")
            subtype = f.get("subtype", "")
            if mimetype.startswith("audio/") or subtype == "voice_message":
                has_audio = True
                break

        # Format output
        print(f"┌─ {user_name} [{time_str}]")

        # Flag audio/voice messages so agents know to transcribe them
        if has_audio:
            print(f"│  🎤 [Voice/Audio Message — transcribe using utils transcript API]")

        # Handle multi-line messages
        for line in text.split("\n"):
            print(f"│  {line}")

        print("└" + "─" * 79)

    print(f"\n📊 Total: {len(messages)} messages from {channel_display}")
    _get_logger().info(f"READ {len(messages)} messages from {channel_display}")


def cmd_upload(client: SlackClient, tokens: SlackTokens, args) -> None:
    """Upload a file to a channel as the configured agent identity."""
    config = SlackConfig.load(args.config_file)

    # Determine channel: CLI arg > config default
    channel = (
        args.channel
        if hasattr(args, "channel") and args.channel
        else config.get_default_channel()
    )

    if not channel:
        print(
            "❌ No channel specified and no default channel configured", file=sys.stderr
        )
        print("\n💡 To set a default channel:", file=sys.stderr)
        print(
            "   python slack_interface.py config --set-channel '#channel-name'",
            file=sys.stderr,
        )
        print("\n   Or specify channel with -c:", file=sys.stderr)
        print(
            "   python slack_interface.py upload file.png -c '#channel'",
            file=sys.stderr,
        )
        sys.exit(1)

    # The configured agent identity is REQUIRED — every CLI command posts as
    # that agent. This is set once via:
    #   python slack_interface.py config --set-agent <agent>
    agent = config.default_agent.lower() if config.default_agent else None
    if not agent:
        print("❌ No default agent configured", file=sys.stderr)
        print(
            "\n🤖 The 'upload' command posts files as the configured agent.",
            file=sys.stderr,
        )
        print("\n💡 First, set your default agent:", file=sys.stderr)
        print(
            "   python slack_interface.py config --set-agent phantom", file=sys.stderr
        )
        print(
            f"\n🤖 Available agents: {', '.join(AGENT_AVATARS.keys())}", file=sys.stderr
        )
        sys.exit(1)

    if agent not in AGENT_AVATARS:
        print(f"❌ Invalid agent in config: {agent}", file=sys.stderr)
        print(f"\n💡 Set a valid agent:", file=sys.stderr)
        print(
            f"   python slack_interface.py config --set-agent phantom", file=sys.stderr
        )
        print(f"\n🤖 Valid agents: {', '.join(AGENT_AVATARS.keys())}", file=sys.stderr)
        sys.exit(1)

    # Use bot token for uploads
    token = tokens.bot_token
    if not token:
        print("❌ No valid token available", file=sys.stderr)
        sys.exit(1)

    # Check scopes required by the permalink+unfurl upload flow.
    scopes = client.get_scopes(token) or []
    required_scopes = (
        "files:write",
        "files:read",
        "chat:write",
        "chat:write.customize",
        "links:read",
    )
    if scopes:
        missing = [s for s in required_scopes if s not in scopes]
        if missing:
            print(
                f"⚠️ Warning: Token may be missing scopes required for "
                f"agent-authored uploads: {', '.join(missing)}",
                file=sys.stderr,
            )
            print(
                "   The upload may succeed but the message may post under the "
                "default bot identity or the file preview may not render.",
                file=sys.stderr,
            )

    file_path = args.file
    title = args.title if hasattr(args, "title") and args.title else None
    comment = args.message if hasattr(args, "message") and args.message else None
    thread = args.thread if hasattr(args, "thread") and args.thread else None

    # Instantiate SlackInterface and reuse tokens/config we already loaded so
    # we don't re-read /dev/shm/mcp-token or ~/.agent_settings.json.
    # upload_file() handles channel name → ID resolution internally.
    slack = SlackInterface.__new__(SlackInterface)
    slack.tokens = tokens
    slack.config = config
    slack.client = client
    slack._token = token

    agent_info = get_agent_avatar(agent)
    channel_display = channel if channel.startswith("#") else f"ID:{channel}"
    print(f"\n📤 Uploading to {channel_display}...")
    print(f"   File: {file_path}")
    print(f"   As: {agent_info['name']} ({agent_info['role']})")
    print(f"   Avatar: {agent_info['emoji']} Custom image")
    if title:
        print(f"   Title: {title}")
    if comment:
        print(f"   Comment: {comment[:50]}{'...' if len(comment) > 50 else ''}")

    logger = _get_logger()
    try:
        result = slack.upload_file(
            file_path=file_path,
            channel=channel,
            title=title,
            comment=comment,
            thread_ts=thread,
            agent=agent,
        )
    except (ValueError, RuntimeError) as e:
        print(f"❌ Failed to upload: {e}", file=sys.stderr)
        logger.error(f"UPLOAD FAILED: {file_path} to {channel_display}: {e}")
        sys.exit(1)

    if result.get("ok"):
        file_info = result.get("file") or {}
        print(f"✅ File uploaded to {channel_display}")
        if agent_info:
            print(f"   Identity: {agent_info['name']} (configured agent)")
        if file_info.get("id"):
            print(f"   File ID: {file_info.get('id')}")
        if file_info.get("title"):
            print(f"   Title: {file_info.get('title')}")
        if result.get("permalink"):
            print(f"   Permalink: {result['permalink']}")
        if result.get("message_ts"):
            print(f"   Message ts: {result['message_ts']}")
        logger.info(
            f"UPLOAD OK as {agent_info['name']}: {file_path} to {channel_display}"
        )
    else:
        error = (
            result.get("upload_error") or result.get("message_error") or "Unknown error"
        )
        print(f"❌ Failed to upload: {error}", file=sys.stderr)
        logger.error(f"UPLOAD FAILED: {file_path} to {channel_display}: {error}")
        if error == "missing_scope":
            print(
                "\n💡 The 'files:write' scope is required for file uploads.",
                file=sys.stderr,
            )
            print(
                "   Add this scope to your Slack app at: https://api.slack.com/apps",
                file=sys.stderr,
            )
        elif error == "channel_not_found":
            print(
                "\n💡 Channel not found. Make sure the bot is a member of the channel.",
                file=sys.stderr,
            )
        sys.exit(1)


def cmd_scopes(client: SlackClient, tokens: SlackTokens, args) -> None:
    """Show available scopes for each token."""
    print("\n" + "=" * 70)
    print("🔑 SLACK TOKEN SCOPES")
    print("=" * 70)

    token_info = [
        ("Bot Token (xoxb)", tokens.bot_token),
    ]

    for name, token in token_info:
        print(f"\n📦 {name}:")

        if not token:
            print("   ❌ Not available")
            continue

        # Mask token for display
        masked = token[:15] + "..." + token[-8:]
        print(f"   Token: {masked}")

        # Test auth
        auth_result = client.test_auth(token)
        if auth_result.get("ok"):
            print(f"   ✅ Valid")
            print(f"   User: {auth_result.get('user', 'N/A')}")
            print(f"   Team: {auth_result.get('team', 'N/A')}")
            print(f"   URL: {auth_result.get('url', 'N/A')}")
        else:
            print(f"   ❌ Invalid: {auth_result.get('error', 'Unknown error')}")
            continue

        # Get scopes
        scopes = client.get_scopes(token)
        if scopes:
            print(f"\n   📋 Scopes ({len(scopes)}):")
            # Group scopes by category
            categories = {}
            for scope in sorted(scopes):
                category = scope.split(":")[0] if ":" in scope else scope.split(".")[0]
                if category not in categories:
                    categories[category] = []
                categories[category].append(scope)

            for category in sorted(categories.keys()):
                print(f"      [{category}]")
                for scope in categories[category]:
                    print(f"         • {scope}")
        else:
            print("   ⚠️  No scopes found (may be a legacy token)")

    # Show required scopes info
    print("\n" + "-" * 70)
    print("📋 REQUIRED SCOPES BY FEATURE:")
    print("-" * 70)
    print("   Basic Operations:")
    print("      • channels:read      - List channels")
    print("      • channels:history   - Read channel messages")
    print("      • chat:write         - Send messages")
    print("      • users:read         - List users")
    print("   File Uploads:")
    print("      • files:write        - Upload files")
    print("      • files:read         - Read file info (optional)")
    print("   Channel Management:")
    print("      • channels:join      - Join public channels")
    print("      • channels:manage    - Create/archive channels")
    print("   Private Channels:")
    print("      • groups:read        - List private channels")
    print("      • groups:history     - Read private channel messages")
    print("=" * 70 + "\n")


def cmd_channels(client: SlackClient, tokens: SlackTokens, args) -> None:
    """List all channels."""
    token = tokens.bot_token
    if not token:
        print("❌ No valid token available", file=sys.stderr)
        return

    print("\n🔍 Fetching channels...")

    channel_types = (
        args.types
        if hasattr(args, "types") and args.types
        else "public_channel,private_channel"
    )
    channels = client.list_channels(token, types=channel_types)

    if not channels:
        print("❌ No channels found or error occurred")
        return

    # Sort by member count
    channels.sort(key=lambda x: x.get("num_members", 0), reverse=True)

    print(f"\n📢 Found {len(channels)} channels:\n")
    print(f"{'#':<4} {'Channel Name':<35} {'ID':<15} {'Members':<10} {'Private':<8}")
    print("-" * 75)

    for i, ch in enumerate(channels, 1):
        name = ch.get("name", "unknown")
        cid = ch.get("id", "N/A")
        members = ch.get("num_members", 0)
        is_private = "🔒" if ch.get("is_private") else ""
        print(f"{i:<4} #{name:<34} {cid:<15} {members:<10} {is_private}")

    print("-" * 75)

    # Save to file if requested
    if hasattr(args, "output") and args.output:
        with open(args.output, "w") as f:
            json.dump(channels, f, indent=2)
        print(f"\n💾 Saved to {args.output}")


def cmd_users(client: SlackClient, tokens: SlackTokens, args) -> None:
    """List all users."""
    token = tokens.bot_token
    if not token:
        print("❌ No valid token available", file=sys.stderr)
        return

    print("\n🔍 Fetching users...")
    users = client.list_users(token)

    if not users:
        print("❌ No users found or error occurred")
        return

    # Filter out bots and deleted users unless requested
    if not (hasattr(args, "all") and args.all):
        users = [u for u in users if not u.get("is_bot") and not u.get("deleted")]

    print(f"\n👥 Found {len(users)} users:\n")
    print(f"{'#':<4} {'Username':<20} {'Real Name':<30} {'ID':<15}")
    print("-" * 70)

    for i, user in enumerate(users, 1):
        username = user.get("name", "unknown")
        real_name = user.get(
            "real_name", user.get("profile", {}).get("real_name", "N/A")
        )
        uid = user.get("id", "N/A")
        print(f"{i:<4} @{username:<19} {real_name:<30} {uid:<15}")

    print("-" * 70)

    if hasattr(args, "output") and args.output:
        with open(args.output, "w") as f:
            json.dump(users, f, indent=2)
        print(f"\n💾 Saved to {args.output}")


def cmd_history(client: SlackClient, tokens: SlackTokens, args) -> None:
    """Get channel history."""
    token = tokens.bot_token
    if not token:
        print("❌ No valid token available", file=sys.stderr)
        return

    channel = args.channel
    limit = args.limit if hasattr(args, "limit") else 20

    print(f"\n🔍 Fetching history for {channel}...")
    messages = client.get_channel_history(token, channel, limit)

    if not messages:
        print("❌ No messages found or error occurred")
        return

    print(f"\n💬 Last {len(messages)} messages:\n")

    for msg in reversed(messages):
        user = msg.get("user", "unknown")
        text = msg.get("text", "")[:100]
        ts = msg.get("ts", "")

        # Check for bot messages
        if msg.get("bot_id") and msg.get("username"):
            user = msg.get("username")

        try:
            dt = datetime.fromtimestamp(float(ts))
            time_str = dt.strftime("%H:%M:%S")
        except:
            time_str = ts

        print(f"[{time_str}] {user}: {text}")


def cmd_replies(client: SlackClient, tokens: SlackTokens, args) -> None:
    """Get thread replies."""
    # Use bot token (has channels:history scope)
    token = tokens.bot_token
    if not token:
        print("❌ No valid token available", file=sys.stderr)
        return

    thread_ts = args.thread_ts
    limit = args.limit if hasattr(args, "limit") else 50

    # Get channel from args or config
    channel = args.channel if hasattr(args, "channel") and args.channel else None
    if not channel:
        config_path = (
            args.config_file
            if hasattr(args, "config_file") and args.config_file
            else DEFAULT_CONFIG_PATH
        )
        config = SlackConfig.load(config_path)
        channel = config.default_channel_id or config.default_channel

    if not channel:
        print("❌ No channel specified and no default configured", file=sys.stderr)
        print(
            "💡 Set default: python slack_interface.py config --set-channel &quot;#channel&quot;",
            file=sys.stderr,
        )
        return

    print(f"\n🧵 Fetching replies for thread {thread_ts}...")
    messages = client.get_thread_replies(token, channel, thread_ts, limit)

    if not messages:
        print("❌ No replies found or error occurred")
        return

    print(f"\n💬 Thread with {len(messages)} messages:\n")
    print("=" * 80)

    for i, msg in enumerate(messages):
        user = msg.get("user", "unknown")
        text = msg.get("text", "")
        ts = msg.get("ts", "")

        # Check for bot messages
        if msg.get("bot_id") and msg.get("username"):
            user = msg.get("username")

        try:
            dt = datetime.fromtimestamp(float(ts))
            time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except:
            time_str = ts

        # Mark parent vs reply
        prefix = "📌 PARENT" if i == 0 else f"↳ Reply {i}"

        print(f"┌─ {user} [{time_str}] {prefix}")
        for line in text.split("\n"):
            print(f"│  {line}")
        print("└" + "─" * 79)

    print(f"\n📊 Total: {len(messages)} messages in thread")


def cmd_join(client: SlackClient, tokens: SlackTokens, args) -> None:
    """Join a channel."""
    token = tokens.bot_token
    if not token:
        print("❌ No valid token available", file=sys.stderr)
        return

    channel = args.channel
    print(f"\n🚪 Joining {channel}...")

    result = client.join_channel(token, channel)

    if result.get("ok"):
        ch = result.get("channel", {})
        print(f"✅ Joined #{ch.get('name', channel)}")
    else:
        print(f"❌ Failed: {result.get('error', 'Unknown error')}")


def cmd_create(client: SlackClient, tokens: SlackTokens, args) -> None:
    """Create a new channel."""
    token = tokens.bot_token
    if not token:
        print("❌ No valid token available", file=sys.stderr)
        return

    name = args.name
    is_private = args.private if hasattr(args, "private") else False

    print(f"\n🆕 Creating {'private ' if is_private else ''}channel #{name}...")

    result = client.create_channel(token, name, is_private)

    if result.get("ok"):
        ch = result.get("channel", {})
        print(f"✅ Created #{ch.get('name', name)}")
        print(f"   ID: {ch.get('id')}")
    else:
        print(f"❌ Failed: {result.get('error', 'Unknown error')}")


def cmd_info(client: SlackClient, tokens: SlackTokens, args) -> None:
    """Get channel info."""
    token = tokens.bot_token
    if not token:
        print("❌ No valid token available", file=sys.stderr)
        return

    channel = args.channel
    print(f"\n🔍 Getting info for {channel}...")

    result = client.get_channel_info(token, channel)

    if result.get("ok"):
        ch = result.get("channel", {})
        print(f"\n📢 Channel: #{ch.get('name', 'N/A')}")
        print(f"   ID: {ch.get('id', 'N/A')}")
        print(f"   Members: {ch.get('num_members', 0)}")
        print(f"   Private: {'Yes' if ch.get('is_private') else 'No'}")
        print(f"   Archived: {'Yes' if ch.get('is_archived') else 'No'}")
        print(f"   Topic: {ch.get('topic', {}).get('value', 'N/A')}")
        print(f"   Purpose: {ch.get('purpose', {}).get('value', 'N/A')}")
    else:
        print(f"❌ Failed: {result.get('error', 'Unknown error')}")


# ============================================================================
# Main Entry Point
# ============================================================================


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Slack Interface CLI - Interact with Slack from the command line",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
First-time setup:
  %(prog)s config --set-channel "#your-channel"
  %(prog)s config --set-agent nova

Examples:
  %(prog)s agents                          List available agents
  %(prog)s say "Hello team!"               Send message as configured agent
  %(prog)s read -l 20                      Read last 20 messages
  %(prog)s upload design.png -m "Review"   Upload file with comment
  %(prog)s scopes                          Show token scopes

For more info: https://github.com/NinjaTech-AI/agent-team-logo-creator
        """,
    )
    parser.add_argument(
        "-T",
        "--token-file",
        default="/dev/shm/mcp-token",
        help="Path to MCP token file (default: /dev/shm/mcp-token)",
    )
    parser.add_argument(
        "-C",
        "--config-file",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Agents command
    subparsers.add_parser("agents", help="List all available agents with avatars")

    # Config command
    config_parser = subparsers.add_parser("config", help="Show or set configuration")
    config_parser.add_argument(
        "--set-channel",
        metavar="CHANNEL",
        help='Set default channel (e.g., "#logo-creator" or "C0AAAAMBR1R")',
    )
    config_parser.add_argument(
        "--set-channel-id",
        metavar="CHANNEL_ID",
        help='Set default channel ID directly (skips API lookup, e.g., "C0AAAAMBR1R")',
    )
    config_parser.add_argument(
        "--set-workspace-id",
        metavar="WORKSPACE_ID",
        help='Set workspace/team ID directly (e.g., "T0A9Q27KD1T")',
    )
    config_parser.add_argument(
        "--set-agent",
        metavar="AGENT",
        help="Set default agent (nova, pixel, bolt, scout)",
    )

    # Say command (send to default channel as configured agent)
    say_parser = subparsers.add_parser("say", help="Send message as configured agent")
    say_parser.add_argument("message", help="Message text")
    say_parser.add_argument("-t", "--thread", help="Thread timestamp for reply")
    say_parser.add_argument(
        "-a", "--agent", help="Override default agent (e.g., phantom, nova, bolt)"
    )
    say_parser.add_argument(
        "--blocks",
        help="JSON-encoded Block Kit blocks array. Overrides auto-conversion.",
    )
    say_parser.add_argument(
        "--no-auto-blocks",
        action="store_true",
        help="Disable markdown table -> markdown block auto-conversion.",
    )

    # Read command (read messages from default channel)
    read_parser = subparsers.add_parser(
        "read", help="Read messages from default channel"
    )
    read_parser.add_argument("-c", "--channel", help="Override default channel")
    read_parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=50,
        help="Number of messages to fetch (default: 50)",
    )

    # Upload command (upload file to channel)
    upload_parser = subparsers.add_parser("upload", help="Upload a file to a channel")
    upload_parser.add_argument("file", help="Path to file to upload")
    upload_parser.add_argument("-c", "--channel", help="Override default channel")
    upload_parser.add_argument("-m", "--message", help="Comment to post with file")
    upload_parser.add_argument("--title", help="Title for the file")
    upload_parser.add_argument("-t", "--thread", help="Thread timestamp for reply")

    # Scopes command
    subparsers.add_parser("scopes", help="Show available scopes for each token")

    # Channels command
    channels_parser = subparsers.add_parser("channels", help="List all channels")
    channels_parser.add_argument(
        "-t",
        "--types",
        default="public_channel,private_channel",
        help="Channel types (comma-separated)",
    )
    channels_parser.add_argument("-o", "--output", help="Save to JSON file")

    # Users command
    users_parser = subparsers.add_parser("users", help="List all users")
    users_parser.add_argument(
        "-a", "--all", action="store_true", help="Include bots and deleted users"
    )
    users_parser.add_argument("-o", "--output", help="Save to JSON file")

    # History command
    history_parser = subparsers.add_parser("history", help="Get channel history")
    history_parser.add_argument("channel", help="Channel ID or name")
    history_parser.add_argument(
        "-l", "--limit", type=int, default=20, help="Number of messages (default: 20)"
    )

    # Replies command
    replies_parser = subparsers.add_parser("replies", help="Get thread replies")
    replies_parser.add_argument(
        "thread_ts", help="Thread timestamp (e.g., 1234567890.123456)"
    )
    replies_parser.add_argument("-c", "--channel", help="Override default channel")
    replies_parser.add_argument(
        "-l", "--limit", type=int, default=50, help="Number of replies (default: 50)"
    )

    # Join command
    join_parser = subparsers.add_parser("join", help="Join a channel")
    join_parser.add_argument("channel", help="Channel ID or name")

    # Create command
    create_parser = subparsers.add_parser("create", help="Create a channel")
    create_parser.add_argument("name", help="Channel name")
    create_parser.add_argument(
        "-p", "--private", action="store_true", help="Create as private channel"
    )

    # Info command
    info_parser = subparsers.add_parser("info", help="Get channel info")
    info_parser.add_argument("channel", help="Channel ID or name")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Load tokens
    tokens = get_slack_tokens(args.token_file)

    if not tokens.bot_token:
        print("=" * 70, file=sys.stderr)
        print("❌ SLACK NOT CONNECTED", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        print(file=sys.stderr)
        print(
            "No Slack bot token found. Please connect your Slack workspace first.",
            file=sys.stderr,
        )
        print(file=sys.stderr)
        print("👉 To connect Slack:", file=sys.stderr)
        print(
            "   Click the 'Connect' button in the chat interface to link your",
            file=sys.stderr,
        )
        print(
            "   Slack workspace. This will automatically provide the necessary",
            file=sys.stderr,
        )
        print("   bot token (xoxb-*).", file=sys.stderr)
        print(file=sys.stderr)
        print("⚠️  Note: Only bot tokens (xoxb-*) are supported.", file=sys.stderr)
        print("   User tokens (xoxp-*) are NOT accepted.", file=sys.stderr)
        print(file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        print(file=sys.stderr)
        print("🔍 Technical Details:", file=sys.stderr)
        print(f"   • Token file checked: {args.token_file}", file=sys.stderr)
        print(f"   • Environment variables checked:", file=sys.stderr)
        print(f"     - SLACK_BOT_TOKEN", file=sys.stderr)
        print(f"     - SLACK_MCP_XOXB_TOKEN", file=sys.stderr)
        print(file=sys.stderr)
        print(
            "💡 Alternative: If you have a bot token, set it manually:", file=sys.stderr
        )
        print("   export SLACK_BOT_TOKEN='xoxb-your-bot-token-here'", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        sys.exit(1)

    # Create client
    client = SlackClient(tokens)

    # Execute command
    commands = {
        "agents": cmd_agents,
        "config": cmd_config,
        "say": cmd_say,
        "read": cmd_read,
        "upload": cmd_upload,
        "scopes": cmd_scopes,
        "channels": cmd_channels,
        "users": cmd_users,
        "history": cmd_history,
        "replies": cmd_replies,
        "join": cmd_join,
        "create": cmd_create,
        "info": cmd_info,
    }

    if args.command in commands:
        commands[args.command](client, tokens, args)
    else:
        parser.print_help()


# ============================================================================
# Python API for Programmatic Access
# ============================================================================


class SlackInterface:
    """
    High-level Python API for Slack Interface.

    This class provides a convenient way to interact with Slack from Python code.
    It handles token loading, configuration, and provides simple methods for
    common operations.

    Attributes:
        tokens: SlackTokens instance with available tokens
        config: SlackConfig instance with user configuration
        client: SlackClient instance for API calls

    Example:
        from slack_interface import SlackInterface

        # Initialize (auto-loads tokens and config)
        slack = SlackInterface()

        # Check connection
        if not slack.is_connected:
            print("Please connect Slack first!")
            exit(1)

        # Send message to default channel
        slack.say("Hello from Python!")

        # Send to specific channel with custom identity
        slack.say("Hello!", channel="#general",
                  username="Nova", icon_url="https://...")

        # Upload a file
        slack.upload_file("design.png", comment="New design!")

        # Get channel history
        messages = slack.get_history(limit=10)
        for msg in messages:
            print(f"{msg.get('user')}: {msg.get('text')}")
    """

    def __init__(
        self,
        token_file: str = "/dev/shm/mcp-token",
        config_file: str = DEFAULT_CONFIG_PATH,
    ):
        """
        Initialize Slack Interface with tokens and config.

        Args:
            token_file: Path to MCP token file (default: /dev/shm/mcp-token)
            config_file: Path to config file (default: ~/.agent_settings.json)
        """
        self.tokens = get_slack_tokens(token_file)
        self.config = SlackConfig.load(config_file)
        self.client = SlackClient(self.tokens)
        self._token = self.tokens.bot_token

    @property
    def default_channel(self) -> Optional[str]:
        """Get the default channel (ID preferred, then name)."""
        return self.config.get_default_channel()

    @property
    def default_channel_name(self) -> Optional[str]:
        """Get the default channel name (e.g., "#logo-creator")."""
        return self.config.default_channel

    @property
    def is_connected(self) -> bool:
        """Check if Slack is connected (tokens available)."""
        return self._token is not None

    def say(
        self,
        message: str,
        channel: Optional[str] = None,
        thread_ts: Optional[str] = None,
        username: Optional[str] = None,
        icon_emoji: Optional[str] = None,
        icon_url: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> Dict:
        """
        Send a message to the default channel or specified channel.

        Posts under the configured agent (``default_agent`` in
        ``~/.agent_settings.json``) by default. Pass ``username`` /
        ``icon_url`` / ``icon_emoji`` to override, or ``agent`` to post as
        a different known agent.
        """
        if not self.is_connected:
            raise RuntimeError(
                "Slack not connected. Please click the 'Connect' button in the "
                "chat interface to link your Slack workspace."
            )

        target_channel = channel or self.default_channel
        if not target_channel:
            raise ValueError(
                "No channel specified and no default channel configured. "
                "Set default with: slack.set_default_channel('#channel-name')"
            )

        # Use bot token for custom username/icon support
        token = self.tokens.bot_token or self._token

        # Auto-resolve agent identity from default_agent (matches upload_file).
        agent_name = agent or self.config.default_agent
        agent_config = get_agent_avatar(agent_name) if agent_name else None
        if agent_config:
            if username is None:
                username = agent_config.get("name")
            if icon_url is None and icon_emoji is None:
                icon_url = agent_config.get("icon_url")
                if not icon_url:
                    icon_emoji = agent_config.get("icon_emoji")

        return self.client.send_message(
            token,
            target_channel,
            message,
            thread_ts,
            username=username,
            icon_emoji=icon_emoji,
            icon_url=icon_url,
        )

    def upload_file(
        self,
        file_path: str,
        channel: Optional[str] = None,
        title: Optional[str] = None,
        comment: Optional[str] = None,
        thread_ts: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> Dict:
        """
        Upload a file to the default channel (or specified channel) and post
        it under the configured agent's identity.

        Strategy (the "permalink + unfurl" hack):

          1. Upload the bytes to Slack via files.uploadV2 **without** sharing
             to a channel. The file is now stored in Slack and we have its
             permalink, but it is not yet visible in any channel.
          2. Fetch the file's permalink via files.info.
          3. Post a chat.postMessage as the agent (username + icon_url) that
             contains the permalink in the message text, with
             unfurl_links=true and unfurl_media=true so Slack renders an
             inline preview card on the agent's message.

        Why this shape:
          - chat.postMessage is the ONLY Slack endpoint that honours
            `username` / `icon_url` overrides. The files upload endpoints
            do not.
          - By uploading without sharing and then posting the permalink
            ourselves, the visible-to-humans message author is the
            configured agent (e.g. "Phantom") — not the default bot — and
            Slack unfurls the permalink into a preview of the file.
          - The file still lives in Slack (it appears in the workspace's
            Files section once unfurled and accessed), so no external
            hosting / pre-signed URLs are required.

        Requires the following scopes on the bot token:
          - files:write          — upload the bytes
          - files:read           — read the permalink via files.info
          - chat:write           — post the agent message
          - chat:write.customize — honour username / icon_url
          - links:read           — allow Slack to unfurl its own permalinks

        Args:
            file_path: Path to the file on disk.
            channel:   Optional channel override. Defaults to the configured
                       default channel.
            title:     Optional file title (defaults to the filename).
            comment:   Optional narrative text to include above the permalink
                       in the agent's message.
            thread_ts: Optional thread timestamp to post into.
            agent:     Optional agent identifier. Defaults to the configured
                       default_agent.

        Returns:
            Dict with:
              - ok:          bool
              - file:        file info dict from the upload (id, title, ...)
              - permalink:   Slack permalink to the uploaded file
              - message_ts:  timestamp of the agent's chat.postMessage
              - agent:       {"id": ..., "name": ...} when resolved
              - upload_error / message_error: error string on failure
        """
        if not self.is_connected:
            raise RuntimeError("Slack not connected")

        target_channel = channel or self.default_channel
        if not target_channel:
            raise ValueError("No channel specified and no default configured")

        # Resolve channel name to ID if needed
        channel_id = target_channel
        if target_channel.startswith("#"):
            channel_id = self._resolve_channel_id(target_channel)

        token = self.tokens.bot_token or self._token

        # Resolve the agent identity (used for the chat.postMessage override).
        agent_name = agent or self.config.default_agent
        agent_config = get_agent_avatar(agent_name) if agent_name else None

        file_title = title or Path(file_path).name

        result: Dict = {
            "ok": False,
            "file": None,
            "permalink": None,
            "message_ts": None,
        }
        if agent_config:
            result["agent"] = {"id": agent_name, "name": agent_config.get("name")}

        # --- Step 1: upload the bytes WITHOUT sharing to any channel ------
        # Passing channel=None tells upload_file_v2 to skip the channel_id
        # parameter on files.completeUploadExternal, so the file is stored
        # but not posted anywhere. We'll post it ourselves as the agent.
        upload_response = self.client.upload_file_v2(
            token,
            channel=None,
            file_path=file_path,
            title=file_title,
        )
        if not upload_response.get("ok"):
            result["upload_error"] = upload_response.get("error", "unknown_error")
            return result

        files_info = upload_response.get("files") or []
        file_obj = files_info[0] if files_info else upload_response.get("file")
        if not file_obj:
            result["upload_error"] = "no_file_in_upload_response"
            return result
        result["file"] = file_obj
        file_id = file_obj.get("id")

        # --- Step 2: fetch the permalink so we can post it ourselves ------
        permalink = file_obj.get("permalink")
        if not permalink and file_id:
            info = self.client.get_file_info(token, file_id)
            if info.get("ok"):
                permalink = (info.get("file") or {}).get("permalink")
        result["permalink"] = permalink

        if not permalink:
            # Without a permalink we can't give Slack anything to unfurl.
            result["message_error"] = "no_permalink_from_files_info"
            return result

        # --- Step 3: post a chat.postMessage AS THE AGENT with the permalink
        # Slack unfurls the permalink into an inline file preview, and the
        # message author is the agent (username + icon_url).
        text_parts = [f"**{file_title}**"]
        if comment:
            text_parts.append(comment)
        text_parts.append(permalink)
        message_text = "\n".join(text_parts)

        msg_response = self.client.send_message(
            token,
            channel_id,
            message_text,
            thread_ts=thread_ts,
            username=agent_config.get("name") if agent_config else None,
            icon_url=agent_config.get("icon_url") if agent_config else None,
            icon_emoji=None,
            unfurl_links=True,
            unfurl_media=True,
        )
        if msg_response.get("ok"):
            result["ok"] = True
            result["message_ts"] = msg_response.get("ts")
        else:
            result["message_error"] = msg_response.get("error", "unknown_error")

        return result

    def _resolve_channel_id(self, channel_name: str) -> str:
        """
        Resolve a channel name (e.g., '#general') to its ID.

        Args:
            channel_name: Channel name with # prefix

        Returns:
            Channel ID string, or original name if not found
        """
        if not channel_name.startswith("#"):
            return channel_name

        name = channel_name[1:]
        try:
            channels = self.list_channels()
            for ch in channels:
                if ch.get("name") == name:
                    return ch.get("id", channel_name)
        except Exception:
            pass

        return channel_name

    def set_default_channel(
        self, channel: str, config_file: str = DEFAULT_CONFIG_PATH
    ) -> None:
        """
        Set the default channel for future messages.

        Args:
            channel: Channel name (e.g., "#logo-creator") or ID (e.g., "C0AAAAMBR1R")
            config_file: Path to save config (default: ~/.agent_settings.json)
        """
        if channel.startswith("#"):
            # Try to resolve channel ID
            channels = self.list_channels()
            channel_name = channel[1:]
            for ch in channels:
                if ch.get("name") == channel_name:
                    self.config.default_channel = channel
                    self.config.default_channel_id = ch.get("id")
                    break
            else:
                self.config.default_channel = channel
                self.config.default_channel_id = None
        else:
            self.config.default_channel_id = channel

        self.config.save(config_file)

    def list_channels(
        self, types: str = "public_channel,private_channel"
    ) -> List[Dict]:
        """
        List all channels in the workspace.

        Args:
            types: Comma-separated channel types to include

        Returns:
            List of channel dicts with 'id', 'name', 'num_members', etc.
        """
        if not self.is_connected:
            raise RuntimeError("Slack not connected")
        return self.client.list_channels(self._token, types)

    def list_users(self) -> List[Dict]:
        """
        List all users in the workspace.

        Returns:
            List of user dicts with 'id', 'name', 'real_name', etc.
        """
        if not self.is_connected:
            raise RuntimeError("Slack not connected")
        return self.client.list_users(self._token)

    def get_history(self, channel: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """
        Get channel message history.

        Uses the agent-event-cache service when enabled via sandbox_metadata.json,
        otherwise falls back to the S3 channel mirror.

        Args:
            channel: Optional channel override (uses default if not specified)
            limit: Number of messages to retrieve (default: 50)

        Returns:
            List of message dicts (newest first)
        """
        if not self.is_connected:
            raise RuntimeError("Slack not connected")
        target_channel = channel or self.default_channel
        if not target_channel:
            raise ValueError("No channel specified and no default configured")
        # Resolve #channel-name to channel ID
        channel_id = self._resolve_channel_id(target_channel)

        # Try agent-event-cache service if enabled
        if is_event_cache_enabled and is_event_cache_enabled():
            try:
                workspace_id = self.config.default_team_id or ""
                client = AgentEventCacheClient()
                request = GetMessagesRequest(
                    workspace_id=workspace_id,
                    channel_id=channel_id,
                    limit=limit,
                )
                response = client.get_messages(request)
                return response.messages
            except Exception as e:
                # Fall through to S3 on any failure (network, config, validation)
                logging.debug(f"agent-event-cache unavailable, falling back to S3: {e}")

        # Fallback: S3 channel mirror (existing behavior)
        token = self.tokens.bot_token or self._token
        return self.client.get_channel_history(token, channel_id, limit)

    def get_replies(
        self, thread_ts: str, channel: Optional[str] = None, limit: int = 50
    ) -> List[Dict]:
        """
        Get replies to a thread.

        Args:
            thread_ts: Timestamp of the parent message
            channel: Optional channel override (uses default if not specified)
            limit: Number of replies to retrieve (default: 50)

        Returns:
            List of message dicts (parent first, then replies in chronological order)
        """
        if not self.is_connected:
            raise RuntimeError("Slack not connected")
        target_channel = channel or self.default_channel
        if not target_channel:
            raise ValueError("No channel specified and no default configured")
        # Resolve #channel-name to channel ID
        channel_id = self._resolve_channel_id(target_channel)
        # Use bot token for reading (has channels:history scope)
        token = self.tokens.bot_token or self._token
        return self.client.get_thread_replies(token, channel_id, thread_ts, limit)

    def react(
        self, ts: str, emoji: str = "ghost", channel: Optional[str] = None
    ) -> bool:
        """
        Add an emoji reaction to a message.

        Args:
            ts: Message timestamp (ts field from the Slack message)
            emoji: Emoji name without colons (default: "ghost")
            channel: Optional channel override (uses default if not specified)

        Returns:
            True if successful (or already reacted), False on error
        """
        if not self.is_connected:
            return False
        target_channel = channel or self.default_channel
        if not target_channel:
            return False
        channel_id = self._resolve_channel_id(target_channel)
        token = self.tokens.bot_token or self._token
        return self.client.add_reaction(token, channel_id, ts, emoji)

    def join_channel(self, channel: str) -> Dict:
        """
        Join a channel.

        Args:
            channel: Channel ID to join

        Returns:
            API response dict
        """
        if not self.is_connected:
            raise RuntimeError("Slack not connected")
        return self.client.join_channel(self._token, channel)

    def create_channel(self, name: str, is_private: bool = False) -> Dict:
        """
        Create a new channel.

        Args:
            name: Channel name (lowercase, no spaces)
            is_private: Create as private channel (default: False)

        Returns:
            API response dict with 'channel' object on success
        """
        if not self.is_connected:
            raise RuntimeError("Slack not connected")
        return self.client.create_channel(self._token, name, is_private)

    def get_scopes(self) -> List[str]:
        """
        Get available OAuth scopes for the current token.

        Returns:
            List of scope strings
        """
        if not self.is_connected:
            return []
        return self.client.get_scopes(self._token)


# Convenience function for quick messaging
def say(
    message: str,
    channel: Optional[str] = None,
    username: Optional[str] = None,
    icon_emoji: Optional[str] = None,
) -> Dict:
    """
    Quick function to send a message to the default channel.

    This is a convenience wrapper around SlackInterface for simple use cases.

    Args:
        message: Message text to send
        channel: Optional channel override
        username: Optional custom username
        icon_emoji: Optional emoji icon

    Returns:
        Slack API response dict

    Example:
        from slack_interface import say
        say("Hello from Python!")
        say("Hello!", channel="#general")
        say("Hello!", username="Nova", icon_emoji=":star:")
    """
    slack = SlackInterface()
    return slack.say(message, channel, username=username, icon_emoji=icon_emoji)


if __name__ == "__main__":
    main()
