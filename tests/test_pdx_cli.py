"""Unit tests for tools.pdx CLI.

These tests exercise the argparse layout and the two new command handlers
(``cmd_http`` and the proxy-default ``cmd_run``) without touching the
network. ``_proxy_client`` is patched to return a fake whose
``find_account_id`` and ``request`` methods are fully controlled by the
test, and the legacy ``actions-api`` path is exercised through a patched
``_client``.

Every CLI handler ends in ``_emit`` which calls ``sys.exit``; the helpers
below capture that ``SystemExit``, return the parsed JSON envelope, and
assert on the exit code.
"""

from __future__ import annotations

import io
import json

# Make ``src/ninja`` importable as a package root: tests/.. == src/ninja
import os
import sys
import unittest
from contextlib import redirect_stdout
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_NINJA_ROOT = os.path.dirname(_HERE)
if _NINJA_ROOT not in sys.path:
    sys.path.insert(0, _NINJA_ROOT)

from tools import pdx  # noqa: E402  (after sys.path tweak)
from utils.pipedream_proxy import ProxyResponse  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(argv: List[str]) -> Tuple[Dict[str, Any], int]:
    """Run pdx.main(argv), capture stdout JSON + exit code."""
    buf = io.StringIO()
    code: int = 0
    with redirect_stdout(buf):
        try:
            pdx.main(argv)
        except SystemExit as e:
            # _emit always calls sys.exit; argparse may also exit on parse error
            code = int(e.code) if e.code is not None else 0
    text = buf.getvalue().strip()
    if not text:
        return ({}, code)
    # Some failures may print multiple lines (argparse usage); take last JSON line
    last_json: Dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last_json = json.loads(line)
        except json.JSONDecodeError:
            continue
    return (last_json, code)


class _FakeProxyClient:
    """Records calls and returns a configured ProxyResponse."""

    def __init__(
        self,
        *,
        account_id: str = "apn_test123",
        response: Optional[ProxyResponse] = None,
        find_error: Optional[Exception] = None,
        request_error: Optional[Exception] = None,
    ):
        self.account_id = account_id
        self.response = response or ProxyResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=b'{"ok":true,"echoed":"yes"}',
        )
        self.find_error = find_error
        self.request_error = request_error
        self.find_calls: List[str] = []
        self.request_calls: List[Dict[str, Any]] = []

    # PipedreamProxyClient surface used by the CLI
    def find_account_id(self, app_slug: str, require_healthy: bool = True) -> str:
        self.find_calls.append(app_slug)
        if self.find_error is not None:
            raise self.find_error
        return self.account_id

    def request(
        self,
        method: str,
        url: str,
        *,
        account_id: str,
        external_user_id: Optional[str] = None,
        body: Optional[bytes] = None,
        json_body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        query: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> ProxyResponse:
        self.request_calls.append(
            {
                "method": method,
                "url": url,
                "account_id": account_id,
                "body": body,
                "json_body": json_body,
                "headers": dict(headers or {}),
                "query": dict(query or {}),
            }
        )
        if self.request_error is not None:
            raise self.request_error
        return self.response


# ---------------------------------------------------------------------------
# Argparse / wiring
# ---------------------------------------------------------------------------


class BuildParserTest(unittest.TestCase):
    """The parser must accept all new flags and dispatch to the right handler."""

    def test_dispatch_table_has_http_and_run(self) -> None:
        self.assertIn("http", pdx._DISPATCH)
        self.assertIn("run", pdx._DISPATCH)
        self.assertIs(pdx._DISPATCH["http"], pdx.cmd_http)
        self.assertIs(pdx._DISPATCH["run"], pdx.cmd_run)

    def test_run_defaults_to_proxy(self) -> None:
        ns = pdx.build_parser().parse_args(["run", "gmail-get-profile"])
        self.assertEqual(ns.cmd, "run")
        self.assertEqual(ns.action_key, "gmail-get-profile")
        self.assertEqual(ns.via, "proxy")

    def test_run_accepts_via_actions_api(self) -> None:
        ns = pdx.build_parser().parse_args(
            ["run", "gmail-get-profile", "--via", "actions-api"]
        )
        self.assertEqual(ns.via, "actions-api")

    def test_run_rejects_unknown_via(self) -> None:
        with self.assertRaises(SystemExit):
            pdx.build_parser().parse_args(
                ["run", "gmail-get-profile", "--via", "bogus"]
            )

    def test_http_parser_accepts_full_surface(self) -> None:
        ns = pdx.build_parser().parse_args(
            [
                "http",
                "gmail",
                "GET",
                "https://www.googleapis.com/gmail/v1/users/me/profile",
                "--header",
                "X-Foo: bar",
                "--header",
                "Notion-Version:2022-06-28",
                "--query",
                "alt=json",
                "--account-id",
                "apn_override",
                "--json",
                '{"a":1}',
            ]
        )
        self.assertEqual(ns.cmd, "http")
        self.assertEqual(ns.app_slug, "gmail")
        self.assertEqual(ns.method, "GET")
        self.assertEqual(ns.url, "https://www.googleapis.com/gmail/v1/users/me/profile")
        self.assertEqual(ns.header, ["X-Foo: bar", "Notion-Version:2022-06-28"])
        self.assertEqual(ns.query, ["alt=json"])
        self.assertEqual(ns.account_id, "apn_override")
        self.assertEqual(ns.json_body, '{"a":1}')

    def test_http_requires_app_method_url(self) -> None:
        with self.assertRaises(SystemExit):
            pdx.build_parser().parse_args(["http", "gmail", "GET"])  # missing url


# ---------------------------------------------------------------------------
# cmd_http
# ---------------------------------------------------------------------------


class CmdHttpTest(unittest.TestCase):
    def test_get_with_auto_account_emits_envelope(self) -> None:
        fake = _FakeProxyClient()
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            payload, code = _run_cli(
                [
                    "http",
                    "gmail",
                    "GET",
                    "https://www.googleapis.com/gmail/v1/users/me/profile",
                ]
            )

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["app_slug"], "gmail")
        self.assertEqual(payload["account_id"], "apn_test123")
        self.assertEqual(payload["request"]["method"], "GET")
        self.assertEqual(
            payload["request"]["url"],
            "https://www.googleapis.com/gmail/v1/users/me/profile",
        )
        self.assertEqual(payload["response"]["status"], 200)
        # Body decoded as JSON
        self.assertEqual(payload["response"]["body"], {"ok": True, "echoed": "yes"})

        # Account auto-resolved via find_account_id("gmail")
        self.assertEqual(fake.find_calls, ["gmail"])
        self.assertEqual(len(fake.request_calls), 1)
        call = fake.request_calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["account_id"], "apn_test123")
        self.assertIsNone(call["json_body"])

    def test_explicit_account_id_skips_resolver(self) -> None:
        fake = _FakeProxyClient()
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            payload, code = _run_cli(
                [
                    "http",
                    "notion",
                    "GET",
                    "https://api.notion.com/v1/users/me",
                    "--account-id",
                    "apn_explicit",
                    "--header",
                    "Notion-Version:2022-06-28",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(payload["account_id"], "apn_explicit")
        # find_account_id must NOT be called when --account-id is set
        self.assertEqual(fake.find_calls, [])
        # Header was forwarded
        self.assertEqual(
            payload["request"]["headers"], {"Notion-Version": "2022-06-28"}
        )
        self.assertEqual(
            fake.request_calls[0]["headers"], {"Notion-Version": "2022-06-28"}
        )

    def test_post_with_json_body(self) -> None:
        fake = _FakeProxyClient(
            response=ProxyResponse(
                status=201,
                headers={"content-type": "application/json"},
                body=b'{"id":"em_123"}',
            ),
        )
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            payload, code = _run_cli(
                [
                    "http",
                    "resend",
                    "POST",
                    "https://api.resend.com/emails",
                    "--json",
                    '{"to":["a@b.com"],"subject":"hi"}',
                ]
            )

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["response"]["status"], 201)
        self.assertEqual(payload["response"]["body"], {"id": "em_123"})

        call = fake.request_calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["json_body"], {"to": ["a@b.com"], "subject": "hi"})
        self.assertIsNone(call["body"])

    def test_query_string_forwarded(self) -> None:
        fake = _FakeProxyClient()
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            payload, code = _run_cli(
                [
                    "http",
                    "gmail",
                    "GET",
                    "https://www.googleapis.com/gmail/v1/users/me/messages",
                    "--query",
                    "maxResults=5",
                    "--query",
                    "labelIds=INBOX",
                ]
            )
        self.assertEqual(code, 0)
        call = fake.request_calls[0]
        self.assertEqual(call["query"], {"maxResults": "5", "labelIds": "INBOX"})

    def test_invalid_json_body_returns_error_envelope(self) -> None:
        fake = _FakeProxyClient()
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            payload, code = _run_cli(
                [
                    "http",
                    "gmail",
                    "POST",
                    "https://example.com/x",
                    "--json",
                    "{not json",
                ]
            )
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertIn("invalid JSON", payload["error"])
        # No upstream request must have been made
        self.assertEqual(fake.request_calls, [])

    def test_bad_header_format_fails_cleanly(self) -> None:
        fake = _FakeProxyClient()
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            payload, code = _run_cli(
                [
                    "http",
                    "gmail",
                    "GET",
                    "https://example.com/x",
                    "--header",
                    "no-separator-here",
                ]
            )
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertIn("--header", payload["error"])

    def test_account_resolution_failure_is_envelope(self) -> None:
        fake = _FakeProxyClient(find_error=RuntimeError("no accounts for 'gmail'"))
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            payload, code = _run_cli(
                [
                    "http",
                    "gmail",
                    "GET",
                    "https://example.com/x",
                ]
            )
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertIn("could not resolve account", payload["error"])
        self.assertEqual(payload["app_slug"], "gmail")

    def test_proxy_request_failure_is_envelope(self) -> None:
        fake = _FakeProxyClient(request_error=RuntimeError("upstream went boom"))
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            payload, code = _run_cli(
                [
                    "http",
                    "gmail",
                    "GET",
                    "https://example.com/x",
                ]
            )
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertIn("proxy request failed", payload["error"])
        self.assertEqual(payload["app_slug"], "gmail")
        self.assertEqual(payload["account_id"], "apn_test123")

    def test_4xx_response_is_ok_false_but_exit_zero(self) -> None:
        # Non-2xx upstream responses are still successful proxy calls;
        # the CLI emits ok=False but exits 0 (envelope, not error).
        fake = _FakeProxyClient(
            response=ProxyResponse(
                status=404,
                headers={"content-type": "application/json"},
                body=b'{"error":"not found"}',
            ),
        )
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            payload, code = _run_cli(
                [
                    "http",
                    "gmail",
                    "GET",
                    "https://example.com/missing",
                ]
            )
        self.assertEqual(code, 0)  # successful proxy call
        self.assertFalse(payload["ok"])  # but upstream said 404
        self.assertEqual(payload["response"]["status"], 404)
        self.assertEqual(payload["response"]["body"], {"error": "not found"})


# ---------------------------------------------------------------------------
# cmd_run (proxy default)
# ---------------------------------------------------------------------------


class CmdRunProxyTest(unittest.TestCase):
    def test_mapped_action_routed_through_proxy(self) -> None:
        fake = _FakeProxyClient(
            response=ProxyResponse(
                status=200,
                headers={"content-type": "application/json"},
                body=b'{"emailAddress":"me@x.com","messagesTotal":42}',
            ),
        )
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            payload, code = _run_cli(["run", "gmail-get-profile"])

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["via"], "proxy")
        self.assertEqual(payload["action_key"], "gmail-get-profile")
        self.assertEqual(payload["app_slug"], "gmail")
        self.assertEqual(payload["account_id"], "apn_test123")
        self.assertEqual(
            payload["response"]["body"],
            {"emailAddress": "me@x.com", "messagesTotal": 42},
        )

        # Verify the proxy was invoked with the curated signature
        self.assertEqual(fake.find_calls, ["gmail"])
        call = fake.request_calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(
            call["url"],
            "https://www.googleapis.com/gmail/v1/users/me/profile",
        )

    def test_path_template_interpolation_via_args(self) -> None:
        fake = _FakeProxyClient(
            response=ProxyResponse(
                status=201,
                headers={"content-type": "application/json"},
                body=b'{"number":7}',
            ),
        )
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            payload, code = _run_cli(
                [
                    "run",
                    "github-create-issue",
                    "--arg",
                    "repoFullname=NinjaTech-AI/phantom",
                    "--arg",
                    "title=Hello",
                    "--arg",
                    "body=World",
                ]
            )

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        call = fake.request_calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            call["url"],
            "https://api.github.com/repos/NinjaTech-AI/phantom/issues",
        )
        self.assertEqual(call["json_body"], {"title": "Hello", "body": "World"})

    def test_args_json_merges_with_repeated_arg(self) -> None:
        fake = _FakeProxyClient(
            response=ProxyResponse(
                status=200,
                headers={},
                body=b"{}",
            ),
        )
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            payload, code = _run_cli(
                [
                    "run",
                    "github-create-issue",
                    "--args",
                    '{"repoFullname":"NinjaTech-AI/phantom"}',
                    "--arg",
                    "title=Y",
                ]
            )
        self.assertEqual(code, 0)
        call = fake.request_calls[0]
        self.assertEqual(
            call["url"],
            "https://api.github.com/repos/NinjaTech-AI/phantom/issues",
        )
        self.assertEqual(call["json_body"], {"title": "Y"})

    def test_unmapped_action_returns_clean_error_envelope(self) -> None:
        fake = _FakeProxyClient()
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            payload, code = _run_cli(["run", "github-some-unmapped-key"])

        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action_key"], "github-some-unmapped-key")
        self.assertIn("not in the proxy action map", payload["error"])
        self.assertIn("hint", payload)
        self.assertIn("supported_actions", payload)
        self.assertIsInstance(payload["supported_actions"], list)
        self.assertGreater(len(payload["supported_actions"]), 0)
        # And no proxy traffic should have happened
        self.assertEqual(fake.find_calls, [])
        self.assertEqual(fake.request_calls, [])

    def test_missing_required_prop_returns_action_render_error(self) -> None:
        fake = _FakeProxyClient()
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            # github-create-issue requires repoFullname + title; omit title
            payload, code = _run_cli(
                [
                    "run",
                    "github-create-issue",
                    "--arg",
                    "repoFullname=NinjaTech-AI/phantom",
                    # missing title
                ]
            )
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action_key"], "github-create-issue")
        self.assertEqual(fake.request_calls, [])

    def test_invalid_args_json_fails_before_proxy(self) -> None:
        fake = _FakeProxyClient()
        with mock.patch.object(pdx, "_proxy_client", return_value=fake):
            payload, code = _run_cli(
                [
                    "run",
                    "gmail-get-profile",
                    "--args",
                    "not-json",
                ]
            )
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertIn("invalid JSON", payload["error"])
        self.assertEqual(fake.request_calls, [])


# ---------------------------------------------------------------------------
# cmd_run (legacy --via actions-api)
# ---------------------------------------------------------------------------


class CmdRunActionsApiTest(unittest.TestCase):
    def test_actions_api_path_uses_legacy_client(self) -> None:
        fake_pd = mock.MagicMock()
        fake_pd.run_action.return_value = {
            "id": "ev_123",
            "exports": {"$return_value": {"ok": True}},
        }

        fake_proxy = _FakeProxyClient()
        with mock.patch.object(pdx, "_client", return_value=fake_pd), mock.patch.object(
            pdx, "_proxy_client", return_value=fake_proxy
        ):
            payload, code = _run_cli(
                [
                    "run",
                    "gmail-get-profile",
                    "--via",
                    "actions-api",
                    "--arg",
                    "foo=bar",
                ]
            )

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["via"], "actions-api")
        self.assertEqual(payload["action_key"], "gmail-get-profile")
        self.assertEqual(payload["configured_props"], {"foo": "bar"})
        self.assertEqual(
            payload["result"],
            {
                "id": "ev_123",
                "exports": {"$return_value": {"ok": True}},
            },
        )
        # Legacy client called once with the right kwargs
        fake_pd.run_action.assert_called_once_with(
            "gmail-get-profile",
            configured_props={"foo": "bar"},
        )
        # And the proxy client was NOT used at all on this path
        self.assertEqual(fake_proxy.find_calls, [])
        self.assertEqual(fake_proxy.request_calls, [])

    def test_actions_api_failure_is_envelope(self) -> None:
        fake_pd = mock.MagicMock()
        fake_pd.run_action.side_effect = RuntimeError(
            "Connect component API not enabled for this organization"
        )

        with mock.patch.object(pdx, "_client", return_value=fake_pd):
            payload, code = _run_cli(
                [
                    "run",
                    "gmail-get-profile",
                    "--via",
                    "actions-api",
                ]
            )

        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertIn("run_action failed", payload["error"])
        self.assertEqual(payload["action_key"], "gmail-get-profile")
        self.assertEqual(payload["via"], "actions-api")


if __name__ == "__main__":
    unittest.main()
