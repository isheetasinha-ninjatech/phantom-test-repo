#!/usr/bin/env bash
# install.sh — Setup script for Phantom browser automation agent
#
# Usage:
#   ./install.sh --channel "#my-channel" --channel-id "C0AAAAMBR1R"
#
# What this does:
#   1. Installs Python dependencies (requirements.txt)
#   2. Creates the logs directory
#   3. Configures Slack channel (agent is always 'phantom')
#   4. Installs and enables phantom-sync.service, phantom.service, phantom-monitor.service, phantom-dashboard.service, and phantom-integrations.service
#
# Prerequisites (must be provided manually — not handled by this script):
#   - s3_config.json at repo root or /root/  (AWS credentials for Slack S3 cache)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Parse arguments --------------------------------------------------------
SLACK_CHANNEL=""
SLACK_CHANNEL_ID=""
SLACK_WORKSPACE_ID=""
SLACK_AGENT="phantom"  # always phantom — only one agent in this repo
PHANTOM_MODE_ARG=""    # slack (default) | whatsapp
WHATSAPP_CHAT_JID=""   # optional pre-seed for Ninja mode

usage() {
    echo "Usage: $0 [--mode slack|whatsapp] --channel CHANNEL --channel-id CHANNEL_ID [--workspace-id WORKSPACE_ID]"
    echo ""
    echo "Options:"
    echo "  --mode MODE                  Runtime mode: 'slack' (default) or 'whatsapp'"
    echo "  --channel CHANNEL            Channel name (slack: '#my-channel'; whatsapp: a human label)"
    echo "  --channel-id CHANNEL_ID      Channel ID (slack: 'C0AAAAMBR1R'; whatsapp: chat JID '...@g.us' or '...@s.whatsapp.net')"
    echo "  --workspace-id WORKSPACE_ID  Slack workspace/team ID (optional, slack mode only)"
    echo "  --chat-jid JID               WhatsApp chat JID to pre-seed (whatsapp mode, alternative to --channel-id)"
    echo "  --help                       Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 --channel '#my-channel' --channel-id 'C0AAAAMBR1R'"
    echo "  $0 --mode whatsapp --channel 'Ops Bridge' --chat-jid '120363xxxxxxxxxxxx@g.us'"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)         PHANTOM_MODE_ARG="$2"; shift 2 ;;
        --channel)      SLACK_CHANNEL="$2"; shift 2 ;;
        --channel-id)   SLACK_CHANNEL_ID="$2"; shift 2 ;;
        --workspace-id) SLACK_WORKSPACE_ID="$2"; shift 2 ;;
        --chat-jid)     WHATSAPP_CHAT_JID="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

# Resolve effective mode: explicit --mode wins; otherwise honor PHANTOM_MODE env
# (loaded from .env at runtime). Default to slack to preserve existing behavior.
PHANTOM_MODE_EFFECTIVE="${PHANTOM_MODE_ARG:-${PHANTOM_MODE:-slack}}"
PHANTOM_MODE_EFFECTIVE="$(echo "$PHANTOM_MODE_EFFECTIVE" | tr '[:upper:]' '[:lower:]')"

case "$PHANTOM_MODE_EFFECTIVE" in
    slack|whatsapp) ;;
    *)
        echo "ERROR: invalid --mode '$PHANTOM_MODE_EFFECTIVE' (expected: slack | whatsapp)"
        exit 1
        ;;
esac

if [[ "$PHANTOM_MODE_EFFECTIVE" == "slack" ]]; then
    if [[ -z "$SLACK_CHANNEL" || -z "$SLACK_CHANNEL_ID" ]]; then
        echo "ERROR: --channel and --channel-id are required in slack mode"
        usage
        exit 1
    fi
else
    # WhatsApp mode — --chat-jid takes precedence over --channel-id; either works.
    if [[ -z "$WHATSAPP_CHAT_JID" && -n "$SLACK_CHANNEL_ID" ]]; then
        WHATSAPP_CHAT_JID="$SLACK_CHANNEL_ID"
    fi
fi

echo "=== Phantom Browser Automation — Setup ==="
echo ""

# --- 1. Python dependencies -------------------------------------------------
echo "▶ Installing Python dependencies..."
pip install -q -r "$SCRIPT_DIR/requirements.txt"
echo "  ✓ Python packages installed"

# Ensure the phantom package is importable by adding its parent to PYTHONPATH
PHANTOM_PARENT="$(cd "$SCRIPT_DIR/.." && pwd)"
if ! grep -q "$PHANTOM_PARENT" /etc/environment 2>/dev/null; then
    echo "PYTHONPATH=\"${PHANTOM_PARENT}:\${PYTHONPATH:-}\"" >> /etc/environment
fi
export PYTHONPATH="${PHANTOM_PARENT}:${PYTHONPATH:-}"
echo "  ✓ PYTHONPATH configured (${PHANTOM_PARENT})"

# --- 1.5. Install `pdx` CLI (Pipedream LLM wrapper) -------------------------
# `pdx` is a tiny JSON-first CLI that exposes connected Pipedream
# integrations to the LLM. Symlink it into /usr/local/bin so every
# shell (supervisor, orchestrator, manual) can invoke `pdx ...`.
PDX_SRC="$SCRIPT_DIR/bin/pdx"
PDX_DST="/usr/local/bin/pdx"
if [[ -f "$PDX_SRC" ]]; then
    chmod +x "$PDX_SRC"
    ln -sf "$PDX_SRC" "$PDX_DST"
    echo "  ✓ pdx CLI installed → $PDX_DST"
else
    echo "  ⚠ bin/pdx not found — skipping pdx install"
fi

# --- 2. Log directory -------------------------------------------------------
mkdir -p /workspace/logs
echo "  ✓ Log directory ready (/workspace/logs)"

# --- 2.5. Timezone ----------------------------------------------------------
# Align the sandbox clock with the operator's Slack timezone so every
# subsequent log line, cron tick, Slack message, and git commit happens
# in the human's local time. Non-blocking: we warn and continue on any
# failure so install never aborts because of a clock-config hiccup.
#
# The script lives inside the deployed package
# (src/phantom/initial_setup_scripts/) so it ships through the CDK
# PublishStack zip. It used to live at the repo root, where the
# packaging step skipped it and every deployed agent silently fell
# back to Etc/UTC.
echo ""
echo "▶ Aligning system timezone with Slack user profile..."
TZ_SCRIPT="$SCRIPT_DIR/initial_setup_scripts/set_timezone.py"
if [[ -f "$TZ_SCRIPT" ]]; then
    # Route stdout to /dev/null — we print our own one-line confirmation below.
    # Keep stderr so real errors still surface in the install log.
    if python "$TZ_SCRIPT" --quiet >/dev/null; then
        CURRENT_TZ="$(cat /etc/timezone 2>/dev/null || readlink /etc/localtime 2>/dev/null | sed 's#.*/zoneinfo/##')"
        echo "  ✓ Timezone: ${CURRENT_TZ:-unknown}"
    else
        echo "  ⚠ set_timezone.py exited non-zero — continuing with the current system timezone."
    fi
else
    echo "  ⚠ ${TZ_SCRIPT} not found — skipping timezone sync."
fi

# --- 3. Channel configuration — must come before systemd step --------------
echo ""

if [[ "$PHANTOM_MODE_EFFECTIVE" == "slack" ]]; then
    echo "▶ Configuring Slack..."

    # Verify s3_config.json exists before invoking slack_interface.py
    S3_CONFIG_FOUND=false
    for candidate in "/root/s3_config.json" "$SCRIPT_DIR/s3_config.json" "/root/ninja-squad/s3_config.json" "/workspace/ninja-squad/s3_config.json"; do
        if [[ -f "$candidate" ]]; then
            S3_CONFIG_FOUND=true
            break
        fi
    done

    if [[ "$S3_CONFIG_FOUND" != "true" ]]; then
        echo "  ✗ s3_config.json not found — cannot configure Slack"
        echo "    Create s3_config.json (at repo root or /root/) with:"
        echo "      aws_access_key_id, aws_secret_access_key, bucket_name"
        echo "    Then re-run: $0 --channel '$SLACK_CHANNEL'"
        exit 1
    fi

    python "$SCRIPT_DIR/slack_interface.py" config --set-channel "$SLACK_CHANNEL" --set-channel-id "$SLACK_CHANNEL_ID"
    echo "  ✓ Slack channel set to: $SLACK_CHANNEL"

    if [[ -n "$SLACK_WORKSPACE_ID" ]]; then
        python "$SCRIPT_DIR/slack_interface.py" config --set-workspace-id "$SLACK_WORKSPACE_ID"
        echo "  ✓ Slack workspace ID set to: $SLACK_WORKSPACE_ID"
    fi

    python "$SCRIPT_DIR/slack_interface.py" config --set-agent "$SLACK_AGENT"
    echo "  ✓ Slack agent set to: $SLACK_AGENT (phantom)"
else
    echo "▶ Configuring WhatsApp (Ninja mode)..."

    # Persist mode + WhatsApp settings into ~/.agent_settings.json so the
    # mode resolver and CLI pick them up without needing PHANTOM_MODE in the
    # environment. We also still honor PHANTOM_MODE in .env.
    WA_LABEL="$SLACK_CHANNEL" WA_JID="$WHATSAPP_CHAT_JID" python - <<'PY'
import json, os
from pathlib import Path
p = Path.home() / ".agent_settings.json"
data = {}
if p.exists():
    try:
        data = json.loads(p.read_text())
    except Exception:
        data = {}
data["mode"] = "whatsapp"
data.setdefault("default_agent", "phantom")
wa = data.get("whatsapp") if isinstance(data.get("whatsapp"), dict) else {}
label = os.environ.get("WA_LABEL", "").strip()
jid = os.environ.get("WA_JID", "").strip()
if label:
    wa["channel_label"] = label
if jid:
    wa["bound_chat_jid"] = jid
data["whatsapp"] = wa
p.write_text(json.dumps(data, indent=2))
print(f"  settings written: {p}")
PY
    echo "  ✓ ~/.agent_settings.json updated (mode=whatsapp)"

    # Pre-seed gateway bound.json when a JID was provided. This lets the
    # gateway skip QR/pairing entirely on first boot.
    if [[ -n "$WHATSAPP_CHAT_JID" ]]; then
        WA_AUTH_DIR="/workspace/phantom/whatsapp/auth"
        mkdir -p "$WA_AUTH_DIR"
        cat > "$WA_AUTH_DIR/bound.json" <<JSON
{
  "chat_jid": "$WHATSAPP_CHAT_JID",
  "bound_via": "install",
  "bound_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
        echo "  ✓ Pre-seeded bound chat: $WHATSAPP_CHAT_JID"
    else
        echo "  ℹ No --chat-jid provided — pair via the dashboard /whatsapp panel (auto-group default, pairing-code optional)."
    fi
fi

# --- 4. Systemd services ----------------------------------------------------
echo ""
echo "▶ Installing systemd services..."

# Shared services (sync + dashboard + integrations) install in both modes.
cp "$SCRIPT_DIR/systemd/phantom-sync.service"         /etc/systemd/system/phantom-sync.service
cp "$SCRIPT_DIR/systemd/phantom.service"              /etc/systemd/system/phantom.service
cp "$SCRIPT_DIR/systemd/phantom-dashboard.service"    /etc/systemd/system/phantom-dashboard.service
cp "$SCRIPT_DIR/systemd/phantom-integrations.service" /etc/systemd/system/phantom-integrations.service

if [[ "$PHANTOM_MODE_EFFECTIVE" == "slack" ]]; then
    cp "$SCRIPT_DIR/systemd/phantom-monitor.service" /etc/systemd/system/phantom-monitor.service
    systemctl daemon-reload
    systemctl enable phantom-sync.service phantom.service phantom-monitor.service phantom-dashboard.service phantom-integrations.service
    systemctl start  phantom-sync.service phantom.service phantom-monitor.service phantom-dashboard.service phantom-integrations.service
    echo "  ✓ phantom-sync.service installed, enabled and started"
    echo "  ✓ phantom.service installed and enabled (single work cycle, restarts on failure)"
    echo "  ✓ phantom-monitor.service installed, enabled and started (continuous Slack watcher)"
    echo "  ✓ phantom-dashboard.service installed, enabled and started (port 9000)"
    echo "  ✓ phantom-integrations.service installed, enabled and started (port 9020)"
else
    cp "$SCRIPT_DIR/systemd/phantom-whatsapp-gateway.service" /etc/systemd/system/phantom-whatsapp-gateway.service
    cp "$SCRIPT_DIR/systemd/phantom-whatsapp-monitor.service" /etc/systemd/system/phantom-whatsapp-monitor.service

    # Disable the Slack monitor if it was previously installed; the WhatsApp
    # monitor replaces it. Keep the unit file off the active list to avoid
    # both polling channels at once.
    if systemctl list-unit-files | grep -q '^phantom-monitor\.service'; then
        systemctl disable --now phantom-monitor.service 2>/dev/null || true
    fi

    # Make sure the WhatsApp gateway has its Node deps installed before the
    # service starts. `npm ci` is idempotent: <1 s after the first install.
    if [[ -f "$SCRIPT_DIR/whatsapp/package.json" ]]; then
        echo "▶ Installing WhatsApp gateway Node deps..."
        (cd "$SCRIPT_DIR/whatsapp" && npm ci --silent) || \
            (cd "$SCRIPT_DIR/whatsapp" && npm install --silent)
        echo "  ✓ WhatsApp gateway deps installed"
    fi

    systemctl daemon-reload
    systemctl enable phantom-sync.service phantom.service \
        phantom-whatsapp-gateway.service phantom-whatsapp-monitor.service \
        phantom-dashboard.service phantom-integrations.service
    # Use `restart` instead of `start` so that a snapshot-resurrected gateway
    # picks up the channel_label we just wrote to ~/.agent_settings.json.
    # `systemctl start` is a no-op on an already-running unit, which is the
    # case on Suna App Store installs (snapshot brings the gateway up with the
    # publisher's cached auto_group_name before install.sh ever runs). Without
    # this restart, the customer's auto-group gets the publisher's stale name.
    systemctl restart phantom-sync.service phantom.service \
        phantom-whatsapp-gateway.service phantom-whatsapp-monitor.service \
        phantom-dashboard.service phantom-integrations.service
    echo "  ✓ phantom-sync.service installed, enabled and started"
    echo "  ✓ phantom.service installed and enabled (single work cycle, restarts on failure)"
    echo "  ✓ phantom-whatsapp-gateway.service installed, enabled and started (loopback bearer-token gateway)"
    echo "  ✓ phantom-whatsapp-monitor.service installed, enabled and started (continuous WhatsApp watcher)"
    echo "  ✓ phantom-dashboard.service installed, enabled and started (port 9000 — /whatsapp panel)"
    echo "  ✓ phantom-integrations.service installed, enabled and started (port 9020)"

    # Install pre-publish hook so Suna's snapshot doesn't leak the publisher's
    # WhatsApp Baileys session to every installer.
    # See whatsapp/wipe-state.sh for what it scrubs.
    WIPE_SRC="$SCRIPT_DIR/whatsapp/wipe-state.sh"
    if [[ -f "$WIPE_SRC" ]]; then
        install -D -m 755 "$WIPE_SRC" \
            /workspace/.agent_hooks/publish/10_wipe_whatsapp_state.sh
        echo "  ✓ pre-publish wipe hook installed (/workspace/.agent_hooks/publish/10_wipe_whatsapp_state.sh)"
    else
        echo "  ⚠ $WIPE_SRC not found — snapshot will leak auth state"
    fi
fi

# --- 5. VNC password-free configuration ------------------------------------
echo ""
echo "▶ Configuring VNC (removing password requirement)..."

SUPERVISOR_CONF="/etc/supervisor/conf.d/supervisord.conf"

if [[ -f "$SUPERVISOR_CONF" ]]; then
    # Replace -rfbauth flag with -nopw in x11vnc command
    sed -i 's|x11vnc -display :99 -forever -shared -rfbauth /root/.vnc/passwd -rfbport 5901|x11vnc -display :99 -forever -shared -nopw -rfbport 5901|g' "$SUPERVISOR_CONF"

    # Force supervisord to reread and apply updated config
    supervisorctl reread
    supervisorctl update
    supervisorctl restart x11vnc

    echo "  ✓ VNC configured to run without password (-nopw)"
    echo "  ✓ x11vnc restarted with new config"
else
    echo "  ⚠ Supervisor config not found at $SUPERVISOR_CONF — skipping VNC patch"
fi

# --- Done -------------------------------------------------------------------
echo ""
echo "=== Setup complete ==="
echo ""

echo "Useful commands:"
echo "  systemctl status <service_name>             # Check service status"
echo "  journalctl -u <service_name> -f             # Follow service logs"
echo "  Dashboard: http://localhost:9000"
