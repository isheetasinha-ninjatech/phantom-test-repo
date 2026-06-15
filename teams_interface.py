"""Microsoft Teams Interface CLI (POC).

This mirrors the small command shape of ``slack_interface.py`` and
``whatsapp_interface.py`` while using Microsoft Graph as the transport.

Typical setup:

    python -m phantom.teams_interface config \
      --set-access-token "$MICROSOFT_GRAPH_ACCESS_TOKEN" \
      --set-team-id "$TEAM_ID" \
      --set-channel-id "$CHANNEL_ID" \
      --set-default channel

    python -m phantom.teams_interface say "Phantom is online"
    python -m phantom.teams_interface read --limit 20

The POC expects a Microsoft Graph access token with Teams message scopes.
Token lookup order is: explicit CLI flag, environment, nested
``~/.agent_settings.json["teams"]``, then ``/dev/shm/mcp-token``.
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional


SETTINGS_PATH = Path.home() / ".agent_settings.json"
DEFAULT_CONFIG_PATH = str(SETTINGS_PATH)
MCP_TOKEN_PATH = "/dev/shm/mcp-token"
GRAPH_BASE_URL = os.environ.get(
    "MICROSOFT_GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0"
)
TOKEN_BASE_URL = os.environ.get(
    "MICROSOFT_LOGIN_BASE_URL", "https://login.microsoftonline.com"
)

TOKEN_ENV_KEYS = (
    "TEAMS_ACCESS_TOKEN",
    "MS_TEAMS_ACCESS_TOKEN",
    "MICROSOFT_TEAMS_ACCESS_TOKEN",
    "MICROSOFT_GRAPH_ACCESS_TOKEN",
    "GRAPH_ACCESS_TOKEN",
)

MCP_TOKEN_KEYS = (
    "Microsoft Teams",
    "MicrosoftTeams",
    "MS Teams",
    "MSTeams",
    "Teams",
    "microsoft_teams",
    "Microsoft Graph",
    "MicrosoftGraph",
)


class TeamsConfigError(RuntimeError):
    """Raised when the Teams POC is missing destination or token config."""


class TeamsAPIError(RuntimeError):
    """Raised when Microsoft Graph returns a non-success response."""

    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        super().__init__(
            f"Microsoft Graph request failed: status={status} body={payload!r}"
        )


@dataclass
class TeamsConfig:
    access_token: Optional[str] = None
    tenant_id: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    team_id: Optional[str] = None
    channel_id: Optional[str] = None
    chat_id: Optional[str] = None
    default_destination: Optional[str] = None
    self_user_id: Optional[str] = None
    self_app_id: Optional[str] = None
    last_read_ids: dict[str, str] = field(default_factory=dict)
    access_token_expires_at: Optional[int] = None

    @classmethod
    def load(cls, filepath: str = DEFAULT_CONFIG_PATH) -> "TeamsConfig":
        try:
            with open(filepath, "r") as f:
                data = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            data = {}

        block = data.get("teams")
        if not isinstance(block, dict):
            block = {}

        return cls(
            access_token=_str_or_none(block.get("access_token")),
            tenant_id=_str_or_none(block.get("tenant_id")),
            client_id=_str_or_none(block.get("client_id")),
            client_secret=_str_or_none(block.get("client_secret")),
            team_id=_str_or_none(block.get("team_id")),
            channel_id=_str_or_none(block.get("channel_id")),
            chat_id=_str_or_none(block.get("chat_id")),
            default_destination=_str_or_none(block.get("default_destination")),
            self_user_id=_str_or_none(block.get("self_user_id")),
            self_app_id=_str_or_none(block.get("self_app_id")),
            last_read_ids=(
                block.get("last_read_ids")
                if isinstance(block.get("last_read_ids"), dict)
                else {}
            ),
            access_token_expires_at=(
                int(block["access_token_expires_at"])
                if isinstance(block.get("access_token_expires_at"), (int, float))
                else None
            ),
        )

    def to_settings(self, *, include_secret: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "access_token": self.access_token,
            "tenant_id": self.tenant_id,
            "client_id": self.client_id,
            "client_secret": self.client_secret if include_secret else None,
            "team_id": self.team_id,
            "channel_id": self.channel_id,
            "chat_id": self.chat_id,
            "default_destination": self.default_destination,
            "self_user_id": self.self_user_id,
            "self_app_id": self.self_app_id,
            "last_read_ids": self.last_read_ids or None,
            "access_token_expires_at": self.access_token_expires_at,
        }
        return {k: v for k, v in data.items() if v not in (None, "", {})}

    def save(self, filepath: str = DEFAULT_CONFIG_PATH) -> None:
        try:
            with open(filepath, "r") as f:
                settings = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        settings["teams"] = self.to_settings()
        with open(filepath, "w") as f:
            json.dump(settings, f, indent=2)


@dataclass(frozen=True)
class TeamsDestination:
    kind: str
    team_id: Optional[str] = None
    channel_id: Optional[str] = None
    chat_id: Optional[str] = None

    @property
    def key(self) -> str:
        if self.kind == "chat":
            return f"chat:{self.chat_id}"
        return f"channel:{self.team_id}:{self.channel_id}"

    @property
    def label(self) -> str:
        if self.kind == "chat":
            return f"chat {self.chat_id}"
        return f"team {self.team_id} / channel {self.channel_id}"

    def messages_path(self) -> str:
        if self.kind == "chat":
            return f"/chats/{_quote(self.chat_id)}/messages"
        return (
            f"/teams/{_quote(self.team_id)}/channels/"
            f"{_quote(self.channel_id)}/messages"
        )

    def reply_path(self, message_id: str) -> str:
        if self.kind == "chat":
            raise TeamsConfigError(
                "Teams chat messages do not support channel-style threaded replies"
            )
        return (
            f"/teams/{_quote(self.team_id)}/channels/{_quote(self.channel_id)}"
            f"/messages/{_quote(message_id)}/replies"
        )

    def reaction_path(self, message_id: str, reply_to_id: Optional[str] = None) -> str:
        if self.kind == "chat":
            return (
                f"/chats/{_quote(self.chat_id)}/messages/{_quote(message_id)}"
                f"/setReaction"
            )
        if reply_to_id and reply_to_id != message_id:
            return (
                f"/teams/{_quote(self.team_id)}/channels/{_quote(self.channel_id)}"
                f"/messages/{_quote(reply_to_id)}/replies/{_quote(message_id)}/setReaction"
            )
        return (
            f"/teams/{_quote(self.team_id)}/channels/{_quote(self.channel_id)}"
            f"/messages/{_quote(message_id)}/setReaction"
        )

    def files_folder_path(self) -> str:
        if self.kind != "channel":
            raise TeamsConfigError("Teams file upload currently requires channel mode")
        return (
            f"/teams/{_quote(self.team_id)}/channels/"
            f"{_quote(self.channel_id)}/filesFolder"
        )


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() in {"br", "p", "div", "li"}:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "div", "li"}:
            self._newline()

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _str_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _quote(value: Optional[str]) -> str:
    if not value:
        raise TeamsConfigError("missing Teams destination value")
    return urllib.parse.quote(value, safe="")


def _mask(value: Optional[str], *, keep: int = 5) -> str:
    if not value:
        return "-"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def html_to_text(content: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(content or "")
    return parser.text()


def text_to_teams_html(text: str) -> str:
    escaped = html.escape(text or "")
    return escaped.replace("\n", "<br>")


def parse_mcp_tokens(filepath: str = MCP_TOKEN_PATH) -> dict[str, Any]:
    tokens: dict[str, Any] = {}
    try:
        with open(filepath, "r") as f:
            content = f.read()
    except FileNotFoundError:
        return tokens
    except OSError:
        return tokens

    for line in content.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith("{"):
            try:
                tokens[key] = json.loads(value)
                continue
            except json.JSONDecodeError:
                pass
        tokens[key] = value
    return tokens


def _token_from_payload(payload: Any) -> Optional[str]:
    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict):
        return None
    for key in ("access_token", "token", "bot_token", "oauth_access_token"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def token_from_mcp_file(filepath: str = MCP_TOKEN_PATH) -> Optional[str]:
    all_tokens = parse_mcp_tokens(filepath)
    for key in MCP_TOKEN_KEYS:
        token = _token_from_payload(all_tokens.get(key))
        if token:
            return token
    lowered = {str(key).lower(): value for key, value in all_tokens.items()}
    for key in MCP_TOKEN_KEYS:
        token = _token_from_payload(lowered.get(key.lower()))
        if token:
            return token
    return None


def _env_token() -> Optional[str]:
    for key in TOKEN_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            return value
    return None


def _client_credentials_token(config: TeamsConfig) -> Optional[tuple[str, int]]:
    tenant_id = config.tenant_id or os.environ.get("MICROSOFT_TENANT_ID")
    client_id = config.client_id or os.environ.get("MICROSOFT_CLIENT_ID")
    client_secret = config.client_secret or os.environ.get("MICROSOFT_CLIENT_SECRET")
    if not (tenant_id and client_id and client_secret):
        return None

    url = (
        f"{TOKEN_BASE_URL.rstrip('/')}/{urllib.parse.quote(tenant_id)}"
        "/oauth2/v2.0/token"
    )
    form = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise TeamsAPIError(e.code, raw)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise TeamsConfigError(f"could not fetch Microsoft Graph token: {e}") from e

    token = payload.get("access_token")
    if not token:
        raise TeamsConfigError(
            f"token response did not include access_token: {payload!r}"
        )
    expires_in = int(payload.get("expires_in") or 3600)
    return str(token), int(time.time()) + max(60, expires_in - 60)


def get_access_token(
    *,
    explicit_token: Optional[str] = None,
    config_file: str = DEFAULT_CONFIG_PATH,
    mcp_file: str = MCP_TOKEN_PATH,
    cache_mcp_token: bool = True,
) -> str:
    if explicit_token:
        return explicit_token

    env_token = _env_token()
    if env_token:
        return env_token

    config = TeamsConfig.load(config_file)
    if config.access_token and (
        not config.access_token_expires_at
        or config.access_token_expires_at > int(time.time()) + 60
    ):
        return config.access_token

    mcp_token = token_from_mcp_file(mcp_file)
    if mcp_token:
        if cache_mcp_token:
            config.access_token = mcp_token
            config.save(config_file)
        return mcp_token

    client_token = _client_credentials_token(config)
    if client_token:
        token, expires_at = client_token
        config.access_token = token
        config.access_token_expires_at = expires_at
        config.save(config_file)
        return token

    raise TeamsConfigError(
        "No Microsoft Graph token found. Set MICROSOFT_GRAPH_ACCESS_TOKEN "
        "or run `python -m phantom.teams_interface config --set-access-token <token>`."
    )


def graph_request(
    method: str,
    path_or_url: str,
    *,
    token: str,
    body: Optional[dict[str, Any]] = None,
    query: Optional[dict[str, Any]] = None,
    timeout: float = 20.0,
) -> tuple[int, Any, dict[str, str]]:
    if path_or_url.startswith("https://"):
        url = path_or_url
    else:
        url = f"{GRAPH_BASE_URL.rstrip('/')}/{path_or_url.lstrip('/')}"

    if query:
        encoded = urllib.parse.urlencode(query)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{encoded}"

    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, _decode_json(raw), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, _decode_json(raw), dict(e.headers or {})
    except urllib.error.URLError as e:
        return 0, {"error": "connection_failed", "detail": str(e.reason)}, {}


def graph_request_bytes(
    method: str,
    path_or_url: str,
    *,
    token: str,
    data: bytes,
    content_type: str = "application/octet-stream",
    timeout: float = 60.0,
) -> tuple[int, Any, dict[str, str]]:
    if path_or_url.startswith("https://"):
        url = path_or_url
    else:
        url = f"{GRAPH_BASE_URL.rstrip('/')}/{path_or_url.lstrip('/')}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": content_type or "application/octet-stream",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, _decode_json(raw), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, _decode_json(raw), dict(e.headers or {})
    except urllib.error.URLError as e:
        return 0, {"error": "connection_failed", "detail": str(e.reason)}, {}


def _decode_json(raw: bytes) -> Any:
    if not raw:
        return {}
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _ensure_ok(status: int, payload: Any) -> Any:
    if 200 <= status < 300:
        return payload
    raise TeamsAPIError(status, payload)


def _message_body(message: str, *, is_html: bool = False) -> dict[str, Any]:
    return {
        "body": {
            "contentType": "html",
            "content": message if is_html else text_to_teams_html(message),
        }
    }


def _guess_content_type(filename: str, fallback: Optional[str] = None) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return fallback or guessed or "application/octet-stream"


def _guess_audio_content_type(filename: str, fallback: Optional[str] = None) -> str:
    if fallback:
        return fallback
    content_type = _guess_content_type(filename)
    if Path(filename).suffix.lower() == ".webm" and content_type == "video/webm":
        return "audio/webm"
    return content_type


def _is_audio_content_type(content_type: str) -> bool:
    return (content_type or "").lower().startswith("audio/")


def _safe_upload_name(filename: str) -> str:
    name = Path(filename or "attachment").name.strip()
    return name or "attachment"


def message_with_emojis(message: str, emojis: Optional[list[str]] = None) -> str:
    suffix = " ".join(
        str(emoji).strip() for emoji in (emojis or []) if str(emoji).strip()
    )
    if not suffix:
        return message
    return f"{message} {suffix}".strip()


REACTION_ALIASES = {
    "like": "like",
    "thumbs up": "like",
    "thumbsup": "like",
    "+1": "like",
    "haha": "laugh",
    "laugh": "laugh",
    "laughing": "laugh",
    "lol": "laugh",
    "heart": "heart",
    "love": "heart",
    "cry": "sad",
    "crying": "sad",
    "tears": "sad",
    "sad": "sad",
    "angry": "angry",
    "mad": "angry",
    "surprised": "surprised",
    "wow": "surprised",
}


def normalize_reaction_type(reaction: str) -> str:
    text = str(reaction or "").strip()
    if not text:
        return "✅"
    lowered = text.lower().strip(":")
    return REACTION_ALIASES.get(lowered, text)


def reaction_type_from_text(text: str) -> Optional[str]:
    lowered = f" {str(text or '').lower()} "
    for alias, reaction in sorted(
        REACTION_ALIASES.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", lowered):
            return reaction
    return None


def message_with_file_links(
    message: str,
    uploaded_files: list[dict[str, Any]],
    *,
    is_html: bool = False,
) -> str:
    files = [
        item
        for item in uploaded_files
        if isinstance(item, dict) and (item.get("webUrl") or item.get("web_url"))
    ]
    if not files:
        return message

    if is_html:
        links = []
        for item in files:
            url = html.escape(str(item.get("webUrl") or item.get("web_url") or ""))
            name = html.escape(str(item.get("name") or url))
            links.append(f'<a href="{url}">{name}</a>')
        separator = "<br><br>" if message else ""
        return f"{message}{separator}" + "<br>".join(links)

    lines = [message] if message else []
    lines.append("")
    for item in files:
        url = str(item.get("webUrl") or item.get("web_url") or "")
        name = str(item.get("name") or url)
        lines.append(f"{name}: {url}")
    return "\n".join(lines).strip()


def _pick_destination(config: TeamsConfig, args: argparse.Namespace) -> TeamsDestination:
    team_id = _str_or_none(getattr(args, "team_id", None)) or config.team_id
    channel_id = _str_or_none(getattr(args, "channel_id", None)) or config.channel_id
    chat_id = _str_or_none(getattr(args, "chat_id", None)) or config.chat_id
    explicit_chat = bool(_str_or_none(getattr(args, "chat_id", None)))
    explicit_channel = bool(
        _str_or_none(getattr(args, "team_id", None))
        or _str_or_none(getattr(args, "channel_id", None))
    )

    if explicit_chat and explicit_channel:
        raise TeamsConfigError(
            "use either --chat-id or --team-id/--channel-id, not both"
        )

    if explicit_chat:
        return TeamsDestination(kind="chat", chat_id=chat_id)
    if explicit_channel:
        if not (team_id and channel_id):
            raise TeamsConfigError(
                "--team-id and --channel-id are both required for channel mode"
            )
        return TeamsDestination(kind="channel", team_id=team_id, channel_id=channel_id)

    if config.default_destination == "chat" and chat_id:
        return TeamsDestination(kind="chat", chat_id=chat_id)
    if team_id and channel_id:
        return TeamsDestination(kind="channel", team_id=team_id, channel_id=channel_id)
    if chat_id:
        return TeamsDestination(kind="chat", chat_id=chat_id)

    raise TeamsConfigError(
        "No Teams destination configured. Set team/channel IDs or chat ID with "
        "`python -m phantom.teams_interface config`."
    )


def normalize_message(item: dict[str, Any]) -> dict[str, Any]:
    body = item.get("body") if isinstance(item.get("body"), dict) else {}
    from_obj = item.get("from") if isinstance(item.get("from"), dict) else {}
    user = from_obj.get("user") if isinstance(from_obj.get("user"), dict) else {}
    app = (
        from_obj.get("application")
        if isinstance(from_obj.get("application"), dict)
        else {}
    )
    raw_content = str(body.get("content") or "")
    content_type = str(body.get("contentType") or "html").lower()
    text = raw_content if content_type == "text" else html_to_text(raw_content)
    return {
        "id": str(item.get("id") or ""),
        "created": item.get("createdDateTime") or item.get("lastModifiedDateTime") or "",
        "from": user.get("displayName") or app.get("displayName") or "Unknown",
        "from_user_id": user.get("id"),
        "from_application_id": app.get("id"),
        "text": text,
        "web_url": item.get("webUrl"),
        "raw": item,
    }


class TeamsInterface:
    def __init__(
        self,
        *,
        access_token: Optional[str] = None,
        config_file: str = DEFAULT_CONFIG_PATH,
    ) -> None:
        self.config_file = config_file
        self.config = TeamsConfig.load(config_file)
        self.token = get_access_token(
            explicit_token=access_token,
            config_file=config_file,
            cache_mcp_token=True,
        )

    def destination(self, args: Optional[argparse.Namespace] = None) -> TeamsDestination:
        if args is None:
            args = argparse.Namespace(team_id=None, channel_id=None, chat_id=None)
        return _pick_destination(self.config, args)

    def say(
        self,
        message: str,
        *,
        destination: Optional[TeamsDestination] = None,
        reply_to: Optional[str] = None,
        is_html: bool = False,
    ) -> dict[str, Any]:
        destination = destination or self.destination()
        path = (
            destination.reply_path(reply_to)
            if reply_to
            else destination.messages_path()
        )
        status, payload, _ = graph_request(
            "POST",
            path,
            token=self.token,
            body=_message_body(message, is_html=is_html),
        )
        return _ensure_ok(status, payload)

    def react(
        self,
        message_id: str,
        *,
        reaction_type: str = "✅",
        reply_to_id: Optional[str] = None,
        destination: Optional[TeamsDestination] = None,
    ) -> dict[str, Any]:
        destination = destination or self.destination()
        path = destination.reaction_path(message_id, reply_to_id=reply_to_id)
        status, payload, _ = graph_request(
            "POST",
            path,
            token=self.token,
            body={"reactionType": normalize_reaction_type(reaction_type)},
        )
        _ensure_ok(status, payload)
        return {
            "ok": True,
            "message_id": message_id,
            "reply_to_id": reply_to_id,
            "reaction_type": normalize_reaction_type(reaction_type),
        }

    def upload_bytes_to_channel(
        self,
        filename: str,
        content: bytes,
        *,
        content_type: Optional[str] = None,
        destination: Optional[TeamsDestination] = None,
    ) -> dict[str, Any]:
        destination = destination or self.destination()
        if destination.kind != "channel":
            raise TeamsConfigError("Teams file upload currently requires channel mode")

        status, payload, _ = graph_request(
            "GET",
            destination.files_folder_path(),
            token=self.token,
        )
        folder = _ensure_ok(status, payload)
        parent = folder.get("parentReference") if isinstance(folder, dict) else {}
        drive_id = parent.get("driveId") if isinstance(parent, dict) else None
        folder_id = folder.get("id") if isinstance(folder, dict) else None
        if not (drive_id and folder_id):
            raise TeamsAPIError(status, folder)

        upload_name = _safe_upload_name(filename)
        upload_path = (
            f"/drives/{_quote(str(drive_id))}/items/{_quote(str(folder_id))}:/"
            f"{urllib.parse.quote(upload_name, safe='')}:/content"
        )
        status, payload, _ = graph_request_bytes(
            "PUT",
            upload_path,
            token=self.token,
            data=content,
            content_type=_guess_content_type(upload_name, content_type),
        )
        return _ensure_ok(status, payload)

    def upload_file_to_channel(
        self,
        path: str,
        *,
        content_type: Optional[str] = None,
        destination: Optional[TeamsDestination] = None,
        require_audio: bool = False,
    ) -> dict[str, Any]:
        file_path = Path(path).expanduser()
        if not file_path.exists():
            raise TeamsConfigError(f"file does not exist: {file_path}")
        if not file_path.is_file():
            raise TeamsConfigError(f"not a file: {file_path}")
        resolved_content_type = (
            _guess_audio_content_type(file_path.name, content_type)
            if require_audio
            else _guess_content_type(file_path.name, content_type)
        )
        if require_audio and not _is_audio_content_type(resolved_content_type):
            raise TeamsConfigError(
                f"audio upload requires an audio/* content type; got "
                f"{resolved_content_type} for {file_path.name}"
            )
        try:
            content = file_path.read_bytes()
        except OSError as e:
            raise TeamsConfigError(f"could not read file {file_path}: {e}") from e
        return self.upload_bytes_to_channel(
            file_path.name,
            content,
            content_type=resolved_content_type,
            destination=destination,
        )

    def get_messages(
        self,
        *,
        destination: Optional[TeamsDestination] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        destination = destination or self.destination()
        top = max(1, min(int(limit), 50))
        status, payload, _ = graph_request(
            "GET",
            destination.messages_path(),
            token=self.token,
            query={"$top": top},
        )
        payload = _ensure_ok(status, payload)
        items = payload.get("value") if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return []
        return [normalize_message(x) for x in items if isinstance(x, dict)]

    def get_replies(
        self,
        parent_message_id: str,
        *,
        destination: Optional[TeamsDestination] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        destination = destination or self.destination()
        if destination.kind != "channel":
            return []
        top = max(1, min(int(limit), 50))
        status, payload, _ = graph_request(
            "GET",
            destination.reply_path(parent_message_id),
            token=self.token,
            query={"$top": top},
        )
        payload = _ensure_ok(status, payload)
        items = payload.get("value") if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return []
        return [normalize_message(x) for x in items if isinstance(x, dict)]

    def get_me(self) -> dict[str, Any]:
        status, payload, _ = graph_request("GET", "/me", token=self.token)
        return _ensure_ok(status, payload)

    def list_joined_teams(self) -> list[dict[str, Any]]:
        status, payload, _ = graph_request("GET", "/me/joinedTeams", token=self.token)
        payload = _ensure_ok(status, payload)
        items = payload.get("value") if isinstance(payload, dict) else []
        return items if isinstance(items, list) else []

    def list_channels(self, team_id: Optional[str] = None) -> list[dict[str, Any]]:
        team = team_id or self.config.team_id
        if not team:
            raise TeamsConfigError("team_id is required to list channels")
        status, payload, _ = graph_request(
            "GET",
            f"/teams/{_quote(team)}/channels",
            token=self.token,
            query={"$top": 50},
        )
        payload = _ensure_ok(status, payload)
        items = payload.get("value") if isinstance(payload, dict) else []
        return items if isinstance(items, list) else []


def _read_with_cursor(
    client: TeamsInterface,
    destination: TeamsDestination,
    *,
    limit: int,
    since_id: Optional[str],
    no_save: bool,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    messages = client.get_messages(destination=destination, limit=limit)
    latest_id = messages[0]["id"] if messages else None
    cursor = since_id or client.config.last_read_ids.get(destination.key)
    if cursor:
        filtered = []
        for message in messages:
            if message["id"] == cursor:
                break
            filtered.append(message)
        messages = filtered

    if latest_id and not no_save and not since_id:
        client.config.last_read_ids[destination.key] = latest_id
        client.config.save(client.config_file)

    return messages, latest_id


def cmd_config(args: argparse.Namespace) -> int:
    config = TeamsConfig.load(args.config_file)

    if args.clear:
        config = TeamsConfig()
    if args.set_access_token:
        config.access_token = args.set_access_token
        config.access_token_expires_at = None
    if args.clear_token:
        config.access_token = None
        config.access_token_expires_at = None
    if args.set_tenant_id:
        config.tenant_id = args.set_tenant_id
    if args.set_client_id:
        config.client_id = args.set_client_id
    if args.set_client_secret:
        config.client_secret = args.set_client_secret
    if args.clear_client_secret:
        config.client_secret = None
    if args.set_team_id:
        config.team_id = args.set_team_id
        if not config.default_destination:
            config.default_destination = "channel"
    if args.set_channel_id:
        config.channel_id = args.set_channel_id
        if not config.default_destination:
            config.default_destination = "channel"
    if args.set_chat_id:
        config.chat_id = args.set_chat_id
        if not config.default_destination:
            config.default_destination = "chat"
    if args.set_default:
        config.default_destination = args.set_default
    if args.set_self_user_id:
        config.self_user_id = args.set_self_user_id
    if args.set_self_app_id:
        config.self_app_id = args.set_self_app_id

    changed = any(
        [
            args.clear,
            args.set_access_token,
            args.clear_token,
            args.set_tenant_id,
            args.set_client_id,
            args.set_client_secret,
            args.clear_client_secret,
            args.set_team_id,
            args.set_channel_id,
            args.set_chat_id,
            args.set_default,
            args.set_self_user_id,
            args.set_self_app_id,
        ]
    )
    if changed:
        config.save(args.config_file)

    print("Microsoft Teams configuration:")
    print(f"  default_destination: {config.default_destination or '-'}")
    print(f"  team_id:             {config.team_id or '-'}")
    print(f"  channel_id:          {config.channel_id or '-'}")
    print(f"  chat_id:             {config.chat_id or '-'}")
    print(f"  self_user_id:        {config.self_user_id or '-'}")
    print(f"  self_app_id:         {config.self_app_id or '-'}")
    print(f"  tenant_id:           {config.tenant_id or '-'}")
    print(f"  client_id:           {config.client_id or '-'}")
    print(f"  client_secret:       {_mask(config.client_secret)}")
    print(f"  access_token:        {_mask(config.access_token)}")
    if changed:
        print(f"wrote Teams settings to {args.config_file}")
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    try:
        client = TeamsInterface(
            access_token=args.access_token, config_file=args.config_file
        )
        destination = _pick_destination(client.config, args)
        uploaded_files = [
            client.upload_file_to_channel(
                path,
                destination=destination,
                require_audio=require_audio,
            )
            for require_audio, paths in (
                (False, args.attach_file or []),
                (True, args.attach_audio or []),
            )
            for path in paths
        ]
        message = message_with_emojis(args.message, args.emoji)
        result = client.say(
            message_with_file_links(
                message,
                uploaded_files,
                is_html=args.html,
            ),
            destination=destination,
            reply_to=args.reply_to,
            is_html=args.html,
        )
    except (TeamsConfigError, TeamsAPIError) as e:
        sys.stderr.write(f"send failed: {e}\n")
        return 2 if isinstance(e, TeamsConfigError) else 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    try:
        client = TeamsInterface(
            access_token=args.access_token, config_file=args.config_file
        )
        destination = _pick_destination(client.config, args)
        uploaded = client.upload_file_to_channel(
            args.file,
            content_type=args.content_type,
            destination=destination,
            require_audio=args.audio,
        )
        message = message_with_emojis(
            args.message or f"Uploaded {uploaded.get('name') or Path(args.file).name}",
            args.emoji,
        )
        sent = client.say(
            message_with_file_links(message, [uploaded], is_html=args.html),
            destination=destination,
            reply_to=args.reply_to,
            is_html=args.html,
        )
    except (TeamsConfigError, TeamsAPIError) as e:
        sys.stderr.write(f"upload failed: {e}\n")
        return 2 if isinstance(e, TeamsConfigError) else 1

    print(json.dumps({"upload": uploaded, "message": sent}, indent=2))
    return 0


def cmd_audio(args: argparse.Namespace) -> int:
    args.audio = True
    if not args.content_type:
        args.content_type = _guess_audio_content_type(args.file)
    return cmd_upload(args)


def cmd_react(args: argparse.Namespace) -> int:
    try:
        client = TeamsInterface(
            access_token=args.access_token, config_file=args.config_file
        )
        destination = _pick_destination(client.config, args)
        result = client.react(
            args.message_id,
            reaction_type=args.reaction,
            reply_to_id=args.reply_to,
            destination=destination,
        )
    except (TeamsConfigError, TeamsAPIError) as e:
        sys.stderr.write(f"reaction failed: {e}\n")
        return 2 if isinstance(e, TeamsConfigError) else 1

    print(json.dumps(result, indent=2))
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    try:
        client = TeamsInterface(
            access_token=args.access_token, config_file=args.config_file
        )
        destination = _pick_destination(client.config, args)
        messages, latest_id = _read_with_cursor(
            client,
            destination,
            limit=args.limit,
            since_id=args.since_id,
            no_save=args.no_save,
        )
    except (TeamsConfigError, TeamsAPIError) as e:
        sys.stderr.write(f"read failed: {e}\n")
        return 2 if isinstance(e, TeamsConfigError) else 1

    if args.json:
        print(
            json.dumps(
                {
                    "destination": destination.key,
                    "latest_id": latest_id,
                    "items": messages,
                },
                indent=2,
            )
        )
        return 0

    if not messages:
        print("no new Teams messages")
        return 0

    for msg in reversed(messages):
        print(
            f"id={msg['id']}  {msg.get('created') or '?'}  "
            f"from:{msg.get('from') or '?'}"
        )
        for line in (msg.get("text") or "").splitlines() or [""]:
            print(f"    {line}")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    try:
        client = TeamsInterface(
            access_token=args.access_token, config_file=args.config_file
        )
        me = client.get_me()
    except (TeamsConfigError, TeamsAPIError) as e:
        sys.stderr.write(f"whoami failed: {e}\n")
        return 2 if isinstance(e, TeamsConfigError) else 1

    if args.save_self and me.get("id"):
        client.config.self_user_id = str(me["id"])
        client.config.save(client.config_file)
    print(json.dumps(me, indent=2))
    return 0


def cmd_teams(args: argparse.Namespace) -> int:
    try:
        client = TeamsInterface(
            access_token=args.access_token, config_file=args.config_file
        )
        teams = client.list_joined_teams()
    except (TeamsConfigError, TeamsAPIError) as e:
        sys.stderr.write(f"teams failed: {e}\n")
        return 2 if isinstance(e, TeamsConfigError) else 1
    if args.json:
        print(json.dumps({"value": teams}, indent=2))
    else:
        for team in teams:
            print(f"{team.get('displayName') or '?'}\n  id: {team.get('id') or '?'}")
    return 0


def cmd_channels(args: argparse.Namespace) -> int:
    try:
        client = TeamsInterface(
            access_token=args.access_token, config_file=args.config_file
        )
        channels = client.list_channels(args.team_id)
    except (TeamsConfigError, TeamsAPIError) as e:
        sys.stderr.write(f"channels failed: {e}\n")
        return 2 if isinstance(e, TeamsConfigError) else 1
    if args.json:
        print(json.dumps({"value": channels}, indent=2))
    else:
        for channel in channels:
            print(f"{channel.get('displayName') or '?'}\n  id: {channel.get('id') or '?'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phantom.teams_interface")
    parser.add_argument("--access-token", help="Microsoft Graph bearer token")
    parser.add_argument(
        "--config-file",
        default=DEFAULT_CONFIG_PATH,
        help=f"settings file (default {DEFAULT_CONFIG_PATH})",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_config = sub.add_parser("config", help="show or set Teams configuration")
    p_config.add_argument("--set-access-token")
    p_config.add_argument("--clear-token", action="store_true")
    p_config.add_argument("--set-tenant-id")
    p_config.add_argument("--set-client-id")
    p_config.add_argument("--set-client-secret")
    p_config.add_argument("--clear-client-secret", action="store_true")
    p_config.add_argument("--set-team-id")
    p_config.add_argument("--set-channel-id")
    p_config.add_argument("--set-chat-id")
    p_config.add_argument("--set-default", choices=("channel", "chat"))
    p_config.add_argument("--set-self-user-id")
    p_config.add_argument("--set-self-app-id")
    p_config.add_argument("--clear", action="store_true")
    p_config.set_defaults(func=cmd_config)

    p_say = sub.add_parser("say", help="send a Teams message")
    p_say.add_argument("message")
    p_say.add_argument("--team-id")
    p_say.add_argument("--channel-id")
    p_say.add_argument("--chat-id")
    p_say.add_argument("--reply-to", help="channel message id to reply to")
    p_say.add_argument(
        "--html", action="store_true", help="send message as raw Teams HTML"
    )
    p_say.add_argument(
        "--attach-file",
        action="append",
        help="upload a local file to the Teams channel Files folder and include its link",
    )
    p_say.add_argument(
        "--attach-audio",
        action="append",
        help="upload a local audio file and include its link",
    )
    p_say.add_argument(
        "--emoji",
        action="append",
        help="append a native Unicode emoji to the message; can be repeated",
    )
    p_say.set_defaults(func=cmd_say)

    p_upload = sub.add_parser("upload", help="upload a file and post its Teams link")
    p_upload.add_argument("file")
    p_upload.add_argument("--team-id")
    p_upload.add_argument("--channel-id")
    p_upload.add_argument("--chat-id")
    p_upload.add_argument("-m", "--message", help="message to post with the file link")
    p_upload.add_argument("--reply-to", help="channel message id to reply to")
    p_upload.add_argument(
        "--html", action="store_true", help="send message as raw Teams HTML"
    )
    p_upload.add_argument(
        "--content-type",
        help="override detected content type, for example audio/webm",
    )
    p_upload.add_argument(
        "--audio",
        action="store_true",
        help="require the uploaded file to resolve to an audio/* content type",
    )
    p_upload.add_argument(
        "--emoji",
        action="append",
        help="append a native Unicode emoji to the message; can be repeated",
    )
    p_upload.set_defaults(func=cmd_upload)

    p_audio = sub.add_parser(
        "audio", help="upload an audio file and post its Teams link"
    )
    p_audio.add_argument("file")
    p_audio.add_argument("--team-id")
    p_audio.add_argument("--channel-id")
    p_audio.add_argument("--chat-id")
    p_audio.add_argument("-m", "--message", help="message to post with the audio link")
    p_audio.add_argument("--reply-to", help="channel message id to reply to")
    p_audio.add_argument(
        "--html", action="store_true", help="send message as raw Teams HTML"
    )
    p_audio.add_argument(
        "--content-type",
        help="override detected content type, for example audio/webm",
    )
    p_audio.add_argument(
        "--emoji",
        action="append",
        help="append a native Unicode emoji to the message; can be repeated",
    )
    p_audio.set_defaults(func=cmd_audio)

    p_react = sub.add_parser("react", help="add an emoji reaction to a Teams message")
    p_react.add_argument("message_id")
    p_react.add_argument("reaction", nargs="?", default="✅")
    p_react.add_argument("--team-id")
    p_react.add_argument("--channel-id")
    p_react.add_argument("--chat-id")
    p_react.add_argument(
        "--reply-to",
        help="parent channel message id when reacting to a thread reply",
    )
    p_react.set_defaults(func=cmd_react)

    p_read = sub.add_parser("read", help="read recent Teams messages")
    p_read.add_argument("--team-id")
    p_read.add_argument("--channel-id")
    p_read.add_argument("--chat-id")
    p_read.add_argument("--limit", type=int, default=20)
    p_read.add_argument(
        "--since-id", help="only show messages before this known latest id"
    )
    p_read.add_argument("--json", action="store_true")
    p_read.add_argument("--no-save", action="store_true")
    p_read.set_defaults(func=cmd_read)

    p_me = sub.add_parser("whoami", help="call Microsoft Graph /me")
    p_me.add_argument(
        "--save-self",
        action="store_true",
        help="persist /me id as teams.self_user_id",
    )
    p_me.set_defaults(func=cmd_whoami)

    p_teams = sub.add_parser("teams", help="list joined Teams")
    p_teams.add_argument("--json", action="store_true")
    p_teams.set_defaults(func=cmd_teams)

    p_channels = sub.add_parser("channels", help="list channels for a team")
    p_channels.add_argument("--team-id")
    p_channels.add_argument("--json", action="store_true")
    p_channels.set_defaults(func=cmd_channels)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
