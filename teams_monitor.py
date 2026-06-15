#!/usr/bin/env python3
"""Agent Monitor - Watches Microsoft Teams for Phantom tasks (POC).

This is intentionally smaller than the Teams monitor. It polls Microsoft Graph
for recent channel/chat messages, queues messages that mention the configured
agent, and invokes the same Claude wrapper that the Teams monitor uses.

Usage:
    python teams_monitor.py --interval 60
    python -m phantom.teams_monitor --once
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
        reaction_type_from_text,
    )
except ImportError:  # pragma: no cover - supports direct script execution
    from agents_config import AGENTS
    from teams_interface import (
        TeamsAPIError,
        TeamsConfigError,
        TeamsDestination,
        TeamsInterface,
        reaction_type_from_text,
    )


REPO_ROOT = Path(__file__).parent
CONFIG_PATH = Path.home() / ".agent_settings.json"
SEEN_MESSAGES_FILE = REPO_ROOT / ".teams_seen_messages.json"
LOG_DIR = REPO_ROOT / "logs"
CLAUDE_DEBUG_LOG_FILE = LOG_DIR / "teams_monitor_claude.log"
POLL_INTERVAL = 60
MAX_RUNTIME = 24 * 60 * 60

_teams_instance: Optional[TeamsInterface] = None
ANSI_PATTERNS = (
    re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]"),
    re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)"),
    re.compile(r"\x1b[\(\)][A-Za-z0-9]"),
    re.compile(r"\x1b[78]"),
    re.compile(r"\x1b[ -/]*[@-~]"),
)
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


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


def load_monitor_state() -> tuple[set[str], list[str]]:
    try:
        if SEEN_MESSAGES_FILE.exists():
            data = json.loads(SEEN_MESSAGES_FILE.read_text())
            seen = data.get("seen") if isinstance(data, dict) else []
            threads = data.get("threads") if isinstance(data, dict) else []
            return {str(x) for x in seen}, [str(x) for x in threads]
    except Exception:
        pass
    return set(), []


def load_seen_messages() -> set[str]:
    seen, _ = load_monitor_state()
    return seen


def save_monitor_state(seen: set[str], threads: list[str]) -> None:
    recent = sorted(seen)[-300:]
    recent_threads = threads[-100:]
    SEEN_MESSAGES_FILE.write_text(
        json.dumps({"seen": recent, "threads": recent_threads}, indent=2)
    )


def save_seen_messages(seen: set[str]) -> None:
    _, threads = load_monitor_state()
    save_monitor_state(seen, threads)


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


def _attachment_context(message: dict[str, Any]) -> str:
    raw = message.get("raw") if isinstance(message.get("raw"), dict) else {}
    if not isinstance(raw, dict):
        return ""

    entries: list[str] = []
    seen: set[tuple[str, str]] = set()
    for field in ("attachments", "files"):
        items = raw.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(
                item.get("name")
                or item.get("title")
                or item.get("contentType")
                or "attachment"
            ).strip()
            url = str(
                item.get("webUrl")
                or item.get("contentUrl")
                or item.get("thumbnailUrl")
                or ""
            ).strip()
            key = (name, url)
            if key in seen:
                continue
            seen.add(key)
            entries.append(f"- {name}{': ' + url if url else ''}")

    if not entries:
        return ""

    return "\nAttachments:\n" + "\n".join(entries)


def _reaction_emoji(message: dict[str, Any]) -> str:
    raw = message.get("raw") if isinstance(message.get("raw"), dict) else {}
    if not isinstance(raw, dict):
        return "✅"

    audio_suffixes = {
        ".mp3",
        ".wav",
        ".m4a",
        ".aac",
        ".ogg",
        ".oga",
        ".webm",
        ".mp4",
        ".mpeg",
    }

    for field in ("attachments", "files"):
        items = raw.get(field)
        if not isinstance(items, list):
            continue
        if not items:
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            content_type = str(item.get("contentType") or item.get("mimetype") or "")
            if content_type.lower().startswith("audio/"):
                return "🎵"
            name = str(item.get("name") or item.get("title") or "")
            url = str(item.get("webUrl") or item.get("contentUrl") or "")
            suffix = (Path(name).suffix or Path(url).suffix).lower()
            if suffix in audio_suffixes:
                return "🎵"
        return "✅"

    return "✅"


def _reply_command(
    message: dict[str, Any],
    destination: TeamsDestination,
    *,
    config_path: Path = CONFIG_PATH,
) -> str:
    cli = f"python teams_interface.py --config-file {shlex.quote(str(config_path))}"
    if destination.kind == "channel":
        reply_to = str(message.get("reply_to_id") or message["id"])
        return f'{cli} say "message" --reply-to {shlex.quote(reply_to)}'
    return f'{cli} say "message"'


def build_response_prompt(
    agent: dict[str, Any],
    message: dict[str, Any],
    destination: TeamsDestination,
) -> str:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    thread_note = ""
    if destination.kind == "channel":
        thread_note = (
            "\nThe Python monitor will post your final response to Teams "
            f"thread root id {message.get('reply_to_id') or message.get('id')}."
        )
    return f"""You are {agent['name']} {agent['emoji']}, the {agent['role']}.

You are running as Phantom's Microsoft Teams monitor. The current time is {now}.
Read this Microsoft Teams message, do the requested work, and return ONLY the text
that should be posted back to Teams.

Do not call teams_interface.py and do not post to Teams yourself.{thread_note}
Keep the response short unless the user asks for detailed output.
Do not ask for confirmation when the task is clear.

From: {message.get('from', 'Unknown')}
Time: {message.get('created', 'Unknown')}
Teams message id: {message.get('id', 'Unknown')}
Teams thread root id: {message.get('reply_to_id') or message.get('id', 'Unknown')}
Text: {message.get('text', '')}
{_attachment_context(message)}
"""


def strip_terminal_control(text: str) -> str:
    cleaned = text or ""
    for pattern in ANSI_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return CONTROL_CHARS_RE.sub("", cleaned)


def _clean_claude_text(text: str) -> str:
    cleaned = strip_terminal_control(text).strip()
    return cleaned or "I handled that, but did not get a response body to post."


def _mark_sent_message_seen(
    seen_messages: set[str],
    sent: dict[str, Any],
    destination: TeamsDestination,
    reply_to_id: Optional[str],
) -> None:
    sent_id = str(sent.get("id") or "")
    if not sent_id:
        return
    if destination.kind == "channel" and reply_to_id:
        seen_messages.add(f"reply:{reply_to_id}:{sent_id}")
    else:
        seen_messages.add(sent_id)


def _is_reaction_request(message: dict[str, Any]) -> bool:
    text = (message.get("text") or "").lower()
    return bool(
        re.search(r"\breact(?:ion)?\b", text)
        and re.search(r"\b(last|previous|prior|above)\b", text)
    )


def _requested_reaction_emoji(
    message: dict[str, Any],
    target_message: Optional[dict[str, Any]] = None,
) -> str:
    text = message.get("text") or ""
    # Prefer any literal non-ASCII symbol the user provided, keeping this tiny
    # and dependency-free rather than trying to maintain a shortcode table.
    for char in text:
        if ord(char) > 0x2600:
            return char
    requested = reaction_type_from_text(text)
    if requested:
        return requested
    if "audio" in text.lower() or "voice" in text.lower():
        return "🎧"
    if target_message:
        return _reaction_emoji(target_message)
    return "✅"


def _reaction_target(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    target = message.get("previous_message")
    return target if isinstance(target, dict) and target.get("id") else None


def _sender_key(message: dict[str, Any]) -> Optional[str]:
    user_id = str(message.get("from_user_id") or "").strip()
    if user_id:
        return f"user:{user_id}"
    sender = str(message.get("from") or "").strip().lower()
    if sender and sender != "unknown":
        return f"name:{sender}"
    return None


def _wants_own_previous_message(message: dict[str, Any]) -> bool:
    text = (message.get("text") or "").lower()
    return bool(re.search(r"\bmy\s+(?:last|previous|prior)\s+message\b", text))


def _choose_previous_message(
    message: dict[str, Any],
    *,
    previous_any: Optional[dict[str, Any]],
    previous_human: Optional[dict[str, Any]],
    previous_by_sender: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if _wants_own_previous_message(message):
        key = _sender_key(message)
        if key and previous_by_sender.get(key):
            return previous_by_sender[key]
    return previous_human or previous_any


def run_batched_response(
    teams: TeamsInterface,
    agent: dict[str, Any],
    pending_messages: list[dict[str, Any]],
    destination: TeamsDestination,
    seen_messages: set[str],
) -> bool:
    if not pending_messages:
        return True

    print(
        f"Processing {len(pending_messages)} Teams message(s)...",
        flush=True,
    )
    ok = True
    for i, message in enumerate(pending_messages, 1):
        try:
            reply_to_id = (
                str(message.get("reply_to_id") or message.get("id") or "")
                if destination.kind == "channel"
                else None
            )

            if _is_reaction_request(message):
                target = _reaction_target(message)
                if not target:
                    print(
                        "Teams reaction target not found; skipping Claude fallback",
                        flush=True,
                    )
                    ok = False
                    continue

                reaction_type = _requested_reaction_emoji(message, target)
                target_reply_to_id = (
                    str(target.get("reply_to_id") or target.get("parent_message_id") or "")
                    or None
                )
                teams.react(
                    str(target["id"]),
                    reaction_type=reaction_type,
                    reply_to_id=target_reply_to_id,
                    destination=destination,
                )
                print(
                    f"Reacted to Teams message {target['id']} with {reaction_type}",
                    flush=True,
                )
                continue

            prompt = build_response_prompt(agent, message, destination)
            _log_claude_request(prompt)
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
                ok = False
                continue

            response_text = _clean_claude_text(result.stdout)
            sent = teams.say(
                response_text,
                destination=destination,
                reply_to=reply_to_id,
            )
            _log_block("TEAMS SEND RESPONSE", json.dumps(sent, indent=2))
            _mark_sent_message_seen(seen_messages, sent, destination, reply_to_id)
            message_id = str(message.get("id") or "")
            if message_id:
                try:
                    emoji_str = _reaction_emoji(message)
                    teams.react(
                        message_id,
                        reaction_type=emoji_str,
                        reply_to_id=str(message.get("reply_to_id") or "") or None,
                        destination=destination,
                    )
                    print(f"Teams reaction posted for message {message_id}", flush=True)
                except Exception as e:
                    print(f"Teams reaction error on message {message_id}: {e}", flush=True)
            print(
                f"Posted Teams response {i}/{len(pending_messages)} "
                f"to {reply_to_id or destination.key}",
                flush=True,
            )
        except subprocess.TimeoutExpired:
            print("Claude Teams response timed out", flush=True)
            ok = False
        except Exception as e:
            print(f"Teams response error: {e}", flush=True)
            ok = False

    print(f"Teams batch debug log: {CLAUDE_DEBUG_LOG_FILE}", flush=True)
    return ok


def collect_pending_messages(
    teams: TeamsInterface,
    agent: dict[str, Any],
    seen_messages: set[str],
    watched_threads: Optional[list[str]] = None,
    *,
    limit: int,
    thread_limit: int,
    all_human: bool,
) -> tuple[list[dict[str, Any]], TeamsDestination]:
    destination = teams.destination()
    messages = teams.get_messages(destination=destination, limit=limit)
    pending: list[dict[str, Any]] = []
    watched_threads = watched_threads if watched_threads is not None else []

    def remember_thread(message_id: str) -> None:
        if not message_id:
            return
        if message_id in watched_threads:
            watched_threads.remove(message_id)
        watched_threads.append(message_id)

    parent_by_id = {str(message.get("id") or ""): message for message in messages}

    def remember_previous_candidate(
        message: dict[str, Any],
        *,
        previous_by_sender: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
        previous_human = None
        if is_human_message(message) and not is_own_message(message, teams):
            previous_human = message
            sender_key = _sender_key(message)
            if sender_key:
                previous_by_sender[sender_key] = message
        return message, previous_human

    # Graph returns latest-first; process oldest-first for natural batching.
    previous_message: Optional[dict[str, Any]] = None
    previous_human_message: Optional[dict[str, Any]] = None
    previous_by_sender: dict[str, dict[str, Any]] = {}
    for message in reversed(messages):
        message_id = str(message.get("id") or "")
        if not message_id or message_id in seen_messages:
            if message_id:
                previous_message, candidate_human = remember_previous_candidate(
                    message,
                    previous_by_sender=previous_by_sender,
                )
                previous_human_message = candidate_human or previous_human_message
            continue
        remember_thread(message_id)
        seen_messages.add(message_id)
        if should_respond_to_message(message, agent, teams, all_human=all_human):
            message["reply_to_id"] = message_id
            target = _choose_previous_message(
                message,
                previous_any=previous_message,
                previous_human=previous_human_message,
                previous_by_sender=previous_by_sender,
            )
            if target:
                message["previous_message"] = target
            pending.append(message)
        previous_message, candidate_human = remember_previous_candidate(
            message,
            previous_by_sender=previous_by_sender,
        )
        previous_human_message = candidate_human or previous_human_message

    if destination.kind == "channel" and thread_limit > 0:
        for message in messages:
            remember_thread(str(message.get("id") or ""))

        for parent_id in list(reversed(watched_threads[-thread_limit:])):
            replies = teams.get_replies(parent_id, destination=destination, limit=20)
            previous_reply: Optional[dict[str, Any]] = parent_by_id.get(parent_id)
            previous_reply_human: Optional[dict[str, Any]] = None
            previous_reply_by_sender: dict[str, dict[str, Any]] = {}
            if previous_reply:
                _, previous_reply_human = remember_previous_candidate(
                    previous_reply,
                    previous_by_sender=previous_reply_by_sender,
                )
            for reply in reversed(replies):
                reply_id = str(reply.get("id") or "")
                seen_id = f"reply:{parent_id}:{reply_id}"
                if not reply_id or seen_id in seen_messages:
                    if reply_id:
                        reply["reply_to_id"] = parent_id
                        reply["parent_message_id"] = parent_id
                        previous_reply, candidate_human = remember_previous_candidate(
                            reply,
                            previous_by_sender=previous_reply_by_sender,
                        )
                        previous_reply_human = candidate_human or previous_reply_human
                    continue
                seen_messages.add(seen_id)
                reply["reply_to_id"] = parent_id
                reply["parent_message_id"] = parent_id
                if should_respond_to_message(reply, agent, teams, all_human=all_human):
                    target = _choose_previous_message(
                        reply,
                        previous_any=previous_reply,
                        previous_human=previous_reply_human,
                        previous_by_sender=previous_reply_by_sender,
                    )
                    if target:
                        reply["previous_message"] = target
                    pending.append(reply)
                previous_reply, candidate_human = remember_previous_candidate(
                    reply,
                    previous_by_sender=previous_reply_by_sender,
                )
                previous_reply_human = candidate_human or previous_reply_human

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
        "--thread-limit",
        type=int,
        default=10,
        help="recent Teams channel threads to scan for replies per cycle",
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

    seen_messages, watched_threads = load_monitor_state()
    start_time = time.time()

    while True:
        try:
            pending, destination = collect_pending_messages(
                teams,
                agent,
                seen_messages,
                watched_threads,
                limit=args.limit,
                thread_limit=args.thread_limit,
                all_human=args.all_human,
            )
            print(f"Teams poll queued {len(pending)} message(s)", flush=True)
            if pending:
                run_batched_response(teams, agent, pending, destination, seen_messages)
            save_monitor_state(seen_messages, watched_threads)
        except (TeamsConfigError, TeamsAPIError) as e:
            print(f"Teams poll failed: {e}", file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            save_monitor_state(seen_messages, watched_threads)
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