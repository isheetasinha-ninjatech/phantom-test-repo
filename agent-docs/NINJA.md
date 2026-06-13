# Ninja Mode (WhatsApp) — Operator Guide

Ninja is the WhatsApp runtime for Phantom. It replaces the Slack monitor with a
local WhatsApp gateway (Baileys) plus a poll-based monitor. The orchestrator,
dashboard, sync, and integrations services are shared across both runtimes —
only the **interaction channel** changes.

This document covers:

1. Activation
2. The mode resolver
3. QR + pairing-code flows
4. Single-chat binding semantics
5. Sending and receiving in code
6. Safety + privacy disclosure
7. Operational endpoints

---

## 1. Activation

There is exactly one switch:

```bash
# .env
PHANTOM_MODE=whatsapp
```

When the orchestrator starts, `phantom.mode.get_phantom_mode()` resolves to
`whatsapp` and:

- The orchestrator builds a WhatsApp-flavored prompt (references
  `python3 -m phantom.whatsapp_interface` instead of `slack_interface`).
- `install.sh --mode whatsapp …` installs `phantom-whatsapp-gateway.service`
  and `phantom-whatsapp-monitor.service` and disables `phantom-monitor.service`.
- The dashboard exposes a `/whatsapp` panel (QR + pairing code + bound JID).

To return to Slack: remove `PHANTOM_MODE` from `.env` (or set it to `slack`)
and re-run `install.sh` without `--mode whatsapp`. Existing Slack behavior is
untouched.

---

## 2. Mode Resolver

`phantom/mode.py` resolves `PHANTOM_MODE` in this order:

1. `PHANTOM_MODE` environment variable (loaded from `.env` by dotenv).
2. `mode` (or `whatsapp.mode`) in `~/.agent_settings.json`.
3. Default: `slack`.

Quick check:

```bash
python -m phantom.mode
# => slack | whatsapp
```

Invalid values fall through to the next layer; an entirely unrecognized config
yields `slack`.

Two additional Ninja-specific env vars control the bind step:

```bash
# Bind method when no chat is bound after QR link.
#   auto_group   — gateway creates a self-only group and binds (default)
#   pairing_code — gateway issues a 6-char code; you send it from the bound chat
WHATSAPP_BIND_METHOD=auto_group

# Subject used by the auto_group bind method.
WHATSAPP_AUTO_GROUP_NAME=🥷 Ninja
```

Dashboard dropdown overrides `WHATSAPP_BIND_METHOD` at runtime (in-memory only;
gateway restart resets to the env value).

---

## 3. Pairing

There are three ways to bind Ninja to a WhatsApp chat:

### 3a. QR (initial device link)

The gateway prints/exposes a QR PNG at `GET /qr`. Scan it from your phone
(WhatsApp → Settings → Linked Devices → Link a Device). After scanning, the
session reaches `connection=open`.

The gateway then moves to `awaiting_bind_method`. A 5-second grace window
lets the operator confirm the bind method via the dashboard dropdown
(`POST /bind_method` resets the timer). After the window the chosen method
executes:

- `auto_group` → see 3b
- `pairing_code` → see 3c

On first boot (no `last_active_method`) there is **no automatic timer** —
the operator must pick a method via dropdown / `POST /bind_method` before
the flow proceeds. This prevents surprise group creation after install-seeded
binds.

### 3b. Auto-group (default, no chat required)

When `WHATSAPP_BIND_METHOD=auto_group` (or unset), the gateway calls
Baileys' `groupCreate(subject, [selfJid])`, creating a self-only group
with the operator as owner. The group's JID is persisted in `bound.json`
with `bound_via: "auto_group"`, and the dashboard surfaces the invite link
(`https://chat.whatsapp.com/<invite_code>`) so the operator can join the
group from the same phone.

The auto-bind path bypasses `WHATSAPP_ALLOW_GROUP_CREATE`; that flag only
gates the manual `POST /groups/create` HTTP route.

On `groupCreate` failure, the gateway transitions to `auto_group_failed`,
surfaces `last_error` in `/status` and the dashboard, and waits for an
operator-driven `POST /retry_bind` (or a dropdown switch to `pairing_code`).
There is no automatic retry and no fallback.

### 3c. Pairing code (chat binding)

Fetch the code:

```bash
curl -H "Authorization: Bearer $WHATSAPP_GATEWAY_TOKEN" \
     http://127.0.0.1:8090/pairing_code
# { "code": "QK7H4P", "expires_at": 1717..., "issued_at": ... }
```

Or use the dashboard `/whatsapp` panel ("Get pairing code"). Then send that
code as a normal WhatsApp message **from the chat you want to bind**. The
gateway accepts the code whether it was sent by you (the linked account) or
another participant — the natural operator workflow is to send it from the
same phone Ninja is linked to.

On match, the gateway:

1. Sets `bound.json` to that chat JID with `bound_via: "pairing_code"`.
2. Clears the active code.
3. Bumps the `pairing_bind_ok` counter.
4. Sends a one-time confirmation: `🥷 Ninja: bound. From now on I only listen here.`
5. **Consumes the pairing message** — it never lands in the inbox.

The code body is **never** logged. Only `pairing_code_issued` /
`pairing_code_cleared` events plus the code length appear in the gateway log.

Alternative: pre-seed `bound.json` at install time via
`install.sh --mode whatsapp --chat-jid '120363…@g.us'`, or POST `/bind` at
runtime.

---

## 4. Single-Chat Binding

Ninja is **strictly single-chat**. After binding:

- Any message from a `remoteJid` other than the bound chat is dropped at the
  gateway and counted as `dropped_not_bound_chat`.
- `POST /unbind` (or the dashboard "Unbind" button) clears the binding and
  re-issues a pairing code so the operator can rebind without re-scanning the
  QR.
- The bound chat persists in `whatsapp/auth/bound.json` and survives restarts.

JID forms:

- Group chats: `120363xxxxxxxxxxxxxx@g.us`
- Direct messages: `15551112222@s.whatsapp.net`

Both are valid `--chat-jid` values.

---

## 5. Sending and Receiving

The Python CLI (`python3 -m phantom.whatsapp_interface`) is the canonical
interface. Important verbs:

```bash
# Read new messages (cursor-based, persists in ~/.agent_settings.json)
python3 -m phantom.whatsapp_interface read --json --limit 20

# Reply into a group
python3 -m phantom.whatsapp_interface say "your reply" \
    --group-jid 120363...@g.us

# Reply into a DM
python3 -m phantom.whatsapp_interface say "your reply" \
    --to 15551112222@s.whatsapp.net

# Status / bind / unbind
python3 -m phantom.whatsapp_interface bind --status --json
python3 -m phantom.whatsapp_interface bind --chat-jid 120363...@g.us
python3 -m phantom.whatsapp_interface unbind
```

The `phantom-whatsapp-monitor.service` (Python `monitor_whatsapp.py`) polls
`/messages?since=<seq>&limit=50` every ~3 s, filters by `bound_chat_jid`, and
dispatches each batch to `claude-wrapper.sh` with a built-in prompt that
includes a reply hint referencing the JID forms above.

**Outbound prefix.** Every Ninja reply is prefixed with `🥷 Ninja: ` by the
monitor (not the gateway). This keeps the gateway CLI literal — `say "hi"`
sends `hi`, not `🥷 Ninja: hi`.

### Reactions (ack + completion)

The monitor reacts to every inbound message in the bound chat with 👀 before
dispatching to Claude, and swaps it to ✅ when the dispatch completes
successfully. On failure (timeout, non-zero exit, etc.) the 👀 stays in place
— mirrors Slack's silent-on-failure semantic. Override with:

- `PHANTOM_AGENT_EMOJI` (default `👀`) — reused from Slack mode; Slack
  defaults to `👻` when unset, WhatsApp to `👀`.
- `PHANTOM_AGENT_DONE_EMOJI` (default `✅`) — set to empty string to skip the
  completion swap (👀 stays). Slack mode ignores this variable.

Reaction sends count toward the personal-account outbound budget — one per
inbound, one more on completion. At single-chat ninja volumes this is
negligible; revisit if a batch reaches dozens of messages per poll.

### Media (voice / image / pdf)

Inbound media is decrypted by the gateway (Baileys mediaKey → bytes), written
0600 to `WHATSAPP_MEDIA_DIR` (default `/tmp/phantom-wa-media`) under a
sha256-keyed filename, and surfaced on each inbox record:

```jsonc
{
  "text": "<caption or empty>",
  "media_kind": "voice|image|pdf",
  "media_id": "a1b2c3...",         // sha256[:16]; pass to /media/:id
  "media_mimetype": "audio/ogg",
  "media_seconds": 12,             // voice only
  "media_bytes": 41281,
  "media_filename": "report.pdf",  // pdf only
  "sender_name": "Alex",           // Baileys pushName (display-only)
  "quoted_message_key": "...:3EB0...",
  "quoted_text": "earlier line, truncated at 1000 chars + ellipsis…",
  "quoted_sender": "1555@s.whatsapp.net"
}
```

The monitor builds per-kind prompt blocks (🎤 voice → Whisper, 🖼 image →
vision, 📄 pdf → document) that point Claude at:

```bash
# Fetch decrypted bytes (gateway URL + bearer auth from env/settings — no
# secrets are embedded in the prompt itself).
python3 -m phantom.whatsapp_interface fetch-media --media-id <id> --out /tmp/wa_<id>

# Upload a local file as image or document (synthesizes a "🥷 Ninja: shared a/an …"
# caption when --ninja-prefix is set and no caption was supplied).
python3 -m phantom.whatsapp_interface upload --kind image --file /tmp/x.png \
    --caption "<text>" --ninja-prefix --group-jid 120363…@g.us
```

Limits + lifecycle:

- `WHATSAPP_MAX_AUDIO_SECONDS` (default 600) — voice notes are pre-flight
  rejected before decrypt; the agent sees `[voice note too long]` as text.
- `WHATSAPP_MAX_MEDIA_BYTES` (default 64 MiB) — applies to every kind.
- `WHATSAPP_MEDIA_TTL_SECONDS` (default 3600) — in-process sweeper unlinks
  files past TTL. There is **no** persistence across gateway restarts.
- History-sync media is dropped entirely (cold-start filter at the monitor
  already skips backlog; we don't want placeholder records bypassing it).
- Outbound media is registered in the loopback ring by `message_id` so the
  fromMe notify upsert for our own send is recognised and skipped, even when
  the caption is empty.

Unsupported kinds (video, sticker, non-PDF document) are counted under
`media_skipped_total[<kind>]` and ignored.

---

## 6. Safety + Privacy

WhatsApp linked-device automation against a personal account can trigger
rate limits or bans. Operate with these constraints:

- **Single-chat only.** The hard filter at the gateway prevents accidental
  messages to other contacts.
- **Outbound prefix.** Every Ninja reply is visibly tagged so participants
  know they're talking to an automated agent.
- **No body leakage.** Pairing-code messages are consumed at the gateway and
  never logged or persisted. The code itself is logged as length-only.
- **Loopback gateway.** The gateway binds to `127.0.0.1` only. All endpoints
  except `/health` require `Authorization: Bearer $WHATSAPP_GATEWAY_TOKEN`.
- **Dedicated number recommended.** Prefer a dedicated phone + eSIM for
  Ninja's WhatsApp account rather than a personal number.

The dashboard `/whatsapp` panel surfaces a short safety disclosure to remind
operators of these constraints.

---

## 7. Operational Endpoints (Gateway)

All loopback, all require the bearer token unless noted:

| Method | Path                | Purpose                                        |
|--------|---------------------|------------------------------------------------|
| GET    | `/health`           | Liveness (no auth).                            |
| GET    | `/status`           | Connection state, bound JID, ninja_state, etc. |
| GET    | `/qr`               | QR PNG (binary).                               |
| GET    | `/pairing_code`     | Current 6-char code + expiry (read-only).      |
| POST   | `/bind`             | `{ "chat_jid": "...@g.us" }` — manual bind.    |
| POST   | `/unbind`           | Drop binding, hand off to bind flow.           |
| GET    | `/bind_method`      | Active method + source + grace window + override snapshot. |
| POST   | `/bind_method`      | `{ "method": "auto_group" \| "pairing_code" }` — sets in-memory override. 409 if bound or `bind_in_progress`. |
| POST   | `/retry_bind`       | Re-fire from `auto_group_failed`. 409 otherwise. |
| GET    | `/messages`         | `?since=<seq>&limit=N` — ring buffer cursor.   |
| POST   | `/send_text`        | Outbound message (used by `say`).              |
| POST   | `/react`            | `{ message_key, emoji, from_me?, participant? }` — set/replace bot reaction. |
| GET    | `/media/:id`        | Decrypted media bytes (Content-Type from inbox).|
| POST   | `/send_media`       | Multipart upload (`file`, `kind`, `caption`, …). |

`/status` adds these fields in Ninja mode:

```jsonc
{
  "bound_chat_jid": "120363...@g.us",   // or null
  "bound_via": "auto_group",             // or "pairing_code" | "api" | "install"
  "bound_at": "2026-06-09T12:34:56Z",
  "ninja_state": "bound",                // see state set below
  "pairing_code_active": false,
  "pairing_code_expires_at": null,
  "bind_method": "auto_group",          // resolved active method
  "bind_method_source": "env",          // "override" | "env" | "default"
  "last_active_method": "auto_group",   // null after api/install binds
  "last_error": null,                    // populated only in auto_group_failed
  "invite_code": "GfH4...",              // populated when bound_via=auto_group
  "invite_code_error": null,             // populated when invite-code fetch failed
  "auto_group_name": "🥷 Ninja",
  "grace_remaining_ms": 0
}
```

`ninja_state` ∈ `{starting, waiting_for_qr, connected, awaiting_bind_method,
waiting_for_chat_pairing, creating_group, auto_group_failed, bound,
reconnecting, logged_out}`.

Event counters worth watching in the gateway log:

- `pairing_code_issued` / `pairing_code_cleared` (length only)
- `pairing_bind_ok`
- `dropped_not_bound_chat`
- `auto_group_create_ok` / `auto_group_create_err`
- `bind_method_override_set` / `retry_bind_requested`
- `invite_code_fetch_err`
- `media_ok_by_kind` / `media_decrypt_err` / `media_too_long` / `media_too_large`
- `media_history_skip` / `media_skipped_total` (unsupported kinds)
- `media_sent_ok` / `media_sent_err` / `dropped_send_media_not_bound_chat`

---

## TL;DR

```bash
# 1. Toggle the mode
echo "PHANTOM_MODE=whatsapp" >> .env

# 2. Install (the new units)
./install.sh --mode whatsapp --channel "Ops Bridge" \
    --chat-jid "120363xxxxxxxxxxxxxx@g.us"

# 3. Open the panel
open http://localhost:9000/whatsapp

# 4. Read, reply, audit
python3 -m phantom.whatsapp_interface read --json
python3 -m phantom.whatsapp_interface say "ack" --group-jid "120363...@g.us"
journalctl -u phantom-whatsapp-gateway -f
journalctl -u phantom-whatsapp-monitor -f
```
