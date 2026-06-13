"""Tests for monitor.py response decision logic."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_PHANTOM_ROOT = os.path.dirname(_HERE)
if _PHANTOM_ROOT not in sys.path:
    sys.path.insert(0, _PHANTOM_ROOT)

import monitor  # noqa: E402

AGENT = {"name": "Phantom", "mentions": ["phantom", "@phantom"]}


def msg(**fields) -> dict:
    base = {"type": "message", "ts": "1.0", "text": ""}
    base.update(fields)
    return base


HUMAN = lambda text="": msg(user="U1", text=text)
OWN_BOT = lambda text="": msg(bot_id="B1", subtype="bot_message", text=text)
SUBAGENT = lambda persona, text="": msg(
    bot_id="B1", subtype="bot_message", username=persona, text=text
)
THIRD_BOT = lambda text="": msg(
    bot_id="B2", subtype="bot_message", app_id="A2", username="GitHub", text=text
)


def _patch_own_identity(bot_id="BPHANTOM", user_id="UPHANTOM"):
    return mock.patch.object(
        monitor,
        "_get_own_identity",
        return_value={"bot_id": bot_id, "user_id": user_id},
    )


class IsBotMessageTest(unittest.TestCase):
    def test_classification(self):
        self.assertFalse(monitor.is_bot_message(HUMAN("hi")))
        self.assertTrue(monitor.is_bot_message(OWN_BOT("hi")))
        self.assertTrue(monitor.is_bot_message(msg(subtype="bot_message")))
        self.assertTrue(monitor.is_bot_message(msg(app_id="A1")))
        # Defensive: username on a human payload must not flip it.
        self.assertFalse(monitor.is_bot_message(msg(user="U1", username="Phantom")))


class ShouldRespondTest(unittest.TestCase):
    """The whole policy as a truth table.

    OWN_BOT here is a peer bot (B1), not Phantom itself; own-post detection
    is covered by OwnPostDetectionTest.
    """

    CASES = [
        # (label,                                message,                                expected)
        ("human, no mention", HUMAN("good morning"), True),
        ("human, mentions phantom", HUMAN("hey phantom"), True),
        ("human, audio", msg(user="U1", files=[{"mimetype": "audio/webm"}]), True),
        ("own bot filler (the bug)", OWN_BOT("Working on it..."), False),
        (
            "own bot post containing 'phantom'",
            OWN_BOT("Phantom finished the task."),
            True,
        ),
        (
            "sub-agent relay to phantom",
            SUBAGENT("Pixel", "phantom please pick this up"),
            True,
        ),
        (
            "sub-agent status without phantom",
            SUBAGENT("Nova", "search complete"),
            False,
        ),
        ("third-party bot, no mention", THIRD_BOT("PR #42 merged"), False),
        ("third-party bot pinging phantom", THIRD_BOT("@phantom CI failed"), True),
    ]

    def test_truth_table(self):
        with _patch_own_identity(bot_id="BPHANTOM"):
            for label, message, expected in self.CASES:
                with self.subTest(label):
                    self.assertEqual(
                        monitor.should_respond_to_message(message, AGENT),
                        expected,
                    )

    def test_check_for_mention_is_alias(self):
        self.assertIs(monitor.check_for_mention, monitor.should_respond_to_message)


class OwnPostDetectionTest(unittest.TestCase):
    """Phantom must never respond to its own posts."""

    OWN_BOT_ID = "BPHANTOM"

    def test_skips_own_filler(self):
        own = msg(
            bot_id=self.OWN_BOT_ID,
            subtype="bot_message",
            username="Phantom",
            text="Hi, I'm Phantom \u2014 your Browser Automation Agent.",
        )
        with _patch_own_identity(self.OWN_BOT_ID):
            self.assertFalse(monitor.is_own_post(OWN_BOT("hi")))  # peer bot
            self.assertTrue(monitor.is_own_post(own))
            self.assertFalse(monitor.should_respond_to_message(own, AGENT))


class ShouldReactWithGhostTest(unittest.TestCase):
    """Ghost-emoji ack is tighter than the response policy."""

    OWN_BOT_ID = "BPHANTOM"

    def test_truth_table(self):
        own = msg(
            bot_id=self.OWN_BOT_ID,
            subtype="bot_message",
            username="Phantom",
            text="anything mentioning phantom",
        )
        cases = [
            ("human, no mention", HUMAN("hi"), True),
            ("human, with mention", HUMAN("hey phantom"), True),
            ("own post", own, False),
            ("third-party bot, silent", THIRD_BOT("PR #42 merged"), False),
            ("third-party bot, ping", THIRD_BOT("@phantom CI failed"), True),
        ]
        with _patch_own_identity(self.OWN_BOT_ID):
            for label, message, expected in cases:
                with self.subTest(label):
                    self.assertEqual(
                        monitor.should_react_with_ghost(message, AGENT),
                        expected,
                    )


PHANTOM_AGENT = {
    "name": "Phantom",
    "role": "Browser Automation Agent",
    "emoji": "👻",
    "mentions": ["phantom", "@phantom"],
}


class IsHumanMessageTest(unittest.TestCase):
    def test_classification(self):
        self.assertTrue(monitor.is_human_message(HUMAN("hi")))
        self.assertFalse(monitor.is_human_message(OWN_BOT("hi")))
        self.assertFalse(monitor.is_human_message(THIRD_BOT("hi")))
        # System events (channel_join, topic change, etc.) are not humans.
        self.assertFalse(
            monitor.is_human_message(msg(user="U1", subtype="channel_join"))
        )
        # Payload with no user at all (rare) is not a human either.
        self.assertFalse(monitor.is_human_message(msg()))


class ShouldPostWelcomeTest(unittest.TestCase):
    def test_empty_channel_posts(self):
        self.assertTrue(monitor.should_post_welcome([]))

    def test_only_bot_chatter_still_posts(self):
        self.assertTrue(
            monitor.should_post_welcome([OWN_BOT("starting up"), THIRD_BOT("CI green")])
        )

    def test_any_human_blocks(self):
        self.assertFalse(
            monitor.should_post_welcome([OWN_BOT("hi"), HUMAN("good morning")])
        )

    def test_existing_welcome_marker_blocks(self):
        # The opening signature inside any prior bot post is enough to
        # suppress a re-post, even without a hidden marker.
        prior = OWN_BOT(
            f"... {monitor._WELCOME_SIGNATURE} Browser Automation Agent ..."
        )
        self.assertFalse(monitor.should_post_welcome([prior]))


class BuildWelcomeMessageTest(unittest.TestCase):
    def test_contains_dashboards_browser_and_signature(self):
        text = monitor.build_welcome_message(PHANTOM_AGENT)
        # Live browser (noVNC), activity dashboard, connect-apps dashboard
        self.assertIn("0.0.0.0:6080", text)
        self.assertIn("0.0.0.0:9000", text)
        self.assertIn("0.0.0.0:9020", text)
        # Virtual-employee positioning + integration count + browser/API split
        self.assertIn("virtual employee", text)
        self.assertIn("3,000", text)
        self.assertIn("Browser", text)
        self.assertIn("Integrations", text)
        # Identity
        self.assertIn("Phantom", text)
        self.assertIn("Browser Automation Agent", text)
        # Idempotency anchor
        self.assertIn(monitor._WELCOME_SIGNATURE, text)

    def test_no_pipedream_branding_leak(self):
        # 'Pipedream' is an implementation detail \u2014 must not appear in
        # the user-facing welcome.
        self.assertNotIn("Pipedream", monitor.build_welcome_message(PHANTOM_AGENT))

    def test_multilingual_framing(self):
        # Phantom is multilingual; the welcome must invite users in
        # any language and must not imply English-only.
        text = monitor.build_welcome_message(PHANTOM_AGENT)
        self.assertIn("any language", text)
        self.assertNotIn("plain English", text)
        self.assertNotIn("in English", text)

    def test_no_literal_at_phantom_mention(self):
        # Slack auto-linkifies a literal '@phantom' in message text to a
        # broken user mention. The welcome must not contain that token.
        text = monitor.build_welcome_message(PHANTOM_AGENT)
        self.assertNotIn("@phantom", text)
        self.assertNotIn("@Phantom", text)

    def test_idempotent_via_signature(self):
        # The text we build must trip our own should_post_welcome guard
        # if it appears as a previous bot post in history.
        text = monitor.build_welcome_message(PHANTOM_AGENT)
        prior = OWN_BOT(text)
        self.assertFalse(monitor.should_post_welcome([prior]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
