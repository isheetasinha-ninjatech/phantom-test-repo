#!/usr/bin/env python3
"""
tools/pdx.py — Ninja × Pipedream LLM CLI wrapper
==================================================

A lightweight, JSON-first CLI that exposes a user's connected Pipedream
integrations to an LLM agent. Designed for tool-calling workflows.

Every command prints a single JSON object on stdout and exits 0 on
success or non-zero on error (with `{"ok": false, "error": "..."}`).
The schema is stable so the LLM can parse reliably.

Subcommands
-----------
    pdx status
        Show environment, project, and the current external_user_id.

    pdx list
        List *connected* apps (integrations the user has onboarded).
        This is what the LLM should call first to know what's available.

    pdx apps [--q QUERY] [--limit N]
        Browse the Pipedream catalog (all apps, connected or not).

    pdx actions <app_slug>
        Enumerate the actions available for an app (from GitHub registry).

    pdx describe <action_key>
        Show the JSON-schema-ish props for a specific action — what the
        LLM needs to supply when running it.

    pdx run <action_key> [--args JSON] [--arg k=v ...] [--via proxy|actions-api]
        Invoke an action on behalf of the onboarded user. By default
        runs through the Connect Proxy (free plan). Pass
        ``--via actions-api`` to use the paid Connect Components API.

    pdx http <app_slug> <METHOD> <url> [--json JSON] [--header K:V] [--query k=v]
        Send an authenticated upstream HTTP request via the Pipedream
        Connect Proxy. Works for any proxy-enabled app (oauth or keys)
        without needing a registered component.

    pdx connect <app_slug>
        Mint a Connect token and return the OAuth link.

    pdx tools [--apps SLUG,SLUG] [--limit N]
        Emit OpenAI-style function-calling schema for every action of
        every connected app (or a filtered subset). Feed this directly
        to `tools=[...]` in an LLM request.

Exit codes
----------
    0   success
    1   usage / bad args
    2   not configured (no pipedream block in agent_settings.json)
    3   runtime error (API failure, etc.)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

# We import lazily inside _client() so `pdx --help` works even when
# the SDK or credentials aren't installed yet.

# ───────────────────────────── constants ──────────────────────────────

_GH_API = "https://api.github.com/repos/PipedreamHQ/pipedream/contents/components"
_GH_RAW = "https://raw.githubusercontent.com/PipedreamHQ/pipedream/master/components"

# Pipedream slug → GitHub component folder (most are identity)
_APP_SLUG_TO_GH: Dict[str, str] = {
    "slack_v2": "slack_v2",
    "slack_bot": "slack_bot",
    "github": "github",
    "gitlab": "gitlab",
    "google_sheets": "google_sheets",
    "google_drive": "google_drive",
    "google_calendar": "google_calendar",
    "gmail": "gmail",
    "notion": "notion",
    "hubspot": "hubspot",
    "salesforce_rest_api": "salesforce_rest_api",
    "openai": "openai",
    "anthropic": "anthropic",
    "telegram_bot_api": "telegram_bot_api",
    "linear_app": "linear_app",
    "jira": "jira",
    "zendesk": "zendesk",
    "discord_bot": "discord_bot",
    "discord": "discord",
    "stripe": "stripe",
    "twilio": "twilio",
    "airtable_oauth": "airtable_oauth",
    "dropbox": "dropbox",
    "asana": "asana",
    "trello": "trello",
    "monday": "monday",
    "mysql": "mysql",
    "postgresql": "postgresql",
    "mongodb": "mongodb",
    "aws": "aws",
    "sendgrid": "sendgrid",
    "zoom": "zoom",
    "microsoft_teams": "microsoft_teams",
    "outlook": "outlook",
    "calendly": "calendly",
    "typeform": "typeform",
    "google_forms": "google_forms",
    "supabase": "supabase",
    "pinecone": "pinecone",
    "shopify_developer_app": "shopify_developer_app",
}


# ───────────────────────────── helpers ────────────────────────────────


def _emit(data: Dict[str, Any], *, exit_code: int = 0) -> None:
    """Print one JSON object on stdout and exit."""
    json.dump(data, sys.stdout, ensure_ascii=False, default=str, indent=None)
    sys.stdout.write("\n")
    sys.stdout.flush()
    sys.exit(exit_code)


def _fail(error: str, *, exit_code: int = 3, **extra: Any) -> None:
    """Print a standard error envelope and exit non-zero."""
    payload = {"ok": False, "error": error, **extra}
    _emit(payload, exit_code=exit_code)


def _client():
    """Lazily build a PipedreamClient; fail-fast with a useful error."""
    try:
        from utils.pipedream import PipedreamClient  # type: ignore
    except Exception as e:
        _fail(f"utils.pipedream not importable: {e}", exit_code=2)
    try:
        return PipedreamClient()
    except Exception as e:
        _fail(
            f"Pipedream not configured: {e}",
            exit_code=2,
            hint="Run Ninja's Slack onboarding to install credentials, "
            'or check ~/.agent_settings.json["pipedream"].',
        )


def _gh_get(url: str, timeout: int = 8) -> Any:
    req = urllib.request.Request(
        url, headers={"User-Agent": "pdx/1.0", "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _gh_raw(url: str, timeout: int = 5) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "pdx/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def _slug_to_title(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("-", " ").split())


# ─────────────────── .mjs parser for action metadata ──────────────────

_RE_NAME = re.compile(r'\bname:\s*["`\'](.*?)["`\']', re.DOTALL)
_RE_DESC = re.compile(r'\bdescription:\s*["`\'](.*?)["`\']', re.DOTALL)
_RE_VER = re.compile(r'\bversion:\s*["`\']([\d.]+)["`\']')
_RE_KEY = re.compile(r'\bkey:\s*["`\'](.*?)["`\']')


def _parse_action_meta(mjs: str) -> Dict[str, str]:
    n = _RE_NAME.search(mjs)
    d = _RE_DESC.search(mjs)
    v = _RE_VER.search(mjs)
    k = _RE_KEY.search(mjs)
    return {
        "key": k.group(1) if k else "",
        "name": n.group(1) if n else "",
        "description": (d.group(1) if d else "").replace("\\n", " ").strip(),
        "version": v.group(1) if v else "",
    }


def _extract_props_block(mjs: str) -> Optional[str]:
    """Return the raw text inside the top-level `props: { ... }` block."""
    i = mjs.find("props:")
    if i < 0:
        return None
    # Find the first '{' after 'props:'
    start = mjs.find("{", i)
    if start < 0:
        return None
    depth = 0
    for j in range(start, len(mjs)):
        c = mjs[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return mjs[start + 1 : j]
    return None


_PROP_TYPE_MAP = {
    "string": "string",
    "string[]": "array",
    "integer": "integer",
    "integer[]": "array",
    "boolean": "boolean",
    "object": "object",
    "any": "string",
    "app": "string",  # app references — opaque to LLM
    "$.interface.http": "string",
    "$.service.db": "object",
}


def _parse_props(props_block: str) -> Dict[str, Dict[str, Any]]:
    """
    Parse a top-level `props: { ... }` block into a flat schema.
    Only top-level props are returned; app references and propDefinitions
    are surfaced as "string" with a note.
    """
    props: Dict[str, Dict[str, Any]] = {}
    # Walk top-level entries only
    depth = 0
    i = 0
    n = len(props_block)
    entries: List[str] = []
    entry_start = 0
    while i < n:
        c = props_block[i]
        if c == "{" or c == "[" or c == "(":
            depth += 1
        elif c == "}" or c == "]" or c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            entries.append(props_block[entry_start:i])
            entry_start = i + 1
        i += 1
    tail = props_block[entry_start:].strip()
    if tail:
        entries.append(tail)

    for raw in entries:
        raw = raw.strip().rstrip(",").strip()
        if not raw:
            continue

        # Form A:  name: { ... }
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*\{(.*)\}\s*$", raw, re.DOTALL)
        if m:
            pname = m.group(1)
            body = m.group(2)

            t_match = re.search(r'\btype:\s*["`\'](.*?)["`\']', body)
            label_m = re.search(r'\blabel:\s*["`\'](.*?)["`\']', body, re.DOTALL)
            desc_m = re.search(r'\bdescription:\s*["`\'](.*?)["`\']', body, re.DOTALL)
            opt_m = re.search(r"\boptional:\s*(true|false)", body)
            default_m = re.search(
                r'\bdefault:\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\d+|true|false)',
                body,
            )
            is_ref = "propDefinition" in body

            raw_type = (
                t_match.group(1) if t_match else ("string" if is_ref else "string")
            )
            mapped = _PROP_TYPE_MAP.get(raw_type, "string")

            entry: Dict[str, Any] = {"type": mapped, "raw_type": raw_type}
            if label_m:
                entry["label"] = label_m.group(1)
            if desc_m:
                entry["description"] = desc_m.group(1).replace("\\n", " ").strip()[:300]
            if opt_m:
                entry["required"] = opt_m.group(1) == "false"
            else:
                entry[
                    "required"
                ] = not is_ref  # propDefinition refs are usually optional-ish
            if default_m:
                try:
                    entry["default"] = json.loads(default_m.group(1).replace("'", '"'))
                except Exception:
                    entry["default"] = default_m.group(1).strip("\"'")
            if is_ref:
                entry["propDefinition"] = True

            props[pname] = entry
            continue

        # Form B:  bare identifier (e.g. `github,` at top of props) → app ref
        m2 = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)$", raw)
        if m2:
            props[m2.group(1)] = {
                "type": "string",
                "raw_type": "app",
                "appReference": True,
                "required": True,
                "description": f"Connected {m2.group(1)} account (auto-filled by Pipedream).",
            }
            continue

    return props


def _action_schema_from_mjs(
    app_slug: str, action_slug: str, mjs: str
) -> Dict[str, Any]:
    meta = _parse_action_meta(mjs)
    props_block = _extract_props_block(mjs) or ""
    props = _parse_props(props_block)
    # Drop app references from the user-facing schema — Pipedream fills those.
    public_props = {k: v for k, v in props.items() if not v.get("appReference")}
    return {
        "ok": True,
        "app_slug": app_slug,
        "action_slug": action_slug,
        "key": meta["key"] or f"{app_slug}-{action_slug}",
        "name": meta["name"] or _slug_to_title(action_slug),
        "description": meta["description"],
        "version": meta["version"],
        "props": public_props,
        "app_refs": [k for k, v in props.items() if v.get("appReference")],
    }


# ───────────────────────── action enumeration ─────────────────────────

_cache: Dict[str, Any] = {}
_CACHE_TTL = 3600  # seconds


def _list_actions_for_app(app_slug: str) -> List[Dict[str, Any]]:
    ck = ("actions", app_slug)
    hit = _cache.get(ck)
    if hit and time.time() - hit["ts"] < _CACHE_TTL:
        return hit["data"]

    folder = _APP_SLUG_TO_GH.get(app_slug, app_slug)
    url = f"{_GH_API}/{folder}/actions"
    try:
        dirs = _gh_get(url)
    except Exception as e:
        raise RuntimeError(f"No actions registry found for '{app_slug}' ({e})")

    out: List[Dict[str, Any]] = []
    for d in dirs:
        if d.get("type") != "dir" or d["name"].startswith("common"):
            continue
        slug = d["name"]
        # Try to fetch the .mjs (main file is usually `<slug>.mjs`)
        try:
            mjs = _gh_raw(f"{_GH_RAW}/{folder}/actions/{slug}/{slug}.mjs")
        except Exception:
            mjs = ""
        meta = _parse_action_meta(mjs) if mjs else {}
        out.append(
            {
                "key": meta.get("key") or f"{folder}-{slug}",
                "slug": slug,
                "app_slug": app_slug,
                "name": meta.get("name") or _slug_to_title(slug),
                "description": (meta.get("description") or "")[:200],
                "version": meta.get("version", ""),
            }
        )

    _cache[ck] = {"data": out, "ts": time.time()}
    return out


def _describe_action(action_key: str) -> Dict[str, Any]:
    """
    Resolve a full action schema.  Strategy:
      key format is `<app_slug>-<action_slug>` (e.g. `github-create-issue`).
      We try progressively longer app prefixes to find a matching GH folder.
    """
    ck = ("describe", action_key)
    hit = _cache.get(ck)
    if hit and time.time() - hit["ts"] < _CACHE_TTL:
        return hit["data"]

    parts = action_key.split("-")
    # Try [1..N-1] splits — longest app-slug first so e.g. "slack_v2" wins over "slack"
    candidates: List[tuple] = []
    for i in range(len(parts) - 1, 0, -1):
        app = "-".join(parts[:i])
        act = "-".join(parts[i:])
        candidates.append((app, act))
        # Also try underscore variant
        app_u = "_".join(parts[:i])
        if app_u != app:
            candidates.append((app_u, act))

    last_err = None
    for app_slug, action_slug in candidates:
        folder = _APP_SLUG_TO_GH.get(app_slug, app_slug)
        url = f"{_GH_RAW}/{folder}/actions/{action_slug}/{action_slug}.mjs"
        try:
            mjs = _gh_raw(url)
            schema = _action_schema_from_mjs(app_slug, action_slug, mjs)
            _cache[ck] = {"data": schema, "ts": time.time()}
            return schema
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(
        f"Could not resolve action '{action_key}' "
        f"(tried {len(candidates)} app/action splits). Last error: {last_err}"
    )


# ─────────────────── OpenAI tool-schema generation ────────────────────


def _props_to_json_schema(props: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for name, p in props.items():
        entry: Dict[str, Any] = {"type": p.get("type", "string")}
        if p.get("description"):
            entry["description"] = p["description"]
        elif p.get("label"):
            entry["description"] = p["label"]
        if "default" in p:
            entry["default"] = p["default"]
        if p.get("type") == "array":
            entry["items"] = {"type": "string"}
        properties[name] = entry
        if p.get("required"):
            required.append(name)
    schema: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _action_to_openai_tool(schema: Dict[str, Any]) -> Dict[str, Any]:
    # tool name must be ^[a-zA-Z0-9_-]+$ and ≤ 64 chars
    fn_name = schema["key"].replace(".", "_")[:64]
    desc = schema.get("description") or schema.get("name") or schema["key"]
    return {
        "type": "function",
        "function": {
            "name": fn_name,
            "description": desc[:1000],
            "parameters": _props_to_json_schema(schema.get("props", {})),
        },
    }


# ────────────────────────── subcommand impls ──────────────────────────


def cmd_status(args: argparse.Namespace) -> None:
    pd = _client()
    _emit(
        {
            "ok": True,
            "project_id": pd.project_id,
            "environment": pd.environment,
            "external_user_id": pd.external_user_id,
        }
    )


def cmd_list(args: argparse.Namespace) -> None:
    """List *connected* apps for the onboarded user."""
    pd = _client()
    try:
        accounts = pd.list_accounts(limit=200)
    except Exception as e:
        _fail(f"list_accounts failed: {e}")

    # Group by app_slug so the LLM sees one row per integration
    grouped: Dict[str, Dict[str, Any]] = {}
    for a in accounts:
        slug = a.get("app_slug") or a.get("app", {}).get("name_slug") or "?"
        if slug not in grouped:
            grouped[slug] = {
                "app_slug": slug,
                "app_name": a.get("app_name") or a.get("app", {}).get("name") or slug,
                "account_ids": [],
                "healthy": True,
                "has_registry": slug in _APP_SLUG_TO_GH,
            }
        grouped[slug]["account_ids"].append(a.get("id"))
        if not a.get("healthy", True):
            grouped[slug]["healthy"] = False

    _emit({"ok": True, "count": len(grouped), "data": list(grouped.values())})


def cmd_apps(args: argparse.Namespace) -> None:
    pd = _client()
    try:
        apps = pd.list_apps(q=args.q, limit=args.limit)
    except Exception as e:
        _fail(f"list_apps failed: {e}")
    slim = [
        {
            "slug": a.get("name_slug"),
            "name": a.get("name"),
            "description": a.get("description"),
            "auth_type": a.get("auth_type"),
            "categories": a.get("categories", []),
            "has_registry": a.get("name_slug") in _APP_SLUG_TO_GH,
        }
        for a in apps
    ]
    _emit({"ok": True, "count": len(slim), "data": slim})


def cmd_actions(args: argparse.Namespace) -> None:
    try:
        actions = _list_actions_for_app(args.app_slug)
    except Exception as e:
        _fail(str(e))
    _emit(
        {"ok": True, "app_slug": args.app_slug, "count": len(actions), "data": actions}
    )


def cmd_describe(args: argparse.Namespace) -> None:
    try:
        schema = _describe_action(args.action_key)
    except Exception as e:
        _fail(str(e))
    _emit(schema)


def _proxy_client():
    """Lazily build a PipedreamProxyClient; fail-fast with a useful error."""
    try:
        from utils.pipedream_proxy import PipedreamProxyClient  # type: ignore
    except Exception as e:
        _fail(f"utils.pipedream_proxy not importable: {e}", exit_code=2)
    try:
        return PipedreamProxyClient()
    except Exception as e:
        _fail(
            f"Pipedream proxy not configured: {e}",
            exit_code=2,
            hint="Run Ninja's Slack onboarding to install credentials, "
            'or check ~/.agent_settings.json["pipedream"].',
        )


def _parse_kv_args(kv_list: Optional[List[str]], flag_name: str) -> Dict[str, str]:
    """Parse repeated `--flag k=v` into a dict (string values)."""
    out: Dict[str, str] = {}
    for kv in kv_list or []:
        if "=" not in kv:
            _fail(f"{flag_name} expects k=v, got: {kv}", exit_code=1)
        k, v = kv.split("=", 1)
        out[k] = v
    return out


def _parse_header_args(hdr_list: Optional[List[str]]) -> Dict[str, str]:
    """Parse repeated `--header K:V` (or `K=V`) into a dict."""
    out: Dict[str, str] = {}
    for h in hdr_list or []:
        if ":" in h:
            k, v = h.split(":", 1)
        elif "=" in h:
            k, v = h.split("=", 1)
        else:
            _fail(f"--header expects K:V, got: {h}", exit_code=1)
        out[k.strip()] = v.strip()
    return out


def _collect_props(args: argparse.Namespace) -> Dict[str, Any]:
    """Combine `--args <json>` and repeated `--arg k=v` flags."""
    configured: Dict[str, Any] = {}
    if getattr(args, "args_json", None):
        try:
            parsed = json.loads(args.args_json)
        except json.JSONDecodeError as e:
            _fail(f"invalid JSON in --args: {e}", exit_code=1)
        if not isinstance(parsed, dict):
            _fail("--args must be a JSON object", exit_code=1)
        configured.update(parsed)
    for kv in getattr(args, "arg", None) or []:
        if "=" not in kv:
            _fail(f"--arg expects k=v, got: {kv}", exit_code=1)
        k, v = kv.split("=", 1)
        try:
            configured[k] = json.loads(v)
        except json.JSONDecodeError:
            configured[k] = v
    return configured


def cmd_http(args: argparse.Namespace) -> None:
    """Send an authenticated upstream request via the Pipedream Connect Proxy.

    This works for any proxy-enabled app and any auth_type (oauth or keys).
    """
    px = _proxy_client()

    # Resolve account_id (either explicit or via app_slug lookup)
    account_id = getattr(args, "account_id", None)
    if not account_id:
        try:
            account_id = px.find_account_id(args.app_slug)
        except Exception as e:
            _fail(f"could not resolve account: {e}", app_slug=args.app_slug)

    # Body: --json takes precedence, else --data is sent verbatim
    json_body = None
    body = None
    if args.json_body:
        try:
            json_body = json.loads(args.json_body)
        except json.JSONDecodeError as e:
            _fail(f"invalid JSON in --json: {e}", exit_code=1)
    elif args.data:
        body = args.data

    headers = _parse_header_args(args.header)
    query = _parse_kv_args(args.query, "--query")

    try:
        resp = px.request(
            args.method.upper(),
            args.url,
            account_id=account_id,
            json_body=json_body,
            body=body,
            headers=headers,
            query=query,
        )
    except Exception as e:
        _fail(
            f"proxy request failed: {e}",
            app_slug=args.app_slug,
            account_id=account_id,
            method=args.method,
            url=args.url,
        )

    _emit(
        {
            "ok": 200 <= resp.status < 300,
            "app_slug": args.app_slug,
            "account_id": account_id,
            "request": {
                "method": args.method.upper(),
                "url": args.url,
                "headers": headers,
                "query": query,
                "json": json_body,
            },
            "response": resp.to_envelope(),
        }
    )


def cmd_run(args: argparse.Namespace) -> None:
    """Run a Pipedream action.

    By default executes via the Connect Proxy using the curated
    action map (works on the free Connect plan). Use
    ``--via actions-api`` to fall back to the legacy ``actions.run``
    SDK call (requires the paid Connect Components API).
    """
    configured = _collect_props(args)

    if args.via == "actions-api":
        # Legacy path: paid Connect Components API
        pd = _client()
        try:
            result = pd.run_action(args.action_key, configured_props=configured)
        except Exception as e:
            _fail(
                f"run_action failed: {e}",
                action_key=args.action_key,
                configured_props=configured,
                via="actions-api",
            )
        _emit(
            {
                "ok": True,
                "via": "actions-api",
                "action_key": args.action_key,
                "configured_props": configured,
                "result": result,
            }
        )
        return

    # Proxy path (default)
    try:
        from utils.pdx_action_map import ActionRenderError  # type: ignore
        from utils.pdx_action_map import list_supported_actions, render_request
    except Exception as e:
        _fail(f"utils.pdx_action_map not importable: {e}", exit_code=2)

    try:
        rendered = render_request(args.action_key, configured)
    except KeyError:
        _fail(
            f"action {args.action_key!r} is not in the proxy action map",
            exit_code=1,
            action_key=args.action_key,
            hint=(
                "Use `pdx http <app_slug> <METHOD> <path-or-url>` instead, "
                "or run `pdx run --via actions-api <key>` if your project "
                "has the paid Connect Components API enabled."
            ),
            supported_actions=list_supported_actions(),
        )
    except ActionRenderError as e:
        _fail(
            str(e), exit_code=1, action_key=args.action_key, configured_props=configured
        )

    px = _proxy_client()
    try:
        account_id = px.find_account_id(rendered.app_slug)
    except Exception as e:
        _fail(
            f"could not resolve account: {e}",
            app_slug=rendered.app_slug,
            action_key=args.action_key,
        )

    try:
        resp = px.request(
            rendered.method,
            rendered.url,
            account_id=account_id,
            json_body=rendered.json_body,
            headers=rendered.headers,
            query=rendered.query,
        )
    except Exception as e:
        _fail(
            f"proxy request failed: {e}",
            action_key=args.action_key,
            app_slug=rendered.app_slug,
            account_id=account_id,
            method=rendered.method,
            url=rendered.url,
        )

    _emit(
        {
            "ok": 200 <= resp.status < 300,
            "via": "proxy",
            "action_key": args.action_key,
            "app_slug": rendered.app_slug,
            "account_id": account_id,
            "configured_props": configured,
            "request": {
                "method": rendered.method,
                "url": rendered.url,
                "headers": rendered.headers,
                "query": rendered.query,
                "json": rendered.json_body,
            },
            "response": resp.to_envelope(),
        }
    )


def cmd_connect(args: argparse.Namespace) -> None:
    pd = _client()
    try:
        tok = pd._pd.tokens.create(
            external_user_id=pd.external_user_id,
            expires_in=3600,
        )
    except Exception as e:
        _fail(f"create_token failed: {e}")

    link = getattr(tok, "connect_link_url", None)
    if link and args.app_slug:
        sep = "&" if "?" in link else "?"
        link = f"{link}{sep}app={args.app_slug}"

    _emit(
        {
            "ok": True,
            "app_slug": args.app_slug,
            "token": tok.token,
            "connect_link_url": link,
            "expires_at": str(tok.expires_at),
            "external_user_id": pd.external_user_id,
        }
    )


def cmd_tools(args: argparse.Namespace) -> None:
    """Emit OpenAI-style tool schema for every action of every connected app."""
    pd = _client()

    # Figure out which apps to include
    if args.apps:
        slugs = [s.strip() for s in args.apps.split(",") if s.strip()]
    else:
        try:
            accounts = pd.list_accounts(limit=200)
        except Exception as e:
            _fail(f"list_accounts failed: {e}")
        slugs = sorted(
            {
                a.get("app_slug") or a.get("app", {}).get("name_slug") or ""
                for a in accounts
            }
            - {""}
        )

    tools: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for slug in slugs:
        try:
            actions = _list_actions_for_app(slug)
        except Exception as e:
            errors.append({"app_slug": slug, "error": str(e)})
            continue
        for a in actions[: args.limit or 999]:
            try:
                schema = _describe_action(a["key"])
                tools.append(_action_to_openai_tool(schema))
            except Exception as e:
                errors.append({"action_key": a["key"], "error": str(e)})

    _emit(
        {
            "ok": True,
            "apps": slugs,
            "count": len(tools),
            "tools": tools,
            "errors": errors,
        }
    )


# ─────────────────────────────── main ─────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdx",
        description="Ninja × Pipedream LLM CLI wrapper (JSON-first).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show project, environment, user_id.")

    sub.add_parser("list", help="List *connected* integrations.")

    sp = sub.add_parser("apps", help="Browse the Pipedream catalog.")
    sp.add_argument("--q", "-q", help="Search query.")
    sp.add_argument("--limit", "-n", type=int, default=50)

    sp = sub.add_parser("actions", help="List actions for an app.")
    sp.add_argument("app_slug")

    sp = sub.add_parser("describe", help="Show schema for an action.")
    sp.add_argument("action_key")

    sp = sub.add_parser("run", help="Invoke an action.")
    sp.add_argument("action_key")
    sp.add_argument("--args", dest="args_json", help="JSON object of configured_props.")
    sp.add_argument("--arg", action="append", help="Individual k=v pair (repeatable).")
    sp.add_argument(
        "--via",
        choices=("proxy", "actions-api"),
        default="proxy",
        help="Execution path (default: proxy). 'actions-api' uses the paid "
        "Pipedream Connect Components API and may be unavailable.",
    )

    sp = sub.add_parser(
        "http",
        help="Send an authenticated upstream request via the Pipedream Connect Proxy.",
    )
    sp.add_argument("app_slug", help="Pipedream app slug (e.g. 'gmail').")
    sp.add_argument("method", help="HTTP method (GET, POST, PUT, PATCH, DELETE).")
    sp.add_argument("url", help="Upstream URL or relative path (e.g. '/v1/users/me').")
    sp.add_argument(
        "--json", dest="json_body", help="JSON body (string of valid JSON)."
    )
    sp.add_argument("--data", help="Raw body string (mutually exclusive with --json).")
    sp.add_argument(
        "--header",
        action="append",
        default=[],
        help="Request header in 'K:V' form (repeatable).",
    )
    sp.add_argument(
        "--query",
        action="append",
        default=[],
        help="Query string parameter in 'k=v' form (repeatable).",
    )
    sp.add_argument(
        "--account-id",
        dest="account_id",
        help="Override account_id (default: auto-resolve via app_slug).",
    )

    sp = sub.add_parser("connect", help="Get OAuth link to connect an app.")
    sp.add_argument("app_slug", nargs="?", default=None)

    sp = sub.add_parser(
        "tools", help="Emit OpenAI-style tool schema for all connected apps."
    )
    sp.add_argument(
        "--apps", help="Comma-separated app slugs (default: all connected)."
    )
    sp.add_argument(
        "--limit", type=int, default=0, help="Max actions per app (0 = no limit)."
    )

    return p


_DISPATCH = {
    "status": cmd_status,
    "list": cmd_list,
    "apps": cmd_apps,
    "actions": cmd_actions,
    "describe": cmd_describe,
    "run": cmd_run,
    "http": cmd_http,
    "connect": cmd_connect,
    "tools": cmd_tools,
}


def main(argv: Optional[List[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    fn = _DISPATCH.get(args.cmd)
    if not fn:
        _fail(f"unknown command: {args.cmd}", exit_code=1)
    try:
        fn(args)
    except SystemExit:
        raise
    except Exception as e:
        _fail(f"unhandled error: {e}", exit_code=3)


if __name__ == "__main__":
    main()
