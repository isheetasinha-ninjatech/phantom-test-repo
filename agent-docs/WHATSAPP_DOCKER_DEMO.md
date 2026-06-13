# WhatsApp Ninja — Docker Demo

End-to-end walkthrough for running Phantom in **WhatsApp mode** via
`docker compose`, starting from a fresh checkout. Goal: send a WhatsApp
message in your bound chat, get a reply from Claude.

> For the broader operator model (modes, single-chat binding, safety
> contract), see `NINJA.md`. This doc is the Docker-specific runbook.

---

## Prerequisites

- Docker Desktop running
- A WhatsApp account on a phone you can scan a QR with (dedicated test
  number recommended)
- Anthropic / LiteLLM token

## 1. Configure `.env`

```bash
cp .env.example .env
```

Required values for WhatsApp mode (append to / uncomment in `.env`):

```bash
# Switch to WhatsApp
PHANTOM_MODE=whatsapp
WHATSAPP_GATEWAY_TOKEN=$(openssl rand -hex 16)   # paste the actual value

# Claude / LiteLLM
ANTHROPIC_AUTH_TOKEN=sk-...
ANTHROPIC_BASE_URL=https://model-gateway.beta.myninja.ai
ANTHROPIC_MODEL=claude-opus-4-8
```

`NINJA_USER_ID` and the Slack-token block are **not required** in
WhatsApp mode — the entrypoint skips Slack token fetching when
`PHANTOM_MODE=whatsapp`.

## 2. Prepare host directories

```bash
mkdir -p ./data/whatsapp/auth ./logs
```

- `./data/whatsapp/auth/` → Baileys session creds + `bound.json`.
  Persists across `docker compose down`/`up`, so you only pair once.
- `./logs/` → supervisord per-program logs.

## 3. Boot

```bash
docker compose up -d --build
docker compose logs -f phantom
```

First boot installs Node deps into the named volume
`whatsapp_node_modules` (~30-60 s) and compiles the TypeScript gateway.
Subsequent boots reuse the volume.

Wait for the WhatsApp programs to come up:

```bash
docker compose exec phantom supervisorctl status
# whatsapp_gateway          RUNNING ...
# phantom_monitor_whatsapp  RUNNING ...
# phantom_monitor           STOPPED (slack mode is off — expected)
```

## 4. Create a single-member test group

On your phone, in WhatsApp:

1. New chat → **New group**
2. Name it (e.g. `POC Group`)
3. Add yourself only — no other contacts
4. Send any message into the empty group to make it appear in your chat list

This is your bound chat. Phantom's gateway hard-drops every message
from any other JID, so this group is the only thing the bot ever sees.

## 5. Pair

Open the dashboard:

```bash
open http://localhost:9010/whatsapp
```

1. **Scan the QR PNG** from your phone (WhatsApp → Settings → Linked
   Devices → Link a Device).
2. After scan, the panel should show `connection: open`, then
   `ninja_state: waiting_for_chat_pairing`.
3. Click **Get pairing code** — copy the 6-char alphanumeric code.
4. **In `POC Group` on your phone**, send that code as a normal
   message.

The gateway consumes the pairing message (never lands in inbox), saves
the binding to `./data/whatsapp/auth/bound.json`, and posts a
one-time confirmation: `🥷 Ninja: bound. From now on I only listen here.`

Panel now shows `ninja_state: bound` and `bound_chat_jid: 120363…@g.us`.

## 6. Chat with Phantom

Send any message into `POC Group`. With the default 2-second poll, you
should see a reply within ~5-30 s (poll + Claude inference).

Watch the monitor in another terminal:

```bash
docker compose exec phantom tail -f /var/log/supervisor/phantom_monitor_whatsapp.out.log
```

You'll see lines like:

```
📨 dispatching 1 WhatsApp message(s) to Claude...
<Claude's response logged here>
```

The reply lands in the group prefixed with `🥷 Ninja:` so it's clear
which messages are from the bot.

### Cold start: history is skipped

On first boot after pairing, Baileys replays your full chat history
into the gateway's inbox. The monitor's cold-start path **fast-forwards
the cursor past this backlog** — so Phantom does NOT answer historical
messages, only ones you send after the monitor is running. You'll see:

```
🧹 cold start — skipping N backlog message(s); cursor jumped to seq=N. Send a new message to test.
```

### Single-account dev mode

The Docker monitor runs with `--include-from-me`, because in a
typical local dev setup you're sending from the same phone that's
linked to the bot, so every inbound is flagged `from_me`. The gateway's
loopback guard (in `wa-inbound.ts`) still prevents the monitor's *own*
outbound replies from being re-ingested. Remove this flag in
`docker/supervisord.local.conf` if you bind to a chat with multiple
real participants.

## 7. Operational endpoints (loopback inside the container)

All require `Authorization: Bearer $WHATSAPP_GATEWAY_TOKEN`:

```bash
# Status
docker compose exec phantom curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8090/status | jq

# Recent inbox
docker compose exec phantom curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8090/messages?since=0&limit=10" | jq

# Manual send (mirrors what Claude does via whatsapp_interface)
docker compose exec phantom curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"group_jid":"120363…@g.us","text":"hi"}' \
  http://127.0.0.1:8090/send
```

Port `8090` is also bound on the host for off-container debugging.

## 8. Adjust polling

Default poll interval is **2 s** (+1 s jitter), set via
`WHATSAPP_POLL_INTERVAL=2` in `docker/supervisord.local.conf`. To change
without rebuilding:

```bash
# edit docker/supervisord.local.conf, change WHATSAPP_POLL_INTERVAL
docker cp docker/supervisord.local.conf phantom:/docker/supervisord.local.conf
docker compose exec phantom supervisorctl reread
docker compose exec phantom supervisorctl update
```

Resolution order: `--interval` CLI flag > `$WHATSAPP_POLL_INTERVAL` >
module default (3).

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Container exits at boot with `WHATSAPP_GATEWAY_TOKEN is not set` | `.env` missing the token | Set `WHATSAPP_GATEWAY_TOKEN` and `up -d` again |
| Dashboard `/whatsapp` shows `waiting_for_qr` after scan | Stream error 515 (normal) | Wait 5 s for auto-reconnect; rescan only if it persists |
| Panel `bound` but pairing code never matched | You sent the code in a DM, not in the chat you wanted bound; the bind picked up the `@lid` JID | `POST /unbind`, send code again from the right chat |
| `bound_chat_jid` ends in `@lid` instead of `@g.us` | Same as above | `POST /unbind` then `POST /bind` with the real group JID from `/groups` |
| Monitor logs `dispatching` but Claude refuses with impersonation language | Stale Claude session reading historical messages, or prompt got truncated | Restart monitor: `docker compose exec phantom supervisorctl restart phantom_monitor_whatsapp` |
| Self-replies loop (Phantom answers its own messages) | Gateway's `wa-inbound.ts` loopback guard not built | `docker compose exec phantom bash -c "cd src/phantom/whatsapp && npx tsc -p ."` then `supervisorctl restart whatsapp_gateway` |
| Gateway restart fails with `EADDRINUSE` | Orphan Node process holding 8090 | `docker compose exec phantom bash -c "pkill -f dist/server.js"` then `supervisorctl start whatsapp_gateway` |
| Backlog never cleared / monitor dispatches old messages on restart | Persisted cursor in `~/.agent_settings.json` | Clear it: `docker compose exec phantom python3 -c "import json,pathlib; p=pathlib.Path('/root/.agent_settings.json'); d=json.loads(p.read_text()); d['whatsapp']={}; p.write_text(json.dumps(d))"` then restart monitor |

Logs to check (inside container):

```
/var/log/supervisor/whatsapp_gateway.out.log     (JSON Pino)
/var/log/supervisor/whatsapp_gateway.err.log
/var/log/supervisor/phantom_monitor_whatsapp.out.log
/var/log/supervisor/phantom_monitor_whatsapp.err.log
```

## 10. Stop / reset

```bash
# Stop, keep session (next boot reuses the bind, no re-pairing)
docker compose down

# Stop and forget bound chat → must re-pair next boot
docker compose down
rm -rf ./data/whatsapp/auth/*

# Full reset including Node deps (forces clean npm install on next boot)
docker compose down -v          # -v removes the named whatsapp_node_modules volume
rm -rf ./data/whatsapp/auth/*
docker compose up -d --build
```

---

## TL;DR

```bash
cp .env.example .env
# edit .env: PHANTOM_MODE=whatsapp, WHATSAPP_GATEWAY_TOKEN=<hex>, ANTHROPIC_*
mkdir -p ./data/whatsapp/auth ./logs
docker compose up -d --build
open http://localhost:9010/whatsapp           # scan QR
# on phone: create 1-member group, send the pairing code into it
# now send any message in that group → Ninja replies
```
