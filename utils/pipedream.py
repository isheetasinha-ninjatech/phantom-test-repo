#!/usr/bin/env python3
"""
utils/pipedream.py — Pipedream Connect SDK wrapper for Ninja.

Reads credentials from ``~/.agent_settings.json["pipedream"]`` (populated
by ``slack_interface.py`` at startup) and exposes a thin, typed wrapper
around the official ``pipedream`` Python SDK (v2).

Quick reference
---------------
    from utils.pipedream import PipedreamClient

    pd = PipedreamClient()
    user_id = pd.external_user_id          # "T0A9Q27KD1T.C0B1K38ETGV"

    token    = pd.create_connect_token()   # short-lived token for frontend
    accounts = pd.list_accounts()          # connected accounts for this user
    apps     = pd.list_apps(q="github")    # searchable app catalog

CLI
---
    python -m utils.pipedream status
    python -m utils.pipedream token
    python -m utils.pipedream apps [--q QUERY] [--limit N]
    python -m utils.pipedream accounts [--app SLUG]
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS_PATH = Path.home() / ".agent_settings.json"
_PIPEDREAM_API_BASE = "https://api.pipedream.com"

# ---------------------------------------------------------------------------
# Credentials helpers
# ---------------------------------------------------------------------------


def _load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> Dict[str, Any]:
    """Return the raw agent_settings.json dict, or {} on any error."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _load_pipedream_creds(
    path: Path = DEFAULT_SETTINGS_PATH,
) -> Optional[Dict[str, str]]:
    """
    Return the ``pipedream`` block from agent_settings.json, or None if
    the block is absent or any required field is missing.
    """
    settings = _load_settings(path)
    block = settings.get("pipedream")
    if not block or not isinstance(block, dict):
        return None
    required = ("client_id", "client_secret", "project_id", "environment")
    if any(not block.get(f) for f in required):
        return None
    return block


def get_external_user_id(path: Path = DEFAULT_SETTINGS_PATH) -> Optional[str]:
    """
    Derive the Pipedream ``external_user_id`` from agent_settings.json.

    Returns ``"<team_id>.<channel_id>"`` (e.g. ``"T0A9Q27KD1T.C0B1K38ETGV"``)
    or ``None`` if either component is missing.
    """
    settings = _load_settings(path)
    team_id = settings.get("default_team_id")
    channel_id = settings.get("default_channel_id")
    if team_id and channel_id:
        return f"{team_id}.{channel_id}"
    return None


# ---------------------------------------------------------------------------
# PipedreamClient
# ---------------------------------------------------------------------------


class PipedreamClient:
    """
    Thin wrapper around the official ``pipedream`` Python SDK.

    Credentials are read from ``~/.agent_settings.json["pipedream"]`` so no
    secrets are hard-coded anywhere.  The ``external_user_id`` is derived
    automatically from ``default_team_id`` + ``default_channel_id`` in the
    same settings file.

    Parameters
    ----------
    settings_path:
        Override the agent_settings.json path (useful for testing).
    external_user_id:
        Override the derived external user ID.
    """

    def __init__(
        self,
        settings_path: Path = DEFAULT_SETTINGS_PATH,
        external_user_id: Optional[str] = None,
    ) -> None:
        creds = _load_pipedream_creds(settings_path)
        if creds is None:
            raise RuntimeError(
                "Pipedream credentials not found in ~/.agent_settings.json.\n"
                "Run ninja at least once so slack_interface.py downloads "
                "them from S3, or set the credentials manually."
            )

        self._creds = creds
        self._settings_path = settings_path

        # Build the official SDK client — it handles OAuth token refresh.
        try:
            from pipedream import Pipedream  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "The 'pipedream' package is required. "
                "Install it with: pip install pipedream"
            ) from exc

        self._pd = Pipedream(
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
            project_id=creds["project_id"],
            project_environment=creds["environment"],  # "production" | "development"
        )

        # Resolve external_user_id
        self._external_user_id: Optional[
            str
        ] = external_user_id or get_external_user_id(settings_path)

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def external_user_id(self) -> Optional[str]:
        """The Pipedream external_user_id for this sandbox channel."""
        return self._external_user_id

    @property
    def project_id(self) -> str:
        return self._creds["project_id"]

    @property
    def environment(self) -> str:
        return self._creds["environment"]

    # ------------------------------------------------------------------
    # Tokens
    # ------------------------------------------------------------------

    def create_connect_token(
        self,
        *,
        external_user_id: Optional[str] = None,
        expires_in: Optional[int] = None,
        allowed_origins: Optional[List[str]] = None,
        webhook_uri: Optional[str] = None,
        success_redirect_uri: Optional[str] = None,
        error_redirect_uri: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate a short-lived Connect token for this user.

        The token is used to authenticate client-side requests (e.g. the
        integrations dashboard) on behalf of the end user.  Defaults to a
        4-hour TTL.

        Returns
        -------
        dict with ``token`` and ``expires_at`` keys.
        """
        user_id = external_user_id or self._external_user_id
        if not user_id:
            raise ValueError(
                "external_user_id is required to create a connect token. "
                "Set default_team_id and default_channel_id in agent_settings.json."
            )

        kwargs: Dict[str, Any] = {"external_user_id": user_id}
        if expires_in is not None:
            kwargs["expires_in"] = expires_in
        if allowed_origins is not None:
            kwargs["allowed_origins"] = allowed_origins
        if webhook_uri is not None:
            kwargs["webhook_uri"] = webhook_uri
        if success_redirect_uri is not None:
            kwargs["success_redirect_uri"] = success_redirect_uri
        if error_redirect_uri is not None:
            kwargs["error_redirect_uri"] = error_redirect_uri

        response = self._pd.tokens.create(**kwargs)
        return {"token": response.token, "expires_at": response.expires_at}

    # ------------------------------------------------------------------
    # Apps catalog
    # ------------------------------------------------------------------

    def list_apps(
        self,
        *,
        q: Optional[str] = None,
        limit: int = 50,
        has_actions: Optional[bool] = None,
        has_triggers: Optional[bool] = None,
        sort_key: Optional[str] = None,
        sort_direction: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List apps from the Pipedream catalog.

        Parameters
        ----------
        q:
            Full-text search query (e.g. "github", "google sheets").
        limit:
            Maximum number of results to return (default 50).
        has_actions:
            Filter to only apps with actions.
        has_triggers:
            Filter to only apps with triggers.
        sort_key:
            One of ``"name"``, ``"name_slug"``, ``"featured_weight"``.
        sort_direction:
            ``"asc"`` or ``"desc"``.

        Returns
        -------
        List of app dicts with keys: id, name, name_slug, img_src,
        description, auth_type, categories, featured_weight.
        """
        kwargs: Dict[str, Any] = {}
        if q:
            kwargs["q"] = q
        if limit:
            kwargs["limit"] = limit
        if has_actions is not None:
            kwargs["has_actions"] = has_actions
        if has_triggers is not None:
            kwargs["has_triggers"] = has_triggers
        if sort_key is not None:
            kwargs["sort_key"] = sort_key
        if sort_direction is not None:
            kwargs["sort_direction"] = sort_direction

        pager = self._pd.apps.list(**kwargs)
        results: List[Dict[str, Any]] = []
        for app in pager:
            results.append(_app_to_dict(app))
            if len(results) >= limit:
                break
        return results

    # ------------------------------------------------------------------
    # Connected accounts
    # ------------------------------------------------------------------

    def list_accounts(
        self,
        *,
        external_user_id: Optional[str] = None,
        app: Optional[str] = None,
        limit: int = 100,
        include_credentials: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        List connected accounts for a given external user (defaults to this
        sandbox's ``external_user_id``).

        Parameters
        ----------
        external_user_id:
            Override the user.  Defaults to the sandbox channel user.
        app:
            Filter by app slug or ID (e.g. ``"slack"``, ``"github"``).
        limit:
            Maximum results.
        include_credentials:
            Include OAuth credentials in the response (sensitive — only use
            server-side).

        Returns
        -------
        List of account dicts.
        """
        user_id = external_user_id or self._external_user_id
        kwargs: Dict[str, Any] = {}
        if user_id:
            kwargs["external_user_id"] = user_id
        if app:
            kwargs["app"] = app
        if limit:
            kwargs["limit"] = limit
        if include_credentials:
            kwargs["include_credentials"] = True

        pager = self._pd.accounts.list(**kwargs)
        results: List[Dict[str, Any]] = []
        for account in pager:
            results.append(_account_to_dict(account))
            if len(results) >= limit:
                break
        return results

    def delete_account(self, account_id: str) -> bool:
        """
        Delete a connected account by ID.

        Returns True on success.
        """
        try:
            self._pd.accounts.delete(id=account_id)
            return True
        except Exception as exc:
            print(f"⚠️  delete_account({account_id}): {exc}", file=sys.stderr)
            return False

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def list_actions(
        self,
        app: str,
        *,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        List available actions for an app.

        Parameters
        ----------
        app:
            The app slug (e.g. ``"slack"``, ``"github"``).
        limit:
            Maximum results.

        Returns
        -------
        List of action dicts with keys: key, name, description, type.
        """
        import inspect  # noqa: PLC0415

        # SDK uses paginated list with `component_type` filter
        try:
            pager = self._pd.actions.list(app=app, limit=limit)
        except TypeError:
            # Older SDK signature fallback
            pager = self._pd.actions.list(app=app)

        results: List[Dict[str, Any]] = []
        for action in pager:
            results.append(
                {
                    "key": getattr(action, "key", None),
                    "name": getattr(action, "name", None),
                    "description": getattr(action, "description", None),
                    "type": getattr(action, "type", None),
                    "app_slug": app,
                }
            )
            if len(results) >= limit:
                break
        return results

    def run_action(
        self,
        action_key: str,
        *,
        external_user_id: Optional[str] = None,
        configured_props: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute a Pipedream action for this user.

        Parameters
        ----------
        action_key:
            The component key, e.g. ``"slack-send-message"``.
        external_user_id:
            Override the user.  Defaults to the sandbox channel user.
        configured_props:
            A dict of prop name → value to pass to the action.

        Returns
        -------
        The action run result as a dict.
        """
        user_id = external_user_id or self._external_user_id
        if not user_id:
            raise ValueError("external_user_id is required to run an action.")

        response = self._pd.actions.run(
            id=action_key,
            external_user_id=user_id,
            configured_props=configured_props or {},
        )
        # Normalise — SDK returns a typed object; convert to plain dict
        if hasattr(response, "__dict__"):
            return {k: v for k, v in vars(response).items() if not k.startswith("_")}
        return {"result": str(response)}


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _app_to_dict(app: Any) -> Dict[str, Any]:
    return {
        "id": getattr(app, "id", None),
        "name": getattr(app, "name", None),
        "name_slug": getattr(app, "name_slug", None),
        "img_src": getattr(app, "img_src", None),
        "description": getattr(app, "description", None),
        "auth_type": getattr(app, "auth_type", None),
        "categories": list(getattr(app, "categories", None) or []),
        "featured_weight": getattr(app, "featured_weight", None),
        "has_components": getattr(app, "has_components", None),
    }


def _account_to_dict(account: Any) -> Dict[str, Any]:
    app_obj = getattr(account, "app", None)
    app_info: Optional[Dict[str, Any]] = None
    if app_obj is not None:
        app_info = {
            "id": getattr(app_obj, "id", None),
            "name": getattr(app_obj, "name", None),
            "name_slug": getattr(app_obj, "name_slug", None),
            "img_src": getattr(app_obj, "img_src", None),
            "auth_type": getattr(app_obj, "auth_type", None),
            "description": getattr(app_obj, "description", None),
            "categories": list(getattr(app_obj, "categories", None) or []),
        }
    return {
        "id": getattr(account, "id", None),
        "name": getattr(account, "name", None),
        "external_id": getattr(account, "external_id", None),
        "healthy": getattr(account, "healthy", None),
        "dead": getattr(account, "dead", None),
        "app": app_info,
        "created_at": str(getattr(account, "created_at", "") or ""),
        "updated_at": str(getattr(account, "updated_at", "") or ""),
        "error": getattr(account, "error", None),
        "expires_at": str(getattr(account, "expires_at", "") or ""),
        "last_refreshed_at": str(getattr(account, "last_refreshed_at", "") or ""),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_status(pd: PipedreamClient) -> None:
    creds = _load_pipedream_creds(pd._settings_path)
    settings = _load_settings(pd._settings_path)
    print("✅ Pipedream credentials loaded")
    print(f"   project_id     : {creds['project_id']}")
    print(f"   environment    : {creds['environment']}")
    print(f"   external_user_id: {pd.external_user_id}")
    print()
    print("   Slack identity:")
    print(f"     team_id      : {settings.get('default_team_id', '(not set)')}")
    print(f"     channel_id   : {settings.get('default_channel_id', '(not set)')}")
    print(f"     workspace    : {settings.get('workspace', '(not set)')}")
    print(f"     channel      : {settings.get('default_channel', '(not set)')}")


def _cli_token(pd: PipedreamClient) -> None:
    result = pd.create_connect_token()
    print(json.dumps(result, indent=2, default=str))


def _cli_apps(pd: PipedreamClient, args: Any) -> None:
    apps = pd.list_apps(
        q=getattr(args, "q", None) or None,
        limit=getattr(args, "limit", None) or 20,
    )
    for app in apps:
        cats = ", ".join(app["categories"]) if app["categories"] else ""
        print(f"  {app['name_slug']:30s}  {app['name']:30s}  {cats}")
    print(f"\n  ({len(apps)} results)")


def _cli_accounts(pd: PipedreamClient, args: Any) -> None:
    accounts = pd.list_accounts(
        app=getattr(args, "app", None) or None,
    )
    if not accounts:
        user_id = pd.external_user_id or "(unknown)"
        print(f"No connected accounts found for user {user_id!r}.")
        print("Use the integrations dashboard to connect apps.")
        return
    for acct in accounts:
        app_name = (acct.get("app") or {}).get("name", "?")
        healthy = "✅" if acct.get("healthy") else "⚠️ "
        print(f"  {healthy}  {app_name:25s}  id={acct['id']}  name={acct['name']}")
    print(f"\n  ({len(accounts)} accounts)")


def main(argv: Optional[List[str]] = None) -> None:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        prog="python -m utils.pipedream",
        description="Pipedream Connect CLI for Ninja",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status", help="Show credentials and identity")
    sub.add_parser("token", help="Create a Connect token for this user")

    apps_p = sub.add_parser("apps", help="List available apps")
    apps_p.add_argument("--q", default="", help="Search query")
    apps_p.add_argument("--limit", type=int, default=20, help="Max results")

    accounts_p = sub.add_parser("accounts", help="List connected accounts")
    accounts_p.add_argument("--app", default="", help="Filter by app slug")

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        sys.exit(0)

    try:
        pd = PipedreamClient()
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    except ImportError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)

    if args.cmd == "status":
        _cli_status(pd)
    elif args.cmd == "token":
        _cli_token(pd)
    elif args.cmd == "apps":
        _cli_apps(pd, args)
    elif args.cmd == "accounts":
        _cli_accounts(pd, args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
