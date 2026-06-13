# Phantom Memory

## Session Log
<!-- Phantom will record session notes here -->

### 2026-06-13 — WhatsApp inbox check (post-reinstall)
- Fresh deploy completed (the prior pass caught the install mid-flight with `/workspace/phantom` moved to `phantom.bak` and the gateway crash-looping on CHDIR). Now `/workspace/phantom` is back and the gateway is healthy: `GET /health` → `{"ok":true,"state":"qr"}`.
- Inbox: `read --json` → empty (`items: []`). No new requests.
- Bind status: `connection: qr`, `ninja_state: waiting_for_qr`, `bound_chat_jid: null` — **not linked**. Needs a human to scan the QR (dashboard `/whatsapp` panel or gateway `GET /qr`) before any messages flow.
- No actionable work; cannot reply (no chat bound). Recorded here only.
- Follow-up pass (later 2026-06-13): re-checked — unchanged. Inbox empty, still `waiting_for_qr`, no bound chat. Gateway restarted again (new `inbox_epoch`). Still blocked on human QR scan.

## Technical Decisions
<!-- Technical choices and their rationale -->

- **Manual WhatsApp CLI invocation:** run from `/workspace`, not `/workspace/phantom`. From inside `/workspace/phantom`, `import phantom` resolves to the inner regular package `/workspace/phantom/phantom/` (has `__init__.py`) which does NOT contain `whatsapp_interface.py` → ModuleNotFoundError. The CLI file lives at the repo root `/workspace/phantom/whatsapp_interface.py`, imported as `phantom.whatsapp_interface` only when `/workspace` is the cwd + on PYTHONPATH. Use a single command (compound `cd … &&` may require approval): `env -C /workspace PYTHONPATH=/workspace python3 -m phantom.whatsapp_interface <cmd>`. systemd services get PYTHONPATH via `/etc/environment`.

## Pending Items
<!-- Items to follow up on -->

- **Awaiting human QR scan** to link the device + bind a chat. Until `connection: open` with a `bound_chat_jid`, the inbox stays empty and there is no chat to reply into. Re-check `bind --status --json` next session.
- Workspace has been redeployed repeatedly across recent passes (memory wiped each time, gateway `inbox_epoch` changes). Treat memory as best-effort; the QR-not-linked state is the recurring blocker.
