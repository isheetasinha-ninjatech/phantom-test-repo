#!/usr/bin/env python3
"""
Phantom Integrations Dashboard  — port 9020
Full Pipedream Connect UI: browse apps, view actions, connect via OAuth.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, Response, jsonify, request
from flask_cors import CORS


# ─── path setup ─────────────────────────────────────────────────────────────
def _find_phantom_src() -> Optional[Path]:
    for c in [
        Path("/workspace/phantom/src/phantom"),
        Path(__file__).parent.parent,
        Path("/workspace/phantom"),
    ]:
        if (c / "utils" / "pipedream.py").exists():
            return c
    return None


_src = _find_phantom_src()
if _src and str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

# ─── Flask app ───────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

AGENT_SETTINGS = Path.home() / ".agent_settings.json"

# ─── Pipedream client (lazy, cached) ─────────────────────────────────────────
_pd = None
_pd_lock = threading.Lock()
_pd_err: Optional[str] = None


def _client():
    global _pd, _pd_err
    with _pd_lock:
        if _pd:
            return _pd
        try:
            from utils.pipedream import PipedreamClient  # type: ignore

            _pd = PipedreamClient()
            _pd_err = None
        except Exception as e:
            _pd_err = str(e)
    return _pd


def _settings() -> Dict[str, Any]:
    try:
        return json.loads(AGENT_SETTINGS.read_text())
    except Exception:
        return {}


# ─── GitHub actions fetcher ──────────────────────────────────────────────────
_actions_cache: Dict[str, Any] = {}
_actions_cache_lock = threading.Lock()

_GH_API = "https://api.github.com/repos/PipedreamHQ/pipedream/contents/components"
_GH_RAW = "https://raw.githubusercontent.com/PipedreamHQ/pipedream/master/components"

# Mapping from Pipedream app slug → GitHub component folder name
APP_SLUG_TO_GH: Dict[str, str] = {
    "slack_v2": "slack_v2",
    "slack_bot": "slack_bot",
    "github": "github",
    "google_sheets": "google_sheets",
    "google_drive": "google_drive",
    "google_calendar": "google_calendar",
    "notion": "notion",
    "hubspot": "hubspot",
    "salesforce_rest_api": "salesforce_rest_api",
    "openai": "openai",
    "anthropic": "anthropic",
    "telegram_bot_api": "telegram_bot_api",
    "linear_app": "linear_app",
    "jira": "jira",
    "zendesk": "zendesk",
    "gmail": "gmail",
    "discord_bot": "discord_bot",
    "discord": "discord",
    "stripe": "stripe",
    "shopify_developer_app": "shopify_developer_app",
    "twilio": "twilio",
    "airtable_oauth": "airtable_oauth",
    "dropbox": "dropbox",
    "asana": "asana",
    "trello": "trello",
    "monday": "monday",
    "gitlab": "gitlab",
    "mysql": "mysql",
    "postgresql": "postgresql",
    "mongodb": "mongodb",
    "aws": "aws",
    "sendgrid": "sendgrid",
    "zoom": "zoom",
    "microsoft_teams": "microsoft_teams",
    "outlook": "outlook",
    "calendar_hero": "calendly",
    "calendly": "calendly",
    "typeform": "typeform",
    "google_forms": "google_forms",
    "supabase": "supabase",
    "pinecone": "pinecone",
}


def _gh_get(url: str) -> Any:
    req = urllib.request.Request(
        url, headers={"User-Agent": "phantom-integrations/1.0"}
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def _slug_to_title(slug: str) -> str:
    """'create-issue-comment' → 'Create Issue Comment'"""
    return " ".join(w.capitalize() for w in slug.replace("-", " ").split())


def _parse_action_meta(mjs_content: str) -> Dict[str, str]:
    """Extract name, description, version from .mjs source."""
    name = re.search(r'name:\s*["`\'](.*?)["`\']', mjs_content)
    desc = re.search(r'description:\s*["`\'](.*?)["`\']', mjs_content, re.DOTALL)
    ver = re.search(r'version:\s*["`\']([\d.]+)["`\']', mjs_content)
    return {
        "name": name.group(1) if name else "",
        "description": (desc.group(1) if desc else "")
        .replace("\\n", " ")
        .strip()[:180],
        "version": ver.group(1) if ver else "",
    }


def _fetch_actions_for_app(app_slug: str) -> List[Dict[str, Any]]:
    """Return list of action dicts for a given app slug (GitHub-backed)."""
    with _actions_cache_lock:
        cached = _actions_cache.get(app_slug)
        if cached and (time.time() - cached["ts"]) < 3600:
            return cached["data"]

    gh_folder = APP_SLUG_TO_GH.get(app_slug, app_slug)
    results: List[Dict[str, Any]] = []

    try:
        url = f"{_GH_API}/{gh_folder}/actions"
        dirs = _gh_get(url)
        action_dirs = [
            d
            for d in dirs
            if d.get("type") == "dir" and not d["name"].startswith("common")
        ]
        # Fetch metadata for each action (first 30 only, parallel-ish)
        for d in action_dirs[:30]:
            slug = d["name"]
            key = f"{gh_folder}-{slug}"
            meta: Dict[str, str] = {}
            # Try to parse the .mjs file for name + description
            try:
                raw_url = f"{_GH_RAW}/{gh_folder}/actions/{slug}/{slug}.mjs"
                mjs = (
                    urllib.request.urlopen(
                        urllib.request.Request(
                            raw_url, headers={"User-Agent": "phantom/1.0"}
                        ),
                        timeout=5,
                    )
                    .read()
                    .decode(errors="ignore")
                )
                meta = _parse_action_meta(mjs)
            except Exception:
                pass
            results.append(
                {
                    "key": meta.get("name") and key or key,
                    "slug": slug,
                    "name": meta.get("name") or _slug_to_title(slug),
                    "description": meta.get("description", ""),
                    "version": meta.get("version", ""),
                    "app_slug": app_slug,
                }
            )
    except Exception as e:
        results = [{"error": str(e), "app_slug": app_slug}]

    with _actions_cache_lock:
        _actions_cache[app_slug] = {"data": results, "ts": time.time()}
    return results


# ─── API routes ──────────────────────────────────────────────────────────────


@app.route("/api/status")
def api_status():
    pd = _client()
    s = _settings()
    agent = {
        "team_id": s.get("default_team_id", ""),
        "team_name": s.get("workspace", ""),
        "team_domain": s.get("default_team_domain", ""),
        "channel": s.get("default_channel", ""),
        "channel_id": s.get("default_channel_id", ""),
        "project_id": (s.get("pipedream") or {}).get("project_id", ""),
        "environment": (s.get("pipedream") or {}).get("environment", ""),
        "pipedream_ok": bool(s.get("pipedream")),
        "external_user_id": (
            f"{s.get('default_team_id','')}.{s.get('default_channel_id','')}"
            if s.get("default_team_id") and s.get("default_channel_id")
            else None
        ),
    }
    if pd is None:
        return jsonify({"ok": False, "error": _pd_err, "agent": agent})
    return jsonify(
        {
            "ok": True,
            "project_id": pd.project_id,
            "environment": pd.environment,
            "external_user_id": pd.external_user_id,
            "agent": agent,
        }
    )


@app.route("/api/apps")
def api_apps():
    pd = _client()
    if pd is None:
        return jsonify({"ok": False, "error": _pd_err, "data": []}), 503
    q = request.args.get("q") or None
    limit = min(int(request.args.get("limit", 60)), 200)
    sort_k = request.args.get("sort_key", "featured_weight")
    sort_d = request.args.get("sort_direction", "desc")
    try:
        apps = pd.list_apps(q=q, limit=limit, sort_key=sort_k, sort_direction=sort_d)
        # Tag which apps have known GH actions
        for a in apps:
            a["has_gh_actions"] = a.get("name_slug") in APP_SLUG_TO_GH
        return jsonify({"ok": True, "data": apps, "count": len(apps)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "data": []}), 500


@app.route("/api/apps/<app_slug>/actions")
def api_app_actions(app_slug: str):
    actions = _fetch_actions_for_app(app_slug)
    return jsonify(
        {"ok": True, "data": actions, "count": len(actions), "app_slug": app_slug}
    )


@app.route("/api/accounts")
def api_accounts():
    pd = _client()
    if pd is None:
        return jsonify({"ok": False, "error": _pd_err, "data": []}), 503
    try:
        accounts = pd.list_accounts(app=request.args.get("app") or None, limit=200)
        return jsonify({"ok": True, "data": accounts, "count": len(accounts)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "data": []}), 500


@app.route("/api/accounts/<account_id>", methods=["DELETE"])
def api_delete_account(account_id: str):
    pd = _client()
    if pd is None:
        return jsonify({"ok": False, "error": _pd_err}), 503
    ok = pd.delete_account(account_id)
    return jsonify({"ok": ok})


@app.route("/api/connect/token", methods=["POST"])
def api_connect_token():
    pd = _client()
    if pd is None:
        return jsonify({"ok": False, "error": _pd_err}), 503
    body = request.get_json(silent=True) or {}
    expires_in = int(body.get("expires_in", 3600))
    success_uri = body.get("success_redirect_uri")
    error_uri = body.get("error_redirect_uri")
    webhook_uri = body.get("webhook_uri")
    try:
        kwargs: Dict[str, Any] = {"expires_in": expires_in}
        if success_uri:
            kwargs["success_redirect_uri"] = success_uri
        if error_uri:
            kwargs["error_redirect_uri"] = error_uri
        if webhook_uri:
            kwargs["webhook_uri"] = webhook_uri
        result = pd.create_connect_token(**kwargs)
        # The SDK returns token + expires_at; Pipedream also has connect_link_url
        # Get it directly from SDK response
        raw = pd._pd.tokens.create(
            external_user_id=pd.external_user_id,
            expires_in=expires_in,
            **({"success_redirect_uri": success_uri} if success_uri else {}),
            **({"error_redirect_uri": error_uri} if error_uri else {}),
        )
        return jsonify(
            {
                "ok": True,
                "token": raw.token,
                "connect_link_url": getattr(raw, "connect_link_url", None),
                "expires_at": str(raw.expires_at),
            }
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/oauth/success")
def oauth_success():
    """Redirect target after successful OAuth — tells the dashboard to refresh."""
    return Response(
        """<!DOCTYPE html><html><head>
<script>
  if(window.opener){ window.opener.postMessage({type:'pipedream_oauth_success'},'*'); }
  window.close();
</script></head><body style="background:#0d0d0f;color:#e2e4eb;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh">
<div style="text-align:center">
  <div style="font-size:48px;margin-bottom:16px">✅</div>
  <h2>Connected!</h2><p>You can close this window.</p>
</div></body></html>""",
        mimetype="text/html",
    )


@app.route("/oauth/error")
def oauth_error():
    return Response(
        """<!DOCTYPE html><html><head>
<script>
  if(window.opener){ window.opener.postMessage({type:'pipedream_oauth_error'},'*'); }
  window.close();
</script></head><body style="background:#0d0d0f;color:#e2e4eb;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh">
<div style="text-align:center">
  <div style="font-size:48px;margin-bottom:16px">❌</div>
  <h2>Connection failed</h2><p>You can close this window.</p>
</div></body></html>""",
        mimetype="text/html",
    )


# ─── Frontend SPA ─────────────────────────────────────────────────────────────
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Phantom Integrations</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0d0f;--s1:#16181c;--s2:#1e2028;--s3:#252830;
  --border:#2a2d36;--text:#e2e4eb;--muted:#7a7f94;--faint:#4a4f62;
  --purple:#7c3aed;--purple-l:#a78bfa;--purple-d:#6d28d9;
  --green:#22c55e;--yellow:#eab308;--red:#ef4444;--blue:#3b82f6;
  --r:10px;--font:system-ui,-apple-system,'Segoe UI',sans-serif
}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px;line-height:1.5}
/* layout */
.layout{display:flex;height:100vh;overflow:hidden}
/* sidebar */
.sidebar{
  width:228px;min-width:228px;background:var(--s1);
  border-right:1px solid var(--border);
  display:flex;flex-direction:column;
}
.sb-logo{
  display:flex;align-items:center;gap:10px;
  padding:18px 16px 16px;border-bottom:1px solid var(--border);
}
.sb-logo-icon{
  width:32px;height:32px;border-radius:8px;
  background:linear-gradient(135deg,#7c3aed,#4f46e5);
  display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;
}
.sb-logo-title{font-size:15px;font-weight:700;color:var(--purple-l)}
.sb-logo-sub{font-size:11px;color:var(--muted);margin-top:1px}
.sb-nav{padding:10px 0;flex:1;overflow-y:auto}
.sb-section{
  font-size:10.5px;font-weight:600;letter-spacing:.07em;
  color:var(--faint);text-transform:uppercase;
  padding:12px 16px 4px;
}
.nav-item{
  display:flex;align-items:center;gap:9px;
  padding:8px 16px;cursor:pointer;
  color:var(--muted);font-size:13.5px;font-weight:500;
  border-left:3px solid transparent;transition:all .12s;
  white-space:nowrap;
}
.nav-item:hover{color:var(--text);background:rgba(255,255,255,.04)}
.nav-item.active{color:var(--purple-l);border-left-color:var(--purple);background:rgba(124,58,237,.08)}
.nav-item svg{width:15px;height:15px;flex-shrink:0;opacity:.8}
.nav-item.active svg{opacity:1}
.nav-badge{
  margin-left:auto;font-size:10.5px;padding:1px 6px;
  border-radius:10px;background:rgba(124,58,237,.2);color:var(--purple-l);
  font-weight:600;min-width:18px;text-align:center;
}
.sb-footer{
  padding:12px 14px;border-top:1px solid var(--border);
  font-size:11.5px;color:var(--muted);
}
.sb-footer .env-dot{
  display:inline-block;width:7px;height:7px;
  border-radius:50%;background:var(--green);margin-right:5px;
}
/* main */
.main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
.topbar{
  background:var(--s1);border-bottom:1px solid var(--border);
  padding:12px 20px;display:flex;align-items:center;gap:12px;flex-shrink:0;
}
.topbar-title{font-size:15px;font-weight:700;flex:1}
.topbar-chips{display:flex;gap:6px;align-items:center}
.chip{
  font-size:11px;padding:3px 9px;border-radius:20px;
  border:1px solid rgba(124,58,237,.3);background:rgba(124,58,237,.1);
  color:var(--purple-l);
}
.chip.green{border-color:rgba(34,197,94,.3);background:rgba(34,197,94,.08);color:var(--green)}
.chip.grey{border-color:var(--border);background:var(--s2);color:var(--muted)}
/* pages */
.page{display:none;flex:1;overflow-y:auto;padding:20px}
.page.active{display:block}
/* search */
.search-wrap{position:relative;margin-bottom:16px}
.search-wrap .ico{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--muted);pointer-events:none}
.search-wrap .ico svg{width:14px;height:14px}
.search-input{
  width:100%;background:var(--s2);border:1px solid var(--border);
  border-radius:var(--r);padding:9px 12px 9px 34px;
  color:var(--text);font-size:13.5px;outline:none;transition:border .15s;
}
.search-input:focus{border-color:var(--purple)}
.search-input::placeholder{color:var(--muted)}
/* filters */
.filter-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;align-items:center}
.filter-btn{
  padding:5px 12px;border-radius:20px;font-size:12px;font-weight:500;
  border:1px solid var(--border);background:var(--s2);color:var(--muted);
  cursor:pointer;transition:all .12s;
}
.filter-btn:hover,.filter-btn.active{
  border-color:rgba(124,58,237,.4);background:rgba(124,58,237,.1);color:var(--purple-l);
}
/* app grid */
.app-grid{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(185px,1fr));
  gap:12px;
}
.app-card{
  background:var(--s1);border:1px solid var(--border);border-radius:var(--r);
  padding:14px;cursor:pointer;transition:all .15s;
  display:flex;flex-direction:column;gap:10px;position:relative;
}
.app-card:hover{
  border-color:rgba(124,58,237,.5);background:var(--s2);
  transform:translateY(-2px);box-shadow:0 6px 24px rgba(0,0,0,.35);
}
.app-card.connected{border-color:rgba(34,197,94,.3)}
.app-card-top{display:flex;align-items:flex-start;gap:10px}
.app-icon{
  width:38px;height:38px;border-radius:9px;
  object-fit:contain;background:var(--s3);flex-shrink:0;
  border:1px solid var(--border);padding:4px;
}
.app-icon-ph{
  width:38px;height:38px;border-radius:9px;
  background:linear-gradient(135deg,var(--purple),#4f46e5);
  display:flex;align-items:center;justify-content:center;
  color:#fff;font-size:15px;font-weight:700;flex-shrink:0;
}
.app-meta{min-width:0;flex:1}
.app-name{font-size:13px;font-weight:600;line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.app-cat{font-size:11px;color:var(--muted);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.app-auth-tag{
  font-size:10px;padding:1px 6px;border-radius:8px;
  background:var(--s3);border:1px solid var(--border);color:var(--faint);
}
.app-desc{font-size:11.5px;color:var(--muted);line-height:1.4;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.app-footer{display:flex;align-items:center;gap:6px;margin-top:auto}
.connect-btn{
  flex:1;padding:6px;border-radius:7px;font-size:12px;font-weight:600;
  text-align:center;cursor:pointer;transition:all .15s;border:none;
  background:rgba(124,58,237,.15);border:1px solid rgba(124,58,237,.3);color:var(--purple-l);
}
.connect-btn:hover{background:rgba(124,58,237,.3);border-color:var(--purple)}
.connect-btn.connected-btn{
  background:rgba(34,197,94,.08);border-color:rgba(34,197,94,.25);color:var(--green);
}
.connect-btn.connected-btn:hover{background:rgba(34,197,94,.15)}
.view-actions-btn{
  padding:6px 8px;border-radius:7px;font-size:11px;font-weight:500;
  border:1px solid var(--border);background:var(--s2);color:var(--muted);
  cursor:pointer;transition:all .12s;white-space:nowrap;
}
.view-actions-btn:hover{border-color:var(--purple);color:var(--purple-l)}
.connected-dot{
  width:7px;height:7px;border-radius:50%;background:var(--green);flex-shrink:0;
}
/* section headers */
.sec-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.sec-title{font-size:15px;font-weight:700}
.sec-sub{font-size:12px;color:var(--muted)}
/* accounts list */
.acct-list{display:flex;flex-direction:column;gap:8px}
.acct-card{
  background:var(--s1);border:1px solid var(--border);border-radius:var(--r);
  padding:12px 14px;display:flex;align-items:center;gap:12px;
  transition:background .12s;
}
.acct-card:hover{background:var(--s2)}
.acct-icon{width:36px;height:36px;border-radius:8px;object-fit:contain;background:var(--s3);padding:4px;border:1px solid var(--border);flex-shrink:0}
.acct-icon-ph{width:36px;height:36px;border-radius:8px;background:linear-gradient(135deg,var(--purple),#4f46e5);display:flex;align-items:center;justify-content:center;color:#fff;font-size:14px;font-weight:700;flex-shrink:0}
.acct-info{flex:1;min-width:0}
.acct-name{font-size:13.5px;font-weight:600}
.acct-app{font-size:12px;color:var(--muted)}
.acct-status{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--muted)}
.sdot{width:7px;height:7px;border-radius:50%;background:var(--green);flex-shrink:0}
.sdot.dead{background:var(--red)}.sdot.unknown{background:var(--yellow)}
.acct-actions{display:flex;gap:6px}
.acct-btn{
  padding:5px 10px;border-radius:6px;font-size:11.5px;cursor:pointer;transition:all .12s;
  border:1px solid var(--border);background:var(--s2);color:var(--muted);
}
.acct-btn:hover{border-color:var(--purple-l);color:var(--purple-l)}
.acct-btn.del{border-color:rgba(239,68,68,.2);background:rgba(239,68,68,.05);color:var(--red)}
.acct-btn.del:hover{background:rgba(239,68,68,.15);border-color:var(--red)}
/* actions panel (slide-in) */
.actions-panel{
  position:fixed;top:0;right:0;height:100vh;
  width:min(520px,95vw);background:var(--s1);
  border-left:1px solid var(--border);
  display:flex;flex-direction:column;
  transform:translateX(100%);transition:transform .25s cubic-bezier(.4,0,.2,1);
  z-index:200;box-shadow:-4px 0 32px rgba(0,0,0,.4);
}
.actions-panel.open{transform:translateX(0)}
.ap-header{
  padding:16px 18px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:12px;flex-shrink:0;
}
.ap-header-icon{
  width:36px;height:36px;border-radius:8px;
  object-fit:contain;background:var(--s3);padding:4px;border:1px solid var(--border);
}
.ap-header-info{flex:1;min-width:0}
.ap-app-name{font-size:14px;font-weight:700}
.ap-sub{font-size:12px;color:var(--muted);margin-top:1px}
.ap-close{
  width:30px;height:30px;border-radius:7px;
  background:var(--s2);border:1px solid var(--border);
  cursor:pointer;display:flex;align-items:center;justify-content:center;
  color:var(--muted);transition:all .12s;flex-shrink:0;
}
.ap-close:hover{border-color:var(--purple);color:var(--purple-l)}
.ap-connect-row{
  padding:12px 18px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:10px;flex-shrink:0;
  background:rgba(124,58,237,.04);
}
.ap-connect-btn{
  padding:8px 16px;border-radius:7px;font-size:13px;font-weight:600;
  background:var(--purple);border:none;color:#fff;cursor:pointer;transition:background .12s;
}
.ap-connect-btn:hover{background:var(--purple-d)}
.ap-connect-btn.connected{background:rgba(34,197,94,.15);color:var(--green);border:1px solid rgba(34,197,94,.3);}
.ap-connect-btn.connected:hover{background:rgba(34,197,94,.25)}
.ap-connect-info{font-size:12px;color:var(--muted);flex:1;min-width:0}
.ap-search{padding:12px 18px;border-bottom:1px solid var(--border);flex-shrink:0}
.ap-body{flex:1;overflow-y:auto;padding:12px 18px}
.action-item{
  padding:11px 12px;border-radius:8px;border:1px solid var(--border);
  background:var(--s2);margin-bottom:8px;
  display:flex;align-items:flex-start;gap:10px;
  transition:border-color .12s;
}
.action-item:hover{border-color:rgba(124,58,237,.35)}
.action-icon{
  width:28px;height:28px;border-radius:6px;
  background:rgba(124,58,237,.12);border:1px solid rgba(124,58,237,.2);
  display:flex;align-items:center;justify-content:center;
  color:var(--purple-l);flex-shrink:0;margin-top:1px;
}
.action-icon svg{width:13px;height:13px}
.action-info{flex:1;min-width:0}
.action-name{font-size:13px;font-weight:600;line-height:1.3}
.action-desc{font-size:11.5px;color:var(--muted);line-height:1.4;margin-top:2px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.action-version{font-size:10px;color:var(--faint);margin-top:3px}
/* status page */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;margin-bottom:20px}
.stat-card{background:var(--s1);border:1px solid var(--border);border-radius:var(--r);padding:16px}
.stat-label{font-size:11.5px;color:var(--muted);margin-bottom:5px}
.stat-val{font-size:22px;font-weight:700}
.stat-val.purple{color:var(--purple-l)}.stat-val.green{color:var(--green)}
.info-tbl{width:100%;border-collapse:collapse}
.info-tbl td{padding:9px 12px;border-bottom:1px solid var(--border);font-size:13px}
.info-tbl td:first-child{color:var(--muted);width:180px}
.info-tbl tr:last-child td{border-bottom:none}
.info-box{background:var(--s1);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;margin-bottom:20px}
/* modal */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:300;display:none;align-items:center;justify-content:center}
.overlay.open{display:flex}
.modal{background:var(--s1);border:1px solid var(--border);border-radius:14px;padding:24px;width:min(480px,92vw);box-shadow:0 24px 60px rgba(0,0,0,.5)}
.modal-title{font-size:17px;font-weight:700;margin-bottom:6px}
.modal-sub{color:var(--muted);font-size:13px;margin-bottom:18px}
.modal-foot{display:flex;gap:8px;justify-content:flex-end;margin-top:20px}
.btn{padding:8px 16px;border-radius:7px;font-size:13px;font-weight:600;cursor:pointer;transition:all .15s;border:none}
.btn-primary{background:var(--purple);color:#fff}.btn-primary:hover{background:var(--purple-d)}
.btn-outline{background:transparent;border:1px solid var(--border);color:var(--text)}.btn-outline:hover{border-color:var(--purple);color:var(--purple-l)}
.connect-progress{
  display:flex;flex-direction:column;align-items:center;gap:12px;padding:20px 0;text-align:center;
}
.connect-progress .app-icon-modal{width:52px;height:52px;border-radius:12px;padding:6px;background:var(--s2);border:1px solid var(--border);object-fit:contain}
.connect-progress h3{font-size:16px;font-weight:700}
.connect-progress p{font-size:13px;color:var(--muted);max-width:340px}
.open-link-btn{
  display:inline-block;padding:10px 24px;border-radius:8px;
  background:var(--purple);color:#fff;font-weight:600;font-size:14px;
  cursor:pointer;transition:background .12s;border:none;margin-top:4px;
}
.open-link-btn:hover{background:var(--purple-d)}
.waiting-indicator{
  display:flex;align-items:center;gap:8px;
  font-size:12px;color:var(--muted);margin-top:8px;
}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--yellow);animation:pulse 1.5s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.8)}}
/* empty + loading */
.empty{text-align:center;padding:50px 20px;color:var(--muted)}
.empty-ico{font-size:40px;margin-bottom:12px}
.empty h3{font-size:15px;font-weight:600;color:var(--text);margin-bottom:6px}
.empty p{font-size:13px;max-width:300px;margin:0 auto 16px}
.spin{width:22px;height:22px;border:2px solid var(--border);border-top-color:var(--purple);border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-ctr{display:flex;justify-content:center;padding:40px}
/* toast */
.toast{
  position:fixed;bottom:18px;right:18px;
  background:var(--s2);border:1px solid var(--border);
  border-radius:9px;padding:10px 16px;font-size:13px;color:var(--text);
  box-shadow:0 4px 20px rgba(0,0,0,.4);z-index:1000;
  transform:translateY(60px);opacity:0;transition:all .3s;
}
.toast.show{transform:translateY(0);opacity:1}
.toast.ok{border-color:rgba(34,197,94,.4)}
.toast.err{border-color:rgba(239,68,68,.4)}
/* panel overlay */
.panel-bg{position:fixed;inset:0;z-index:199;display:none}
.panel-bg.open{display:block}
</style>
</head>
<body>
<div class="layout">
<!-- ─── Sidebar ─────────────────────────────────── -->
<nav class="sidebar">
  <div class="sb-logo">
    <div class="sb-logo-icon">🔌</div>
    <div>
      <div class="sb-logo-title">Integrations</div>
      <div class="sb-logo-sub">Phantom Connect</div>
    </div>
  </div>
  <div class="sb-nav">
    <div class="sb-section">Overview</div>
    <div class="nav-item active" data-page="status" onclick="nav('status',this)">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6.5"/><path d="M8 5v3.5l2 1.5"/></svg>
      Status
    </div>
    <div class="sb-section">Integrations</div>
    <div class="nav-item" data-page="apps" onclick="nav('apps',this)">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1.5" y="1.5" width="5" height="5" rx="1.2"/><rect x="9.5" y="1.5" width="5" height="5" rx="1.2"/><rect x="1.5" y="9.5" width="5" height="5" rx="1.2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1.2"/></svg>
      Browse Apps
    </div>
    <div class="nav-item" data-page="accounts" onclick="nav('accounts',this)">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M13 14v-1a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v1"/><circle cx="8" cy="6" r="2.5"/></svg>
      Connected Accounts
      <span class="nav-badge" id="acct-badge" style="display:none">0</span>
    </div>
  </div>
  <div class="sb-footer">
    <span class="env-dot"></span>
    <span id="sb-env">production</span>
    &nbsp;·&nbsp;
    <span id="sb-project">—</span>
  </div>
</nav>

<!-- ─── Main ───────────────────────────────────── -->
<div class="main">
  <div class="topbar">
    <div class="topbar-title" id="topbar-title">Status</div>
    <div class="topbar-chips">
      <span class="chip green" id="chip-env">–</span>
      <span class="chip grey" id="chip-uid" style="display:none">–</span>
    </div>
  </div>

  <!-- Status page -->
  <div id="page-status" class="page active">
    <div class="stat-grid">
      <div class="stat-card"><div class="stat-label">Project ID</div><div class="stat-val purple" id="s-project">—</div></div>
      <div class="stat-card"><div class="stat-label">Environment</div><div class="stat-val" id="s-env">—</div></div>
      <div class="stat-card"><div class="stat-label">Connected Accounts</div><div class="stat-val green" id="s-accounts">—</div></div>
      <div class="stat-card"><div class="stat-label">Slack Workspace</div><div class="stat-val" id="s-workspace">—</div></div>
    </div>
    <div class="sec-hdr"><div class="sec-title">Identity & Configuration</div></div>
    <div class="info-box"><table class="info-tbl" id="s-table"><tr><td colspan="2" style="color:var(--muted)">Loading…</td></tr></table></div>
    <div class="sec-hdr"><div class="sec-title">Quick Actions</div></div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <button class="btn btn-primary" onclick="nav('apps',document.querySelector('[data-page=apps]'))">+ Connect an App</button>
      <button class="btn btn-outline" onclick="nav('accounts',document.querySelector('[data-page=accounts]'))">View Accounts</button>
      <button class="btn btn-outline" onclick="loadStatus()">↺ Refresh</button>
    </div>
  </div>

  <!-- Apps page -->
  <div id="page-apps" class="page">
    <div class="search-wrap">
      <div class="ico"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/></svg></div>
      <input class="search-input" id="app-q" placeholder="Search 3,000+ apps — GitHub, Slack, Notion, Salesforce…" oninput="onSearch(this.value)"/>
    </div>
    <div class="filter-row" id="filter-row">
      <span style="font-size:12px;color:var(--muted)">Filter:</span>
      <button class="filter-btn active" onclick="setFilter('all',this)">All</button>
      <button class="filter-btn" onclick="setFilter('oauth',this)">OAuth</button>
      <button class="filter-btn" onclick="setFilter('keys',this)">API Key</button>
      <button class="filter-btn" onclick="setFilter('connected',this)">Connected</button>
    </div>
    <div id="apps-ctr"><div class="loading-ctr"><div class="spin"></div></div></div>
  </div>

  <!-- Accounts page -->
  <div id="page-accounts" class="page">
    <div class="sec-hdr">
      <div class="sec-title">Connected Accounts</div>
      <button class="btn btn-primary" onclick="nav('apps',document.querySelector('[data-page=apps]'))">+ Connect App</button>
    </div>
    <div id="accounts-ctr"><div class="loading-ctr"><div class="spin"></div></div></div>
  </div>
</div>
</div>

<!-- ─── Actions slide panel ─────────────────────── -->
<div class="panel-bg" id="panel-bg" onclick="closePanel()"></div>
<div class="actions-panel" id="actions-panel">
  <div class="ap-header">
    <img id="ap-icon" class="ap-header-icon" src="" onerror="this.style.display='none'" alt=""/>
    <div class="ap-header-info">
      <div class="ap-app-name" id="ap-name">—</div>
      <div class="ap-sub" id="ap-sub">—</div>
    </div>
    <div class="ap-close" onclick="closePanel()">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 1l12 12M13 1L1 13"/></svg>
    </div>
  </div>
  <div class="ap-connect-row">
    <div class="ap-connect-info" id="ap-connect-info">Authorise this app to use it with Phantom.</div>
    <button class="ap-connect-btn" id="ap-connect-btn" onclick="triggerConnect()">Connect</button>
  </div>
  <div class="ap-search">
    <div class="search-wrap" style="margin:0">
      <div class="ico"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14"/></svg></div>
      <input class="search-input" id="action-q" placeholder="Search actions…" oninput="filterActions(this.value)" style="font-size:13px"/>
    </div>
  </div>
  <div class="ap-body" id="ap-body"><div class="loading-ctr"><div class="spin"></div></div></div>
</div>

<!-- ─── Connect modal ──────────────────────────── -->
<div class="overlay" id="connect-modal">
  <div class="modal">
    <div id="modal-body">Loading…</div>
    <div class="modal-foot" id="modal-foot">
      <button class="btn btn-outline" onclick="closeModal()">Cancel</button>
    </div>
  </div>
</div>

<!-- ─── Toast ──────────────────────────────────── -->
<div class="toast" id="toast"></div>

<script>
/* ══════════════════════════════════════════════════════════════════
   State
══════════════════════════════════════════════════════════════════ */
let _status = null;
let _apps = [];
let _accounts = [];
let _connectedSlugs = new Set();
let _activeFilter = 'all';
let _searchTimer = null;
let _panelSlug = null;
let _panelName = null;
let _panelIcon = null;
let _allPanelActions = [];
let _connectToken = null;
let _connectCheckInterval = null;

/* ══════════════════════════════════════════════════════════════════
   Navigation
══════════════════════════════════════════════════════════════════ */
const PAGE_TITLES = {status:'Status', apps:'Browse Apps', accounts:'Connected Accounts'};

function nav(pageId, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + pageId).classList.add('active');
  el.classList.add('active');
  document.getElementById('topbar-title').textContent = PAGE_TITLES[pageId];
  if (pageId === 'apps' && _apps.length === 0) loadApps();
  if (pageId === 'accounts') loadAccounts();
}

/* ══════════════════════════════════════════════════════════════════
   Toast
══════════════════════════════════════════════════════════════════ */
function toast(msg, type='') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  setTimeout(() => el.classList.remove('show'), 3500);
}

/* ══════════════════════════════════════════════════════════════════
   Status page
══════════════════════════════════════════════════════════════════ */
async function loadStatus() {
  try {
    const d = await api('/api/status');
    _status = d;
    const ag = d.agent || {};
    document.getElementById('s-project').textContent   = d.project_id || ag.project_id || '—';
    document.getElementById('s-env').textContent       = d.environment || ag.environment || '—';
    document.getElementById('s-workspace').textContent = ag.team_name || '—';
    document.getElementById('sb-env').textContent      = d.environment || '—';
    document.getElementById('sb-project').textContent  = (d.project_id || '').slice(-8) || '—';
    document.getElementById('chip-env').textContent    = d.ok ? (d.environment || '?') : '⚠ not configured';
    if (d.external_user_id) {
      const c = document.getElementById('chip-uid');
      c.textContent = d.external_user_id; c.style.display = '';
    }
    const rows = [
      ['Slack Team ID',      ag.team_id || '—'],
      ['Slack Channel',      (ag.channel||'—') + (ag.channel_id ? '  ('+ag.channel_id+')':'')],
      ['Team Domain',        ag.team_domain || '—'],
      ['External User ID',   d.external_user_id || '—'],
      ['Project ID',         d.project_id || ag.project_id || '—'],
      ['Environment',        d.environment || ag.environment || '—'],
      ['Pipedream Creds',    ag.pipedream_ok ? '✅ Installed' : '❌ Missing'],
    ];
    document.getElementById('s-table').innerHTML = rows.map(
      ([k,v]) => `<tr><td>${k}</td><td>${esc(v)}</td></tr>`
    ).join('');
    // account count
    api('/api/accounts').then(a => {
      document.getElementById('s-accounts').textContent = a.ok ? a.count : '—';
      updateBadge(a.count);
    });
  } catch(e) { console.error(e); }
}

function updateBadge(n) {
  const b = document.getElementById('acct-badge');
  if (n > 0) { b.textContent = n; b.style.display = ''; }
  else        { b.style.display = 'none'; }
}

/* ══════════════════════════════════════════════════════════════════
   Apps page
══════════════════════════════════════════════════════════════════ */
async function loadApps(q='') {
  const ctr = document.getElementById('apps-ctr');
  ctr.innerHTML = '<div class="loading-ctr"><div class="spin"></div></div>';
  const params = new URLSearchParams({limit:96, sort_key:'featured_weight', sort_direction:'desc'});
  if (q) params.set('q', q);
  try {
    const d = await api('/api/apps?' + params);
    if (!d.ok) { ctr.innerHTML = err(d.error); return; }
    _apps = d.data;
    renderApps();
  } catch(e) { ctr.innerHTML = err(e); }
}

function renderApps() {
  const q = (document.getElementById('action-q') || {value:''}).value;
  let list = _apps;
  if (_activeFilter === 'oauth')      list = list.filter(a => a.auth_type === 'oauth');
  else if (_activeFilter === 'keys')  list = list.filter(a => a.auth_type === 'keys');
  else if (_activeFilter === 'connected') list = list.filter(a => _connectedSlugs.has(a.name_slug));

  const ctr = document.getElementById('apps-ctr');
  if (!list.length) {
    ctr.innerHTML = `<div class="empty"><div class="empty-ico">🔍</div><h3>No apps found</h3><p>Try a different search or filter.</p></div>`;
    return;
  }
  ctr.innerHTML = `<div style="font-size:12px;color:var(--muted);margin-bottom:12px">${list.length} apps</div>
    <div class="app-grid">${list.map(appCard).join('')}</div>`;
}

function appCard(a) {
  const slug = a.name_slug || '';
  const connected = _connectedSlugs.has(slug);
  const letter = (a.name || slug || '?')[0].toUpperCase();
  const icon = a.img_src
    ? `<img class="app-icon" src="${esc(a.img_src)}" onerror="this.outerHTML='<div class=app-icon-ph>${letter}</div>'" alt=""/>`
    : `<div class="app-icon-ph">${letter}</div>`;
  const cat = (a.categories||[]).slice(0,1).join('');
  const auth = a.auth_type ? `<span class="app-auth-tag">${esc(a.auth_type)}</span>` : '';
  const connectLabel = connected ? 'Connected ✓' : 'Connect';
  const connectClass = connected ? 'connect-btn connected-btn' : 'connect-btn';
  const hasActions = a.has_gh_actions;
  return `<div class="app-card${connected?' connected':''}" data-slug="${slug}">
    <div class="app-card-top">
      ${icon}
      <div class="app-meta">
        <div class="app-name" title="${esc(a.name||slug)}">${esc(a.name||slug)}</div>
        <div class="app-cat">${esc(cat)} ${auth}</div>
      </div>
      ${connected ? '<div class="connected-dot" title="Connected"></div>' : ''}
    </div>
    ${a.description ? `<div class="app-desc">${esc(a.description)}</div>` : ''}
    <div class="app-footer">
      <button class="${connectClass}" onclick="event.stopPropagation();startConnect('${slug}','${esc(a.name||slug)}','${esc(a.img_src||'')}')">
        ${connectLabel}
      </button>
      ${hasActions ? `<button class="view-actions-btn" onclick="event.stopPropagation();openPanel('${slug}','${esc(a.name||slug)}','${esc(a.img_src||'')}')">Actions ›</button>` : ''}
    </div>
  </div>`;
}

function onSearch(v) {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(() => loadApps(v), 380);
}

function setFilter(f, el) {
  _activeFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  if (f === 'connected' && _accounts.length === 0) {
    loadAccounts().then(renderApps);
  } else {
    renderApps();
  }
}

/* ══════════════════════════════════════════════════════════════════
   Accounts page
══════════════════════════════════════════════════════════════════ */
async function loadAccounts() {
  const ctr = document.getElementById('accounts-ctr');
  if (ctr) ctr.innerHTML = '<div class="loading-ctr"><div class="spin"></div></div>';
  try {
    const d = await api('/api/accounts');
    if (!d.ok) { if (ctr) ctr.innerHTML = err(d.error); return; }
    _accounts = d.data;
    _connectedSlugs = new Set(_accounts.map(a => (a.app||{}).name_slug).filter(Boolean));
    updateBadge(d.count);
    document.getElementById('s-accounts').textContent = d.count;
    if (!ctr) return;
    if (d.count === 0) {
      ctr.innerHTML = `<div class="empty">
        <div class="empty-ico">🔗</div>
        <h3>No connected accounts yet</h3>
        <p>Browse the app catalog and click <strong>Connect</strong> to add an integration.</p>
        <button class="btn btn-primary" onclick="nav('apps',document.querySelector('[data-page=apps]'))">Browse Apps</button>
      </div>`;
      return;
    }
    ctr.innerHTML = `<div class="acct-list">${d.data.map(acctCard).join('')}</div>`;
  } catch(e) { if (ctr) ctr.innerHTML = err(e); }
}

function acctCard(a) {
  const ap = a.app || {};
  const letter = (ap.name || '?')[0].toUpperCase();
  const icon = ap.img_src
    ? `<img class="acct-icon" src="${esc(ap.img_src)}" onerror="this.outerHTML='<div class=acct-icon-ph>${letter}</div>'" alt=""/>`
    : `<div class="acct-icon-ph">${letter}</div>`;
  const healthy = a.dead ? 'dead' : (a.healthy === false ? 'unknown' : '');
  const statusLabel = a.dead ? 'Dead' : (a.healthy ? 'Healthy' : 'Unknown');
  return `<div class="acct-card">
    ${icon}
    <div class="acct-info">
      <div class="acct-name">${esc(a.name || a.id)}</div>
      <div class="acct-app">${esc(ap.name || '—')} · ${esc(ap.auth_type || '—')}</div>
    </div>
    <div class="acct-status"><div class="sdot ${healthy}"></div>${statusLabel}</div>
    <div class="acct-actions">
      ${ap.name_slug ? `<button class="acct-btn" onclick="openPanel('${esc(ap.name_slug)}','${esc(ap.name||'')}','${esc(ap.img_src||'')}')">Actions</button>` : ''}
      <button class="acct-btn del" onclick="deleteAccount('${esc(a.id)}',this)">Disconnect</button>
    </div>
  </div>`;
}

async function deleteAccount(id, btn) {
  if (!confirm('Disconnect this account?')) return;
  btn.disabled = true; btn.textContent = '…';
  const d = await api('/api/accounts/' + id, {method:'DELETE'});
  if (d.ok) { toast('Account disconnected', 'ok'); loadAccounts(); }
  else      { toast('Error: ' + d.error, 'err'); btn.disabled=false; btn.textContent='Disconnect'; }
}

/* ══════════════════════════════════════════════════════════════════
   Actions panel
══════════════════════════════════════════════════════════════════ */
async function openPanel(slug, name, iconUrl) {
  _panelSlug = slug;
  _panelName = name;
  _panelIcon = iconUrl;
  _allPanelActions = [];
  document.getElementById('ap-name').textContent = name;
  document.getElementById('ap-icon').src = iconUrl;
  document.getElementById('ap-sub').textContent = slug;
  document.getElementById('action-q').value = '';
  // Connect button state
  const isConnected = _connectedSlugs.has(slug);
  const connectBtn = document.getElementById('ap-connect-btn');
  const connectInfo = document.getElementById('ap-connect-info');
  if (isConnected) {
    connectBtn.textContent = '✓ Connected';
    connectBtn.className = 'ap-connect-btn connected';
    connectInfo.textContent = 'This app is connected. Click to reconnect or add another account.';
  } else {
    connectBtn.textContent = 'Connect';
    connectBtn.className = 'ap-connect-btn';
    connectInfo.textContent = `Authorise ${name} to use its actions with Phantom.`;
  }
  document.getElementById('ap-body').innerHTML = '<div class="loading-ctr"><div class="spin"></div></div>';
  document.getElementById('actions-panel').classList.add('open');
  document.getElementById('panel-bg').classList.add('open');
  try {
    const d = await api('/api/apps/' + encodeURIComponent(slug) + '/actions');
    _allPanelActions = d.data || [];
    renderPanelActions(_allPanelActions);
  } catch(e) {
    document.getElementById('ap-body').innerHTML = err(e);
  }
}

function renderPanelActions(actions) {
  const body = document.getElementById('ap-body');
  if (!actions || !actions.length) {
    body.innerHTML = `<div class="empty" style="padding:30px 10px">
      <div class="empty-ico">⚡</div>
      <h3>No actions found</h3>
      <p>This app may use a different integration path or actions aren't available in the public registry.</p>
    </div>`;
    return;
  }
  body.innerHTML = `<div style="font-size:11.5px;color:var(--muted);margin-bottom:10px">${actions.length} actions available</div>`
    + actions.map(actionItem).join('');
}

function actionItem(a) {
  if (a.error) return `<div style="color:var(--red);font-size:12px;padding:10px">${esc(a.error)}</div>`;
  return `<div class="action-item">
    <div class="action-icon">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M9 1L5 9h5l-3 6"/>
      </svg>
    </div>
    <div class="action-info">
      <div class="action-name">${esc(a.name)}</div>
      ${a.description ? `<div class="action-desc">${esc(a.description)}</div>` : ''}
      ${a.version ? `<div class="action-version">v${esc(a.version)}</div>` : ''}
    </div>
  </div>`;
}

function filterActions(q) {
  if (!q) { renderPanelActions(_allPanelActions); return; }
  const lq = q.toLowerCase();
  renderPanelActions(_allPanelActions.filter(a =>
    (a.name||'').toLowerCase().includes(lq) ||
    (a.description||'').toLowerCase().includes(lq) ||
    (a.slug||'').toLowerCase().includes(lq)
  ));
}

function closePanel() {
  document.getElementById('actions-panel').classList.remove('open');
  document.getElementById('panel-bg').classList.remove('open');
}

function triggerConnect() {
  closePanel();
  if (_panelSlug) startConnect(_panelSlug, _panelName, _panelIcon);
}

/* ══════════════════════════════════════════════════════════════════
   Connect flow (OAuth via Pipedream)
══════════════════════════════════════════════════════════════════ */
async function startConnect(slug, name, iconUrl) {
  // Show modal with loading state
  openModal(`<div class="connect-progress">
    <div class="empty-ico">🔌</div>
    <h3>Connecting ${esc(name)}</h3>
    <p>Preparing secure OAuth flow…</p>
    <div class="spin"></div>
  </div>`);
  // Mint a token
  let tokenData;
  try {
    tokenData = await api('/api/connect/token', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        expires_in: 3600,
        success_redirect_uri: window.location.origin + '/oauth/success',
        error_redirect_uri:   window.location.origin + '/oauth/error',
      })
    });
    if (!tokenData.ok) throw new Error(tokenData.error);
  } catch(e) {
    setModalBody(`<div style="text-align:center;padding:20px">
      <div style="font-size:36px;margin-bottom:12px">❌</div>
      <h3 class="modal-title">Token error</h3>
      <p class="modal-sub">${esc(String(e))}</p>
    </div>`);
    return;
  }
  _connectToken = tokenData;
  // Build the connect URL with the app pre-selected
  const baseUrl = tokenData.connect_link_url || `https://pipedream.com/_static/connect.html?token=${tokenData.token}&connectLink=true`;
  const connectUrl = baseUrl + `&app=${encodeURIComponent(slug)}`;
  const icon = iconUrl ? `<img src="${esc(iconUrl)}" class="app-icon-modal" onerror="this.style.display='none'" alt=""/>` : `<div class="empty-ico">🔌</div>`;
  setModalBody(`<div class="connect-progress">
    ${icon}
    <h3>Connect ${esc(name)}</h3>
    <p>Click the button below to authorise <strong>${esc(name)}</strong>.<br/>After connecting, this window will update automatically.</p>
    <button class="open-link-btn" id="open-oauth-btn" onclick="openOAuthWindow('${esc(connectUrl)}')">
      Open ${esc(name)} OAuth →
    </button>
    <div class="waiting-indicator" id="waiting-row" style="display:none">
      <div class="pulse"></div>
      <span>Waiting for connection…</span>
    </div>
    <p style="font-size:11px;color:var(--faint);margin-top:8px">Token expires ${new Date(tokenData.expires_at).toLocaleTimeString()}</p>
  </div>`);
  setModalFoot(`<button class="btn btn-outline" onclick="closeModal()">Cancel</button>`);
}

let _oauthWindow = null;
function openOAuthWindow(url) {
  document.getElementById('open-oauth-btn').textContent = 'Reconnect if needed ↗';
  document.getElementById('waiting-row').style.display = 'flex';
  _oauthWindow = window.open(url, '_blank', 'width=600,height=700,scrollbars=yes');
  // Poll for window close
  clearInterval(_connectCheckInterval);
  _connectCheckInterval = setInterval(() => {
    if (_oauthWindow && _oauthWindow.closed) {
      clearInterval(_connectCheckInterval);
      onOAuthComplete();
    }
  }, 1000);
}

function onOAuthComplete() {
  closeModal();
  toast('Checking connection…');
  setTimeout(async () => {
    await loadAccounts();
    renderApps();
    toast('Accounts refreshed ✓', 'ok');
  }, 1500);
}

// Listen for postMessage from /oauth/success
window.addEventListener('message', e => {
  if (e.data && e.data.type === 'pipedream_oauth_success') {
    clearInterval(_connectCheckInterval);
    if (_oauthWindow) _oauthWindow.close();
    onOAuthComplete();
    toast('Connected! ✅', 'ok');
  } else if (e.data && e.data.type === 'pipedream_oauth_error') {
    clearInterval(_connectCheckInterval);
    toast('OAuth failed — please try again', 'err');
    closeModal();
  }
});

/* ══════════════════════════════════════════════════════════════════
   Modal helpers
══════════════════════════════════════════════════════════════════ */
function openModal(body, foot) {
  setModalBody(body);
  setModalFoot(foot || '<button class="btn btn-outline" onclick="closeModal()">Close</button>');
  document.getElementById('connect-modal').classList.add('open');
}
function setModalBody(html) {
  document.getElementById('modal-body').innerHTML = html;
}
function setModalFoot(html) {
  document.getElementById('modal-foot').innerHTML = html;
}
function closeModal() {
  document.getElementById('connect-modal').classList.remove('open');
  clearInterval(_connectCheckInterval);
}
document.getElementById('connect-modal').addEventListener('click', e => {
  if (e.target.id === 'connect-modal') closeModal();
});

/* ══════════════════════════════════════════════════════════════════
   Helpers
══════════════════════════════════════════════════════════════════ */
async function api(url, opts) {
  const r = await fetch(url, opts);
  return r.json();
}
function err(e) {
  return `<div style="color:var(--red);padding:20px;font-size:13px">Error: ${esc(String(e))}</div>`;
}
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* ══════════════════════════════════════════════════════════════════
   Init
══════════════════════════════════════════════════════════════════ */
loadStatus();
loadAccounts();  // pre-load so connected-filter + cards work immediately
</script>
</body>
</html>"""


@app.route("/")
def index():
    return Response(_HTML, mimetype="text/html")


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("INTEGRATIONS_PORT", 9020))
    print(f"🔌 Phantom Integrations Dashboard → http://0.0.0.0:{port}", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
