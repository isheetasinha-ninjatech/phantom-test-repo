#!/usr/bin/env python3
"""
Ninja Agent Dashboard
Combines agent identity, real-time logs, and Claude Code monitor data
in a single Flask application (no separate claude_monitor process needed).
"""

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request
from flask_cors import CORS
from utils.pricing import get_pricing as get_pricing_for_model

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AGENT_SETTINGS_FILE = Path.home() / ".agent_settings.json"


def _find_project_root() -> Path:
    """Find the project root by locating orchestrator.py under /workspace/."""
    for child in Path("/workspace").iterdir():
        if child.is_dir() and (child / "orchestrator.py").exists():
            return child
    # Fallback
    return Path("/workspace/ninja")


NINJA_SQUAD_DIR = _find_project_root()

LOGS_DIR = Path("/workspace/logs")
AVATAR_BASE_URL = (
    "https://sites.super.betamyninja.ai/03e7e7b7-929a-4476-a11d-d7acad3951a4/a90f52f3"
)

# Claude session data
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CACHE_TTL = 10  # seconds

# Agent definitions
AGENTS = {
    "ninja": {
        "name": "Ninja",
        "role": "Browser Automation Agent",
        "emoji": "\U0001f47b",
        "color": "#38bdf8",
        "icon_url": f"{AVATAR_BASE_URL}/ninja.png",
    },
}


# ---------------------------------------------------------------------------
# Agent helpers
# ---------------------------------------------------------------------------
def get_agent_info():
    """Read current agent from settings file."""
    try:
        with open(AGENT_SETTINGS_FILE) as f:
            settings = json.load(f)
        agent_id = settings.get("default_agent", "ninja")
        agent = AGENTS.get(agent_id, AGENTS["ninja"]).copy()
        agent["id"] = agent_id
        agent["channel"] = settings.get("default_channel", "")
        agent["workspace"] = settings.get("workspace", "")
        return agent
    except Exception:
        return {**AGENTS["ninja"], "id": "ninja", "channel": "", "workspace": ""}


# ---------------------------------------------------------------------------
# Log file helpers
# ---------------------------------------------------------------------------
def get_log_files():
    """Get all log files sorted by modification time (newest first)."""
    files = []
    if LOGS_DIR.exists():
        for f in LOGS_DIR.glob("*.log"):
            files.append(
                {
                    "name": f.name,
                    "path": str(f),
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                }
            )
    files.sort(key=lambda x: x["modified"], reverse=True)
    return files


def tail_file(filepath, lines=500):
    """Read last N lines from a file in reverse order (newest first)."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        result = all_lines[-lines:] if len(all_lines) > lines else all_lines
        result.reverse()
        return "".join(result)
    except Exception as e:
        return f"Error reading log: {e}"


# ---------------------------------------------------------------------------
# Claude Monitor (integrated) - parses JSONL session files directly
# ---------------------------------------------------------------------------
class SessionData:
    """Parsed data from a single JSONL session file."""

    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_write_5m_tokens = 0
        self.cache_write_1h_tokens = 0
        self.cache_read_tokens = 0
        self.tool_uses = {}  # name -> count
        self.messages = 0
        self.prompts = []  # [{timestamp, content, response, uuid}]
        self.timeline = []  # [{timestamp, input_tokens, output_tokens, ...}]
        self.session_id = ""
        self.start_time = None
        self.last_time = None
        self.model = ""  # Model used in this session


def parse_jsonl_file(filepath: str) -> SessionData:
    """Parse a Claude JSONL session file and extract stats."""
    data = SessionData()
    data.session_id = Path(filepath).stem

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type", "")
                timestamp = entry.get("timestamp", "")

                # Track time range
                if timestamp:
                    if data.start_time is None or timestamp < data.start_time:
                        data.start_time = timestamp
                    if data.last_time is None or timestamp > data.last_time:
                        data.last_time = timestamp

                msg = entry.get("message", {})

                # Count messages
                if entry_type in ("user", "assistant"):
                    data.messages += 1

                # Extract model from assistant messages
                msg_model = msg.get("model", "")
                if msg_model and not data.model:
                    data.model = msg_model

                # Extract usage from assistant messages
                # Deduplicate: JSONL logs streaming chunks with identical usage
                usage = msg.get("usage", {})
                if usage:
                    inp = usage.get("input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    cr = usage.get("cache_read_input_tokens", 0)

                    # Split cache writes into 5m and 1h using the nested cache_creation object
                    cache_detail = usage.get("cache_creation", {})
                    cw_5m = cache_detail.get("ephemeral_5m_input_tokens", 0)
                    cw_1h = cache_detail.get("ephemeral_1h_input_tokens", 0)
                    # Fallback: if no cache_creation detail, assign all to 5m
                    if not cache_detail:
                        cw_5m = usage.get("cache_creation_input_tokens", 0)
                        cw_1h = 0

                    # Dedup: skip counting if usage is identical to previous entry
                    usage_key = (inp, out, cr, cw_5m, cw_1h)
                    is_dup = (
                        hasattr(data, "_prev_usage") and usage_key == data._prev_usage
                    )
                    data._prev_usage = usage_key

                    if not is_dup:
                        data.input_tokens += inp
                        data.output_tokens += out
                        data.cache_write_5m_tokens += cw_5m
                        data.cache_write_1h_tokens += cw_1h
                        data.cache_read_tokens += cr

                    # Timeline entry (only for non-duplicate)
                    if (
                        not is_dup
                        and timestamp
                        and (inp or out or cr or cw_5m or cw_1h)
                    ):
                        data.timeline.append(
                            {
                                "timestamp": timestamp,
                                "input_tokens": inp,
                                "output_tokens": out,
                                "cache_read_tokens": cr,
                                "cache_write_5m_tokens": cw_5m,
                                "cache_write_1h_tokens": cw_1h,
                            }
                        )

                # Extract tool uses from assistant messages
                content = msg.get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            name = item.get("name", "unknown")
                            data.tool_uses[name] = data.tool_uses.get(name, 0) + 1

                # Build trajectories: user text prompt → (tool_use → tool_result)* → final text
                if entry_type == "user":
                    user_content = msg.get("content", "")
                    if isinstance(user_content, str) and user_content.strip():
                        # New text prompt — start a new trajectory
                        data.prompts.append(
                            {
                                "timestamp": timestamp,
                                "content": user_content[:2000],
                                "response": "",
                                "uuid": entry.get("uuid", ""),
                                "steps": [],  # [{type, name/text, timestamp}]
                                "tool_count": 0,
                                "step_count": 0,
                            }
                        )
                    elif isinstance(user_content, list) and data.prompts:
                        # Tool results — part of current trajectory
                        data.prompts[-1]["step_count"] += 1
                        for item in user_content:
                            if (
                                isinstance(item, dict)
                                and item.get("type") == "tool_result"
                            ):
                                output = item.get("content", "")
                                if isinstance(output, list):
                                    output = " ".join(
                                        p.get("text", "")
                                        for p in output
                                        if isinstance(p, dict)
                                    )
                                data.prompts[-1]["steps"].append(
                                    {
                                        "type": "tool_result",
                                        "name": item.get("tool_use_id", ""),
                                        "output": str(output)[:1500],
                                        "timestamp": timestamp,
                                    }
                                )

                # Capture assistant actions (tool calls + text) inside the trajectory
                if entry_type == "assistant" and data.prompts:
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict):
                                if item.get("type") == "tool_use":
                                    tool_name = item.get("name", "unknown")
                                    tool_input = item.get("input", {})
                                    # Build a short summary of the tool input
                                    input_summary = ""
                                    if isinstance(tool_input, dict):
                                        cmd = tool_input.get(
                                            "command",
                                            tool_input.get(
                                                "description",
                                                tool_input.get("content", ""),
                                            ),
                                        )
                                        if cmd:
                                            input_summary = str(cmd)[:800]
                                        elif tool_input:
                                            # Fallback: show the full input dict as text
                                            input_summary = str(tool_input)[:800]
                                    data.prompts[-1]["tool_count"] += 1
                                    data.prompts[-1]["steps"].append(
                                        {
                                            "type": "tool_use",
                                            "name": tool_name,
                                            "input": input_summary,
                                            "timestamp": timestamp,
                                        }
                                    )
                                elif item.get("type") == "text":
                                    text_val = item.get("text", "").strip()
                                    if text_val:
                                        data.prompts[-1]["steps"].append(
                                            {
                                                "type": "assistant_text",
                                                "text": text_val[:1500],
                                                "timestamp": timestamp,
                                            }
                                        )
                                        # Keep updating response to capture the latest text
                                        data.prompts[-1]["response"] = text_val[:3000]

    except Exception as e:
        print(f"Error parsing {filepath}: {e}")

    return data


class StatsCache:
    """Caches aggregated stats with TTL."""

    def __init__(self):
        self._cache = {}
        self._last_update = 0
        self._lock = threading.Lock()

    def get_stats(self):
        with self._lock:
            now = time.time()
            if now - self._last_update < CACHE_TTL and self._cache:
                return self._cache
            self._cache = self._compute_stats()
            self._last_update = now
            return self._cache

    def _find_jsonl_files(self):
        """Find all JSONL session files."""
        files = []
        if CLAUDE_PROJECTS_DIR.exists():
            for jsonl in CLAUDE_PROJECTS_DIR.rglob("*.jsonl"):
                files.append(str(jsonl))
        return files

    def _compute_stats(self):
        """Parse all session files and compute aggregate stats."""
        files = self._find_jsonl_files()
        sessions = []
        total = SessionData()
        all_tool_uses = {}
        all_prompts = []
        all_timeline = []

        models_seen = {}  # model -> count of messages using it
        for f in files:
            sd = parse_jsonl_file(f)
            sessions.append(
                {
                    "session_id": sd.session_id,
                    "messages": sd.messages,
                    "input_tokens": sd.input_tokens,
                    "output_tokens": sd.output_tokens,
                    "cache_write_5m_tokens": sd.cache_write_5m_tokens,
                    "cache_write_1h_tokens": sd.cache_write_1h_tokens,
                    "cache_read_tokens": sd.cache_read_tokens,
                    "tool_uses": sum(sd.tool_uses.values()),
                    "start_time": sd.start_time,
                    "last_time": sd.last_time,
                    "model": sd.model,
                }
            )
            if sd.model:
                models_seen[sd.model] = models_seen.get(sd.model, 0) + 1

            total.input_tokens += sd.input_tokens
            total.output_tokens += sd.output_tokens
            total.cache_write_5m_tokens += sd.cache_write_5m_tokens
            total.cache_write_1h_tokens += sd.cache_write_1h_tokens
            total.cache_read_tokens += sd.cache_read_tokens
            total.messages += sd.messages

            for name, count in sd.tool_uses.items():
                all_tool_uses[name] = all_tool_uses.get(name, 0) + count

            all_prompts.extend(sd.prompts)
            all_timeline.extend(sd.timeline)

        total_tool_uses = sum(all_tool_uses.values())

        # Sort tool uses by count
        tool_summary = sorted(
            [{"name": k, "count": v} for k, v in all_tool_uses.items()],
            key=lambda x: x["count"],
            reverse=True,
        )

        # Sort prompts by timestamp (newest first)
        all_prompts.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        # Sort timeline by timestamp
        all_timeline.sort(key=lambda x: x.get("timestamp", ""))

        # Determine the most-used model (current model)
        current_model = ""
        if models_seen:
            current_model = max(models_seen, key=models_seen.get)

        # Calculate cost using the correct pricing for the detected model
        pricing = get_pricing_for_model(current_model)
        cost = (
            (total.input_tokens / 1_000_000) * pricing["input"]
            + (total.output_tokens / 1_000_000) * pricing["output"]
            + (total.cache_write_5m_tokens / 1_000_000) * pricing["cache_write_5m"]
            + (total.cache_write_1h_tokens / 1_000_000) * pricing["cache_write_1h"]
            + (total.cache_read_tokens / 1_000_000) * pricing["cache_read"]
        )

        return {
            "stats": {
                "total_input_tokens": total.input_tokens,
                "total_output_tokens": total.output_tokens,
                "total_cache_write_5m_tokens": total.cache_write_5m_tokens,
                "total_cache_write_1h_tokens": total.cache_write_1h_tokens,
                "total_cache_read_tokens": total.cache_read_tokens,
                "total_messages": total.messages,
                "total_tool_uses": total_tool_uses,
                "total_cost": round(cost, 4),
                "model": current_model,
                "models_seen": models_seen,
                "pricing": pricing,
            },
            "sessions": sessions,
            "tools": {
                "summary": tool_summary,
                "total": total_tool_uses,
            },
            "prompts": all_prompts[:50],
            "timeline": all_timeline,
        }


# Global stats cache instance
stats_cache = StatsCache()


# ---------------------------------------------------------------------------
# Routes - Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/agent")
def api_agent():
    """Get current agent info."""
    return jsonify(get_agent_info())


# ---------------------------------------------------------------------------
# Routes - Log files
# ---------------------------------------------------------------------------
@app.route("/api/logs")
def api_logs():
    """List available log files."""
    return jsonify({"files": get_log_files()})


@app.route("/api/logs/<filename>")
def api_log_content(filename):
    """Get content of a specific log file."""
    # Re-add .log extension if stripped (proxy may block .log URLs)
    if not filename.endswith(".log"):
        filename = filename + ".log"
    filepath = LOGS_DIR / filename
    if not filepath.exists() or not str(filepath).startswith(str(LOGS_DIR)):
        return jsonify({"error": "File not found"}), 404
    lines = int(request.args.get("lines", 200))
    content = tail_file(str(filepath), lines)
    return jsonify({"filename": filename, "content": content})


@app.route("/api/logs/<filename>/stream")
def api_log_stream(filename):
    """Stream log file updates via SSE."""
    if not filename.endswith(".log"):
        filename = filename + ".log"
    filepath = LOGS_DIR / filename
    if not filepath.exists():
        return jsonify({"error": "File not found"}), 404

    def generate():
        with open(str(filepath), "r") as f:
            # Start from end
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
                else:
                    time.sleep(0.5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Routes - Claude Monitor (direct, no proxy)
# ---------------------------------------------------------------------------
@app.route("/api/claude-monitor/stats")
def api_claude_stats():
    """Get aggregate Claude usage stats."""
    data = stats_cache.get_stats()
    return jsonify({"stats": data["stats"]})


@app.route("/api/claude-monitor/sessions")
def api_claude_sessions():
    """Get list of Claude sessions."""
    data = stats_cache.get_stats()
    return jsonify({"sessions": data["sessions"]})


@app.route("/api/claude-monitor/tools/summary")
def api_claude_tools():
    """Get tool usage summary."""
    data = stats_cache.get_stats()
    return jsonify(data["tools"])


@app.route("/api/claude-monitor/timeline")
def api_claude_timeline():
    """Get token usage timeline."""
    data = stats_cache.get_stats()
    return jsonify({"timeline": data["timeline"]})


@app.route("/api/claude-monitor/prompts")
def api_claude_prompts():
    """Get recent user prompts with responses."""
    data = stats_cache.get_stats()
    return jsonify({"prompts": data["prompts"]})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("\U0001f680 Starting Ninja Agent Dashboard...")
    agent = get_agent_info()
    print(f"   Agent: {agent['emoji']} {agent['name']} ({agent['role']})")
    print(f"   Logs: {LOGS_DIR}")
    print(f"   Claude sessions: {CLAUDE_PROJECTS_DIR}")

    # Show initial stats
    data = stats_cache.get_stats()
    s = data["stats"]
    print(f"   Sessions found: {len(data['sessions'])}")
    print(f"   Total messages: {s['total_messages']}")
    print(f"   Total tool uses: {s['total_tool_uses']}")
    print(f"   Total cost: ${s['total_cost']:.4f}")

    port = int(os.environ.get("DASHBOARD_PORT", 9000))
    app.run(host="0.0.0.0", port=port, debug=False)
