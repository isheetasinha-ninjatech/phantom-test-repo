"""
Agent Event Cache Client
========================

HTTP client for the agent-event-cache service that provides Slack message
history. Replaces the S3-backed _read_channel_mirror path when enabled.

Base URL:     https://agent-event-cache.public.<environment>.myninja.ai
Auth:         Bearer ANTHROPIC_AUTH_TOKEN (from /root/.claude/settings.json)
Feature flag: "use_agent_event_cache": true in /dev/shm/sandbox_metadata.json

Endpoint:
    GET /messages
        ?workspace_id=T123
        &channel_id=C123
        &limit=50
        &start_ts=...        (optional)
        &end_ts=...          (optional)
        &thread_ts=...       (optional)
        &next_token=...      (optional)

Response:
    {
        "workspace_id": str,
        "channel_id": str,
        "messages": [...],
        "total": int,
        "next_token": str | null
    }
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SANDBOX_METADATA_FILE = "/dev/shm/sandbox_metadata.json"

SETTINGS_PATHS = [
    Path("/root/.claude/settings.json"),
    Path(__file__).resolve().parent.parent / "settings.json",
]

_metadata_cache: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Config helpers (self-contained — no imports from slack_interface)
# ---------------------------------------------------------------------------


def _load_metadata() -> Dict[str, Any]:
    """Load and cache /dev/shm/sandbox_metadata.json. Returns {} on failure."""
    global _metadata_cache
    if _metadata_cache is not None:
        return _metadata_cache
    try:
        with open(SANDBOX_METADATA_FILE, "r") as f:
            _metadata_cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, IOError):
        _metadata_cache = {}
    return _metadata_cache


def is_event_cache_enabled() -> bool:
    """True if sandbox_metadata.json has "use_agent_event_cache": true."""
    return _load_metadata().get("use_agent_event_cache", False) is True


def _get_base_url() -> str:
    """
    Build service base URL from sandbox_metadata environment field.

    Returns: https://agent-event-cache.public.<env>.myninja.ai
    Raises RuntimeError if environment is unavailable.
    """
    # Allow explicit override for local development (non-public URL via VPN)
    local_mode = os.environ.get("LOCAL_DEVELOPMENT_MODE", "").lower() in (
        "true",
        "1",
        "yes",
    )
    override = os.environ.get("AGENT_EVENT_CACHE_BASE_URL", "").strip()
    if local_mode and override:
        return override

    meta = _load_metadata()
    environment = meta.get("environment", "").strip()
    if not environment:
        raise RuntimeError(
            "Cannot determine agent-event-cache URL: "
            "'environment' missing from /dev/shm/sandbox_metadata.json"
        )
    return f"https://agent-event-cache.public.{environment}.myninja.ai"


def _get_auth_token() -> str:
    """
    Read ANTHROPIC_AUTH_TOKEN from settings.json.
    Falls back to LITELLM_API_KEY env var.
    Raises RuntimeError if no token found.
    """
    # Try env var override first
    token = os.environ.get("LITELLM_API_KEY")
    if token:
        return token

    # Read from settings files
    for path in SETTINGS_PATHS:
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                token = data.get("env", {}).get("ANTHROPIC_AUTH_TOKEN", "")
                if token:
                    return token
            except (json.JSONDecodeError, IOError):
                continue

    raise RuntimeError(
        "ANTHROPIC_AUTH_TOKEN not found in settings.json or LITELLM_API_KEY env var"
    )


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------


class GetMessagesRequest(BaseModel):
    """Request parameters for GET /messages."""

    workspace_id: str = Field(description="Slack workspace/team ID (e.g. T123)")
    channel_id: str = Field(description="Slack channel or DM ID (e.g. C123)")
    start_ts: Optional[float] = Field(
        default=None, description="Lower-bound Unix epoch timestamp (inclusive)"
    )
    end_ts: Optional[float] = Field(
        default=None, description="Upper-bound Unix epoch timestamp (inclusive)"
    )
    limit: int = Field(
        default=50, ge=1, le=200, description="Max messages per page (1-200)"
    )
    next_token: Optional[str] = Field(
        default=None, description="Pagination cursor from a previous response"
    )
    thread_ts: Optional[str] = Field(
        default=None, description="Return only messages belonging to this thread"
    )

    def to_params(self) -> Dict[str, Any]:
        """Convert to query params dict, excluding None values."""
        return {k: v for k, v in self.model_dump().items() if v is not None}


class GetMessagesResponse(BaseModel):
    """Response schema for GET /messages."""

    workspace_id: str
    channel_id: str
    messages: List[Dict[str, Any]]
    total: int
    next_token: Optional[str] = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AgentEventCacheClient:
    """
    Client for the agent-event-cache GET /messages endpoint.

    Usage:
        from clients.agent_event_cache_client import (
            AgentEventCacheClient, GetMessagesRequest, is_event_cache_enabled
        )

        if is_event_cache_enabled():
            client = AgentEventCacheClient()
            request = GetMessagesRequest(
                workspace_id="T0A9Q27KD1T",
                channel_id="C0AAAAMBR1R",
                limit=50,
            )
            response = client.get_messages(request)
            messages = response.messages
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        timeout: int = 15,
    ):
        self._base_url = (base_url or _get_base_url()).rstrip("/")
        self._auth_token = auth_token or _get_auth_token()
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        """Return standard request headers."""
        return {
            "Authorization": f"Bearer {self._auth_token}",
            "Content-Type": "application/json",
        }

    def get_messages(self, request: GetMessagesRequest) -> GetMessagesResponse:
        """
        Fetch messages from the agent-event-cache service.

        Args:
            request: Validated GetMessagesRequest with query parameters

        Returns:
            GetMessagesResponse with messages and pagination info

        Raises:
            requests.HTTPError: On 4xx/5xx responses
            pydantic.ValidationError: If response doesn't match schema
            RuntimeError: On configuration errors
        """
        resp = requests.get(
            f"{self._base_url}/messages",
            params=request.to_params(),
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return GetMessagesResponse.model_validate(resp.json())
