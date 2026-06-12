#!/usr/bin/env bash
# install.sh — Setup script for Ninja browser automation agent
#
# Usage:
#   ./install.sh --channel "#my-channel" --channel-id "C0AAAAMBR1R"
#
# What this does:
#   1. Installs Python dependencies (requirements.txt)
#   2. Creates the logs directory
#   3. Configures Slack channel (agent is always 'ninja')
#   4. Installs and enables ninja-sync.service, ninja.service, ninja-monitor.service, ninja-dashboard.service, and ninja-integrations.service
#
# Prerequisites (must be provided manually — not handled by this script):
#   - s3_config.json at repo root or /root/  (AWS credentials for Slack S3 cache)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Parse arguments --------------------------------------------------------
SLACK_CHANNEL=""
SLACK_CHANNEL_ID=""
SLACK_WORKSPACE_ID=""
SLACK_AGENT="ninja"  # always ninja — only one agent in this repo

usage() {
    echo "Usage: $0 --channel CHANNEL --channel-id CHANNEL_ID [--workspace-id WORKSPACE_ID]"
    echo ""
    echo "Options:"
    echo "  --channel CHANNEL            Slack channel name (required, e.g. '#my-channel')"
    echo "  --channel-id CHANNEL_ID      Slack channel ID (required, e.g. 'C0AAAAMBR1R')"
    echo "  --workspace-id WORKSPACE_ID  Slack workspace/team ID (optional, e.g. 'T0A9Q27KD1T')"
    echo "  --help                       Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 --channel '#my-channel' --channel-id 'C0AAAAMBR1R'"
    echo "  $0 --channel '#my-channel' --channel-id 'C0AAAAMBR1R' --workspace-id 'T0A9Q27KD1T'"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --channel)      SLACK_CHANNEL="$2"; shift 2 ;;
        --channel-id)   SLACK_CHANNEL_ID="$2"; shift 2 ;;
        --workspace-id) SLACK_WORKSPACE_ID="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [[ -z "$SLACK_CHANNEL" || -z "$SLACK_CHANNEL_ID" ]]; then
    echo "ERROR: --channel and --channel-id are required"
    usage
    exit 1
fi

echo "=== Ninja Browser Automation — Setup ==="
echo ""

# --- 1. Python dependencies -------------------------------------------------
echo "▶ Installing Python dependencies..."
pip install -q -r "$SCRIPT_DIR/requirements.txt"
echo "  ✓ Python packages installed"

# Ensure the ninja package is importable by adding its parent to PYTHONPATH
NINJA_PARENT="$(cd "$SCRIPT_DIR/.." && pwd)"
if ! grep -q "$NINJA_PARENT" /etc/environment 2>/dev/null; then
    echo "PYTHONPATH=\"${NINJA_PARENT}:\${PYTHONPATH:-}\"" >> /etc/environment
fi
export PYTHONPATH="${NINJA_PARENT}:${PYTHONPATH:-}"
echo "  ✓ PYTHONPATH configured (${NINJA_PARENT})"

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
# (src/ninja/initial_setup_scripts/) so it ships through the CDK
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

# --- 3. Slack configuration — must come before systemd step ----------------
echo ""
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
echo "  ✓ Slack agent set to: $SLACK_AGENT (ninja)"

# --- 4. Systemd services ----------------------------------------------------
echo ""
echo "▶ Installing systemd services..."
cp "$SCRIPT_DIR/systemd/ninja-sync.service" /etc/systemd/system/ninja-sync.service
cp "$SCRIPT_DIR/systemd/ninja.service"              /etc/systemd/system/ninja.service
cp "$SCRIPT_DIR/systemd/ninja-monitor.service"      /etc/systemd/system/ninja-monitor.service
cp "$SCRIPT_DIR/systemd/ninja-dashboard.service"    /etc/systemd/system/ninja-dashboard.service
cp "$SCRIPT_DIR/systemd/ninja-integrations.service" /etc/systemd/system/ninja-integrations.service
systemctl daemon-reload
systemctl enable ninja-sync.service ninja.service ninja-monitor.service ninja-dashboard.service ninja-integrations.service
systemctl start  ninja-sync.service ninja.service ninja-monitor.service ninja-dashboard.service ninja-integrations.service
echo "  ✓ ninja-sync.service installed, enabled and started (removes superninja config, syncs workspace to git)"
echo "  ✓ ninja.service installed and enabled (single work cycle, restarts on failure — waits for browser)"
echo "  ✓ ninja-monitor.service installed, enabled and started (continuous Slack watcher)"
echo "  ✓ ninja-dashboard.service installed, enabled and started (port 9000)"
echo "  ✓ ninja-integrations.service installed, enabled and started (port 9020)"

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

# --- 6. Wait for browser server to be ready  --------------------------------------
# Block auto health check here until the browser is confirmed ready, and fall
# back to starting it manually if supervisord hasn't done so yet.
echo ""
echo "▶ Waiting for browser server to be ready on port 9222..."
BROWSER_TIMEOUT=60
BROWSER_READY=false
for i in $(seq 1 "$BROWSER_TIMEOUT"); do
    if curl -sf http://localhost:9222/json/version >/dev/null 2>&1; then
        BROWSER_READY=true
        echo "  ✓ Browser server ready (${i}s)"
        break
    fi
    sleep 1
done

if [[ "$BROWSER_READY" == "false" ]]; then
    echo "  ⚠ Browser not responding after ${BROWSER_TIMEOUT}s — attempting manual start..."
    python "$SCRIPT_DIR/ninja/browser_server.py" start || true
    sleep 5
    if curl -sf http://localhost:9222/json/version >/dev/null 2>&1; then
        echo "  ✓ Browser started successfully"
    else
        echo "  ⚠ Browser could not be started — health check may still fail"
    fi
fi

# --- Done -------------------------------------------------------------------
echo ""
echo "=== Setup complete ==="
echo ""

echo "Useful commands:"
echo "  systemctl status <service_name>             # Check service status"
echo "  journalctl -u <service_name> -f             # Follow service logs"
echo "  Dashboard: http://localhost:9000"
