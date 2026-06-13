# Microsoft Teams Interface POC

This POC lets Phantom receive tasks from Microsoft Teams and post results back
through Microsoft Graph, using the same agent execution path as the Slack
monitor.

It is intentionally small:

- `teams_interface.py` is the Teams CLI/API layer.
- `teams_monitor.py` polls recent Teams messages and invokes Phantom.
- Teams channels default to mention-only handling, so normal channel chatter is
  ignored unless it contains `phantom` or `@phantom`.

## Requirements

You need a Microsoft Graph access token with Teams message permissions for the
target channel or chat. For channel mode, the token must be allowed to read and
send channel messages. For chat mode, the token must be allowed to read and send
chat messages.

For a fast POC, use a delegated Graph token for a dedicated Phantom service
account. Then save that service account's `/me` id as `teams.self_user_id` so
the monitor skips Phantom's own posts.

## Configure Channel Mode

```bash
python3 -m phantom.teams_interface config \
  --set-access-token "$MICROSOFT_GRAPH_ACCESS_TOKEN" \
  --set-team-id "$TEAM_ID" \
  --set-channel-id "$CHANNEL_ID" \
  --set-default channel

python3 -m phantom.teams_interface whoami --save-self
```

Send a test message:

```bash
python3 -m phantom.teams_interface say "Phantom Teams POC is online"
```

Read recent messages:

```bash
python3 -m phantom.teams_interface read --limit 20
```

Run the monitor:

```bash
python3 -m phantom.teams_monitor --interval 60
```

In Teams, type a request such as:

```text
phantom pull the latest pricing details for our top 5 competitors and summarize them
```

Phantom queues the message, runs the same Claude wrapper used by the Slack
monitor, and replies in the Teams channel thread using:

```bash
python3 -m phantom.teams_interface say "message" --reply-to <message_id>
```

## Configure Chat Mode

```bash
python3 -m phantom.teams_interface config \
  --set-access-token "$MICROSOFT_GRAPH_ACCESS_TOKEN" \
  --set-chat-id "$CHAT_ID" \
  --set-default chat

python3 -m phantom.teams_interface say "Phantom Teams chat POC is online"
python3 -m phantom.teams_monitor --interval 60
```

Chat messages do not use the channel-thread `--reply-to` flow in this POC.
Phantom posts a normal chat message as its response.

## Useful Discovery Commands

```bash
python3 -m phantom.teams_interface teams
python3 -m phantom.teams_interface channels --team-id "$TEAM_ID"
python3 -m phantom.teams_interface whoami --save-self
python3 -m phantom.teams_interface config
```

## Token Sources

`teams_interface.py` looks for a Microsoft Graph token in this order:

1. `--access-token`
2. Environment variables such as `MICROSOFT_GRAPH_ACCESS_TOKEN`
3. `~/.agent_settings.json["teams"]["access_token"]`
4. `/dev/shm/mcp-token` entries named `Microsoft Teams`, `Teams`, or
   `microsoft_teams`
5. Optional client credentials stored under the `teams` settings block

The CLI stores Teams config in a nested `teams` object and preserves existing
top-level Slack settings.

## Notes And Limitations

- This is a Graph polling POC, not a full Microsoft Bot Framework app.
- Channel mode replies in a Teams thread via Graph message replies.
- The default monitor mode requires a Phantom mention. Use `--all-human` only
  in a dedicated test channel.
- Delegated tokens post as the delegated account. A dedicated Phantom service
  account makes own-message skipping and audit trails much cleaner.
