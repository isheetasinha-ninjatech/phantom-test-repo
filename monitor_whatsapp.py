#!/usr/bin/env python3
"""WhatsApp Ninja Monitor — Phantom runtime for PHANTOM_MODE=whatsapp.

Mirrors monitor.py for the WhatsApp transport:

  - Polls the local Baileys gateway (src/phantom/whatsapp/) at
    GET /messages?since={cursor}&limit=50 every N seconds.
  - Filters to the single bound chat (gateway already drops messages
    from other chats, but we double-check the channel_id here).
  - Normalizes each inbound message into the same dict shape monitor.py
    builds for Slack, then dispatches to Claude Code via
    claude-wrapper.sh. The reply hint in the prompt points Claude at
    `whatsapp_interface say --ninja-prefix`, which prepends "🥷 Ninja: "
    so the human can always tell the bot apart.
  - Cursor (last_read_seq + last_read_inbox_epoch) lives in
    ~/.agent_settings.json under the `whatsapp` sub-object, same shape
    the `whatsapp_interface.py read` CLI already uses.

This module is only activated when PHANTOM_MODE=whatsapp. The default
Slack mode is unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from phantom.whatsapp_interface import react as _gateway_react

REPO_ROOT = Path(__file__).parent
SETTINGS_PATH = Path.home() / ".agent_settings.json"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8090"

POLL_INTERVAL = 1  # seconds between polls (was 3s; lowered to cut inbound→dispatch latency)
POLL_JITTER = 0.3  # seconds of jitter
MAX_RUNTIME = 24 * 60 * 60  # 24 hours
IDLE_LOG_EVERY = 30  # seconds between idle (unbound) log lines


# ---------------------------------------------------------------------------
# settings I/O (compatible with whatsapp_interface.py)
# ---------------------------------------------------------------------------


def _read_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_whatsapp_settings(patch: dict[str, Any]) -> None:
    settings = _read_settings()
    existing = settings.get("whatsapp") if isinstance(settings.get("whatsapp"), dict) else {}
    merged = {**existing, **patch}
    settings["whatsapp"] = merged
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))


def _settings_whatsapp() -> dict[str, Any]:
    s = _read_settings().get("whatsapp")
    return s if isinstance(s, dict) else {}


# ---------------------------------------------------------------------------
# gateway HTTP client
# ---------------------------------------------------------------------------


def _gateway_url() -> str:
    return (
        os.environ.get("WHATSAPP_GATEWAY_URL")
        or _settings_whatsapp().get("gateway_url")
        or DEFAULT_GATEWAY_URL
    )


def _gateway_token() -> Optional[str]:
    return (
        os.environ.get("WHATSAPP_GATEWAY_TOKEN")
        or _settings_whatsapp().get("gateway_token")
    )


def _request(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    body: Optional[dict[str, Any]] = None,
    timeout: float = 15.0,
) -> tuple[int, bytes]:
    data = None
    headers: dict[str, str] = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        return 0, f"connection failed: {e.reason}".encode("utf-8")


def _gateway_send(base: str, token: Optional[str], chat_jid: str, text: str) -> bool:
    """Best-effort send into the bound chat (used to surface hard failures)."""
    if not chat_jid or not text:
        return False
    if chat_jid.endswith("@g.us"):
        body: dict[str, Any] = {"group_jid": chat_jid, "text": text}
    else:
        digits = chat_jid.split("@", 1)[0].split(":", 1)[0]
        if not digits.isdigit():
            return False
        body = {"to": digits, "text": text}
    status, _ = _request("POST", f"{base}/send", token=token, body=body, timeout=10.0)
    return status == 200


def _get_status(base: str, token: Optional[str]) -> Optional[dict[str, Any]]:
    status, raw = _request("GET", f"{base}/status", token=token)
    if status != 200:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _get_messages(
    base: str, token: Optional[str], since: int, limit: int
) -> Optional[dict[str, Any]]:
    url = f"{base}/messages?since={since}&limit={limit}"
    status, raw = _request("GET", url, token=token)
    if status != 200:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Cursor state
# ---------------------------------------------------------------------------


def _load_cursor() -> tuple[int, Optional[int]]:
    """Return (last_read_seq, last_read_inbox_epoch) from settings."""
    s = _settings_whatsapp()
    raw_seq = s.get("monitor_last_read_seq", s.get("last_read_seq"))
    raw_epoch = s.get("monitor_last_read_inbox_epoch", s.get("last_read_inbox_epoch"))
    seq = int(raw_seq) if isinstance(raw_seq, (int, float)) and raw_seq >= 0 else 0
    epoch = int(raw_epoch) if isinstance(raw_epoch, (int, float)) and raw_epoch > 0 else None
    return seq, epoch


def _save_cursor(seq: int, epoch: Optional[int]) -> None:
    patch: dict[str, Any] = {"monitor_last_read_seq": seq}
    if epoch is not None:
        patch["monitor_last_read_inbox_epoch"] = epoch
    _write_whatsapp_settings(patch)


# ---------------------------------------------------------------------------
# Agent dispatch
# ---------------------------------------------------------------------------


def _build_prompt(pending: list[dict[str, Any]], bound_chat_jid: str, gateway_base: str) -> str:
    """Mirror monitor.py's batched prompt for WhatsApp inbound messages.

    Reply commands point Claude at the whatsapp_interface CLI so it can
    target the same bound chat the message came from. Media blocks tell
    the agent how to fetch + handle each kind (voice → Whisper, image →
    vision, pdf → document read).
    """
    # Reply hint shared across all messages in the batch.
    if bound_chat_jid.endswith("@g.us"):
        reply_hint = (
            f'python3 -m phantom.whatsapp_interface say "<your reply>" '
            f'--ninja-prefix --group-jid {bound_chat_jid}'
        )
        upload_target = f"--group-jid {bound_chat_jid}"
    else:
        digits = "".join(c for c in bound_chat_jid.split("@")[0] if c.isdigit())
        reply_hint = (
            f'python3 -m phantom.whatsapp_interface say "<your reply>" '
            f'--ninja-prefix --to {digits}'
        )
        upload_target = f"--to {digits}"

    lines: list[str] = []
    for i, msg in enumerate(pending, 1):
        text = (msg.get("text") or "").replace("\n", " ")
        # Sender fallback chain: pushName > participant > user_id.
        sender = msg.get("sender_name") or msg.get("participant") or msg.get("user") or "unknown"

        block = [
            f"--- Message {i} (whatsapp) ---",
            f"From: {sender}",
            f"Time: {msg.get('timestamp', '')}",
            f"Text: {text}" if text else "Text: (none)",
        ]

        # Quoted / reply context — shown verbatim from the gateway (already
        # truncated at 1000 chars + ellipsis).
        quoted_text = msg.get("quoted_text")
        if quoted_text:
            qsender = msg.get("quoted_sender") or "(unknown)"
            block.append(f"↪️ Replying to {qsender}: {quoted_text}")

        # Media block: per-kind instructions. The agent fetches the bytes
        # via `whatsapp_interface fetch-media` (auth + base resolved from
        # settings/env — never embed the literal token in the prompt).
        # The freshness-per-media_id directive lives in the global header.
        kind = msg.get("media_kind")
        media_id = msg.get("media_id")
        if kind and media_id:
            fetch_cmd = (
                f'python3 -m phantom.whatsapp_interface fetch-media '
                f'--media-id {media_id} --out /tmp/wa_{media_id}'
            )
            if kind == "voice":
                seconds = msg.get("media_seconds")
                dur = f"{seconds}s" if isinstance(seconds, (int, float)) and seconds else "?"
                block.append(
                    f"🎤 Voice note ({dur}, {msg.get('media_mimetype') or '?'}).\n"
                    f"   Fetch: {fetch_cmd}\n"
                    f"   Transcribe with Whisper (LiteLLM /v1/audio/transcriptions, "
                    f"model whisper-1) and treat the transcript as the user's text."
                )
            elif kind == "image":
                block.append(
                    f"🖼 Image ({msg.get('media_mimetype') or '?'}).\n"
                    f"   Fetch: {fetch_cmd}\n"
                    f"   Read it with the vision-capable Claude model "
                    f"(send as input_image content block) and respond about what you see."
                )
            elif kind == "pdf":
                fname = msg.get("media_filename") or f"{media_id}.pdf"
                block.append(
                    f"📄 PDF document: {fname}\n"
                    f"   Fetch: {fetch_cmd}\n"
                    f"   Read the PDF (Claude document content block) and answer "
                    f"questions about its contents."
                )
            elif kind == "archive":
                fname = msg.get("media_filename") or f"{media_id}.zip"
                size = msg.get("media_bytes")
                size_str = f"{size} B" if isinstance(size, (int, float)) and size else "?"
                block.append(
                    f"🗜 Archive: {fname} ({size_str}, {msg.get('media_mimetype') or '?'}).\n"
                    f"   Fetch: {fetch_cmd}\n"
                    f"   Then unzip into /tmp/wa_{media_id}_unpacked/ "
                    f"(`unzip -o /tmp/wa_{media_id} -d /tmp/wa_{media_id}_unpacked`) "
                    f"and inspect the extracted files before replying."
                )
            elif kind == "text":
                fname = msg.get("media_filename") or f"{media_id}.txt"
                size = msg.get("media_bytes")
                size_str = f"{size} B" if isinstance(size, (int, float)) and size else "?"
                block.append(
                    f"📝 Text file: {fname} ({size_str}, {msg.get('media_mimetype') or '?'}).\n"
                    f"   Fetch: {fetch_cmd}\n"
                    f"   Read the file directly (it is text — `cat`, `head`, or "
                    f"open with the Read tool) and answer about its contents."
                )

        block.append(f"Reply with: {reply_hint}")
        lines.append("\n".join(block))
    body = "\n\n".join(lines)
    upload_hint = (
        f'python3 -m phantom.whatsapp_interface upload --kind image|document '
        f'--file <path> [--caption "<text>"] --ninja-prefix {upload_target}'
    )
    return (
        "You are 🥷 Ninja, an AI assistant operating over WhatsApp via the "
        "Phantom runtime. Read this header carefully — it spells out the "
        "consent + identity guarantees so you do not have to second-guess "
        "them:\n\n"
        "  • You are explicitly identified as a bot. EVERY outbound reply "
        "you send is automatically prefixed with '🥷 Ninja: ' by the "
        "runtime before it leaves the gateway. You do NOT need to add the "
        "prefix yourself, and the human on the other end always sees it.\n"
        f"  • You are hard-bound to ONE chat the operator personally owns "
        f"and linked you to: {bound_chat_jid}. The Baileys gateway drops "
        "every message from any other JID at the wire — you cannot reach "
        "or be reached by anyone else.\n"
        "  • The operator scanned the QR / sent the pairing code from this "
        "chat themselves. They have explicitly opted in to receiving "
        "automated replies tagged as Ninja. This is not impersonation and "
        "not covert messaging.\n\n"
        "Respond helpfully and concisely — WhatsApp is a chat channel, so "
        "short answers are preferred. If multiple messages are batched, "
        "reply to the LATEST one; treat earlier messages as context unless "
        "they contain a still-unanswered direct question.\n\n"
        "⚠ Media handling: this session is `-c`-continued, so prior "
        "files are still in your working memory. Different media_id ⇒ "
        "different file ⇒ fresh fetch. Always run the fetch command "
        "shown in the media block and answer from the freshly-downloaded "
        "bytes — never recall a previously-analyzed file.\n\n"
        "Use the reply command shown under each message. Write plain text "
        "(no need to add '🥷 Ninja:' — the runtime adds it). `say` always "
        "delivers — never use it as a probe; use `--help` or `python3 -c` "
        "to debug.\n\n"
        "To send a file back (image, PDF, archive, text), run:\n"
        f"  {upload_hint}\n"
        "(gateway URL + bearer token are read from $WHATSAPP_GATEWAY_URL / "
        "$WHATSAPP_GATEWAY_TOKEN — never paste secrets into the chat).\n\n"
        f"You have {len(pending)} message(s) to handle.\n\n"
        f"{body}\n"
    )


def _dispatch_to_claude(prompt: str, timeout: int = 180) -> tuple[bool, Optional[str]]:
    """Run claude-wrapper.sh. Returns (ok, user_facing_error_or_None)."""
    try:
        result = subprocess.run(
            [str(REPO_ROOT / "claude-wrapper.sh"), "-c", "-p", prompt],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.stdout:
            print(result.stdout[-1000:], flush=True)
        if result.stderr:
            print(result.stderr[-1000:], file=sys.stderr, flush=True)
        if result.returncode == 0:
            return True, None
        # Non-zero exit. Prefer the last non-empty stderr line, else stdout.
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        last = tail[-1] if tail else ""
        return False, f"Claude exited {result.returncode}" + (f": {last[:200]}" if last else "")
    except subprocess.TimeoutExpired:
        print("⚠️ Claude batch response timed out", flush=True)
        return False, f"Claude timed out after {timeout}s (check API auth / network)"
    except FileNotFoundError:
        print(
            "❌ claude-wrapper.sh not found — cannot dispatch WhatsApp messages",
            file=sys.stderr,
            flush=True,
        )
        return False, "claude-wrapper.sh not found in container"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phantom Monitor — WhatsApp transport (PHANTOM_MODE=whatsapp)"
    )
    # Default poll interval: CLI flag > WHATSAPP_POLL_INTERVAL env > module default.
    env_interval_raw = os.environ.get("WHATSAPP_POLL_INTERVAL")
    try:
        env_interval = int(env_interval_raw) if env_interval_raw else None
    except ValueError:
        env_interval = None
    parser.add_argument(
        "--interval",
        "-i",
        type=int,
        default=env_interval if env_interval and env_interval > 0 else POLL_INTERVAL,
        help=(
            f"Poll interval in seconds (default {POLL_INTERVAL}; "
            "override via $WHATSAPP_POLL_INTERVAL or --interval)"
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=50, help="Max messages per poll (default 50)"
    )
    parser.add_argument(
        "--include-from-me",
        action="store_true",
        help="Also dispatch messages flagged from_me (local-test only — in production"
        " Phantom must not reply to its own outbound)",
    )
    args = parser.parse_args()

    base = _gateway_url().rstrip("/")
    token = _gateway_token()
    last_seq, last_epoch = _load_cursor()

    print(
        f"\n╔══════════════════════════════════════════════════════════════╗"
        f"\n║  🥷 WhatsApp Ninja Monitor"
        f"\n╠══════════════════════════════════════════════════════════════╣"
        f"\n║  Gateway:    {base}"
        f"\n║  Polling:    every {args.interval}s (+{POLL_JITTER}s jitter)"
        f"\n║  Cursor:     seq={last_seq} epoch={last_epoch}"
        f"\n╚══════════════════════════════════════════════════════════════╝\n",
        flush=True,
    )

    start_time = time.time()
    last_idle_log = 0.0
    # On cold start (no persisted cursor) we skip whatever history Baileys
    # has already replayed into the gateway inbox — the operator does not
    # want the agent to answer messages from before the monitor was
    # actually running. This flips to False once we've fast-forwarded.
    cold_start_skip_done = last_seq != 0

    try:
        while True:
            if time.time() - start_time >= MAX_RUNTIME:
                print("⏰ Max runtime reached. Exiting.", flush=True)
                return 0

            st = _get_status(base, token)
            if not st:
                if time.time() - last_idle_log > IDLE_LOG_EVERY:
                    print(
                        f"⏳ gateway not reachable at {base}; retrying…",
                        flush=True,
                    )
                    last_idle_log = time.time()
                time.sleep(args.interval)
                continue

            bound_chat_jid = st.get("bound_chat_jid")
            bound_via = st.get("bound_via")
            ninja_state = st.get("ninja_state") or st.get("connection")
            if not bound_chat_jid:
                if time.time() - last_idle_log > IDLE_LOG_EVERY:
                    print(
                        f"⏸  not bound yet (ninja_state={ninja_state}); "
                        "show dashboard QR + pairing code to operator.",
                        flush=True,
                    )
                    last_idle_log = time.time()
                time.sleep(args.interval)
                continue

            # Cursor invalidation on epoch change (gateway restart).
            server_epoch_raw = st.get("inbox_epoch")
            server_epoch = (
                int(server_epoch_raw)
                if isinstance(server_epoch_raw, (int, float)) and server_epoch_raw > 0
                else None
            )
            if (
                last_epoch is not None
                and server_epoch is not None
                and server_epoch != last_epoch
            ):
                print(
                    f"🔄 inbox_epoch changed ({last_epoch} → {server_epoch}); "
                    "resetting cursor and re-arming cold-start skip",
                    flush=True,
                )
                last_seq = 0
                last_epoch = server_epoch
                # Re-arm cold-start skip so the backfill from the new bind
                # (or restarted gateway) doesn't get dispatched.
                cold_start_skip_done = False

            payload = _get_messages(base, token, last_seq, args.limit)
            if payload is None:
                time.sleep(args.interval)
                continue

            raw_items = payload.get("items") or []
            latest_seq = int(payload.get("latest_seq") or last_seq)

            # Cold-start fast-forward: on first successful bound poll with
            # no persisted cursor, jump straight to latest_seq so we do not
            # dispatch the entire chat history Baileys backfilled.
            if not cold_start_skip_done:
                cold_start_skip_done = True
                if latest_seq > last_seq:
                    skipped = len(raw_items)
                    last_seq = latest_seq
                    response_epoch_for_save = payload.get("inbox_epoch")
                    if (
                        isinstance(response_epoch_for_save, (int, float))
                        and response_epoch_for_save > 0
                    ):
                        last_epoch = int(response_epoch_for_save)
                    _save_cursor(last_seq, last_epoch)
                    print(
                        f"🧹 cold start — skipping {skipped} backlog message(s); "
                        f"cursor jumped to seq={last_seq}. Send a new message to test.",
                        flush=True,
                    )
                time.sleep(args.interval)
                continue
            response_epoch_raw = payload.get("inbox_epoch")
            response_epoch = (
                int(response_epoch_raw)
                if isinstance(response_epoch_raw, (int, float)) and response_epoch_raw > 0
                else None
            )

            # auto_group is self-only — every message is from_me, so treat
            # that bind type as implicit --include-from-me. The gateway's
            # recentlySent loopback filter still suppresses our own replies.
            include_from_me = args.include_from_me or bound_via == "auto_group"
            pending: list[dict[str, Any]] = []
            for it in raw_items:
                if it.get("from_me") and not include_from_me:
                    continue
                # Accept media-only messages (text may be empty for voice
                # notes and caption-less images). Drop only when there is
                # neither text nor any media payload.
                if not it.get("text") and not it.get("media_kind"):
                    continue
                # In group binds the inbound record's channel_id is
                # {self}:g:{group_local}; in DM binds it's {self}:{peer}.
                # We accept any record the gateway returns (it has already
                # been filtered by allowedChatJid).
                pending.append(
                    {
                        "user": it.get("user_id") or "unknown",
                        "text": it.get("text") or "",
                        "timestamp": it.get("ts"),
                        "seq": it.get("seq"),
                        "channel_id": it.get("channel_id"),
                        "type": "whatsapp",
                        # Baileys key bits needed to react to this exact message.
                        "message_key": it.get("message_key"),
                        "from_me": bool(it.get("from_me")),
                        "participant": it.get("participant"),
                        # Sender display + media + quoted context.
                        "sender_name": it.get("sender_name"),
                        "media_kind": it.get("media_kind"),
                        "media_id": it.get("media_id"),
                        "media_mimetype": it.get("media_mimetype"),
                        "media_seconds": it.get("media_seconds"),
                        "media_bytes": it.get("media_bytes"),
                        "media_filename": it.get("media_filename"),
                        "quoted_message_key": it.get("quoted_message_key"),
                        "quoted_text": it.get("quoted_text"),
                        "quoted_sender": it.get("quoted_sender"),
                    }
                )

            if pending:
                # Surface inbound→dispatch latency so we can spot whether
                # the wait was poll-cadence (gateway→monitor) or model-bound
                # (Claude CLI). Uses the oldest pending msg's ts as anchor.
                oldest_ts_ms = min((p.get("timestamp") or 0) for p in pending) or 0
                pending_age_s = (
                    (time.time() * 1000 - oldest_ts_ms) / 1000.0
                    if oldest_ts_ms
                    else 0.0
                )
                dispatch_started = time.time()
                print(
                    f"📨 dispatching {len(pending)} WhatsApp message(s) to Claude... "
                    f"(inbound→dispatch={pending_age_s:.1f}s)",
                    flush=True,
                )

                # Mirror Slack's ghost-ack: 👀 every inbound before dispatch,
                # swap to ✅ when claude-wrapper exits cleanly. Best-effort —
                # gateway/network errors are swallowed and never block dispatch.
                ack_emoji = os.environ.get("PHANTOM_AGENT_EMOJI", "👀").strip()
                done_emoji = os.environ.get("PHANTOM_AGENT_DONE_EMOJI", "✅").strip()
                acked: list[dict[str, Any]] = []
                if ack_emoji:
                    for msg in pending:
                        key = msg.get("message_key")
                        if not key:
                            continue
                        if _gateway_react(
                            base,
                            token,
                            key,
                            ack_emoji,
                            from_me=bool(msg.get("from_me")),
                            participant=msg.get("participant"),
                        ):
                            acked.append(
                                {
                                    "message_key": key,
                                    "from_me": bool(msg.get("from_me")),
                                    "participant": msg.get("participant"),
                                }
                            )
                    print(f"{ack_emoji} acked {len(acked)} message(s)", flush=True)

                prompt = _build_prompt(pending, bound_chat_jid, base)
                claude_started = time.time()
                ok, err_msg = _dispatch_to_claude(prompt)
                claude_elapsed = time.time() - claude_started
                total_elapsed = time.time() - dispatch_started
                print(
                    f"⏱  claude={claude_elapsed:.1f}s total={total_elapsed:.1f}s "
                    f"(inbound→reply≈{pending_age_s + total_elapsed:.1f}s)",
                    flush=True,
                )

                # Surface hard failures back into the bound chat.
                if not ok and err_msg:
                    user_text = f"🥷 Ninja: ❌ {err_msg}"
                    if not _gateway_send(base, token, bound_chat_jid, user_text):
                        print(
                            f"⚠️ failed to surface error to chat: {err_msg}",
                            file=sys.stderr,
                            flush=True,
                        )

                # Completion swap: only on clean dispatch AND when a done
                # emoji is configured. Empty PHANTOM_AGENT_DONE_EMOJI means
                # "leave 👀 in place" — never pass empty string to Baileys
                # (which would *remove* the existing reaction).
                if ok and done_emoji and acked:
                    done = 0
                    for a in acked:
                        if _gateway_react(
                            base,
                            token,
                            a["message_key"],
                            done_emoji,
                            from_me=a["from_me"],
                            participant=a["participant"],
                        ):
                            done += 1
                    if done:
                        print(f"{done_emoji} marked {done} message(s) done", flush=True)
                last_idle_log = 0.0  # reset idle log timing after activity

            # Always advance cursor up to the gateway's latest, regardless
            # of whether we had pending items (e.g. fromMe traffic).
            if raw_items and latest_seq > last_seq:
                last_seq = latest_seq
                if response_epoch is not None:
                    last_epoch = response_epoch
                _save_cursor(last_seq, last_epoch)

            time.sleep(args.interval + random.uniform(0, POLL_JITTER))

    except KeyboardInterrupt:
        print("\n👋 Monitor stopped", flush=True)
        _save_cursor(last_seq, last_epoch)
        return 0


if __name__ == "__main__":
    sys.exit(main())
