#!/usr/bin/env bash
# wipe-whatsapp-state.sh — pre-publish hook to scrub publisher's WhatsApp session.
#
# Invoked by Suna's _publish_sandbox flow via:
#   mkdir -p /workspace/.agent_hooks/publish && \
#   python /workspace/.agent_hooks/run_all_hooks.py publish
#
# Without this, the Firecracker snapshot uploaded to s3://ninja-app-store-*/
# private_assets/<app_id>/sandbox/snapshot.lz4 would contain the publisher's
# Baileys auth state — every installer would inherit a live, logged-in
# WhatsApp session that posts as the publisher.
#
# Installed by src/phantom/install.sh (WhatsApp mode) into:
#   /workspace/.agent_hooks/publish/10_wipe_whatsapp_state.sh
#
# Numbered "10_" prefix matches the convention used in .agent_hooks/shutdown/.

set -euo pipefail

echo "▶ wipe-whatsapp-state: scrubbing Baileys auth + bind state"

# Stop the gateway + monitor first so they don't rewrite state mid-wipe.
systemctl stop phantom-whatsapp-gateway.service \
               phantom-whatsapp-monitor.service 2>/dev/null || true

# 1. Baileys auth dir (creds.json, app-state-sync-*, sender-key-*, session-*).
WA_AUTH_DIR="/workspace/phantom/whatsapp/auth"
if [[ -d "$WA_AUTH_DIR" ]]; then
    find "$WA_AUTH_DIR" -mindepth 1 -delete
    echo "  ✓ $WA_AUTH_DIR emptied"
fi

# 2. Bind metadata (chat JID the publisher paired to).
rm -f "$WA_AUTH_DIR/bound.json" 2>/dev/null || true
rm -f /workspace/phantom/whatsapp/bound.json 2>/dev/null || true

# 3. Decrypted media cache (paths used by MediaStore + tmpfs fallback).
rm -rf /workspace/phantom/whatsapp/media 2>/dev/null || true
rm -rf /tmp/wa-media 2>/dev/null || true

# 4. Conversation / monitor logs that may contain message bodies.
rm -f /workspace/logs/whatsapp-*.log 2>/dev/null || true
rm -f /workspace/logs/monitor-*.log 2>/dev/null || true
rm -f /workspace/logs/phantom-whatsapp-*.log 2>/dev/null || true

# 5. ~/.agent_settings.json — keep mode=whatsapp, drop the entire whatsapp
# block (identity fields AND monitor cursors). Any field inside `whatsapp`
# is publisher-specific runtime state — installer should start fresh.
python3 - <<'PY'
import json, os, sys
p = os.path.expanduser("~/.agent_settings.json")
if not os.path.exists(p):
    sys.exit(0)
try:
    with open(p) as f:
        d = json.load(f)
except Exception:
    sys.exit(0)
d["whatsapp"] = {}
with open(p, "w") as f:
    json.dump(d, f, indent=2)
print("  ✓ ~/.agent_settings.json scrubbed (kept mode=whatsapp, cleared whatsapp block)")
PY

# 6. Environment seed files that may carry chat IDs.
for env_file in /etc/systemd/system/phantom-whatsapp-gateway.service.d/*.conf \
                /workspace/phantom/whatsapp/.env; do
    if [[ -f "$env_file" ]]; then
        # Strip lines that pin the bound chat — leave structural env intact.
        sed -i.bak '/^WHATSAPP_ALLOWED_CHAT_JID=/d; /^WHATSAPP_ALLOWED_TO=/d' "$env_file" 2>/dev/null || true
        rm -f "${env_file}.bak" 2>/dev/null || true
    fi
done

# 7. Stale /workspace/phantom checkout. Snapshot captures this directory; on a
# fresh install phantom-install.sh tries to `git clone` into it and fails
# because it already exists (or worse, `git pull`s the publisher's branch
# instead of the customer repo). This script lives at
# /workspace/.agent_hooks/publish/10_wipe_whatsapp_state.sh — outside
# /workspace/phantom — so deleting the checkout doesn't remove the script
# itself mid-run.
if [[ -d /workspace/phantom ]]; then
    rm -rf /workspace/phantom
    echo "  ✓ /workspace/phantom checkout removed (installer will re-clone customer repo)"
fi

echo "  ✓ wipe-whatsapp-state complete — installer will see a fresh QR on first boot"
