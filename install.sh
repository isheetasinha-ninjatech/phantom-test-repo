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
TEAMS_ID=""
CHANNEL_ID=""
SLACK_AGENT="phantom"  # always phantom — only one agent in this repo

usage() {
    echo "Usage: $0 --channel CHANNEL --channel-id CHANNEL_ID [--workspace-id WORKSPACE_ID]"
    echo ""
    echo "Options:"
    echo "  --teams-id TEAMS_ID          Teams ID (required, e.g. 'T0A9Q27KD1T')"
    echo "  --channel-id CHANNEL_ID      Teams channel ID (required, e.g. 'C0AAAAMBR1R')"
    # echo "  --workspace-id WORKSPACE_ID  Teams workspace/team ID (optional, e.g. 'T0A9Q27KD1T')"
    echo "  --help                       Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 --teams-id 'T0A9Q27KD1T' --channel-id 'C0AAAAMBR1R'"
    echo "  $0 --teams-id 'T0A9Q27KD1T' --channel-id 'C0AAAAMBR1R' --workspace-id 'T0A9Q27KD1T'"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --teams-id)     TEAMS_ID="$2"; shift 2 ;;
        --channel-id)   CHANNEL_ID="$2"; shift 2 ;;
        # --workspace-id) WORKSPACE_ID="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [[ -z "$TEAMS_ID" || -z "$CHANNEL_ID" ]]; then
    echo "ERROR: --channel and --channel-id are required"
    usage
    exit 1
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

# --- 3. Teams configuration — must come before systemd step ----------------
echo ""
echo "▶ Configuring Teams..."

# Verify s3_config.json exists before invoking slack_interface.py
S3_CONFIG_FOUND=false
for candidate in "/root/s3_config.json" "$SCRIPT_DIR/s3_config.json" "/root/ninja-squad/s3_config.json" "/workspace/ninja-squad/s3_config.json"; do
    if [[ -f "$candidate" ]]; then
        S3_CONFIG_FOUND=true
        break
    fi
done

if [[ "$S3_CONFIG_FOUND" != "true" ]]; then
    echo "  ⚠ s3_config.json not found — skipping Slack config (Teams mode)"
fi

python "$SCRIPT_DIR/teams_interface.py" config --set-team-id "$TEAMS_ID" --set-channel-id "$CHANNEL_ID"
echo "  ✓ Teams team ID set to: $TEAMS_ID"
echo "  ✓ Teams channel set to: $CHANNEL_ID"

# if [[ -n "$SLACK_WORKSPACE_ID" ]]; then
#     python "$SCRIPT_DIR/slack_interface.py" config --set-workspace-id "$SLACK_WORKSPACE_ID"
#     echo "  ✓ Slack workspace ID set to: $SLACK_WORKSPACE_ID"
# fi

# python "$SCRIPT_DIR/slack_interface.py" config --set-agent "$SLACK_AGENT"
# echo "  ✓ Slack agent set to: $SLACK_AGENT (phantom)"

# --- 4. Systemd services ----------------------------------------------------
echo ""
echo "▶ Installing systemd services..."
cp "$SCRIPT_DIR/systemd/phantom-sync.service" /etc/systemd/system/phantom-sync.service
cp "$SCRIPT_DIR/systemd/phantom.service"              /etc/systemd/system/phantom.service
cp "$SCRIPT_DIR/systemd/phantom-dashboard.service"    /etc/systemd/system/phantom-dashboard.service
cp "$SCRIPT_DIR/systemd/phantom-integrations.service" /etc/systemd/system/phantom-integrations.service
cp "$SCRIPT_DIR/systemd/phantom-monitor.service"      /etc/systemd/system/phantom-monitor.service
systemctl daemon-reload
systemctl enable phantom-sync.service phantom.service phantom-monitor.service phantom-dashboard.service phantom-integrations.service
systemctl start  phantom-sync.service phantom.service phantom-monitor.service phantom-dashboard.service phantom-integrations.service
echo "  ✓ phantom-sync.service installed, enabled and started (removes superninja config, syncs workspace to git)"
echo "  ✓ phantom.service installed and enabled (single work cycle, restarts on failure)"
echo "  ✓ phantom-monitor.service installed, enabled and started (continuous Teams watcher)"
echo "  ✓ phantom-dashboard.service installed, enabled and started (port 9000)"
echo "  ✓ phantom-integrations.service installed, enabled and started (port 9020)"

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
