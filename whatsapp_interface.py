"""Phantom WhatsApp CLI (POC).

Invoke as a module so subprocess callers stay consistent with other phantom
modules:

    python -m phantom.whatsapp_interface onboard --gateway-url http://127.0.0.1:8090 --to 15551234567
    python -m phantom.whatsapp_interface say "hello"
    python -m phantom.whatsapp_interface say "hello group" --group last
    python -m phantom.whatsapp_interface read --limit 20
    # group create requires the GATEWAY process to start with WHATSAPP_ALLOW_GROUP_CREATE=1
    python -m phantom.whatsapp_interface group create "POC" \\
        --participants 15557654321 --welcome "Phantom POC"
    python -m phantom.whatsapp_interface group list

Talks only to the local gateway (default http://127.0.0.1:8090). Bearer token
is read from --gateway-token, $WHATSAPP_GATEWAY_TOKEN, or the `whatsapp`
sub-object in ~/.agent_settings.json (in that order).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional

from phantom.whatsapp_routing import resolve_whatsapp_to


SETTINGS_PATH = Path.home() / ".agent_settings.json"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8090"


# ---------- settings ----------


def _read_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except Exception:
        return {}


def _write_whatsapp_settings(whatsapp: dict[str, Any]) -> None:
    """Merge the `whatsapp` sub-object only; never touch top-level keys."""
    settings = _read_settings()
    existing = settings.get("whatsapp") if isinstance(settings.get("whatsapp"), dict) else {}
    merged = {**existing, **whatsapp}
    settings["whatsapp"] = merged
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))


def _settings_whatsapp() -> dict[str, Any]:
    s = _read_settings().get("whatsapp")
    return s if isinstance(s, dict) else {}


# ---------- gateway client ----------


def _gateway_url(args: argparse.Namespace) -> str:
    return (
        getattr(args, "gateway_url", None)
        or os.environ.get("WHATSAPP_GATEWAY_URL")
        or _settings_whatsapp().get("gateway_url")
        or DEFAULT_GATEWAY_URL
    )


def _gateway_token(args: argparse.Namespace) -> Optional[str]:
    return (
        getattr(args, "gateway_token", None)
        or os.environ.get("WHATSAPP_GATEWAY_TOKEN")
        or _settings_whatsapp().get("gateway_token")
    )


def _request(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    body: Optional[dict[str, Any]] = None,
    timeout: float = 15.0,
) -> tuple[int, bytes, dict[str, str]]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except urllib.error.URLError as e:
        # Connection refused / DNS / timeout. Use status=0 to signal "not reachable".
        return 0, f"connection failed: {e.reason}".encode("utf-8"), {}


def _header_get(headers: dict[str, str], name: str) -> str:
    """Case-insensitive header lookup (urllib may lowercase keys)."""
    want = name.lower()
    for key, value in headers.items():
        if key.lower() == want:
            return value
    return ""


def _json_or_die(status: int, raw: bytes) -> dict[str, Any]:
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        sys.stderr.write(f"gateway returned non-JSON (status {status}): {raw[:200]!r}\n")
        sys.exit(2)


def _normalize_conversation_id(raw: str) -> str:
    """Normalize a user-supplied conversation_id for exact match against InboxRecord.channel_id.

    Strips non-digits from each colon-separated segment but preserves the literal
    "g" marker used in group conversation_ids (`{self}:g:{group_local}`).
    """
    parts = (raw or "").split(":")
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p == "g":
            out.append("g")
        else:
            digits = "".join(c for c in p if c.isdigit())
            out.append(digits)
    return ":".join(out)


def _conversation_id_to_group_jid(conversation_id: str) -> Optional[str]:
    """Parse `{self}:g:{local}` → `{local}@g.us`."""
    raw = (conversation_id or "").strip()
    parts = raw.split(":")
    if len(parts) != 3 or parts[1].strip() != "g":
        return None
    local = parts[2].strip()
    if not local:
        return None
    # Group local ids are numeric; reject doc placeholders (e.g. 120363XXXXXXXXX).
    if any(c.upper() == "X" for c in local) or not local.isdigit():
        return None
    if len(local) < 12:
        return None
    return f"{local}@g.us"


def resolve_group_target(args: argparse.Namespace) -> Optional[str]:
    """Resolve an explicit group JID from say flags, or None for DM send."""
    group_jid = (getattr(args, "group_jid", None) or "").strip()
    group = (getattr(args, "group", None) or "").strip()
    conversation = (getattr(args, "conversation", None) or "").strip()
    flags = [x for x in [group_jid, group, conversation] if x]
    if len(flags) > 1:
        raise ValueError("use only one of --group-jid, --group, or --conversation")
    if group_jid:
        if group_jid.endswith("@g.us"):
            return group_jid
        local = "".join(c for c in group_jid if c.isdigit()) or group_jid
        return f"{local}@g.us"
    if group:
        if group.lower() == "last":
            last = _settings_whatsapp().get("last_group_jid")
            if not last or not isinstance(last, str):
                raise ValueError("no last_group_jid in settings; create a group first")
            return last.strip()
        if group.endswith("@g.us"):
            return group
        local = "".join(c for c in group if c.isdigit()) or group
        return f"{local}@g.us"
    if conversation:
        jid = _conversation_id_to_group_jid(conversation)
        if not jid:
            raise ValueError(
                "conversation is not a valid group conversation_id "
                "(expected {self}:g:{numeric_group_id}; use `say --group last` or jq .whatsapp.last_group_conversation_id)"
            )
        return jid
    return None


def _iso_utc(ts_ms: Any) -> str:
    """Format a millisecond epoch as ISO-8601 UTC (e.g. 2026-06-04T17:42:13Z)."""
    try:
        n = int(ts_ms)
    except (TypeError, ValueError):
        return "?"
    if n <= 0:
        return "?"
    return datetime.fromtimestamp(n / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- commands ----------


def cmd_onboard(args: argparse.Namespace) -> int:
    base = _gateway_url(args).rstrip("/")
    token = _gateway_token(args)

    # 1. Preflight /health
    status, raw, _ = _request("GET", f"{base}/health")
    if status != 200:
        sys.stderr.write(f"health check failed: status={status} body={raw[:200]!r}\n")
        return 2
    print(f"gateway reachable at {base}")

    # 2. Poll /qr and /status until connection=open
    qr_path = Path(args.qr_out) if args.qr_out else Path.cwd() / "whatsapp-qr.png"
    last_state: Optional[str] = None
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        s_status, s_raw, _ = _request("GET", f"{base}/status", token=token)
        if s_status == 401:
            sys.stderr.write("unauthorized — provide --gateway-token or set WHATSAPP_GATEWAY_TOKEN\n")
            return 2
        if s_status != 200:
            sys.stderr.write(f"status failed: {s_status} {s_raw[:200]!r}\n")
            return 2
        st = _json_or_die(s_status, s_raw)
        conn = st.get("connection")
        if conn != last_state:
            print(f"state: {conn}")
            last_state = conn
        if conn == "open":
            self_e164 = st.get("self_e164")
            print(f"linked as {self_e164}")
            _persist_onboard(args, base, token, self_e164)
            return 0
        if conn in ("qr", "connecting", "starting"):
            q_status, q_raw, q_hdrs = _request("GET", f"{base}/qr", token=token)
            ctype = _header_get(q_hdrs, "Content-Type")
            if q_status == 200 and ctype.startswith("image/png"):
                qr_path.write_bytes(q_raw)
                print(f"QR refreshed: {qr_path}")
            elif q_status == 404:
                pass  # QR not ready yet; keep polling
            elif q_status != 200:
                sys.stderr.write(f"qr fetch failed: status={q_status} body={q_raw[:200]!r}\n")
        time.sleep(args.poll_interval)

    sys.stderr.write("onboard timed out before connection=open\n")
    return 1


def _persist_onboard(
    args: argparse.Namespace,
    base: str,
    token: Optional[str],
    self_e164: Optional[str],
) -> None:
    to = (args.to or "").strip()
    digits_to = "".join(c for c in to if c.isdigit())
    conv: Optional[str] = None
    if self_e164 and digits_to:
        conv = f"{self_e164}:{digits_to}"
    payload: dict[str, Any] = {"gateway_url": base}
    if token:
        payload["gateway_token"] = token
    if digits_to:
        payload["default_to"] = digits_to
    if conv:
        payload["conversation_id"] = conv
    if self_e164:
        payload["self_e164"] = self_e164
    _write_whatsapp_settings(payload)
    print(f"wrote whatsapp settings to {SETTINGS_PATH}")


def cmd_say(args: argparse.Namespace) -> int:
    base = _gateway_url(args).rstrip("/")
    token = _gateway_token(args)
    try:
        group_jid = resolve_group_target(args)
    except ValueError as e:
        sys.stderr.write(f"group target error: {e}\n")
        return 2
    if group_jid and getattr(args, "to", None):
        sys.stderr.write(
            "use either --to (DM) or a group flag (--group-jid, --group, --conversation), not both\n"
        )
        return 2
    # Apply the Ninja prefix here (not at the gateway) so manual ops sends stay literal.
    message = args.message
    if getattr(args, "ninja_prefix", False):
        if not message.startswith("🥷 Ninja:"):
            message = f"🥷 Ninja: {message}"
    if group_jid:
        req_body: dict[str, Any] = {"group_jid": group_jid, "text": message}
    else:
        try:
            to = resolve_whatsapp_to(args.to)
        except ValueError as e:
            sys.stderr.write(f"routing error: {e}\n")
            return 2
        req_body = {"to": to, "text": message}
    status, raw, _ = _request(
        "POST",
        f"{base}/send",
        token=token,
        body=req_body,
    )
    if status == 0:
        sys.stderr.write(
            f"gateway not reachable at {base}; is `npm run dev` running?\n  {raw.decode('utf-8', 'replace')}\n"
        )
        return 2
    if status == 401:
        sys.stderr.write("unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n")
        return 2
    if status == 409:
        sys.stderr.write("gateway is up but not linked yet; run onboard first\n")
        return 2
    body = _json_or_die(status, raw)
    if status != 200 or not body.get("ok"):
        detail = str(body.get("detail", ""))
        if status == 500 and "bad-request" in detail.lower() and req_body.get("group_jid"):
            sys.stderr.write(
                f"send failed: status={status} body={body}\n"
                "hint: group JID may be invalid or you may not be in that group; "
                "use `group list`, `say --group last`, or jq .whatsapp.last_group_conversation_id\n"
            )
        else:
            sys.stderr.write(f"send failed: status={status} body={body}\n")
        return 1
    print(json.dumps(body))
    return 0


def cmd_group_create(args: argparse.Namespace) -> int:
    base = _gateway_url(args).rstrip("/")
    token = _gateway_token(args)
    participants = [p.strip() for p in args.participants if p.strip()]
    if not participants:
        sys.stderr.write("--participants must include at least one number\n")
        return 2
    body: dict[str, Any] = {"subject": args.subject, "participants": participants}
    if args.welcome:
        body["welcome"] = args.welcome
    status, raw, _ = _request("POST", f"{base}/groups/create", token=token, body=body)

    if status == 0:
        sys.stderr.write(
            f"gateway not reachable at {base}; is `npm run dev` running?\n  {raw.decode('utf-8', 'replace')}\n"
        )
        return 2
    if status == 401:
        sys.stderr.write("unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n")
        return 2

    parsed = _json_or_die(status, raw)

    if status == 403 and parsed.get("error") == "group_create_disabled":
        sys.stderr.write(
            "group create is disabled. The GATEWAY process must be restarted with\n"
            "  WHATSAPP_ALLOW_GROUP_CREATE=1\n"
            "(this flag is on the gateway, not the CLI/Python process).\n"
        )
        return 1
    if status == 409 and parsed.get("error") == "not_linked":
        sys.stderr.write("gateway is up but not linked yet; run onboard first\n")
        return 2
    if status == 500 and "no valid participants" in str(parsed.get("detail", "")):
        sys.stderr.write(
            "all participants failed onWhatsApp validation; verify they are active WhatsApp numbers.\n"
        )
        return 1
    if status != 200 or not parsed.get("ok"):
        sys.stderr.write(f"group create failed: status={status} body={parsed}\n")
        return 1

    # Persist last_group_* for later `read --conversation` use.
    persist: dict[str, Any] = {}
    if parsed.get("group_jid"):
        persist["last_group_jid"] = parsed["group_jid"]
    if parsed.get("conversation_id"):
        persist["last_group_conversation_id"] = parsed["conversation_id"]
    if persist:
        _write_whatsapp_settings(persist)

    print(json.dumps(parsed))
    return 0


def cmd_group_list(args: argparse.Namespace) -> int:
    base = _gateway_url(args).rstrip("/")
    token = _gateway_token(args)
    status, raw, _ = _request("GET", f"{base}/groups", token=token)

    if status == 0:
        sys.stderr.write(
            f"gateway not reachable at {base}; is `npm run dev` running?\n  {raw.decode('utf-8', 'replace')}\n"
        )
        return 2
    if status == 401:
        sys.stderr.write("unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n")
        return 2
    if status == 409:
        sys.stderr.write("gateway is up but not linked yet; run onboard first\n")
        return 2
    if status != 200:
        sys.stderr.write(f"group list failed: status={status} body={raw[:200]!r}\n")
        return 1

    payload = _json_or_die(status, raw)
    if not payload.get("ok"):
        sys.stderr.write(f"group list failed: {payload}\n")
        return 1

    groups = payload.get("groups") or []
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    if not groups:
        print("no participating groups")
        return 0

    print(f"{len(groups)} group(s):\n")
    for g in groups:
        subject = g.get("subject") or "?"
        jid = g.get("group_jid") or "?"
        conv = g.get("conversation_id") or "?"
        count = g.get("participant_count", "?")
        print(f"  {subject}")
        print(f"    jid:  {jid}")
        print(f"    conv: {conv}")
        print(f"    members: {count}")
        print()
    return 0


def _read_messages_once(
    base: str, token: Optional[str], since: int, limit: int
) -> tuple[int, dict[str, Any], bytes]:
    url = f"{base}/messages?since={since}&limit={limit}"
    status, raw, _ = _request("GET", url, token=token)
    if status != 200:
        return status, {}, raw
    return status, _json_or_die(status, raw), raw


def cmd_bind(args: argparse.Namespace) -> int:
    """Bind / inspect the single-chat binding (Ninja mode).

    With ``--status`` (or no flags) prints the current binding + active
    pairing code. With ``--chat-jid`` calls ``POST /bind`` to set the
    allowed chat.
    """
    base = _gateway_url(args).rstrip("/")
    token = _gateway_token(args)

    chat_jid = (getattr(args, "chat_jid", None) or "").strip()
    show_status = getattr(args, "status", False) or not chat_jid

    if chat_jid:
        if not (chat_jid.endswith("@g.us") or chat_jid.endswith("@s.whatsapp.net")):
            sys.stderr.write("chat-jid must end with @g.us or @s.whatsapp.net\n")
            return 2
        status, raw, _ = _request(
            "POST", f"{base}/bind", token=token, body={"chat_jid": chat_jid}
        )
        if status == 0:
            sys.stderr.write(f"gateway not reachable at {base}\n")
            return 2
        if status == 401:
            sys.stderr.write("unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n")
            return 2
        body = _json_or_die(status, raw)
        if status != 200 or not body.get("ok"):
            sys.stderr.write(f"bind failed: status={status} body={body}\n")
            return 1
        print(json.dumps(body))

    if show_status:
        s_status, s_raw, _ = _request("GET", f"{base}/status", token=token)
        if s_status == 401:
            sys.stderr.write("unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n")
            return 2
        if s_status != 200:
            sys.stderr.write(f"status failed: {s_status} {s_raw[:200]!r}\n")
            return 1
        st = _json_or_die(s_status, s_raw)
        # Best-effort pairing-code fetch (no error if unsupported).
        p_status, p_raw, _ = _request("GET", f"{base}/pairing_code", token=token)
        pairing = _json_or_die(p_status, p_raw) if p_status == 200 else {}
        if args.json:
            out = {
                "connection": st.get("connection"),
                "ninja_state": st.get("ninja_state"),
                "bound_chat_jid": st.get("bound_chat_jid"),
                "bound_via": st.get("bound_via"),
                "bound_at": st.get("bound_at"),
                "pairing_code": pairing.get("code"),
                "pairing_code_expires_at": pairing.get("expires_at"),
                "bind_method": st.get("bind_method"),
                "bind_method_source": st.get("bind_method_source"),
                "last_active_method": st.get("last_active_method"),
                "last_error": st.get("last_error"),
                "invite_code": st.get("invite_code"),
                "invite_code_error": st.get("invite_code_error"),
                "grace_remaining_ms": st.get("grace_remaining_ms"),
            }
            print(json.dumps(out, indent=2))
        else:
            print(f"connection:    {st.get('connection')}")
            print(f"ninja_state:   {st.get('ninja_state')}")
            print(f"bound_chat:    {st.get('bound_chat_jid') or '(none)'}")
            if st.get("bound_via"):
                print(f"bound_via:     {st.get('bound_via')}")
            if st.get("bind_method"):
                print(f"bind_method:   {st.get('bind_method')} (source={st.get('bind_method_source')})")
            if st.get("invite_code"):
                print(f"invite_link:   https://chat.whatsapp.com/{st.get('invite_code')}")
            elif st.get("invite_code_error"):
                print(f"invite_link:   (unavailable — {st.get('invite_code_error')})")
            if st.get("last_error"):
                print(f"last_error:    {st.get('last_error')}")
            if pairing.get("code"):
                print(f"pairing_code:  {pairing.get('code')}")
                print(f"  expires_at:  {_iso_utc(pairing.get('expires_at'))}")
            elif not st.get("bound_chat_jid") and st.get("ninja_state") == "waiting_for_chat_pairing":
                print("pairing_code:  (none — gateway not yet open or already expired)")
    return 0


def react(
    base: str,
    token: Optional[str],
    message_key: str,
    emoji: str,
    *,
    from_me: bool = False,
    participant: Optional[str] = None,
    timeout: float = 10.0,
) -> bool:
    """POST /react. Best-effort: returns False on any error, never raises."""
    if not message_key or not emoji:
        return False
    body: dict[str, Any] = {
        "message_key": message_key,
        "emoji": emoji,
        "from_me": from_me,
    }
    if participant:
        body["participant"] = participant
    try:
        status, _raw, _ = _request(
            "POST", f"{base.rstrip('/')}/react", token=token, body=body, timeout=timeout
        )
        return status == 200
    except Exception:
        return False


def cmd_react(args: argparse.Namespace) -> int:
    base = _gateway_url(args).rstrip("/")
    token = _gateway_token(args)
    ok = react(
        base,
        token,
        args.message_key,
        args.emoji,
        from_me=bool(args.from_me),
        participant=args.participant,
    )
    if not ok:
        sys.stderr.write(f"react failed for message_key={args.message_key}\n")
        return 1
    print(json.dumps({"ok": True}))
    return 0


def cmd_fetch_media(args: argparse.Namespace) -> int:
    """Download decrypted media bytes from GET /media/:id to a local file."""
    base = _gateway_url(args).rstrip("/")
    token = _gateway_token(args)
    media_id = (args.media_id or "").strip()
    if not media_id:
        sys.stderr.write("--media-id required\n")
        return 2
    out_path = Path(args.out).expanduser() if args.out else Path.cwd() / f"wa_{media_id}"
    url = f"{base}/media/{media_id}"
    headers: dict[str, str] = {"Accept": "*/*"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            status = resp.status
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"fetch-media failed: status={e.code} body={e.read()[:200]!r}\n")
        return 1
    except urllib.error.URLError as e:
        sys.stderr.write(f"gateway not reachable at {base}: {e.reason}\n")
        return 2
    if status != 200:
        sys.stderr.write(f"fetch-media failed: status={status}\n")
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(body)
    try:
        out_path.chmod(0o600)
    except OSError:
        pass
    print(json.dumps({"ok": True, "path": str(out_path), "bytes": len(body), "mimetype": ctype}))
    return 0


def _multipart_encode(
    fields: list[tuple[str, str]],
    file_field: str,
    file_name: str,
    file_mime: str,
    file_bytes: bytes,
) -> tuple[bytes, str]:
    """Hand-roll a multipart/form-data body. Avoids extra deps."""
    import secrets

    boundary = "----PhantomBoundary" + secrets.token_hex(8)
    crlf = b"\r\n"
    out = bytearray()
    for name, value in fields:
        out += f"--{boundary}".encode() + crlf
        out += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        out += value.encode("utf-8") + crlf
    out += f"--{boundary}".encode() + crlf
    out += (
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{file_name}"'
    ).encode() + crlf
    out += f"Content-Type: {file_mime}".encode() + crlf + crlf
    out += file_bytes + crlf
    out += f"--{boundary}--".encode() + crlf
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def cmd_upload(args: argparse.Namespace) -> int:
    """Send a local file as an image or document via POST /send_media."""
    base = _gateway_url(args).rstrip("/")
    token = _gateway_token(args)

    kind = (args.kind or "").lower().strip()
    if kind not in ("image", "document"):
        sys.stderr.write("--kind must be 'image' or 'document'\n")
        return 2
    file_path = Path(args.file).expanduser()
    if not file_path.exists() or not file_path.is_file():
        sys.stderr.write(f"file not found: {file_path}\n")
        return 2
    file_bytes = file_path.read_bytes()

    # Mimetype: --mimetype > python's mimetypes guess > sensible default.
    mimetype = (args.mimetype or "").strip()
    if not mimetype:
        import mimetypes

        guess, _ = mimetypes.guess_type(str(file_path))
        mimetype = guess or ("image/jpeg" if kind == "image" else "application/octet-stream")

    # Synthesized caption (override #4): when --ninja-prefix is set and the
    # caller didn't supply one, synthesize a short notice so the human knows
    # it's a bot upload. Explicit captions get the prefix prepended.
    caption = args.caption if args.caption is not None else ""
    if args.ninja_prefix:
        if not caption:
            caption = f"🥷 Ninja: shared {'an' if kind == 'image' else 'a'} {kind}"
        elif not caption.startswith("🥷 Ninja:"):
            caption = f"🥷 Ninja: {caption}"

    # Route resolution mirrors `cmd_say`.
    try:
        group_jid = resolve_group_target(args)
    except ValueError as e:
        sys.stderr.write(f"group target error: {e}\n")
        return 2
    if group_jid and getattr(args, "to", None):
        sys.stderr.write(
            "use either --to (DM) or a group flag (--group-jid, --group, --conversation), not both\n"
        )
        return 2

    fields: list[tuple[str, str]] = [("kind", kind), ("mimetype", mimetype)]
    if caption:
        fields.append(("caption", caption))
    # filename: explicit > basename. Required server-side for documents.
    filename = (args.filename or file_path.name).strip()
    fields.append(("filename", filename))
    if group_jid:
        fields.append(("group_jid", group_jid))
    else:
        try:
            to = resolve_whatsapp_to(args.to)
        except ValueError as e:
            sys.stderr.write(f"routing error: {e}\n")
            return 2
        fields.append(("to", to))

    body, ctype = _multipart_encode(fields, "file", filename, mimetype, file_bytes)
    headers = {"Content-Type": ctype, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{base}/send_media", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60.0) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read()
    except urllib.error.URLError as e:
        sys.stderr.write(f"gateway not reachable at {base}: {e.reason}\n")
        return 2
    if status == 401:
        sys.stderr.write("unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n")
        return 2
    if status == 409:
        sys.stderr.write("gateway is up but not linked yet; run onboard first\n")
        return 2
    parsed = _json_or_die(status, raw)
    if status != 200 or not parsed.get("ok"):
        sys.stderr.write(f"upload failed: status={status} body={parsed}\n")
        return 1
    print(json.dumps(parsed))
    return 0


def cmd_unbind(args: argparse.Namespace) -> int:
    base = _gateway_url(args).rstrip("/")
    token = _gateway_token(args)
    status, raw, _ = _request("POST", f"{base}/unbind", token=token)
    if status == 0:
        sys.stderr.write(f"gateway not reachable at {base}\n")
        return 2
    if status == 401:
        sys.stderr.write("unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n")
        return 2
    body = _json_or_die(status, raw)
    if status != 200 or not body.get("ok"):
        sys.stderr.write(f"unbind failed: status={status} body={body}\n")
        return 1
    print(json.dumps(body))
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    base = _gateway_url(args).rstrip("/")
    token = _gateway_token(args)

    settings = _settings_whatsapp()
    stored_seq_raw = settings.get("last_read_seq")
    stored_seq = (
        int(stored_seq_raw)
        if isinstance(stored_seq_raw, (int, float)) and stored_seq_raw >= 0
        else 0
    )
    stored_epoch_raw = settings.get("last_read_inbox_epoch")
    stored_epoch: Optional[int] = (
        int(stored_epoch_raw)
        if isinstance(stored_epoch_raw, (int, float)) and stored_epoch_raw > 0
        else None
    )

    since_override = args.since is not None
    if since_override:
        since = max(0, int(args.since))
    else:
        since = stored_seq

    limit = max(1, min(500, int(args.limit)))
    status, payload, raw = _read_messages_once(base, token, since, limit)

    if status == 0:
        sys.stderr.write(
            f"gateway not reachable at {base}; is `npm run dev` running?\n  {raw.decode('utf-8', 'replace')}\n"
        )
        return 2
    if status == 401:
        sys.stderr.write("unauthorized — set WHATSAPP_GATEWAY_TOKEN or pass --gateway-token\n")
        return 2
    if status == 409:
        sys.stderr.write("gateway is up but not linked yet; run onboard first\n")
        return 2
    if status != 200:
        sys.stderr.write(f"read failed: status={status} body={raw[:200]!r}\n")
        return 1

    response_epoch_raw = payload.get("inbox_epoch")
    response_epoch: Optional[int] = (
        int(response_epoch_raw)
        if isinstance(response_epoch_raw, (int, float)) and response_epoch_raw > 0
        else None
    )
    latest_seq_raw = payload.get("latest_seq")
    try:
        latest_seq_int = int(latest_seq_raw)
    except (TypeError, ValueError):
        latest_seq_int = since

    # Cursor invalidation: epoch mismatch (primary) or seq overrun (fallback).
    # Only auto-reset when caller didn't pass --since explicitly.
    needs_reset = False
    if not since_override and since > 0:
        if (
            response_epoch is not None
            and stored_epoch is not None
            and response_epoch != stored_epoch
        ):
            needs_reset = True
        elif stored_seq > latest_seq_int:
            needs_reset = True
    if needs_reset:
        print("cursor reset (gateway inbox restarted)")
        since = 0
        status, payload, raw = _read_messages_once(base, token, since, limit)
        if status != 200:
            sys.stderr.write(f"read failed after reset: status={status} body={raw[:200]!r}\n")
            return 1
        latest_seq_raw = payload.get("latest_seq")
        try:
            latest_seq_int = int(latest_seq_raw)
        except (TypeError, ValueError):
            latest_seq_int = 0
        response_epoch_raw = payload.get("inbox_epoch")
        response_epoch = (
            int(response_epoch_raw)
            if isinstance(response_epoch_raw, (int, float)) and response_epoch_raw > 0
            else None
        )

    raw_items = payload.get("items") or []

    # Client-side filters
    items = list(raw_items)
    if args.conversation:
        target = _normalize_conversation_id(args.conversation)
        items = [it for it in items if it.get("channel_id") == target]
    if args.no_self:
        items = [it for it in items if not it.get("from_me")]

    if args.json:
        out: dict[str, Any] = {"items": items, "latest_seq": latest_seq_int}
        if response_epoch is not None:
            out["inbox_epoch"] = response_epoch
        print(json.dumps(out, indent=2))
    elif not items:
        print(f"no new messages (cursor={since})")
    else:
        for it in items:
            seq = it.get("seq", "?")
            ts = _iso_utc(it.get("ts"))
            chan = it.get("channel_id") or "?"
            from_label = "me" if it.get("from_me") else (it.get("user_id") or "?")
            text = (it.get("text") or "").rstrip("\n")
            print(f"seq={seq}  {ts}  from:{from_label}  chan:{chan}")
            for line in text.split("\n"):
                print(f"    {line}")

    # Persist cursor when gateway returned at least one item (regardless of
    # client-side filtering). Caller can override with --no-save.
    if raw_items and not args.no_save:
        persist: dict[str, Any] = {"last_read_seq": latest_seq_int}
        if response_epoch is not None:
            persist["last_read_inbox_epoch"] = response_epoch
        _write_whatsapp_settings(persist)

    return 0


# ---------- argparse ----------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="phantom.whatsapp_interface")
    p.add_argument("--gateway-url", help=f"gateway base URL (default {DEFAULT_GATEWAY_URL})")
    p.add_argument("--gateway-token", help="bearer token for gateway")

    sub = p.add_subparsers(dest="cmd", required=True)

    p_on = sub.add_parser("onboard", help="link a WhatsApp account by polling QR + status")
    p_on.add_argument("--to", help="default test contact (E.164); persisted to settings")
    p_on.add_argument("--qr-out", help="path to write QR PNG (default ./whatsapp-qr.png)")
    p_on.add_argument("--poll-interval", type=float, default=2.0)
    p_on.add_argument("--timeout", type=float, default=180.0)
    p_on.set_defaults(func=cmd_onboard)

    p_say = sub.add_parser("say", help="send a text message (DM or group)")
    p_say.add_argument("message")
    p_say.add_argument("--to", help="explicit DM destination E.164")
    p_say.add_argument("--group-jid", help="full group JID (120363…@g.us)")
    p_say.add_argument(
        "--ninja-prefix",
        action="store_true",
        help="prepend '🥷 Ninja: ' to the message so recipients see it as a bot reply "
        "(used by monitor_whatsapp.py; manual sends stay literal by default)",
    )
    p_say.add_argument(
        "--group",
        help="group local id, or 'last' for whatsapp.last_group_jid from settings",
    )
    p_say.add_argument(
        "--conversation",
        help="group conversation_id ({self}:g:{group_local}) — DM flags use --to instead",
    )
    p_say.set_defaults(func=cmd_say)

    p_read = sub.add_parser(
        "read",
        help="read inbox messages from the gateway (one-shot; cursor in ~/.agent_settings.json)",
    )
    p_read.add_argument(
        "--since",
        type=int,
        default=None,
        help="override cursor (default: whatsapp.last_read_seq from settings, else 0)",
    )
    p_read.add_argument("--limit", type=int, default=50, help="max items (1..500, default 50)")
    p_read.add_argument(
        "--conversation",
        help="filter by channel_id (DM: {self}:{peer}, group: {self}:g:{group_local})",
    )
    p_read.add_argument("--no-self", action="store_true", help="hide from_me messages")
    p_read.add_argument("--json", action="store_true", help="print raw JSON instead of human lines")
    p_read.add_argument("--no-save", action="store_true", help="do not advance the stored cursor")
    p_read.set_defaults(func=cmd_read)

    p_bind = sub.add_parser(
        "bind",
        help="set or inspect the single-chat binding (Ninja mode)",
    )
    p_bind.add_argument(
        "--chat-jid",
        help="WhatsApp chat JID to bind (e.g. 120363…@g.us or 1234567890@s.whatsapp.net)",
    )
    p_bind.add_argument(
        "--status",
        action="store_true",
        help="print current binding + pairing code (default when --chat-jid is omitted)",
    )
    p_bind.add_argument("--json", action="store_true", help="print raw JSON instead of human lines")
    p_bind.set_defaults(func=cmd_bind)

    p_unbind = sub.add_parser("unbind", help="clear the single-chat binding (Ninja mode)")
    p_unbind.set_defaults(func=cmd_unbind)

    p_fm = sub.add_parser(
        "fetch-media",
        help="download decrypted media bytes for a media_id (writes 0600 file)",
    )
    p_fm.add_argument("--media-id", required=True, help="media_id from `read --json` (sha-prefix)")
    p_fm.add_argument("--out", help="output path (default ./wa_<media_id>)")
    p_fm.set_defaults(func=cmd_fetch_media)

    p_up = sub.add_parser(
        "upload",
        help="send a local file as image or document (multipart POST /send_media)",
    )
    p_up.add_argument("--kind", required=True, help="image | document")
    p_up.add_argument("--file", required=True, help="path to the local file to upload")
    p_up.add_argument("--caption", help="caption text (image/pdf). Optional.")
    p_up.add_argument("--filename", help="override the filename sent to WhatsApp (default basename)")
    p_up.add_argument("--mimetype", help="override mimetype (default: guess from extension)")
    p_up.add_argument("--to", help="DM destination E.164")
    p_up.add_argument("--group-jid", help="full group JID (120363…@g.us)")
    p_up.add_argument("--group", help="group local id, or 'last'")
    p_up.add_argument("--conversation", help="group conversation_id ({self}:g:{group_local})")
    p_up.add_argument(
        "--ninja-prefix",
        action="store_true",
        help="prefix the caption with '🥷 Ninja:' (synthesizes one if --caption is empty)",
    )
    p_up.set_defaults(func=cmd_upload)

    p_react = sub.add_parser("react", help="add/replace a reaction on an inbox message")
    p_react.add_argument("--message-key", required=True, help="message_key from `read --json` (e.g. 1234@s.whatsapp.net:3EB0...)")
    p_react.add_argument("--emoji", required=True, help="reaction emoji (use empty to remove)")
    p_react.add_argument("--from-me", action="store_true", help="original message was sent by us (set for from_me items)")
    p_react.add_argument("--participant", help="sender JID inside a group bind (required for group reactions; from inbox.participant)")
    p_react.set_defaults(func=cmd_react)

    p_group = sub.add_parser("group", help="group operations")
    g_sub = p_group.add_subparsers(dest="group_cmd", required=True)
    p_gc = g_sub.add_parser("create", help="create a new group")
    p_gc.add_argument("subject", help="group subject/name")
    p_gc.add_argument("--participants", nargs="+", required=True, help="E.164 numbers")
    p_gc.add_argument("--welcome", help="optional welcome message after create")
    p_gc.set_defaults(func=cmd_group_create)
    p_gl = g_sub.add_parser("list", help="list WhatsApp groups you participate in")
    p_gl.add_argument("--json", action="store_true", help="print raw JSON")
    p_gl.set_defaults(func=cmd_group_list)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
