# Pipedream Connect Integration Guide

Ninja ships with a Pipedream Connect integration that lets you connect 3,000+
third-party apps (GitHub, Google Sheets, Slack, Notion, …) to your agent and
trigger their actions from Claude's tool-use loop.

---

## Overview

```
S3 bucket (pipedream_credentials.json)
        │
        ▼
slack_interface.py (startup)
  └─ installs creds into ~/.agent_settings.json["pipedream"]
        │
        ├─ utils/pipedream.py      — server-side SDK wrapper
        │         (list apps, list accounts, create tokens, run actions)
        │
        └─ dashboard/integrations_app.py  — web UI on port 9020
                  (browse catalog, connect apps, manage accounts)
```

---

## 1. Credentials setup

Credentials are stored on S3 and downloaded to the agent at startup.

### Template

Upload a file to:

```
s3://<your-bucket>/pipedream-client/pipedream_credentials.json
```

with the following schema:

```json
{
  "_comment": "Pipedream Connect OAuth credentials — DO NOT commit to git",
  "client_id": "YOUR_OAUTH_CLIENT_ID",
  "client_secret": "YOUR_OAUTH_CLIENT_SECRET",
  "project_id": "proj_XXXXXXXXXXXX",
  "environment": "production"
}
```

Keys prefixed with `_` (like `_comment`) are stripped at install time.

### Where to get the values

1. Go to [pipedream.com/settings/api](https://pipedream.com/settings/api)
2. Click **New OAuth Client** → name it, click **Create**
3. Copy `client_id` and `client_secret` (secret is shown only once)
4. `project_id` is visible on your [Projects page](https://pipedream.com/projects) — it starts with `proj_`
5. `environment` is either `development` or `production`

### Rotation

To rotate credentials, upload a new file to the same S3 key. The next time
the Pipedream credentials are checked (at startup or token refresh), the new
values are picked up automatically.

---

## 2. agent_settings.json structure

After startup, `~/.agent_settings.json` contains:

```json
{
  "default_channel": "#channel-name",
  "default_channel_id": "C0B1K38ETGV",
  "default_team_id": "T0A9Q27KD1T",
  "default_team_name": "RenovateAI",
  "default_team_domain": "renovateai-hq",
  "workspace": "RenovateAI",
  "bot_token": "xoxe.xoxb-…",
  "pipedream": {
    "client_id": "…",
    "client_secret": "…",
    "project_id": "proj_24s3vb6",
    "environment": "production"
  }
}
```

The **external_user_id** used for all Pipedream Connect calls is derived as:

```
external_user_id = default_team_id + "." + default_channel_id
                 = "T0A9Q27KD1T.C0B1K38ETGV"
```

This scopes every connected account to the specific Slack channel (workspace +
channel), making it safe to run multiple Ninja instances in different channels
of the same workspace.

---

## 3. Python SDK wrapper (`utils/pipedream.py`)

```python
from utils.pipedream import PipedreamClient

pd = PipedreamClient()

# The Pipedream external_user_id for this sandbox channel
print(pd.external_user_id)  # "T0A9Q27KD1T.C0B1K38ETGV"

# Browse the app catalog
apps = pd.list_apps(q="google calendar", limit=10)
for app in apps:
    print(app["name_slug"], app["name"])

# List connected accounts for this user
accounts = pd.list_accounts()
for account in accounts:
    print(account["id"], account["app"]["name"])

# Create a short-lived Connect token (for the frontend)
result = pd.create_connect_token(expires_in=3600)
print(result["token"])     # used by the integrations dashboard
print(result["expires_at"])

# Run an action (requires a connected account for that app)
result = pd.run_action(
    "slack-send-message",
    configured_props={
        "slack": {"authProvisionId": "apn_abc123"},
        "channel": "#general",
        "text": "Hello from Ninja!",
    },
)
```

### Class reference

| Method                                                   | Description                                 |
| -------------------------------------------------------- | ------------------------------------------- |
| `PipedreamClient(settings_path, external_user_id)`       | Create client from `~/.agent_settings.json` |
| `pd.external_user_id`                                    | `"<team_id>.<channel_id>"`                  |
| `pd.project_id`                                          | Pipedream project ID                        |
| `pd.environment`                                         | `"production"` \| `"development"`           |
| `pd.create_connect_token(expires_in, ...)`               | Short-lived token for frontend OAuth flow   |
| `pd.list_apps(q, limit, has_actions, ...)`               | Browse app catalog (3,000+ apps)            |
| `pd.list_accounts(external_user_id, app, ...)`           | Connected accounts for this user            |
| `pd.delete_account(account_id)`                          | Disconnect an account                       |
| `pd.list_actions(app, limit)`                            | List available actions for an app           |
| `pd.run_action(key, external_user_id, configured_props)` | Execute an action                           |

### CLI

```bash
# Check credentials and identity
python -m utils.pipedream status

# Create a connect token
python -m utils.pipedream token

# Browse apps
python -m utils.pipedream apps --q "github" --limit 10

# List connected accounts
python -m utils.pipedream accounts
python -m utils.pipedream accounts --app slack
```

---

## 4. Integrations Dashboard (port 9020)

The dashboard runs as a Flask app on port **9020**, parallel to the agent
dashboard on port 9000.

### Features

- **Status page** — project ID, environment, external_user_id, Slack identity
- **Browse Apps** — searchable catalog of 3,000+ Pipedream apps with live search
- **Connected Accounts** — view and disconnect authorised app accounts

### Connecting an app

1. Open the dashboard at `http://localhost:9020` (or the public URL)
2. Go to **Browse Apps** and search for the app you want
3. Click **Connect** on the app card
4. The dashboard creates a secure, short-lived Connect token and opens the
   Pipedream OAuth flow in a new tab
5. Authorise the app — the account appears in **Connected Accounts**

### Running manually

```bash
cd /workspace/ninja/src/ninja/dashboard
INTEGRATIONS_PORT=9020 PYTHONPATH=/workspace/ninja/src/ninja python integrations_app.py
```

### Auto-start

The durable production install path is systemd, matching the main Ninja
agent dashboard. `install.sh` copies, enables, and starts
`systemd/ninja-integrations.service` alongside `ninja-dashboard.service`.

The repo also contains a `[program:integrations-dashboard]` block in
`supervisor/supervisord.conf` for environments that consume the bundled
supervisor config directly, but `install.sh` does not merge supervisor configs.

### systemd

`systemd/ninja-integrations.service` runs the dashboard on port 9020.

---

## 5. Using connected accounts in Claude tools

Once an account is connected, Claude can reference it by the account's
`external_id` (same as `external_user_id`) when calling Pipedream actions via
the MCP server or the `run_action()` method.

Example AGENT_PROTOCOL usage (for the `run_action` tool):

```
<tool_call>
  run_pipedream_action(
    key="github-create-issue",
    configured_props={
      "github": {"authProvisionId": "apn_abc123"},
      "repoFullName": "NinjaTech-AI/phantom",
      "title": "Bug: login fails on mobile",
      "body": "Steps to reproduce…"
    }
  )
</tool_call>
```

---

## 6. Failure modes and resilience

| Scenario                            | Behaviour                                                      |
| ----------------------------------- | -------------------------------------------------------------- |
| S3 credentials not yet uploaded     | Silent — ninja boots normally, Pipedream features disabled     |
| S3 access denied / network error    | Warning to stderr, ninja continues without Pipedream           |
| Invalid `project_id` format         | Warning to stderr, credentials not installed                   |
| Wrong `environment` value           | Warning to stderr, credentials not installed                   |
| Dashboard can't reach Pipedream API | Returns `{"ok": false, "error": "…"}` — UI shows error state   |
| Token expired during connect flow   | User is prompted to re-open the dashboard; new token is minted |

No failure mode blocks ninja startup or the main agent loop.

---

## 7. Security notes

- `client_secret` is stored in `~/.agent_settings.json` (root-owned, mode 600)
- The secret is **never** returned to the browser frontend — only short-lived
  connect tokens (4h TTL) are sent to the client
- Connect tokens are scoped to a single `external_user_id` and cannot access
  other users' accounts
- Rotate `client_secret` via the Pipedream API settings page, then re-upload
  the credentials file to S3 — the new secret propagates on the next restart

---

## 8. `pdx` — the LLM-facing CLI wrapper

`tools/pdx.py` is a lightweight, JSON-first command-line wrapper around the
Pipedream Connect SDK, installed as `/usr/local/bin/pdx` by `install.sh`.
Every invocation prints a single JSON object on stdout and exits 0/1/2/3
so the LLM can parse the result reliably.

**Exit codes:** `0` success · `1` bad usage · `2` not configured · `3` runtime error.

### 8.1 Subcommand cheat-sheet

| Command                                               | Purpose                                                    |
| ----------------------------------------------------- | ---------------------------------------------------------- |
| `pdx status`                                          | Project, environment, `external_user_id`                   |
| `pdx list`                                            | Apps the user has already onboarded (use this first!)      |
| `pdx apps --q github --limit 10`                      | Browse the public catalog                                  |
| `pdx actions <app_slug>`                              | Enumerate actions available for an app                     |
| `pdx describe <action_key>`                           | Show the props schema for one action                       |
| `pdx run <action_key> --args '{...}'`                 | Invoke an action via the **Connect Proxy** (default)       |
| `pdx run <key> --via actions-api`                     | Invoke via the paid Connect Components API (legacy path)   |
| `pdx http <app_slug> <METHOD> <url> [--json '{...}']` | Send any authenticated upstream HTTP request via the proxy |
| `pdx connect [app_slug]`                              | Mint a connect token + OAuth link for a new integration    |
| `pdx tools [--apps slack,github]`                     | Emit OpenAI-style tool schema for every connected action   |

### 8.2 Typical LLM session

```bash
# 1. See what's connected
pdx list
# → {"ok":true,"count":2,"data":[{"app_slug":"gmail",...},{"app_slug":"github",...}]}

# 2. Discover actions for an app
pdx actions github
# → {"ok":true,"count":33,"data":[{"key":"github-create-issue",...}, ...]}

# 3. Get the input schema for an action
pdx describe github-create-issue
# → {"ok":true, "props":{"title":{"type":"string","required":true}, ...}}

# 4. Run it
pdx run github-create-issue --arg repoFullname=acme/repo --arg title='Bug' --arg body='...'
# → {"ok":true, "result":{...}}
```

### 8.3 Direct LLM tool-calling integration

Dump an OpenAI-compatible tool schema for **every action of every
currently-connected app** — then pass the result straight into
`tools=[...]` of your chat completion call:

```bash
pdx tools > /tmp/tools.json
# → {"ok":true, "count":51, "tools":[{"type":"function","function":{"name":"gmail-send-email", ...}}]}
```

```python
import json, subprocess
from openai import OpenAI

tools = json.loads(subprocess.check_output(["pdx", "tools"]))["tools"]

resp = OpenAI().chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Send a hello email"}],
    tools=tools,
)

# When the LLM picks a tool, dispatch it back through pdx:
for call in resp.choices[0].message.tool_calls or []:
    out = subprocess.check_output([
        "pdx", "run", call.function.name,
        "--args", call.function.arguments,
    ])
    print(json.loads(out))
```

### 8.4 Error envelope

Every failure follows the same shape so the LLM can react programmatically:

```json
{
  "ok": false,
  "error": "Could not resolve action 'foo-bar' (tried 1 app/action splits). Last error: HTTP Error 404: Not Found"
}
```

For `pdx run` failures, the envelope also includes `action_key` and
`configured_props` so the LLM can retry with adjusted inputs.

### 8.5 Action schema source

Action metadata (name, description, props, required fields) is parsed
from Pipedream's **open-source component registry** at
`github.com/PipedreamHQ/pipedream/components/<app>/actions/<slug>/<slug>.mjs`,
cached in-process for 1 hour. This sidesteps the paid Connect API plan
requirement for discovery — discovery is always free.

## 9. Execution path: Connect Proxy (default) vs Connect Components API (legacy)

`pdx run` defaults to the **Pipedream Connect Proxy**, which is
available on Pipedream's free Connect plan and works for both
`auth_type=oauth` and `auth_type=keys` apps.

### 9.1 Why proxy by default

Pipedream offers two ways to act on behalf of a connected user:

1. **Connect Components / Actions API** (paid) — calls a registered
   component server-side via `actions.run()`. Requires the paid
   Components add-on; if not enabled, every `actions.run` call returns
   `403 Connect component API not enabled for this organization`.

2. **Connect Proxy** (free, default) — Pipedream forwards an
   authenticated HTTP request to the upstream API on the user's
   behalf, automatically injecting the OAuth bearer or API key.
   Documented at <https://pipedream.com/docs/connect/api-proxy>.

The Proxy is the only option that:

- Works on the free Connect plan.
- Supports both `auth_type=oauth` and `auth_type=keys` apps (verified
  empirically — proxy_enabled is true for ~90% of all apps including
  OpenAI, Anthropic, Stripe, SendGrid, Twilio, Resend, Postmark).
- Does **not** require us to return raw end-user credentials to the
  client. Pipedream's docs are explicit: _“Never return user
  credentials to the client.”_

### 9.2 How `pdx run` uses the proxy

For a curated set of known action keys, `tools/pdx.py` translates the
component invocation into a concrete HTTP request and sends it via the
proxy. The mapping lives in `utils/pdx_action_map.py` and looks like:

```python
"github-create-issue": ActionSignature(
    app_slug="github",
    method="POST",
    path_template="https://api.github.com/repos/{repoFullname}/issues",
    required_props=("repoFullname", "title"),
    path_props=("repoFullname",),
    body_props=("title", "body", "labels", "assignees", "milestone"),
),
```

For action keys **not** in the curated map, `pdx run` returns a clean
JSON error pointing the caller to `pdx http`.

### 9.3 `pdx http` — universal proxy passthrough

When you don't have (or don't want) a registered component, the
`pdx http` subcommand sends any authenticated upstream request via the
proxy:

```bash
# Gmail (oauth)
pdx http gmail GET https://www.googleapis.com/gmail/v1/users/me/profile

# Notion (oauth) — needs a custom upstream header
pdx http notion GET https://api.notion.com/v1/users/me \
    --header 'Notion-Version:2022-06-28'

# Resend (keys)
pdx http resend POST https://api.resend.com/emails \
    --json '{"from":"agent@example.com","to":["a@b.com"],"subject":"hi","text":"hello"}'
```

The CLI auto-resolves the user's `account_id` for the given app slug
(use `--account-id` to override). The response envelope is:

```json
{
  "ok": true,
  "app_slug": "gmail",
  "account_id": "apn_...",
  "request":  { "method": "GET", "url": "...", "headers": {}, "query": {}, "json": null },
  "response": { "status": 200, "headers": {...}, "body": {...} }
}
```

### 9.4 Restricted headers

Pipedream rejects requests carrying any of the following standard
headers: `Accept-Encoding`, `Connection`, `Content-Length`, `Cookie`,
`Date`, `Expect`, `Host`, `Keep-Alive`, `Origin`, `Referer`, `Trailer`,
`Transfer-Encoding`, `Upgrade`, `Via`, plus anything starting with
`Proxy-` or `Sec-`. `pdx http` validates this client-side before
sending.

#### Header forwarding (auto-prefixing)

Per the Pipedream Connect Proxy contract, **only headers prefixed with
`x-pd-proxy-` are forwarded to the upstream API**. To keep the CLI
ergonomic, `PipedreamProxyClient.request()` automatically rewrites any
caller-supplied header `Foo: bar` into `x-pd-proxy-Foo: bar` on the
wire. Headers that already carry the prefix (case-insensitive) are left
untouched, and `Content-Type` is passed through unprefixed because the
proxy uses it to frame the request body and Pipedream forwards it
upstream as-is.

That means you can write the natural form everywhere:

```bash
pdx http notion GET https://api.notion.com/v1/users/me \
  --header 'Notion-Version:2022-06-28'
```

…and the proxy will deliver `Notion-Version: 2022-06-28` to Notion.

### 9.5 Falling back to the legacy path

Projects with the paid Connect Components API enabled can still execute
the original SDK path via `--via actions-api`:

```bash
pdx run github-create-issue --args '{...}' --via actions-api
```

This calls `pipedream.actions.run(id=..., external_user_id=..., configured_props=...)`
through the official SDK.

### 9.6 Security guardrails

- End-user credentials are **never** exposed to the LLM, the dashboard
  JS, or any other client. Only Pipedream's proxy handles them.
- The Pipedream OAuth client credentials live in
  `~/.agent_settings.json["pipedream"]` and are loaded server-side
  only.
- `utils/pipedream_proxy.py` validates that requests do not carry
  restricted/sensitive headers before forwarding.
