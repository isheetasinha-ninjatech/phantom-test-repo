#!/usr/bin/env python3
"""Agent Monitor - Watches Microsoft Teams for Phantom tasks (POC).

This is intentionally smaller than the Slack monitor. It polls Microsoft Graph
for recent channel/chat messages, queues messages that mention the configured
agent, and invokes the same Claude wrapper that the Slack monitor uses.

Usage:
    python teams_monitor.py --interval 60
    python -m phantom.teams_monitor --once
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

try:
    from .agents_config import AGENTS
    from .teams_interface import (
        TeamsAPIError,
        TeamsConfigError,
        TeamsDestination,
        TeamsInterface,
    )
except ImportError:  # pragma: no cover - supports direct script execution
    from agents_config import AGENTS
    from teams_interface import (
        TeamsAPIError,
        TeamsConfigError,
        TeamsDestination,
        TeamsInterface,
    )


REPO_ROOT = Path(__file__).parent
CONFIG_PATH = Path.home() / ".agent_settings.json"
SEEN_MESSAGES_FILE = REPO_ROOT / ".teams_seen_messages.json"
LOG_DIR = REPO_ROOT / "logs"
CLAUDE_DEBUG_LOG_FILE = LOG_DIR / "teams_monitor_claude.log"
POLL_INTERVAL = 60
MAX_RUNTIME = 24 * 60 * 60

_teams_instance: Optional[TeamsInterface] = None


def _get_teams() -> TeamsInterface:
    global _teams_instance
    if _teams_instance is None:
        _teams_instance = TeamsInterface()
    return _teams_instance


def load_config() -> dict[str, Any]:
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text())
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def load_seen_messages() -> set[str]:
    try:
        if SEEN_MESSAGES_FILE.exists():
            data = json.loads(SEEN_MESSAGES_FILE.read_text())
            seen = data.get("seen") if isinstance(data, dict) else []
            return {str(x) for x in seen}
    except Exception:
        pass
    return set()


def save_seen_messages(seen: set[str]) -> None:
    recent = sorted(seen)[-300:]
    SEEN_MESSAGES_FILE.write_text(json.dumps({"seen": recent}, indent=2))


def _write_debug_log(text: str) -> None:
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with CLAUDE_DEBUG_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
    except Exception as e:
        print(f"Could not write Teams Claude debug log: {e}", file=sys.stderr)


def _log_block(title: str, body: str) -> None:
    divider = "=" * 24
    text = body if body else "<empty>"
    block = f"\n{divider} {title} {divider}\n{text}\n"
    print(block, flush=True)
    _write_debug_log(block)


def _log_claude_request(prompt: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_debug_log(f"\n\n##### Teams Claude exchange at {ts} #####\n")
    _log_block("CLAUDE REQUEST PROMPT", prompt)


def _log_claude_response(result: subprocess.CompletedProcess[str]) -> None:
    _log_block("CLAUDE RETURN CODE", str(result.returncode))
    _log_block("CLAUDE STDOUT", result.stdout)
    _log_block("CLAUDE STDERR", result.stderr)


def message_mentions_agent(message: dict[str, Any], agent: dict[str, Any]) -> bool:
    text = (message.get("text") or "").lower()
    return any(str(m).lower() in text for m in agent.get("mentions", []))


def is_own_message(message: dict[str, Any], teams: TeamsInterface) -> bool:
    config = teams.config
    from_user_id = message.get("from_user_id")
    from_app_id = message.get("from_application_id")
    if config.self_user_id and from_user_id == config.self_user_id:
        return True
    if config.self_app_id and from_app_id == config.self_app_id:
        return True
    return False


def is_human_message(message: dict[str, Any]) -> bool:
    return bool(message.get("from_user_id"))


def should_respond_to_message(
    message: dict[str, Any],
    agent: dict[str, Any],
    teams: TeamsInterface,
    *,
    all_human: bool = False,
) -> bool:
    if is_own_message(message, teams):
        return False
    if all_human and is_human_message(message):
        return True
    return message_mentions_agent(message, agent)


def _reply_command(
    message: dict[str, Any],
    destination: TeamsDestination,
    *,
    config_path: Path = CONFIG_PATH,
) -> str:
    cli = f"python teams_interface.py --config-file {shlex.quote(str(config_path))}"
    if destination.kind == "channel":
        return f'{cli} say "message" --reply-to {shlex.quote(str(message["id"]))}'
    return f'{cli} say "message"'


def build_batch_prompt(
    agent: dict[str, Any],
    pending_messages: list[dict[str, Any]],
    destination: TeamsDestination,
) -> str:
    messages_text = ""
    for i, msg in enumerate(pending_messages, 1):
        messages_text += f"""
--- Message {i} (microsoft_teams) ---
From: {msg.get('from', 'Unknown')}
Time: {msg.get('created', 'Unknown')}
Teams message id: {msg.get('id', 'Unknown')}
Text: {msg.get('text', '')}
Post the result with: {_reply_command(msg, destination)}
"""

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return f"""You are {agent['name']} {agent['emoji']}, the {agent['role']}.

You are running as Phantom's Microsoft Teams monitor. The current time is {now}.
You have {len(pending_messages)} Teams message(s) that need your response.
Read ALL of them, do the requested work, and respond to EACH ONE in Microsoft Teams.

Use Microsoft Teams for all user-visible replies:
- Channel replies: `python teams_interface.py --config-file {CONFIG_PATH} say "message" --reply-to <message_id>`
- Chat replies: `python teams_interface.py --config-file {CONFIG_PATH} say "message"`

For EACH message, run the exact Teams command shown in that message block.
Do not just write the answer in stdout; the user only sees Microsoft Teams.
If the command fails, report the command error in stdout.

Keep Teams replies short unless the user asks for detailed output.
Do not ask for confirmation when the task is clear.

{messages_text}"""


def run_batched_response(
    agent: dict[str, Any],
    pending_messages: list[dict[str, Any]],
    destination: TeamsDestination,
) -> bool:
    if not pending_messages:
        return True

    prompt = build_batch_prompt(agent, pending_messages, destination)
    print(
        f"Sending {len(pending_messages)} Teams message(s) to Claude for batch response...",
        flush=True,
    )
    _log_claude_request(prompt)
    try:
        result = subprocess.run(
            [
                str(REPO_ROOT / "claude-wrapper.sh"),
                "--permission-mode",
                "bypassPermissions",
                "-c",
                "-p",
                prompt,
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        _log_claude_response(result)
        output = result.stdout + result.stderr
        if result.returncode != 0:
            print(
                f"Claude exited with code {result.returncode}: {output[:400]}",
                flush=True,
            )
            return False
        print(
            f"Claude processed Teams batch; debug log: {CLAUDE_DEBUG_LOG_FILE}",
            flush=True,
        )
        return True
    except subprocess.TimeoutExpired:
        print("Claude Teams batch timed out", flush=True)
        return False
    except Exception as e:
        print(f"Teams batch error: {e}", flush=True)
        return False


def collect_pending_messages(
    teams: TeamsInterface,
    agent: dict[str, Any],
    seen_messages: set[str],
    *,
    limit: int,
    all_human: bool,
) -> tuple[list[dict[str, Any]], TeamsDestination]:
    destination = teams.destination()
    messages = teams.get_messages(destination=destination, limit=limit)
    pending: list[dict[str, Any]] = []

    # Graph returns latest-first; process oldest-first for natural batching.
    for message in reversed(messages):
        message_id = str(message.get("id") or "")
        if not message_id or message_id in seen_messages:
            continue
        seen_messages.add(message_id)
        if should_respond_to_message(message, agent, teams, all_human=all_human):
            pending.append(message)

    return pending, destination


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Agent Monitor - Watch Microsoft Teams"
    )
    parser.add_argument("--agent", "-a", help="agent to run as (default: phantom)")
    parser.add_argument("--interval", "-i", type=int, default=POLL_INTERVAL)
    parser.add_argument(
        "--limit", type=int, default=30, help="messages to poll per cycle"
    )
    parser.add_argument(
        "--all-human",
        action="store_true",
        help="respond to every human message instead of only messages mentioning Phantom",
    )
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    args = parser.parse_args(argv)

    config = load_config()
    agent_id = args.agent or config.get("default_agent") or "phantom"
    agent_id = str(agent_id).lower()
    if agent_id not in AGENTS:
        print(
            f"No valid agent configured. Available agents: {', '.join(AGENTS)}",
            file=sys.stderr,
        )
        return 2
    agent = AGENTS[agent_id]

    try:
        teams = _get_teams()
        destination = teams.destination()
    except (TeamsConfigError, TeamsAPIError) as e:
        print(f"Teams monitor cannot start: {e}", file=sys.stderr)
        return 2

    print(
        f"{agent['name']} Teams monitor watching {destination.label} "
        "every "
        f"{args.interval}s; mode="
        f"{'all human messages' if args.all_human else 'mentions only'}",
        flush=True,
    )

    seen_messages = load_seen_messages()
    start_time = time.time()

    while True:
        try:
            pending, destination = collect_pending_messages(
                teams,
                agent,
                seen_messages,
                limit=args.limit,
                all_human=args.all_human,
            )
            print(f"Teams poll queued {len(pending)} message(s)", flush=True)
            if pending:
                run_batched_response(agent, pending, destination)
            save_seen_messages(seen_messages)
        except (TeamsConfigError, TeamsAPIError) as e:
            print(f"Teams poll failed: {e}", file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            save_seen_messages(seen_messages)
            print("Teams monitor stopped", flush=True)
            return 0

        if args.once:
            return 0
        if time.time() - start_time >= MAX_RUNTIME:
            print("Teams monitor reached max runtime; exiting", flush=True)
            return 0
        time.sleep(max(5, int(args.interval)))


if __name__ == "__main__":
    sys.exit(main())
