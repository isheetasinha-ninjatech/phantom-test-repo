# WhatsApp Interface — Testing Runbook (POC)

End-to-end manual test plan for the WhatsApp POC: **onboard**, **read**, **say**, **group list**, **group create**. The flows assume the gateway in `src/phantom/whatsapp_gateway/` and the CLI `python -m phantom.whatsapp_interface`.

> ⚠️ **Personal-account warning.** Linked-device automation on a personal WhatsApp number can trigger spam limits or bans. Keep volume low, restrict destinations to a single trusted test contact via `WHATSAPP_ALLOWED_TO`, and prefer a dedicated phone + eSIM when possible.

---

## 0. Prerequisites

- Node 20+, `npm`, Python 3.10+
- A second phone (or a colleague) reachable on WhatsApp — your **test contact**
- Your own WhatsApp account ready to be linked (Settings → Linked Devices)

Convenience env (used in every step below):

```bash
export GATEWAY_URL=http://127.0.0.1:8090
export WHATSAPP_GATEWAY_TOKEN="$(openssl rand -hex 16)"
export SELF_E164=15559998888     # your linked number (digits only)
export PEER_E164=15551112222     # test contact (digits only)
```

Install once:

```bash
cd src/phantom/whatsapp_gateway
npm install
cd -
```

---

## 1. Onboard (link the account)

### 1a. Start the gateway

```bash
cd src/phantom/whatsapp_gateway
WHATSAPP_AUTH_DIR=./auth/default \
WHATSAPP_GATEWAY_TOKEN="$WHATSAPP_GATEWAY_TOKEN" \
WHATSAPP_ALLOWED_TO="$PEER_E164" \
WHATSAPP_TO="$PEER_E164" \
WHATSAPP_FORCE_SINGLE_TO=1 \
WHATSAPP_BIND=127.0.0.1 \
PORT=8090 \
npm run dev
```

You should see `gateway listening` in the logs.

**Sanity check** (in another shell):

```bash
curl -s "$GATEWAY_URL/health"
# {"ok":true,"state":"starting"}    # or "qr" / "connecting"
```

### 1b. Run onboard

```bash
PYTHONPATH=src python -m phantom.whatsapp_interface onboard \
  --gateway-url "$GATEWAY_URL" \
  --gateway-token "$WHATSAPP_GATEWAY_TOKEN" \
  --to "$PEER_E164"
```

Expected behavior:

1. `gateway reachable at http://127.0.0.1:8090`
2. State transitions: `starting` → `connecting` → `qr` → `open`
3. While `state=qr`, a PNG is written to `./whatsapp-qr.png` and refreshed as it rotates.
4. Open the QR on your phone (WhatsApp → Settings → **Linked Devices** → **Link a device**).
5. After scan: `linked as 15559998888` and `wrote whatsapp settings to ~/.agent_settings.json`.

**Verify persistence**:

```bash
jq '.whatsapp' ~/.agent_settings.json
# {
#   "gateway_url": "http://127.0.0.1:8090",
#   "gateway_token": "...",
#   "default_to": "15551112222",
#   "conversation_id": "15559998888:15551112222",
#   "self_e164": "15559998888"
# }
```

Top-level Slack keys (`default_channel`, `default_agent`, etc.) must still be present.

### 1c. Status / health re-check

```bash
curl -s -H "Authorization: Bearer $WHATSAPP_GATEWAY_TOKEN" "$GATEWAY_URL/status"
# {"connection":"open","linked":true,"self_e164":"15559998888"}
```

### 1d. Failure modes to verify

| Action | Expected |
|--------|----------|
| Stop the gateway, rerun `onboard` | `gateway not reachable at http://127.0.0.1:8090; is \`npm run dev\` running?` |
| Pass a wrong `--gateway-token` | `unauthorized — provide --gateway-token or set WHATSAPP_GATEWAY_TOKEN` |
| Stop gateway → delete `auth/default/` → restart | New QR; re-link required (auth dir is cleared automatically on remote `loggedOut`) |

---

## 2. Send a message (`say`)

```bash
PYTHONPATH=src python -m phantom.whatsapp_interface say "hello from phantom POC"
```

- `WHATSAPP_FORCE_SINGLE_TO=1` plus `WHATSAPP_TO=$PEER_E164` forces every outbound to your test contact regardless of `--to`.
- Output is JSON: `{"ok":true,"to":"15551112222","jid":"...@s.whatsapp.net","id":"...","conversation_id":"15559998888:15551112222"}`.
- The test contact receives the message on their phone.

### Routing precedence (verify)

```bash
# Force_to wins over everything
WHATSAPP_FORCE_TO=15557654321 PYTHONPATH=src python -m phantom.whatsapp_interface say "forced"

# Without force_single_to, --to is honored — but only if in WHATSAPP_ALLOWED_TO
unset WHATSAPP_FORCE_TO
PYTHONPATH=src python -m phantom.whatsapp_interface say "explicit" --to "$PEER_E164"
```

### Failure modes to verify

| Action | Expected |
|--------|----------|
| `--to` outside `WHATSAPP_ALLOWED_TO` | gateway returns 403 `destination not in WHATSAPP_ALLOWED_TO`; CLI prints `send failed` |
| Gateway not linked (`state != open`) | 409 `not_linked` |
| Gateway stopped | `gateway not reachable ...` |

---

## 3. Read messages

### 3a. Live inbound

Have the test contact send you a WhatsApp message **while the gateway is running**. (The gateway only captures messages received while it is linked and online; there is no history backfill.)

```bash
PYTHONPATH=src python -m phantom.whatsapp_interface read --limit 20
```

Expected output (one block per item):

```
seq=1  2026-06-04T17:42:13Z  from:15551112222  chan:15559998888:15551112222
    hi from the test phone
```

`~/.agent_settings.json` now contains `whatsapp.last_read_seq` advanced to the gateway's `latest_seq`.

### 3b. Cursor behavior

```bash
# Second run with no new traffic → empty
PYTHONPATH=src python -m phantom.whatsapp_interface read
# no new messages (cursor=1)

# Re-read from the beginning, do NOT advance cursor
PYTHONPATH=src python -m phantom.whatsapp_interface read --since 0 --no-save

# Re-read as JSON
PYTHONPATH=src python -m phantom.whatsapp_interface read --since 0 --json
```

### 3c. Filters

```bash
# Hide outbound / from_me items
PYTHONPATH=src python -m phantom.whatsapp_interface read --since 0 --no-self

# Filter by conversation_id (formatting is normalized — whitespace/punctuation OK)
PYTHONPATH=src python -m phantom.whatsapp_interface read --since 0 \
  --conversation "+1 (555) 999-8888:+1 (555) 111-2222"
# Equivalent to:
PYTHONPATH=src python -m phantom.whatsapp_interface read --since 0 \
  --conversation "$SELF_E164:$PEER_E164"
```

### 3d. Echo loop check (optional)

Restart the gateway with `WHATSAPP_ECHO=1` (and `WHATSAPP_ALLOWED_TO` set). The test contact sends a message → gateway replies with `POC: <text>` exactly once. Then run `read --since 0`:

- You see the inbound from the contact (`from:15551112222`, `from_me:false`)
- You see the outbound echo (`from:me`, `from_me:true`) with `chan:` unchanged
- The echo does **not** trigger another echo (fromMe guard + recently-sent ring)

### 3e. Inbox caveats (verify only by inspection)

- Inbox is an in-memory ring of the last **1000** messages received while the gateway is linked.
- Restarting the gateway **clears** the inbox. The cursor in `~/.agent_settings.json` will then point past the new floor.
- If traffic > 1000 messages between `read` calls, items silently drop off the floor. No gap signaling in the POC.

### 3f. Optional history sync after restart

Opt-in via `WHATSAPP_SYNC_FULL_HISTORY=1` on the **gateway** process. Bounds the age of re-ingested messages via `WHATSAPP_HISTORY_SYNC_MAX_AGE_MS` (default `604800000` = 7 days). No disk persistence — recent history is re-fetched from WhatsApp on each connect into the same in-memory ring.

> ⚠️ **Gotcha — bootstrap only fires on a fresh pair.** WhatsApp tracks per-device "history already delivered" state via Baileys' `processedHistoryMessages`. If your `auth/default` was originally paired with `WHATSAPP_SYNC_FULL_HISTORY=0` (the prior default), simply restarting with the flag flipped to `1` will **not** trigger a new `messaging-history.set`. You must either:
> 1. **Delete the auth dir and re-link** to get `INITIAL_BOOTSTRAP` + `RECENT` (recommended for a full test), or
> 2. Generate `messages.upsert type=append` by stopping the gateway, sending yourself messages from another phone, then restarting — Baileys delivers offline messages as `append`.

Runbook A — fresh pair (full bootstrap test):

```bash
# 1. Stop the gateway, wipe auth so we get a fresh pairing.
rm -rf src/phantom/whatsapp_gateway/auth/default

# 2. Restart with sync enabled. WHATSAPP_LOG_LEVEL=info shows each
#    "history_set_received" / "upsert" event as it arrives.
WHATSAPP_AUTH_DIR=./auth/default \
WHATSAPP_GATEWAY_TOKEN="$WHATSAPP_GATEWAY_TOKEN" \
WHATSAPP_ALLOWED_TO="$PEER_E164" \
WHATSAPP_TO="$PEER_E164" \
WHATSAPP_FORCE_SINGLE_TO=1 \
WHATSAPP_BIND=127.0.0.1 \
WHATSAPP_SYNC_FULL_HISTORY=1 \
WHATSAPP_LOG_LEVEL=info \
PORT=8090 \
npm run dev

# 3. Re-scan QR. Then poll /status — `events_seen` lets you diagnose
#    without scraping logs.
curl -sH "Authorization: Bearer $WHATSAPP_GATEWAY_TOKEN" "$GATEWAY_URL/status" | jq
# {"connection":"open","linked":true,"self_e164":"...",
#  "inbox_epoch": 1733349900000,
#  "history_sync_active": false,
#  "sync_full_history": true,
#  "events_seen": {
#    "history_set": 3, "history_set_skipped": 0,
#    "history_set_by_sync_type": {"0": 1, "3": 2},
#    "upsert_notify": 1, "upsert_append": 0, "upsert_other": 0,
#    "ingested_total": 47,
#    "ingested_by_source": {"notify": 1, "append": 0, "history": 46},
#    "skipped_no_ts": 2, "skipped_stale": 0, "skipped_dup": 0
#  }}

# 4. Read. The CLI prints `cursor reset (gateway inbox restarted)` when
#    the gateway's inbox_epoch differs from the persisted last_read_inbox_epoch,
#    then re-issues with since=0.
PYTHONPATH=src python -m phantom.whatsapp_interface read
```

Runbook B — verify only the `append` path on an already-paired auth dir:

```bash
# 1. Stop the gateway (Ctrl-C). Keep auth/ intact.
# 2. From a peer phone, send 1–2 WhatsApp messages to your linked number.
# 3. Restart with sync enabled.
WHATSAPP_SYNC_FULL_HISTORY=1 WHATSAPP_LOG_LEVEL=info npm run dev

# 4. /status should show events_seen.upsert_append > 0 and history_sync_active
#    briefly true; ingested_by_source.append > 0; latest_seq > 0.
```

Diagnosing "history sync looks broken":

| `events_seen` field | What it tells you |
|---------------------|-------------------|
| `history_set` = 0 | WA server did not send a bootstrap → auth dir was paired previously without sync. Use Runbook A. |
| `history_set` > 0 but `history_set_by_sync_type` is `{"4": …}` only | Only `PUSH_NAME` (metadata) arrived. Wait longer or re-pair. |
| `history_set_skipped` > 0 | A syncType outside `{0,2,3,6}` was rejected — increase the accept set in `wa-inbound.ts:ACCEPTED_HISTORY_SYNC_TYPES`. |
| `ingested_total` = 0 but `history_set` > 0 | Messages stripped by age filter (`skipped_stale` rises) — raise `WHATSAPP_HISTORY_SYNC_MAX_AGE_MS`. |
| `upsert_append` = 0 across multiple restart cycles | No offline traffic queued. Use Runbook B. |
| `skipped_no_ts` > 0 | Some history items lacked `messageTimestamp`; expected for status/system items. |

Expectations:

- `/status` flips `history_sync_active` to `false` ~5s after the last `append` (debounced quiet window). Long syncs may flap; best-effort.
- `read` shows recent items (within `WHATSAPP_HISTORY_SYNC_MAX_AGE_MS`), then `last_read_seq` + `last_read_inbox_epoch` are written to `~/.agent_settings.json`.
- Restart **without** `WHATSAPP_SYNC_FULL_HISTORY=1` → empty inbox; `cursor reset` still triggers because `inbox_epoch` changed.
- Filters still apply: `read --conversation "{self}:g:{group_local}"`.

Limits (call out explicitly):

- Not a full archive — ring cap 1000 + age filter; older / overflow messages drop.
- Text only (unchanged): no media/reaction bodies.
- Personal-account risk: enabling history sync increases linked-device traffic and pairing-time work. Keep default off.
- `WHATSAPP_SYNC_FULL_HISTORY` only takes effect at **pair time**. Toggling it on an existing auth dir has no effect on bootstrap (still helps live `append`).

---

## 3.5 List your groups

Requires a linked gateway (`connection: open`). Fetches all groups you participate in from WhatsApp (not just Phantom-created groups).

```bash
PYTHONPATH=src python -m phantom.whatsapp_interface group list
PYTHONPATH=src python -m phantom.whatsapp_interface group list --json
```

Human output shows **subject**, **jid**, **conv** (conversation_id for `read --conversation` / `say --conversation`), and **members** count.

Use a row's `conv` or `jid` to send:

```bash
PYTHONPATH=src python -m phantom.whatsapp_interface say "hello" \
  --group-jid "120363408160743000@g.us"
```

---

## 4. Group create

> Group create is **disabled by default**. The gateway process must be (re)started with `WHATSAPP_ALLOW_GROUP_CREATE=1`. Setting it on the CLI has no effect.

### 4a. Negative path (default: disabled)

While the gateway runs without `WHATSAPP_ALLOW_GROUP_CREATE`:

```bash
PYTHONPATH=src python -m phantom.whatsapp_interface group create "POC" \
  --participants "$PEER_E164"
```

Expected stderr:

```
group create is disabled. The GATEWAY process must be restarted with
  WHATSAPP_ALLOW_GROUP_CREATE=1
(this flag is on the gateway, not the CLI/Python process).
```

Exit code: `1`.

### 4b. Restart gateway with group-create enabled

```bash
cd src/phantom/whatsapp_gateway
WHATSAPP_AUTH_DIR=./auth/default \
WHATSAPP_GATEWAY_TOKEN="$WHATSAPP_GATEWAY_TOKEN" \
WHATSAPP_ALLOWED_TO="$PEER_E164" \
WHATSAPP_TO="$PEER_E164" \
WHATSAPP_FORCE_SINGLE_TO=1 \
WHATSAPP_ALLOW_GROUP_CREATE=1 \
WHATSAPP_BIND=127.0.0.1 \
PORT=8090 \
npm run dev
```

### 4c. Create the group

```bash
PYTHONPATH=src python -m phantom.whatsapp_interface group create "POC Group" \
  --participants "$PEER_E164" \
  --welcome "Phantom POC welcome"
```

Expected JSON on stdout:

```json
{
  "ok": true,
  "group_jid": "120363...@g.us",
  "added": ["15551112222"],
  "skipped": [],
  "conversation_id": "15559998888:g:120363..."
}
```

- The test contact receives a group-invite notification + the welcome message.
- `~/.agent_settings.json` now contains:

```bash
jq '.whatsapp | {last_group_jid, last_group_conversation_id}' ~/.agent_settings.json
# {
#   "last_group_jid": "120363...@g.us",
#   "last_group_conversation_id": "15559998888:g:120363..."
# }
```

### 4d. Verify the group via read

```bash
PYTHONPATH=src python -m phantom.whatsapp_interface read \
  --since 0 \
  --conversation "$(jq -r '.whatsapp.last_group_conversation_id' ~/.agent_settings.json)"
```

You should see the welcome message you sent and any subsequent group messages.

### 4e. Send to the group (`say --group`)

After create, send additional messages without a welcome:

```bash
PYTHONPATH=src python -m phantom.whatsapp_interface say "follow-up in group" --group last

# Or by full JID / conversation_id from step 4c output:
PYTHONPATH=src python -m phantom.whatsapp_interface say "follow-up" \
  --group-jid "$(jq -r '.whatsapp.last_group_jid' ~/.agent_settings.json)"

PYTHONPATH=src python -m phantom.whatsapp_interface say "follow-up" \
  --conversation "$(jq -r '.whatsapp.last_group_conversation_id' ~/.agent_settings.json)"
```

Expected JSON: `{"ok":true,"group_jid":"120363...@g.us","jid":"...","id":"...","conversation_id":"15559998888:g:120363..."}`.

Group sends skip `WHATSAPP_FORCE_SINGLE_TO` and `WHATSAPP_ALLOWED_TO` (those apply to DMs only). No extra gateway flag is required beyond an explicit group target.

Verify with read (step 4d) — you should see the new `from_me` rows.

### 4f. Failure modes to verify

| Action | Expected |
|--------|----------|
| `--participants 19999999999` (not a WhatsApp user) | gateway returns 500 `no valid participants`; CLI prints `all participants failed onWhatsApp validation` |
| Mix valid + invalid participants | group created with only the valid ones; `skipped` lists the rest |
| Gateway restarted **without** `WHATSAPP_ALLOW_GROUP_CREATE=1` | back to 403 disabled |

### 4g. Recommended cleanup

For personal-account safety, restart the gateway **without** `WHATSAPP_ALLOW_GROUP_CREATE` after the demo, and consider leaving the test group manually from your phone.

---

## 5. Settings reference

After running through all flows, `~/.agent_settings.json` contains:

```json
{
  "default_agent": "phantom",
  "whatsapp": {
    "gateway_url": "http://127.0.0.1:8090",
    "gateway_token": "...",
    "default_to": "15551112222",
    "conversation_id": "15559998888:15551112222",
    "self_e164": "15559998888",
    "last_read_seq": 7,
    "last_read_inbox_epoch": 1733349900000,
    "last_group_jid": "120363...@g.us",
    "last_group_conversation_id": "15559998888:g:120363..."
  }
}
```

Top-level Slack keys are preserved at every step.

---

## 6. Quick troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `gateway not reachable at ...` | gateway not running, wrong URL/port | `npm run dev` in `src/phantom/whatsapp_gateway` |
| `unauthorized` (401) | missing/wrong token | pass `--gateway-token` or set `WHATSAPP_GATEWAY_TOKEN` |
| `not_linked` (409) | gateway up but no live WA session | rerun `onboard`; scan QR |
| `destination not in WHATSAPP_ALLOWED_TO` | dest not allowlisted | add to `WHATSAPP_ALLOWED_TO` on the **gateway** and restart |
| `group_create_disabled` (403) | gateway env missing flag | restart gateway with `WHATSAPP_ALLOW_GROUP_CREATE=1` |
| `use either --to (DM) or a group flag` | mixed DM + group on `say` | pick one target mode |
| `no last_group_jid in settings` | `say --group last` before create | run `group create` first |
| QR never appears | gateway logged out or auth dir corrupted | stop gateway, `rm -rf src/phantom/whatsapp_gateway/auth/default`, restart |
| `no new messages` after restart | inbox is in-memory, cleared on restart | not a bug; or enable `WHATSAPP_SYNC_FULL_HISTORY=1` (see §3f) |
| `cursor reset (gateway inbox restarted)` after restart | gateway `inbox_epoch` differs from persisted | expected — CLI re-reads from `since=0` automatically |
| `WHATSAPP_SYNC_FULL_HISTORY=1` set, but `latest_seq=0` after restart | auth dir was paired before sync was enabled; WA won't re-bootstrap | wipe `auth/default` and re-link (§3f Runbook A) |
| `/status events_seen.history_set` always 0 | server isn't sending a bootstrap to this device | fresh pair required (§3f Runbook A) |
