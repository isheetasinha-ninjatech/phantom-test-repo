"""
utils/pipedream_proxy.py — Pipedream Connect Proxy client (server-side).
========================================================================

A minimal, dependency-free wrapper around Pipedream's Connect Proxy API.

The Connect Proxy lets us send authenticated HTTP requests to any
integrated upstream API on behalf of a connected end user, **without**
needing the paid Connect Components / Actions API. Pipedream looks up
the user's connected account, automatically injects the correct auth
credential (OAuth bearer token or API key) and forwards the request to
the upstream API. This works for both ``auth_type=oauth`` and
``auth_type=keys`` apps as long as ``connect.proxy_enabled`` is true on
the app metadata.

Docs: https://pipedream.com/docs/connect/api-proxy

This module deliberately uses only ``urllib`` (stdlib) so it can be
imported in environments without the Pipedream SDK and so it is trivial
to unit-test with ``unittest.mock``.

Security
--------
* Pipedream OAuth client credentials are read from
  ``~/.agent_settings.json["pipedream"]``.
* End-user credentials are **never** returned to the caller. We only
  forward authenticated requests through the proxy.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS_PATH = Path.home() / ".agent_settings.json"

PIPEDREAM_API_BASE = "https://api.pipedream.com/v1"
PIPEDREAM_OAUTH_TOKEN_URL = f"{PIPEDREAM_API_BASE}/oauth/token"

# Pipedream rejects requests with these headers (cannot be forwarded).
# Use the ``x-pd-proxy-`` prefix to forward custom headers to upstream.
RESTRICTED_HEADERS = frozenset(
    h.lower()
    for h in (
        "Accept-Encoding",
        "Access-Control-Request-Headers",
        "Access-Control-Request-Method",
        "Connection",
        "Content-Length",
        "Cookie",
        "Date",
        "DNT",
        "Expect",
        "Host",
        "Keep-Alive",
        "Origin",
        "Permissions-Policy",
        "Referer",
        "TE",
        "Trailer",
        "Transfer-Encoding",
        "Upgrade",
        "Via",
    )
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PipedreamProxyError(RuntimeError):
    """Raised when the proxy itself fails (auth, config, transport)."""


class PipedreamProxyHTTPError(RuntimeError):
    """Raised when the upstream API returned a non-2xx response.

    The wrapped response is still considered useful (callers may
    deliberately inspect 4xx bodies), so we expose it via ``status``,
    ``headers`` and ``body``.
    """

    def __init__(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
        super().__init__(f"upstream returned HTTP {status}")
        self.status = status
        self.headers = dict(headers)
        self.body = body


# ---------------------------------------------------------------------------
# Settings loading
# ---------------------------------------------------------------------------


def _load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _load_pipedream_creds(path: Path = DEFAULT_SETTINGS_PATH) -> Dict[str, str]:
    settings = _load_settings(path)
    pd = (settings.get("pipedream") or {}) if isinstance(settings, dict) else {}
    required = ("client_id", "client_secret", "project_id")
    missing = [k for k in required if not pd.get(k)]
    if missing:
        raise PipedreamProxyError(f"missing pipedream credentials in {path}: {missing}")
    return {
        "client_id": pd["client_id"],
        "client_secret": pd["client_secret"],
        "project_id": pd["project_id"],
        "environment": pd.get("environment", "production"),
    }


def get_external_user_id(path: Path = DEFAULT_SETTINGS_PATH) -> Optional[str]:
    """Return the canonical external_user_id (`<team_id>.<channel_id>`)."""
    settings = _load_settings(path)
    if not isinstance(settings, dict):
        return None
    team = settings.get("default_team_id")
    chan = settings.get("default_channel_id")
    if not team or not chan:
        return None
    return f"{team}.{chan}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def encode_proxy_url(upstream_url: str) -> str:
    """Pipedream Proxy expects the upstream URL as URL-safe Base64 (no padding)."""
    raw = upstream_url.encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


#: Headers we never auto-prefix because they are consumed by the proxy itself
#: (or otherwise cannot be safely renamed). ``Content-Type`` is used by the
#: proxy to frame the request body and Pipedream forwards it to upstream.
_PROXY_PASSTHROUGH_HEADERS = frozenset({"content-type"})

#: Pipedream's documented prefix for upstream-bound headers.
PROXY_HEADER_PREFIX = "x-pd-proxy-"


def _validate_headers(headers: Mapping[str, str]) -> None:
    bad = [h for h in headers if h.lower() in RESTRICTED_HEADERS]
    if bad:
        raise PipedreamProxyError(
            f"these headers are not allowed by the Connect Proxy: {bad}. "
            "Use the 'x-pd-proxy-' prefix to forward custom headers."
        )
    bad_prefixes = [h for h in headers if h.lower().startswith(("proxy-", "sec-"))]
    if bad_prefixes:
        raise PipedreamProxyError(
            f"headers starting with 'Proxy-' or 'Sec-' are not allowed: {bad_prefixes}"
        )


def _prefix_upstream_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    """Auto-prefix caller headers with ``x-pd-proxy-`` so Pipedream forwards them.

    Per Pipedream's Connect Proxy docs, only headers carrying the
    ``x-pd-proxy-`` prefix are forwarded to the upstream API. Anything else
    is interpreted as a header for the proxy itself (or silently dropped).

    This helper preserves the caller's intent by:

    * Keeping headers already prefixed with ``x-pd-proxy-`` (case-insensitive).
    * Keeping a small allowlist of headers used by the proxy for body framing
      (currently only ``Content-Type``), which Pipedream forwards as-is.
    * Rewriting every other header ``Foo: bar`` to ``x-pd-proxy-Foo: bar``.
    """
    out: Dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk.startswith(PROXY_HEADER_PREFIX) or lk in _PROXY_PASSTHROUGH_HEADERS:
            out[k] = v
        else:
            out[f"{PROXY_HEADER_PREFIX}{k}"] = v
    return out


def _http_request(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Mapping[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = 30.0,
) -> Tuple[int, Dict[str, str], bytes]:
    """Tiny HTTP client returning (status, headers, body) without raising on 4xx/5xx."""
    req = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers or {}),
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (
                resp.status,
                {k: v for k, v in resp.getheaders()},
                resp.read(),
            )
    except urllib.error.HTTPError as e:
        return (
            e.code,
            {k: v for k, v in (e.headers.items() if e.headers else [])},
            e.read() if hasattr(e, "read") else b"",
        )


# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------


@dataclass
class _TokenCache:
    token: Optional[str] = None
    expires_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class ProxyResponse:
    """Normalised proxy response returned to callers and the CLI."""

    status: int
    headers: Dict[str, str]
    body: bytes

    def text(self, encoding: str = "utf-8", errors: str = "replace") -> str:
        return self.body.decode(encoding, errors=errors)

    def json(self) -> Any:
        return json.loads(self.body or b"null")

    def to_envelope(self) -> Dict[str, Any]:
        """Best-effort JSON-friendly envelope for CLI output."""
        body_text = self.text()
        try:
            body_value: Any = json.loads(body_text) if body_text else None
        except json.JSONDecodeError:
            body_value = body_text
        return {
            "status": self.status,
            "headers": self.headers,
            "body": body_value,
        }


class PipedreamProxyClient:
    """Server-side wrapper for Pipedream's Connect Proxy.

    Parameters
    ----------
    settings_path
        Path to ``~/.agent_settings.json``. Override for tests.
    creds
        Pre-loaded Pipedream credentials dict. If supplied this skips the
        settings file. Used by tests.
    external_user_id
        Override the default external user. Defaults to ``<team>.<channel>``
        derived from the settings file.
    request_fn
        Injection point for HTTP transport. Defaults to ``_http_request``.
        Tests can substitute a recorder.
    now_fn
        Injection point for time. Defaults to ``time.time``. Tests use this
        to assert token caching behaviour.
    """

    def __init__(
        self,
        *,
        settings_path: Path = DEFAULT_SETTINGS_PATH,
        creds: Optional[Mapping[str, str]] = None,
        external_user_id: Optional[str] = None,
        request_fn: Optional[Any] = None,
        now_fn: Optional[Any] = None,
    ) -> None:
        if creds is None:
            creds = _load_pipedream_creds(settings_path)
        self._client_id = creds["client_id"]
        self._client_secret = creds["client_secret"]
        self._project_id = creds["project_id"]
        self._environment = creds.get("environment", "production")
        self._settings_path = settings_path
        self._external_user_id = external_user_id or get_external_user_id(settings_path)
        self._request = request_fn or _http_request
        self._now = now_fn or time.time
        self._token_cache = _TokenCache()

    # ---- public properties ------------------------------------------------

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def environment(self) -> str:
        return self._environment

    @property
    def external_user_id(self) -> Optional[str]:
        return self._external_user_id

    # ---- token ------------------------------------------------------------

    def get_oauth_token(self) -> str:
        """Return a cached OAuth bearer token, refreshing 60s before expiry."""
        with self._token_cache.lock:
            if (
                self._token_cache.token
                and self._token_cache.expires_at - self._now() > 60
            ):
                return self._token_cache.token

            body = json.dumps(
                {
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                }
            ).encode("utf-8")
            status, _, raw = self._request(
                PIPEDREAM_OAUTH_TOKEN_URL,
                method="POST",
                headers={"Content-Type": "application/json"},
                body=body,
            )
            if not 200 <= status < 300:
                raise PipedreamProxyError(
                    f"oauth token request failed: HTTP {status}: {raw[:200]!r}"
                )
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise PipedreamProxyError(f"oauth token response not JSON: {e}")
            tok = data.get("access_token")
            ttl = float(data.get("expires_in") or 3600)
            if not tok:
                raise PipedreamProxyError(
                    f"oauth token response missing access_token: {data}"
                )
            self._token_cache.token = tok
            self._token_cache.expires_at = self._now() + ttl
            return tok

    # ---- accounts ---------------------------------------------------------

    def list_accounts(
        self,
        *,
        external_user_id: Optional[str] = None,
        app_slug: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List connected accounts (optionally filtered)."""
        token = self.get_oauth_token()
        params: List[Tuple[str, str]] = [("limit", "100")]
        if external_user_id or self._external_user_id:
            params.append(
                ("external_user_id", external_user_id or self._external_user_id or "")
            )
        if app_slug:
            params.append(("app", app_slug))
        url = (
            f"{PIPEDREAM_API_BASE}/connect/{self._project_id}/accounts"
            f"?{urllib.parse.urlencode(params)}"
        )
        status, _, raw = self._request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {token}",
                "x-pd-environment": self._environment,
            },
        )
        if not 200 <= status < 300:
            raise PipedreamProxyError(
                f"list_accounts failed: HTTP {status}: {raw[:200]!r}"
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PipedreamProxyError(f"list_accounts response not JSON: {e}")
        return list(data.get("data") or [])

    def find_account_id(
        self,
        app_slug: str,
        *,
        external_user_id: Optional[str] = None,
        require_healthy: bool = True,
    ) -> str:
        """Resolve an ``account_id`` for the given app and user.

        Raises if no matching account is found.
        """
        accounts = self.list_accounts(
            external_user_id=external_user_id,
            app_slug=app_slug,
        )

        def _slug(a: Dict[str, Any]) -> Optional[str]:
            return (
                (a.get("app") or {}).get("name_slug")
                or a.get("app_name_slug")
                or a.get("name_slug")
            )

        matches = [a for a in accounts if _slug(a) == app_slug]
        if require_healthy:
            healthy = [
                a for a in matches if a.get("healthy", True) and not a.get("dead")
            ]
            if healthy:
                matches = healthy
        if not matches:
            raise PipedreamProxyError(
                f"no connected account for app_slug={app_slug!r} "
                f"and external_user_id={external_user_id or self._external_user_id!r}. "
                f"Connect via the integrations dashboard first."
            )
        # Prefer most-recently updated
        matches.sort(key=lambda a: a.get("updated_at") or "", reverse=True)
        return str(matches[0]["id"])

    # ---- proxy ------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        account_id: str,
        external_user_id: Optional[str] = None,
        body: Union[bytes, str, Dict[str, Any], None] = None,
        json_body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        query: Optional[Mapping[str, str]] = None,
        timeout: float = 30.0,
    ) -> ProxyResponse:
        """Send an authenticated request to ``url`` via the Connect Proxy."""
        if not account_id:
            raise PipedreamProxyError("account_id is required")
        eu = external_user_id or self._external_user_id
        if not eu:
            raise PipedreamProxyError("external_user_id is required")

        # Normalise body
        send_headers: Dict[str, str] = dict(headers or {})
        if json_body is not None and body is not None:
            raise PipedreamProxyError("pass either body= or json_body=, not both")
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            send_headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
            send_headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        # bytes pass through

        # Append query string to upstream URL (proxy preserves it)
        if query:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(list(query.items()))}"

        _validate_headers(send_headers)

        # Auto-prefix upstream-bound headers so Pipedream forwards them.
        # The caller writes "Notion-Version: ..."; the proxy needs
        # "x-pd-proxy-Notion-Version: ..." to forward it upstream.
        forwarded_headers = _prefix_upstream_headers(send_headers)

        token = self.get_oauth_token()
        encoded = encode_proxy_url(url)
        proxy_url = (
            f"{PIPEDREAM_API_BASE}/connect/{self._project_id}/proxy/{encoded}"
            f"?external_user_id={urllib.parse.quote(eu, safe='')}"
            f"&account_id={urllib.parse.quote(account_id, safe='')}"
        )
        proxy_headers = {
            "Authorization": f"Bearer {token}",
            "x-pd-environment": self._environment,
        }
        proxy_headers.update(forwarded_headers)

        status, resp_headers, resp_body = self._request(
            proxy_url,
            method=method,
            headers=proxy_headers,
            body=body,
            timeout=timeout,
        )
        return ProxyResponse(status=status, headers=resp_headers, body=resp_body)

    # Convenience verbs -----------------------------------------------------

    def get(self, url: str, **kw: Any) -> ProxyResponse:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> ProxyResponse:
        return self.request("POST", url, **kw)

    def put(self, url: str, **kw: Any) -> ProxyResponse:
        return self.request("PUT", url, **kw)

    def patch(self, url: str, **kw: Any) -> ProxyResponse:
        return self.request("PATCH", url, **kw)

    def delete(self, url: str, **kw: Any) -> ProxyResponse:
        return self.request("DELETE", url, **kw)


__all__ = [
    "PipedreamProxyClient",
    "PipedreamProxyError",
    "PipedreamProxyHTTPError",
    "ProxyResponse",
    "encode_proxy_url",
    "get_external_user_id",
    "RESTRICTED_HEADERS",
]
