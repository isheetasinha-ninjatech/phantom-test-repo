"""
PostHog analytics client.

Provides a cached Posthog client instance and a convenience ``capture()``
function configured from ``/dev/shm/ph_metadata.json``.

Usage:
    from ninja.utils.posthog_client import capture

    capture(
        event="my_event_name",
        properties={"key": "value"},
    )

The ``distinct_id`` is resolved automatically from
``/dev/shm/sandbox_metadata.json`` (``thread_id`` field).
``sandbox_id`` is resolved from ``/dev/shm/ph_metadata.json``
"""

import json
import os
from functools import cache
from pathlib import Path
from typing import Any, Dict, Optional

from posthog import Posthog

_SANDBOX_METADATA_FILE = Path("/dev/shm/sandbox_metadata.json")
_PH_METADATA_FILE = Path("/dev/shm/ph_metadata.json")


@cache
def _load_ph_metadata() -> Optional[Dict[str, str]]:
    """
    Load PostHog metadata from /dev/shm/ph_metadata.json.

    Expected file format::

        {
          "posthog_host": "https://us.i.posthog.com",
          "posthog_key": "<key>",
          "sandbox_id": "xxxxx"
        }

    Returns:
        Dict with ``posthog_host``, ``posthog_key``, and ``sandbox_id``,
        or None if the file is absent or malformed.
    """
    try:
        with open(_PH_METADATA_FILE, "r") as f:
            data = json.load(f)

        if data.get("posthog_key") or data.get("sandbox_id"):
            return {
                "posthog_host": data.get("posthog_host", "https://us.i.posthog.com"),
                "posthog_key": data.get("posthog_key", ""),
                "sandbox_id": data.get("sandbox_id", ""),
            }
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    return None


@cache
def _load_sandbox_metadata() -> Optional[Dict[str, str]]:
    """
    Load sandbox metadata from /dev/shm/sandbox_metadata.json.

    Returns:
        Dict with at least ``thread_id`` and ``environment``, or None if
        the file is absent or malformed.
    """
    try:
        with open(_SANDBOX_METADATA_FILE, "r") as f:
            data = json.load(f)

        thread_id = data.get("thread_id", "")
        environment = data.get("environment", "")

        if thread_id:
            return {
                "thread_id": thread_id,
                "environment": environment,
            }
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    return None


def _is_local() -> bool:
    """Return True when running in local / docker-compose dev mode."""
    return os.environ.get("LOCAL_DEVELOPMENT_MODE", "").lower() in (
        "true",
        "1",
        "yes",
    )


@cache
def get_posthog_client() -> Posthog:
    """Return a cached Posthog client.

    Reads POSTHOG_KEY and POSTHOG_HOST from /dev/shm/ph_metadata.json.
    Raises AssertionError if no POSTHOG_KEY is available.
    """
    ph_meta = _load_ph_metadata()
    key = (ph_meta or {}).get("posthog_key")
    host = (ph_meta or {}).get("posthog_host", "https://us.i.posthog.com")
    assert key, "POSTHOG_KEY is not configured in ph_metadata.json"
    return Posthog(project_api_key=key, host=host)


def capture(
    event: str,
    properties: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a PostHog event identified by the sandbox ``thread_id``.

    Silently no-ops when:
    - ``posthog_key`` is absent or empty in ``ph_metadata.json``
    - Sandbox metadata is unavailable (no ``thread_id``) in non-local mode

    Args:
        event:      Event name (e.g. ``"task_started"``).
        properties: Optional dict of metadata to attach to the event.
    """
    # Require a PostHog key from ph_metadata.json.
    ph_meta = _load_ph_metadata()
    key = (ph_meta or {}).get("posthog_key")
    if not key:
        return

    props = {**(properties or {})}

    if _is_local():
        props["sandbox_id"] = "local_dev"
        distinct_id = f"local-{os.environ.get('NINJA_USER_ID', 'unknown')}"
        print(
            f"[posthog] capture(distinct_id={distinct_id!r}, event={event!r}, properties={props})"
        )
        return

    # Resolve the distinct_id from sandbox metadata.
    metadata = _load_sandbox_metadata()
    if not metadata:
        return

    props["sandbox_id"] = (ph_meta or {}).get("sandbox_id", "")
    distinct_id = metadata["thread_id"]

    get_posthog_client().capture(
        distinct_id=distinct_id,
        event=event,
        properties=props,
    )
