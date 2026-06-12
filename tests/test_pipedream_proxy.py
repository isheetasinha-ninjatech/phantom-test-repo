"""
Unit tests for utils.pipedream_proxy.

Run:
    PYTHONPATH=src/ninja python3 -m unittest \
        src.ninja.tests.test_pipedream_proxy

Or from the project root:
    cd src/ninja && PYTHONPATH=. python3 -m unittest tests.test_pipedream_proxy -v
"""

from __future__ import annotations

import base64
import json
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from utils.pipedream_proxy import PIPEDREAM_API_BASE  # type: ignore
from utils.pipedream_proxy import (
    PIPEDREAM_OAUTH_TOKEN_URL,
    RESTRICTED_HEADERS,
    PipedreamProxyClient,
    PipedreamProxyError,
    encode_proxy_url,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


CREDS = {
    "client_id": "cid",
    "client_secret": "csec",
    "project_id": "proj_test",
    "environment": "production",
}


def make_recorder(responses):
    """Build a request_fn that pops a queued (status, headers, body) per call."""
    calls: List[Dict[str, Any]] = []
    queue = list(responses)

    def request_fn(url, *, method="GET", headers=None, body=None, timeout=30.0):
        calls.append(
            {
                "url": url,
                "method": method,
                "headers": dict(headers or {}),
                "body": body,
                "timeout": timeout,
            }
        )
        if not queue:
            raise AssertionError(f"unexpected call: {method} {url}")
        return queue.pop(0)

    return request_fn, calls


def make_client(responses, *, external_user_id="usr-1", now=lambda: 1_000_000.0):
    fn, calls = make_recorder(responses)
    return (
        PipedreamProxyClient(
            creds=CREDS,
            external_user_id=external_user_id,
            request_fn=fn,
            now_fn=now,
        ),
        calls,
    )


# ---------------------------------------------------------------------------
# encode_proxy_url
# ---------------------------------------------------------------------------


class EncodeProxyUrlTest(unittest.TestCase):
    def test_encodes_url_safe_no_padding(self):
        url = "https://slack.com/api/chat.postMessage"
        encoded = encode_proxy_url(url)
        # Must round-trip and have no '=' padding
        self.assertNotIn("=", encoded)
        self.assertEqual(
            base64.urlsafe_b64decode(encoded + "==").decode(),
            url,
        )

    def test_handles_query_string(self):
        url = "https://api.example.com/path?x=1&y=hello+world"
        encoded = encode_proxy_url(url)
        self.assertEqual(
            base64.urlsafe_b64decode(encoded + "==").decode(),
            url,
        )


# ---------------------------------------------------------------------------
# OAuth token caching
# ---------------------------------------------------------------------------


class OAuthTokenTest(unittest.TestCase):
    def test_token_obtained_and_cached(self):
        token_body = json.dumps({"access_token": "TOK123", "expires_in": 3600}).encode()
        c, calls = make_client(
            [
                (200, {"Content-Type": "application/json"}, token_body),
            ]
        )

        self.assertEqual(c.get_oauth_token(), "TOK123")
        # Second call must hit the cache (no new request)
        self.assertEqual(c.get_oauth_token(), "TOK123")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], PIPEDREAM_OAUTH_TOKEN_URL)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["headers"]["Content-Type"], "application/json")
        body = json.loads(calls[0]["body"].decode())
        self.assertEqual(body["grant_type"], "client_credentials")
        self.assertEqual(body["client_id"], "cid")

    def test_token_refreshes_within_60s_of_expiry(self):
        t = [1_000_000.0]
        token_a = json.dumps({"access_token": "A", "expires_in": 100}).encode()
        token_b = json.dumps({"access_token": "B", "expires_in": 100}).encode()
        fn, calls = make_recorder(
            [
                (200, {}, token_a),
                (200, {}, token_b),
            ]
        )
        c = PipedreamProxyClient(
            creds=CREDS,
            external_user_id="u",
            request_fn=fn,
            now_fn=lambda: t[0],
        )
        self.assertEqual(c.get_oauth_token(), "A")
        # Advance to 50s before original expiry → refresh required
        t[0] += 50  # elapsed=50, ttl_left=50 < 60 threshold
        self.assertEqual(c.get_oauth_token(), "B")
        self.assertEqual(len(calls), 2)

    def test_token_failure_raises_proxy_error(self):
        c, _ = make_client(
            [
                (401, {}, b'{"error":"unauthorized"}'),
            ]
        )
        with self.assertRaises(PipedreamProxyError):
            c.get_oauth_token()


# ---------------------------------------------------------------------------
# list_accounts / find_account_id
# ---------------------------------------------------------------------------


class AccountResolverTest(unittest.TestCase):
    def _seed_token(self):
        return (
            200,
            {},
            json.dumps({"access_token": "TOK", "expires_in": 3600}).encode(),
        )

    def test_find_account_id_picks_healthy_match(self):
        accounts = {
            "data": [
                {
                    "id": "apn_1",
                    "app": {"name_slug": "github"},
                    "healthy": True,
                    "updated_at": "2024-01-01",
                },
                {
                    "id": "apn_2",
                    "app": {"name_slug": "github"},
                    "healthy": False,
                    "updated_at": "2025-01-01",
                },
                {
                    "id": "apn_3",
                    "app": {"name_slug": "github"},
                    "healthy": True,
                    "updated_at": "2025-06-01",
                },
            ]
        }
        c, calls = make_client(
            [
                self._seed_token(),
                (200, {}, json.dumps(accounts).encode()),
            ]
        )
        self.assertEqual(c.find_account_id("github"), "apn_3")

        # Inspect URL of accounts call
        accounts_call = calls[1]
        self.assertIn("/connect/proj_test/accounts", accounts_call["url"])
        self.assertIn("external_user_id=usr-1", accounts_call["url"])
        self.assertIn("app=github", accounts_call["url"])
        self.assertEqual(accounts_call["headers"]["Authorization"], "Bearer TOK")
        self.assertEqual(accounts_call["headers"]["x-pd-environment"], "production")

    def test_find_account_id_no_match_raises(self):
        c, _ = make_client(
            [
                self._seed_token(),
                (200, {}, json.dumps({"data": []}).encode()),
            ]
        )
        with self.assertRaisesRegex(PipedreamProxyError, "no connected account"):
            c.find_account_id("github")


# ---------------------------------------------------------------------------
# request() — proxy URL/headers/body construction
# ---------------------------------------------------------------------------


class RequestConstructionTest(unittest.TestCase):
    def _seed_token(self):
        return (
            200,
            {},
            json.dumps({"access_token": "TOK", "expires_in": 3600}).encode(),
        )

    def test_get_no_body_builds_correct_proxy_url(self):
        c, calls = make_client(
            [
                self._seed_token(),
                (200, {"Content-Type": "application/json"}, b'{"hello":"world"}'),
            ]
        )
        upstream = "https://api.notion.com/v1/users/me"
        resp = c.get(
            upstream, account_id="apn_42", headers={"Notion-Version": "2022-06-28"}
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), {"hello": "world"})

        proxy_call = calls[1]
        self.assertEqual(proxy_call["method"], "GET")
        # Proxy URL must contain /v1/connect/{project}/proxy/{base64}
        b64 = encode_proxy_url(upstream)
        self.assertIn(
            f"{PIPEDREAM_API_BASE}/connect/proj_test/proxy/{b64}",
            proxy_call["url"],
        )
        # Auth, env, and forwarded header all present.
        # Caller-supplied upstream headers are auto-prefixed with
        # ``x-pd-proxy-`` per Pipedream's Connect Proxy contract.
        self.assertEqual(proxy_call["headers"]["Authorization"], "Bearer TOK")
        self.assertEqual(proxy_call["headers"]["x-pd-environment"], "production")
        self.assertEqual(
            proxy_call["headers"]["x-pd-proxy-Notion-Version"], "2022-06-28"
        )
        self.assertNotIn("Notion-Version", proxy_call["headers"])
        # external_user_id and account_id appear as query params on the proxy URL
        self.assertIn("external_user_id=usr-1", proxy_call["url"])
        self.assertIn("account_id=apn_42", proxy_call["url"])

    def test_post_json_body_sets_content_type(self):
        c, calls = make_client(
            [
                self._seed_token(),
                (200, {}, b'{"id":"abc"}'),
            ]
        )
        c.post(
            "https://api.example.com/v1/things", account_id="apn_1", json_body={"x": 1}
        )
        post_call = calls[1]
        self.assertEqual(post_call["method"], "POST")
        self.assertEqual(post_call["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(post_call["body"].decode()), {"x": 1})

    def test_query_appended_to_upstream_url(self):
        c, calls = make_client(
            [
                self._seed_token(),
                (200, {}, b"[]"),
            ]
        )
        c.get(
            "https://api.example.com/list",
            account_id="apn_x",
            query={"q": "hi", "limit": "5"},
        )
        # Decode the base64 part of the proxy URL and confirm the upstream
        # URL carried the query string.
        proxy_url = calls[1]["url"]
        b64 = proxy_url.split("/proxy/", 1)[1].split("?", 1)[0]
        upstream = base64.urlsafe_b64decode(b64 + "==").decode()
        self.assertIn("q=hi", upstream)
        self.assertIn("limit=5", upstream)

    def test_restricted_header_rejected(self):
        c, _ = make_client([self._seed_token()])
        with self.assertRaises(PipedreamProxyError):
            c.get(
                "https://api.example.com/x",
                account_id="apn_1",
                headers={"Cookie": "evil=1"},
            )

    def test_proxy_prefix_header_rejected(self):
        c, _ = make_client([self._seed_token()])
        with self.assertRaises(PipedreamProxyError):
            c.get(
                "https://api.example.com/x",
                account_id="apn_1",
                headers={"Sec-Fetch-Mode": "cors"},
            )

    def test_body_and_json_both_raises(self):
        c, _ = make_client([self._seed_token()])
        with self.assertRaises(PipedreamProxyError):
            c.post(
                "https://api.example.com/x",
                account_id="apn_1",
                body="raw",
                json_body={"a": 1},
            )

    def test_missing_account_id_raises(self):
        c, _ = make_client([self._seed_token()])
        with self.assertRaises(PipedreamProxyError):
            c.get("https://api.example.com/x", account_id="")

    def test_response_to_envelope_decodes_json(self):
        c, _ = make_client(
            [
                self._seed_token(),
                (200, {"x-foo": "bar"}, b'{"hello":1}'),
            ]
        )
        resp = c.get("https://api.example.com/x", account_id="apn_1")
        env = resp.to_envelope()
        self.assertEqual(env["status"], 200)
        self.assertEqual(env["body"], {"hello": 1})

    def test_response_to_envelope_keeps_text_when_not_json(self):
        c, _ = make_client(
            [
                self._seed_token(),
                (200, {}, b"plain text response"),
            ]
        )
        resp = c.get("https://api.example.com/x", account_id="apn_1")
        env = resp.to_envelope()
        self.assertEqual(env["body"], "plain text response")

    def test_4xx_does_not_raise_returns_envelope(self):
        c, _ = make_client(
            [
                self._seed_token(),
                (422, {}, b'{"error":"bad"}'),
            ]
        )
        resp = c.get("https://api.example.com/x", account_id="apn_1")
        self.assertEqual(resp.status, 422)
        self.assertEqual(resp.json(), {"error": "bad"})


# ---------------------------------------------------------------------------
# Restricted headers list sanity
# ---------------------------------------------------------------------------


class RestrictedHeadersTest(unittest.TestCase):
    def test_well_known_restricted(self):
        for h in ("Cookie", "Host", "Connection", "Content-Length"):
            self.assertIn(h.lower(), RESTRICTED_HEADERS)

    def test_x_pd_proxy_prefix_allowed(self):
        # We don't restrict by prefix here; validation explicitly forbids
        # 'Proxy-' and 'Sec-' prefixes. 'x-pd-proxy-' must be permitted.
        self.assertNotIn("x-pd-proxy-foo", RESTRICTED_HEADERS)


# ---------------------------------------------------------------------------
# Header auto-prefixing (the proxy only forwards x-pd-proxy-* upstream)
# ---------------------------------------------------------------------------


class HeaderPrefixingTest(unittest.TestCase):
    """Pipedream's Connect Proxy only forwards headers prefixed with
    ``x-pd-proxy-``. The client must auto-prefix caller-supplied headers
    so the documented ``--header K:V`` UX still results in upstream
    receiving the header.
    """

    def _seed_token(self):
        return (
            200,
            {},
            json.dumps({"access_token": "TOK", "expires_in": 3600}).encode(),
        )

    def test_arbitrary_header_is_auto_prefixed(self):
        c, calls = make_client(
            [
                self._seed_token(),
                (200, {}, b"{}"),
            ]
        )
        c.get(
            "https://api.example.com/x",
            account_id="apn_1",
            headers={"X-Custom-Thing": "abc"},
        )
        proxy_call = calls[1]
        self.assertEqual(proxy_call["headers"]["x-pd-proxy-X-Custom-Thing"], "abc")
        self.assertNotIn("X-Custom-Thing", proxy_call["headers"])

    def test_already_prefixed_header_is_left_untouched(self):
        c, calls = make_client(
            [
                self._seed_token(),
                (200, {}, b"{}"),
            ]
        )
        c.get(
            "https://api.example.com/x",
            account_id="apn_1",
            headers={"x-pd-proxy-Already-Prefixed": "v"},
        )
        proxy_call = calls[1]
        self.assertEqual(proxy_call["headers"]["x-pd-proxy-Already-Prefixed"], "v")
        # Must not double-prefix.
        self.assertNotIn(
            "x-pd-proxy-x-pd-proxy-Already-Prefixed", proxy_call["headers"]
        )

    def test_already_prefixed_header_case_insensitive(self):
        c, calls = make_client(
            [
                self._seed_token(),
                (200, {}, b"{}"),
            ]
        )
        c.get(
            "https://api.example.com/x",
            account_id="apn_1",
            headers={"X-PD-PROXY-Foo": "v"},
        )
        proxy_call = calls[1]
        # Original casing preserved, no double-prefix
        self.assertEqual(proxy_call["headers"]["X-PD-PROXY-Foo"], "v")
        self.assertNotIn("x-pd-proxy-X-PD-PROXY-Foo", proxy_call["headers"])

    def test_content_type_is_not_prefixed(self):
        # Content-Type is consumed by the proxy for body framing and is
        # forwarded upstream by Pipedream as-is.
        c, calls = make_client(
            [
                self._seed_token(),
                (200, {}, b"{}"),
            ]
        )
        c.post("https://api.example.com/x", account_id="apn_1", json_body={"a": 1})
        proxy_call = calls[1]
        self.assertEqual(proxy_call["headers"]["Content-Type"], "application/json")
        self.assertNotIn("x-pd-proxy-Content-Type", proxy_call["headers"])

    def test_explicit_content_type_override_not_prefixed(self):
        c, calls = make_client(
            [
                self._seed_token(),
                (200, {}, b"{}"),
            ]
        )
        c.post(
            "https://api.example.com/x",
            account_id="apn_1",
            body=b"raw=1",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        proxy_call = calls[1]
        self.assertEqual(
            proxy_call["headers"]["Content-Type"],
            "application/x-www-form-urlencoded",
        )
        self.assertNotIn("x-pd-proxy-Content-Type", proxy_call["headers"])

    def test_proxy_internal_headers_not_overwritten_by_caller(self):
        # Pipedream auth / environment headers must always be set by the
        # client, regardless of what the caller passes.
        c, calls = make_client(
            [
                self._seed_token(),
                (200, {}, b"{}"),
            ]
        )
        c.get(
            "https://api.example.com/x",
            account_id="apn_1",
            headers={"Authorization": "Bearer USER"},
        )
        proxy_call = calls[1]
        # Caller's "Authorization" gets prefixed (it's an upstream header from
        # their POV); proxy's own Authorization to Pipedream stays intact.
        self.assertEqual(proxy_call["headers"]["Authorization"], "Bearer TOK")
        self.assertEqual(
            proxy_call["headers"]["x-pd-proxy-Authorization"], "Bearer USER"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
