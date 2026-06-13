#!/usr/bin/env python3
"""
Agent Monitor - Watches Slack for mentions and triggers agent responses.

This script runs independently and only invokes Claude CLI when the agent
is mentioned in Slack. It polls every 45 seconds and tracks seen messages.

Features:
- Monitors main channel for mentions
- Monitors thread replies to agent's messages
- Batches all messages and sends to Claude in one prompt per cycle
- Exponential backoff on rate limiting

Usage:
    python monitor.py              # Run with configured agent
    python monitor.py --agent phantom # Run as specific agent
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Import centralized agent configuration
from agents_config import AGENTS

# Cron scheduler — see agent-docs/CRON.md and tools/cron.py
from cron_scheduler import claim_cron, get_due_cron_messages

# Direct import of SlackInterface — avoids subprocess spawning overhead
# This single instance is reused for all API calls in the monitor loop
from slack_interface import SlackConfig, SlackInterface, get_slack_tokens

_slack_instance = None
_own_identity_cache: dict | None = None

EMOJI_MAP = {
    "👻": "ghost",
    "🎶": "notes",
    "🎵": "musical_note",
    "💻": "computer",
    "🤖": "robot_face",
    "⚡": "zap",
    "🌟": "star",
    "🔥": "fire",
    "✅": "white_check_mark",
}


def _get_slack() -> "SlackInterface":
    """Get or create a persistent SlackInterface instance."""
    global _slack_instance
    if _slack_instance is None:
        _slack_instance = SlackInterface()
    return _slack_instance


def _get_own_identity() -> dict:
    """Cached auth.test result ({bot_id, user_id, team_id}); {} on failure."""
    global _own_identity_cache
    if _own_identity_cache is not None:
        return _own_identity_cache
    try:
        slack = _get_slack()
        token = slack.tokens.bot_token
        if not token:
            _own_identity_cache = {}
            return _own_identity_cache
        info = slack.client.test_auth(token)
        if info.get("ok"):
            _own_identity_cache = {
                "bot_id": info.get("bot_id"),
                "user_id": info.get("user_id"),
                "team_id": info.get("team_id"),
            }
        else:
            _own_identity_cache = {}
    except Exception:
        _own_identity_cache = {}
    return _own_identity_cache


def is_own_post(message: dict) -> bool:
    """True if ``message`` was posted by this monitor's own bot identity."""
    own = _get_own_identity()
    if not own:
        return False
    if own.get("bot_id") and message.get("bot_id") == own["bot_id"]:
        return True
    if own.get("user_id") and message.get("user") == own["user_id"]:
        return True
    return False


# Configuration
REPO_ROOT = Path(__file__).parent
CONFIG_PATH = Path.home() / ".agent_settings.json"
POLL_INTERVAL = 60  # base seconds
POLL_JITTER = 5  # random jitter seconds
MAX_RUNTIME = 24 * 60 * 60  # 24 hours in seconds
SEEN_MESSAGES_FILE = REPO_ROOT / ".seen_messages.json"
AGENT_MESSAGES_FILE = (
    REPO_ROOT / ".agent_messages.json"
)  # Track agent's own messages for thread monitoring

# Rate limiting configuration
BACKOFF_INITIAL = 60  # Initial backoff: 1 minute
BACKOFF_MAX = 600  # Max backoff: 10 minutes
BACKOFF_MULTIPLIER = 2  # Double the backoff each time


class RateLimitHandler:
    """Handles exponential backoff for rate limiting."""

    def __init__(self):
        self.current_backoff = 0
        self.consecutive_rate_limits = 0
        self.last_rate_limit_time = 0

    def on_rate_limit(self):
        """Called when a rate limit is encountered."""
        self.consecutive_rate_limits += 1
        self.last_rate_limit_time = time.time()

        if self.current_backoff == 0:
            self.current_backoff = BACKOFF_INITIAL
        else:
            self.current_backoff = min(
                self.current_backoff * BACKOFF_MULTIPLIER, BACKOFF_MAX
            )

        print(
            f"⚠️ Rate limited! Backing off for {self.current_backoff}s (attempt #{self.consecutive_rate_limits})",
            flush=True,
        )
        return self.current_backoff

    def on_success(self):
        """Called when a request succeeds."""
        if self.consecutive_rate_limits > 0:
            print(
                f"✅ Rate limit cleared after {self.consecutive_rate_limits} retries",
                flush=True,
            )
        self.current_backoff = 0
        self.consecutive_rate_limits = 0

    def is_backing_off(self) -> bool:
        """Check if we're currently in a backoff period."""
        if self.current_backoff == 0:
            return False
        elapsed = time.time() - self.last_rate_limit_time
        return elapsed < self.current_backoff

    def get_remaining_backoff(self) -> float:
        """Get remaining backoff time in seconds."""
        if not self.is_backing_off():
            return 0
        elapsed = time.time() - self.last_rate_limit_time
        return max(0, self.current_backoff - elapsed)


# Global rate limit handler
rate_limiter = RateLimitHandler()


def load_config() -> dict:
    """Load agent configuration from ~/.agent_settings.json"""
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text())
    except Exception as e:
        print(f"⚠️ Warning: Could not read config: {e}", file=sys.stderr)
    return {}


def load_seen_messages() -> set:
    """Load previously seen message timestamps."""
    try:
        if SEEN_MESSAGES_FILE.exists():
            data = json.loads(SEEN_MESSAGES_FILE.read_text())
            return set(data.get("seen", []))
    except Exception:
        pass
    return set()


def save_seen_messages(seen: set):
    """Save seen message timestamps."""
    try:
        # Keep only last 100 messages to prevent file from growing too large
        recent = sorted(seen)[-100:]
        SEEN_MESSAGES_FILE.write_text(json.dumps({"seen": recent}))
    except Exception as e:
        print(f"⚠️ Warning: Could not save seen messages: {e}", file=sys.stderr)


def load_agent_messages() -> dict:
    """Load agent's own message timestamps for thread monitoring."""
    try:
        if AGENT_MESSAGES_FILE.exists():
            return json.loads(AGENT_MESSAGES_FILE.read_text())
    except Exception:
        pass
    return {"messages": [], "seen_replies": []}


def save_agent_messages(data: dict):
    """Save agent's message timestamps."""
    try:
        # Keep only last 20 messages to monitor
        data["messages"] = data.get("messages", [])[-20:]
        data["seen_replies"] = data.get("seen_replies", [])[-100:]
        AGENT_MESSAGES_FILE.write_text(json.dumps(data))
    except Exception as e:
        print(f"⚠️ Warning: Could not save agent messages: {e}", file=sys.stderr)


def is_rate_limited(output: str) -> bool:
    """Check if output indicates rate limiting."""
    rate_limit_indicators = [
        "ratelimited",
        "rate_limited",
        "rate limit",
        "too many requests",
        "429",
    ]
    output_lower = output.lower()
    return any(indicator in output_lower for indicator in rate_limit_indicators)


def get_thread_replies(thread_ts: str) -> tuple[list, bool]:
    """
    Get replies to a specific thread using direct Python API (no subprocess).

    Returns raw Slack message dicts with all fields.

    Returns:
        Tuple of (messages list, was_rate_limited bool)
    """
    try:
        slack = _get_slack()
        replies = slack.get_replies(thread_ts)
        return replies, False
    except Exception as e:
        error_str = str(e).lower()
        if "ratelimit" in error_str or "rate" in error_str:
            return [], True
        return [], False


def get_last_messages(limit: int = 20) -> tuple[list, bool]:
    """
    Get recent messages from Slack using direct Python API (no subprocess).

    Returns raw Slack message dicts with all fields (ts, reply_count,
    latest_reply, user, text, etc.) — used for both mention checking
    and thread tracking in a single API call.

    Returns:
        Tuple of (messages list, was_rate_limited bool)
    """
    try:
        slack = _get_slack()
        messages = slack.get_history(limit=limit)
        return messages, False
    except Exception as e:
        error_str = str(e).lower()
        if "ratelimit" in error_str or "rate" in error_str:
            return [], True
        print(f"⚠️ Error reading Slack: {e}", file=sys.stderr)
        return [], False


def has_audio_attachment(message: dict) -> bool:
    """Check if a message contains audio/voice attachments."""
    files = message.get("files", [])
    for f in files:
        mimetype = f.get("mimetype", "")
        subtype = f.get("subtype", "")
        if mimetype.startswith("audio/") or subtype == "voice_message":
            return True
    return False


def is_bot_message(message: dict) -> bool:
    """True if the message was posted by any Slack bot/app."""
    return bool(
        message.get("bot_id")
        or message.get("subtype") == "bot_message"
        or message.get("app_id")
    )


def message_mentions_agent(message: dict, agent: dict) -> bool:
    """True if message text contains any of the agent's mention keywords."""
    text = (message.get("text") or "").lower()
    return any(m.lower() in text for m in agent.get("mentions", []))


def should_respond_to_message(message: dict, agent: dict) -> bool:
    """Decide whether the monitor should respond to ``message``.

    Own posts: never. Humans: always. Other bots: only when mentioned.
    """
    if is_own_post(message):
        return False
    if is_bot_message(message):
        return message_mentions_agent(message, agent)
    return True


def should_react_with_ghost(message: dict, agent: dict) -> bool:
    """Tighter than should_respond_to_message: ack humans + bots that mention us; never own posts."""
    if is_own_post(message):
        return False
    if is_human_message(message):
        return True
    if is_bot_message(message):
        return message_mentions_agent(message, agent)
    return False


# Backwards-compatible alias for the old name.
check_for_mention = should_respond_to_message


# ---------------------------------------------------------------------------
# First-run welcome announcement
# ---------------------------------------------------------------------------
# When the monitor wakes up in a freshly-provisioned channel that has no
# human conversation yet, post a one-time announcement so the channel
# isn't a blank slate. We gate on:
#   1. zero human messages in recent history (the channel is "empty"
#      from a user perspective \u2014 system join messages and our own bot
#      pings don't count), AND
#   2. We haven't already welcomed in this channel. Belt-and-suspenders:
#      a persisted ``welcomed`` flag in ``.agent_messages.json`` *and*
#      a history sniff for our distinctive opening signature.


# Distinctive opening phrase used both as the first user-visible line of
# the welcome and as our invisible idempotency anchor when we read back
# channel history. Don't change this string without updating
# build_welcome_message() to match \u2014 the test suite enforces that.
_WELCOME_SIGNATURE = "Hi, I'm Phantom \u2014 your"


def is_human_message(message: dict) -> bool:
    """True for real user messages (not bots, not channel system events)."""
    if is_bot_message(message):
        return False
    if message.get("subtype"):
        # channel_join, channel_topic, bot_message, etc. \u2014 all non-human.
        return False
    return bool(message.get("user"))


def should_post_welcome(messages: list) -> bool:
    """Decide whether to post the first-run welcome based on history.

    Post only when (a) no human has spoken yet and (b) we don't see
    our own welcome signature in any prior post. The persisted
    ``welcomed`` flag is checked separately by ``post_welcome_if_empty``.
    """
    if any(is_human_message(m) for m in messages):
        return False
    for m in messages:
        if _WELCOME_SIGNATURE in (m.get("text") or ""):
            return False
    return True


def build_welcome_message(agent: dict) -> str:
    """Build the welcome announcement text.

    Written in standard Markdown so :func:`slack_interface.send_message`'s
    ``slackify_markdown`` step turns ``[label](url)`` into Slack
    mrkdwn ``<url|label>`` automatically. URLs use the bare
    ``0.0.0.0:<port>`` form so :func:`convert_sandbox_urls` rewrites
    them to the public sandbox host *before* the markdown pass.

    The first user-visible sentence MUST contain ``_WELCOME_SIGNATURE``
    \u2014 it doubles as our invisible idempotency anchor when reading
    back channel history. The test suite enforces this invariant.
    """
    emoji = agent.get("emoji", "\U0001f47b")
    name = agent.get("name", "Phantom")
    role = agent.get("role", "Browser Automation Agent")
    return (
        f"{emoji} **Hi, I'm {name} \u2014 your {role}.**\n"
        "Think of me as a virtual employee on your team. Brief me in "
        "any language \u2014 by message, voice note, or file \u2014 and I'll "
        "get the work done. No clicking, no copy-pasting, no API keys "
        "for you to manage.\n"
        "\n"
        "**\U0001f4bc What you can ask me to do**\n"
        '- **Research & reports** \u2014 "Pull the top 10 competitors for '
        'X, summarise their pricing, give me a one-pager."\n'
        '- **Lead gen & outreach** \u2014 "Find 50 founders of seed-stage '
        'fintechs in NYC and draft a personalised intro email to each."\n'
        '- **Recruiting** \u2014 "Source 20 senior backend engineers from '
        'LinkedIn matching this JD and message them on my behalf."\n'
        '- **Operations & data entry** \u2014 "Update these 30 Salesforce '
        'records from this spreadsheet," or "file my expense reports '
        'from these receipts."\n'
        '- **Travel & bookings** \u2014 "Book the cheapest direct flight '
        'from SFO to JFK next Friday and add it to my calendar."\n'
        '- **Creative work** \u2014 "Generate a flat-style logo for a '
        'coffee app called Brewly," or edit a photo, design a banner, '
        "or mock up a landing page hero image.\n"
        "- **Reports posted right here** \u2014 I deliver results back to "
        "Slack as a message, file, image, or thread you can act on.\n"
        "\n"
        "**\U0001f9f0 How I get things done \u2014 two complementary tools**\n"
        "- \U0001f30d **Browser** \u2014 I drive a real Chromium browser the "
        "way a human would: navigate any website, fill forms, click "
        "through flows, scrape data, log in to dashboards, download "
        "files. *Best for:* anything without an API \u2014 internal admin "
        "tools, niche SaaS, web search, news, social profiles, "
        "research across many sites.\n"
        "- \U0001f50c **Integrations (3,000+ apps)** \u2014 direct, "
        "authenticated access to Slack, Gmail, Google Calendar, "
        "GitHub, Jira, Linear, Notion, Salesforce, HubSpot, Stripe, "
        "Airtable, Asana, LinkedIn, X/Twitter, AWS, and ~3,000 more. "
        "*Best for:* anything with a stable API \u2014 fast, reliable, "
        "rate-limit-friendly, and works in the background even while "
        "you're offline.\n"
        "I pick the right tool for each step automatically; you "
        "don't have to choose.\n"
        "\n"
        "**\U0001f4ac How to brief me**\n"
        "- Just type a message in this channel \u2014 I reply to every "
        "human message.\n"
        "- Include the word `phantom` anywhere in your message to be "
        "explicit, or to ping me from a thread.\n"
        "- Send a *voice note* in any language and I'll transcribe and "
        "act on it.\n"
        "- Drop a *screenshot, PDF, spreadsheet, or any file* with "
        "your request and I'll use it as context.\n"
        "\n"
        "**\U0001f441 Watch me work**\n"
        "- [**Live Browser**](0.0.0.0:6080/vnc.html?autoconnect=true) "
        "\u2014 watch my Chromium session in real time. Take over with "
        "mouse/keyboard if I get stuck.\n"
        "- [**Activity Dashboard**](0.0.0.0:9000) \u2014 live identity, "
        "logs, reasoning trace, and per-task cost.\n"
        "- [**Connect Apps**](0.0.0.0:9020) \u2014 connect new apps "
        "(one-click OAuth) so I can use them. Anything you connect "
        "here becomes a tool I can call."
    )


def post_welcome_if_empty(agent: dict) -> bool:
    """Post the welcome announcement if the channel is empty of humans.

    Returns True iff the message was actually posted. Best-effort: any
    Slack/network error is swallowed so it never blocks monitor startup.

    Idempotency layers:
      1. Persisted ``welcomed`` flag in ``.agent_messages.json``
         (survives monitor restarts in the same sandbox).
      2. History sniff for our welcome signature (covers the case where
         the local state file is wiped or the bot moves to a new
         sandbox but the channel already saw a welcome).
    """
    try:
        agent_data = load_agent_messages()
        if agent_data.get("welcomed"):
            return False

        messages, was_rate_limited = get_last_messages(50)
        if was_rate_limited:
            return False
        if not should_post_welcome(messages):
            # Already welcomed in this channel \u2014 backfill the local flag
            # so we don't pay the history sniff cost on every restart.
            agent_data["welcomed"] = True
            save_agent_messages(agent_data)
            return False

        text = build_welcome_message(agent)
        # say() auto-resolves agent identity from default_agent.
        _get_slack().say(text)

        agent_data["welcomed"] = True
        save_agent_messages(agent_data)
        print("\U0001f44b Posted first-run welcome announcement", flush=True)
        return True
    except Exception as e:
        print(f"\u26a0\ufe0f Welcome announcement skipped: {e}", file=sys.stderr)
        return False


def run_batched_response(agent: dict, pending_messages: list) -> bool:
    """
    Send all pending messages to Claude in a single prompt.
    Claude will respond to all of them at once using slack_interface.py.

    Args:
        agent: Agent configuration dict
        pending_messages: List of message dicts with keys:
            - user: Who sent the message
            - text: Message content
            - timestamp: When it was sent
            - thread_ts: Thread timestamp (if replying to a thread)
            - type: 'mention' or 'thread_reply'

    Returns:
        True if Claude successfully processed the messages
    """
    if not pending_messages:
        return True

    agent_name = agent["name"]
    agent_role = agent["role"]
    agent_emoji = agent["emoji"]

    # Build the messages list for the prompt
    messages_text = ""
    for i, msg in enumerate(pending_messages, 1):
        msg_type = msg.get("type", "mention")

        # Cron items are scheduled jobs, not user messages — render distinctly.
        # The agent posts the result to its single configured Slack channel
        # the same way it answers a normal mention. See agent-docs/CRON.md.
        if msg_type == "cron":
            if msg.get("thread_ts"):
                reply_hint = (
                    f'python slack_interface.py say "message" -t {msg["thread_ts"]}'
                )
            else:
                reply_hint = 'python slack_interface.py say "message"'
            messages_text += f"""
--- Message {i} (cron — scheduled job) ---
Cron ID: {msg.get('cron_id', 'unknown')}
Prompt: {msg.get('text', '')}
Post the result with: {reply_hint}
(See agent-docs/CRON.md. Execute the prompt; do not ask for confirmation.)
"""
            continue

        thread_info = ""
        if msg.get("thread_ts"):
            thread_info = f'\n   Thread: {msg["thread_ts"]} (reply with: python slack_interface.py say "message" -t {msg["thread_ts"]})'
        else:
            thread_info = '\n   Channel: main (reply with: python slack_interface.py say "message")'

        # Include audio file info if present
        audio_info = ""
        if msg.get("audio_files"):
            audio_info = (
                "\n   🎤 AUDIO/VOICE MESSAGE — Must transcribe before responding!"
            )
            for af in msg["audio_files"]:
                audio_info += f"\n   Audio file: {af.get('name', 'audio')} ({af.get('mimetype', 'audio/*')})"
                audio_info += f"\n   Download URL: {af.get('url', 'N/A')}"

        messages_text += f"""
--- Message {i} ({msg_type}) ---
From: {msg.get('user', 'Unknown')}
Time: {msg.get('timestamp', 'Unknown')}
Text: {msg.get('text', '')}{audio_info}{thread_info}
"""

    # Build the batched prompt
    prompt = f"""You are {agent_name} {agent_emoji}, the {agent_role}.

We are running you as a monitor agent, your specification flaw is in agent-docs/MONITOR.md. For scheduled cron items see agent-docs/CRON.md.

The current time is {time.strftime("%Y-%m-%d %H:%M:%S")}. You have {len(pending_messages)} message(s) that need your response. Read ALL of them and respond to EACH ONE.

{messages_text}"""

    print(
        f"\n{agent_emoji} Sending {len(pending_messages)} message(s) to Claude for batch response...",
        flush=True,
    )

    try:
        # Let Claude handle all responses
        result = subprocess.run(
            [str(REPO_ROOT / "claude-wrapper.sh"), "-c", "-p", prompt],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,  # Give Claude more time for multiple messages
        )

        # Check if Claude successfully posted
        output = result.stdout + result.stderr
        success_count = (
            output.count("Message sent")
            + output.count("✅")
            + output.count("Timestamp:")
        )

        if success_count > 0:
            print(
                f"✅ Claude processed batch - {success_count} response indicator(s) found",
                flush=True,
            )
            return True
        else:
            print(
                f"⚠️ Claude batch response (may have posted): {output[:300]}...",
                flush=True,
            )
            return True  # Assume success even if we can't confirm

    except subprocess.TimeoutExpired:
        print("⚠️ Claude batch response timed out", flush=True)
        return False
    except Exception as e:
        print(f"⚠️ Error: {e}", flush=True)
        return False


def main():
    import argparse
    import random

    parser = argparse.ArgumentParser(
        description="Agent Monitor - Watch Slack for mentions"
    )
    parser.add_argument("--agent", "-a", help="Agent to run as (default: from config)")
    parser.add_argument(
        "--interval",
        "-i",
        type=int,
        default=POLL_INTERVAL,
        help="Poll interval in seconds",
    )
    args = parser.parse_args()

    # Get agent from args or config
    config = load_config()
    agent_id = args.agent or config.get("default_agent", "").lower()

    if not agent_id or agent_id not in AGENTS:
        print("❌ No valid agent configured!", file=sys.stderr)
        print(f"Available agents: {', '.join(AGENTS.keys())}", file=sys.stderr)
        print(
            "Set with: python slack_interface.py config --set-agent <name>",
            file=sys.stderr,
        )
        sys.exit(1)

    agent = AGENTS[agent_id]

    print(
        f"""
╔══════════════════════════════════════════════════════════════╗
║  {agent['emoji']} {agent['name']} Monitor - Watching for Slack mentions
╠══════════════════════════════════════════════════════════════╣
║  Agent: {agent['name']} ({agent['role']})
║  Polling: Every {args.interval}s (+{POLL_JITTER}s jitter)
║  Max runtime: {MAX_RUNTIME // 60} minutes
║  Mentions: {', '.join(agent['mentions'])}
║  Thread replies: ✅ Enabled
║  Audio/voice detection: ✅ Enabled
║  Batch mode: ✅ Enabled (one Claude call per cycle)
║  Rate limit backoff: ✅ Enabled ({BACKOFF_INITIAL}s-{BACKOFF_MAX}s)
╚══════════════════════════════════════════════════════════════╝
""",
        flush=True,
    )

    seen_messages = load_seen_messages()
    agent_data = load_agent_messages()
    start_time = time.time()

    # First-run welcome: if the channel has no human messages
    # yet, post an introduction. Idempotent on restart via a
    # hidden marker string.
    post_welcome_if_empty(agent)

    print(f"📡 Starting monitor loop (max {MAX_RUNTIME // 60} minutes)...", flush=True)

    try:
        while True:
            # Check if max runtime exceeded
            elapsed = time.time() - start_time
            if elapsed >= MAX_RUNTIME:
                print(
                    f"\n⏰ Max runtime ({MAX_RUNTIME // 60} minutes) reached. Stopping monitor.",
                    flush=True,
                )
                break

            # Check if we're in a backoff period
            if rate_limiter.is_backing_off():
                remaining = rate_limiter.get_remaining_backoff()
                print(
                    f"⏳ Rate limit backoff: {remaining:.0f}s remaining...", flush=True
                )
                time.sleep(min(remaining, 30))  # Sleep in chunks of max 30s
                continue

            # Collect all pending messages for this cycle
            pending_messages = []

            # Get recent messages
            raw_messages, was_rate_limited = get_last_messages(50)

            if was_rate_limited:
                backoff_time = rate_limiter.on_rate_limit()
                time.sleep(min(backoff_time, 30))
                continue
            else:
                rate_limiter.on_success()

            print(f"📨 Got {len(raw_messages)} messages", flush=True)

            # Check for new mentions in main channel
            for msg in raw_messages:
                msg_id = msg.get("ts", "") or msg.get("timestamp", "")

                if msg_id in seen_messages:
                    continue

                # Thread replies are handled by the thread-replies scan below.
                msg_thread_ts = msg.get("thread_ts")
                if msg_thread_ts and msg_thread_ts != msg_id:
                    seen_messages.add(msg_id)
                    continue

                seen_messages.add(msg_id)

                if check_for_mention(msg, agent):
                    user = msg.get("user", "") or msg.get("username", "Unknown")
                    is_audio = has_audio_attachment(msg)
                    msg_type = "audio_message" if is_audio else "mention"

                    # Ghost-ack only humans + bots that mention us.
                    if should_react_with_ghost(msg, agent):
                        try:
                            _reaction = os.environ.get(
                                "PHANTOM_AGENT_EMOJI", "👻"
                            ).strip()
                            # Slack reactions use emoji names, not emoji chars. Map common ones.
                            _get_slack().react(
                                msg_id, EMOJI_MAP.get(_reaction, "ghost")
                            )
                        except Exception:
                            pass  # best-effort

                    # Build message text — include audio file info if present
                    msg_text = msg.get("text", "")
                    audio_files = []
                    if is_audio:
                        for f in msg.get("files", []):
                            mimetype = f.get("mimetype", "")
                            subtype = f.get("subtype", "")
                            if (
                                mimetype.startswith("audio/")
                                or subtype == "voice_message"
                            ):
                                audio_files.append(
                                    {
                                        "name": f.get("name", "audio"),
                                        "mimetype": mimetype,
                                        "url": f.get("url_private_download", ""),
                                    }
                                )
                        print(f"  🎤 New audio/voice message from {user}", flush=True)
                    else:
                        print(
                            f"  👻 Acked + queued mention from {user}: {msg_text[:50]}...",
                            flush=True,
                        )

                    pending_messages.append(
                        {
                            "user": user,
                            "text": msg_text,
                            "timestamp": msg_id,
                            "thread_ts": None,
                            "type": msg_type,
                            "audio_files": audio_files,
                        }
                    )

            # Check for thread replies — group raw_messages by thread_ts
            # since the S3 channel cache strips reply_count/latest_reply.
            if rate_limiter.consecutive_rate_limits == 0:
                threads_by_ts: dict[str, list] = {}
                for raw_msg in raw_messages:
                    tts = raw_msg.get("thread_ts")
                    raw_ts = raw_msg.get("ts", "") or raw_msg.get("timestamp", "")
                    if tts and tts != raw_ts:
                        threads_by_ts.setdefault(tts, []).append(raw_msg)

                threads_checked = 0
                for thread_ts, replies in threads_by_ts.items():
                    if threads_checked >= 3:  # Limit threads per cycle
                        break
                    threads_checked += 1

                    replies.sort(key=lambda r: float(r.get("ts", "0") or "0"))
                    latest_reply = replies[-1].get("ts", "")
                    reply_key = f"{thread_ts}:{latest_reply}"
                    if reply_key in agent_data.get("seen_replies", []):
                        continue

                    for reply in replies:
                        reply_ts = reply.get("ts", "") or reply.get("timestamp", "")
                        reply_id = f"{thread_ts}:{reply_ts}"

                        if reply_id in agent_data.get("seen_replies", []):
                            continue

                        reply_user = reply.get("user", "") or reply.get("username", "")
                        if not should_respond_to_message(reply, agent):
                            agent_data.setdefault("seen_replies", []).append(reply_id)
                            continue

                        # Ghost-ack only when warranted.
                        if should_react_with_ghost(reply, agent):
                            try:
                                _reaction2 = os.environ.get(
                                    "PHANTOM_AGENT_EMOJI", "👻"
                                ).strip()
                                _get_slack().react(
                                    reply_ts, EMOJI_MAP.get(_reaction2, "ghost")
                                )
                            except Exception:
                                pass  # best-effort
                        print(
                            f"  👻 Acked + queued thread reply from {reply_user}: {reply.get('text', '')[:50]}..."
                        )
                        pending_messages.append(
                            {
                                "user": reply_user or "Unknown",
                                "text": reply.get("text", ""),
                                "timestamp": reply_ts,
                                "thread_ts": thread_ts,
                                "type": "thread_reply",
                            }
                        )

                        # Mark as seen
                        agent_data.setdefault("seen_replies", []).append(reply_id)
                    # Mark latest reply as seen
                    agent_data.setdefault("seen_replies", []).append(reply_key)

            # Inject any due cron jobs into the same batch as Slack mentions.
            # See agent-docs/CRON.md and tools/cron.py.
            for job in get_due_cron_messages(time.time()):
                if claim_cron(job["id"]):
                    pending_messages.append(
                        {
                            "user": "cron",
                            "text": job["prompt"],
                            "timestamp": f"cron:{job['id']}:{int(time.time())}",
                            "thread_ts": job.get("thread_ts"),
                            "type": "cron",
                            "cron_id": job["id"],
                        }
                    )
                    print(
                        f"  ⏰ Cron job '{job['id']}' is due — queued for batch",
                        flush=True,
                    )

            # Process all pending messages in one batch
            if pending_messages:
                print(
                    f"\n📋 Processing {len(pending_messages)} pending message(s) in batch...",
                    flush=True,
                )
                run_batched_response(agent, pending_messages)

            # Save state
            save_seen_messages(seen_messages)
            save_agent_messages(agent_data)

            # Wait for next poll
            jitter = random.uniform(0, POLL_JITTER)
            sleep_time = args.interval + jitter

            if rate_limiter.consecutive_rate_limits > 0:
                sleep_time += BACKOFF_INITIAL / 2
                print(
                    f"💤 Extended sleep due to recent rate limits: {sleep_time:.0f}s",
                    flush=True,
                )

            time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\n\n👋 Monitor stopped")
        save_seen_messages(seen_messages)
        save_agent_messages(agent_data)


if __name__ == "__main__":
    main()
