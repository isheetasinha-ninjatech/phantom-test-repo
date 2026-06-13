"""Tests for phantom.whatsapp_interface group send routing."""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

import phantom.whatsapp_interface as wi  # noqa: E402


class ResolveGroupTargetTest(unittest.TestCase):
    def test_conversation_id(self):
        args = mock.Mock(group_jid="", group=" ", conversation="16044174375:g:120363012345678901")
        self.assertEqual(wi.resolve_group_target(args), "120363012345678901@g.us")

    def test_group_jid_full(self):
        args = mock.Mock(group_jid="120363012345678901@g.us", group=None, conversation=None)
        self.assertEqual(wi.resolve_group_target(args), "120363012345678901@g.us")

    def test_group_local_id(self):
        args = mock.Mock(group_jid=None, group="120363012345678901", conversation=None)
        self.assertEqual(wi.resolve_group_target(args), "120363012345678901@g.us")

    def test_mutually_exclusive_flags(self):
        args = mock.Mock(group_jid="1@g.us", group="2", conversation=None)
        with self.assertRaises(ValueError):
            wi.resolve_group_target(args)

    def test_group_last_from_settings(self):
        args = mock.Mock(group_jid=None, group="last", conversation=None)
        with mock.patch.object(wi, "_settings_whatsapp", return_value={"last_group_jid": "12036399@g.us"}):
            self.assertEqual(wi.resolve_group_target(args), "12036399@g.us")

    def test_group_last_missing(self):
        args = mock.Mock(group_jid=None, group="last", conversation=None)
        with mock.patch.object(wi, "_settings_whatsapp", return_value={}):
            with self.assertRaises(ValueError):
                wi.resolve_group_target(args)


class CmdSayTest(unittest.TestCase):
    def _run_say(self, argv: list[str], settings: dict | None = None) -> tuple[int, dict | None]:
        captured: dict = {}

        def fake_request(method, url, *, token=None, body=None, timeout=15.0):
            captured["method"] = method
            captured["url"] = url
            captured["body"] = body
            return 200, json.dumps({"ok": True, **(body or {})}).encode(), {}

        with mock.patch.object(wi, "_request", side_effect=fake_request):
            with mock.patch.object(wi, "_settings_whatsapp", return_value=settings or {}):
                with mock.patch.dict(os.environ, {"WHATSAPP_TO": "79853846088", "WHATSAPP_ALLOWED_TO": "79853846088"}, clear=False):
                    code = wi.main(argv)
        return code, captured.get("body")

    def test_dm_send_uses_to(self):
        code, body = self._run_say(["say", "hello", "--to", "79853846088"])
        self.assertEqual(code, 0)
        self.assertEqual(body, {"to": "79853846088", "text": "hello"})

    def test_group_jid_send(self):
        code, body = self._run_say(["say", "hi group", "--group-jid", "120363012345678901@g.us"])
        self.assertEqual(code, 0)
        self.assertEqual(body, {"group_jid": "120363012345678901@g.us", "text": "hi group"})

    def test_group_last_send(self):
        code, body = self._run_say(
            ["say", "hi group", "--group", "last"],
            settings={"last_group_jid": "12036399@g.us"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(body, {"group_jid": "12036399@g.us", "text": "hi group"})

    def test_conversation_send(self):
        code, body = self._run_say(
            ["say", "hi group", "--conversation", "16044174375:g:120363012345678901"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(body, {"group_jid": "120363012345678901@g.us", "text": "hi group"})

    def test_to_and_group_exclusive(self):
        code, _ = self._run_say(
            ["say", "nope", "--to", "79853846088", "--group-jid", "120363012345678901@g.us"],
        )
        self.assertEqual(code, 2)


class CmdGroupListTest(unittest.TestCase):
    def test_group_list_human(self):
        payload = {
            "ok": True,
            "groups": [
                {
                    "group_jid": "12036399@g.us",
                    "subject": "Alpha",
                    "participant_count": 3,
                    "conversation_id": "160:g:12036399",
                }
            ],
        }

        def fake_request(method, url, *, token=None, body=None, timeout=15.0):
            self.assertEqual(method, "GET")
            self.assertTrue(url.endswith("/groups"))
            return 200, json.dumps(payload).encode(), {}

        with mock.patch.object(wi, "_request", side_effect=fake_request):
            with mock.patch("sys.stdout", new=mock.MagicMock()) as out:
                code = wi.main(["group", "list"])
        self.assertEqual(code, 0)

    def test_group_list_json(self):
        payload = {"ok": True, "groups": []}

        def fake_request(method, url, *, token=None, body=None, timeout=15.0):
            return 200, json.dumps(payload).encode(), {}

        with mock.patch.object(wi, "_request", side_effect=fake_request):
            with mock.patch("sys.stdout", new=mock.MagicMock()) as out:
                code = wi.main(["group", "list", "--json"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
