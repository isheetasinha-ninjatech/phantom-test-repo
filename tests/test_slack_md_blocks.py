"""Tests for slack_md_blocks.md_to_slack_blocks and its wiring into send_message."""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_NINJA_ROOT = os.path.dirname(_HERE)
if _NINJA_ROOT not in sys.path:
    sys.path.insert(0, _NINJA_ROOT)

from slack_md_blocks import md_to_slack_blocks  # noqa: E402


class TestMdToSlackBlocks(unittest.TestCase):
    """The four tests specified in the proposal."""

    def test_no_table_returns_none(self):
        blocks, _ = md_to_slack_blocks(
            "Just a sentence with **bold** and a [link](https://example.com)."
        )
        self.assertIsNone(blocks)

    def test_table_emits_markdown_block(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        blocks, _ = md_to_slack_blocks(md)
        self.assertIsNotNone(blocks)
        self.assertEqual(blocks[0]["type"], "markdown")

    def test_mixed_paragraph_then_table(self):
        md = "Here are the counts:\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        blocks, _ = md_to_slack_blocks(md)
        types = [b["type"] for b in blocks]
        self.assertEqual(types, ["section", "markdown"])

    def test_headers_become_header_blocks(self):
        blocks, _ = md_to_slack_blocks("# Severity\n\nbody")
        self.assertEqual(blocks[0]["type"], "header")
        self.assertEqual(blocks[0]["text"]["text"], "Severity")


class TestSendMessageWiring(unittest.TestCase):
    """Verify send_message promotes tables to blocks without hitting the network."""

    def _client(self):
        import slack_interface

        client = slack_interface.SlackClient.__new__(slack_interface.SlackClient)
        captured = {}

        def fake_api_call(method, token, params):
            captured["params"] = params
            return {"ok": True, "ts": "1.0", "channel": "C1"}

        client._api_call = fake_api_call  # type: ignore
        return client, captured

    def test_table_text_promoted_to_blocks(self):
        client, captured = self._client()
        client.send_message("xoxb-x", "C1", "| A | B |\n|---|---|\n| 1 | 2 |")
        self.assertEqual(captured["params"]["blocks"][0]["type"], "markdown")

    def test_plain_text_not_promoted(self):
        client, captured = self._client()
        client.send_message("xoxb-x", "C1", "just a normal message")
        self.assertNotIn("blocks", captured["params"])


if __name__ == "__main__":
    unittest.main()
