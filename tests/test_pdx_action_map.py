"""
Unit tests for utils.pdx_action_map.

Run from `src/ninja`:
    PYTHONPATH=. python3 -m unittest tests.test_pdx_action_map -v
"""

from __future__ import annotations

import unittest

from utils.pdx_action_map import ACTION_MAP  # type: ignore
from utils.pdx_action_map import (
    ActionRenderError,
    list_supported_actions,
    render_request,
)


class CuratedMapTest(unittest.TestCase):
    def test_supported_actions_is_sorted(self):
        actions = list_supported_actions()
        self.assertEqual(actions, sorted(actions))
        self.assertGreaterEqual(len(actions), 5)

    def test_every_action_has_required_fields(self):
        for key, sig in ACTION_MAP.items():
            self.assertTrue(sig.app_slug, f"missing app_slug for {key}")
            self.assertIn(
                sig.method,
                {"GET", "POST", "PUT", "PATCH", "DELETE"},
                f"bad method for {key}",
            )
            self.assertTrue(
                sig.path_template.startswith(("http://", "https://", "/")),
                f"bad path_template for {key}: {sig.path_template}",
            )


class RenderRequestTest(unittest.TestCase):
    def test_simple_get_no_props(self):
        r = render_request("github-get-current-user", {})
        self.assertEqual(r.app_slug, "github")
        self.assertEqual(r.method, "GET")
        self.assertEqual(r.url, "https://api.github.com/user")
        self.assertEqual(r.json_body, None)
        self.assertEqual(r.query, {})
        self.assertEqual(r.headers, {})

    def test_path_props_interpolated(self):
        r = render_request(
            "github-create-issue",
            {
                "repoFullname": "acme/widgets",
                "title": "Bug",
                "body": "details",
                "labels": ["bug"],
            },
        )
        self.assertEqual(r.method, "POST")
        self.assertEqual(r.url, "https://api.github.com/repos/acme/widgets/issues")
        self.assertEqual(
            r.json_body,
            {
                "title": "Bug",
                "body": "details",
                "labels": ["bug"],
            },
        )

    def test_body_extra_fields_dropped_when_none(self):
        r = render_request(
            "github-create-issue",
            {
                "repoFullname": "acme/widgets",
                "title": "Bug",
                "labels": None,  # explicit None must be omitted
                "assignees": [],  # falsy but not None — still preserved
            },
        )
        # None value dropped; empty list kept (we only drop None)
        self.assertEqual(r.json_body, {"title": "Bug", "assignees": []})

    def test_missing_required_prop_raises(self):
        with self.assertRaises(ActionRenderError):
            render_request("github-create-issue", {"repoFullname": "a/b"})

    def test_static_headers_attached(self):
        r = render_request("notion-retrieve-self", {})
        self.assertEqual(r.headers.get("Notion-Version"), "2022-06-28")

    def test_unknown_action_raises_keyerror(self):
        with self.assertRaises(KeyError):
            render_request("does-not-exist", {})

    def test_keys_app_resend(self):
        r = render_request(
            "resend-send-email",
            {
                "from": "a@b.com",
                "to": ["c@d.com"],
                "subject": "hi",
                "text": "hello",
            },
        )
        self.assertEqual(r.app_slug, "resend")
        self.assertEqual(r.url, "https://api.resend.com/emails")
        self.assertEqual(r.method, "POST")
        self.assertEqual(
            r.json_body,
            {
                "from": "a@b.com",
                "to": ["c@d.com"],
                "subject": "hi",
                "text": "hello",
            },
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
