"""
utils/pdx_action_map.py — curated map of Pipedream action_key → HTTP signature.
==============================================================================

Pipedream's Connect Components (Actions) API requires a paid plan and is
not available to most projects. To keep ``pdx run`` working without that
API, we maintain a small, curated map that translates the common
component ``action_key`` values into a concrete HTTP request that we can
send through the Connect Proxy.

Why a manual map?
-----------------
The component .mjs files in
``github.com/PipedreamHQ/pipedream/components/`` declare props
declaratively, but the actual HTTP request is built imperatively inside
each ``run()`` body which calls into per-app helpers (e.g.
``github.app.mjs::createIssue``). There is no machine-readable mapping
shipped by Pipedream that we can rely on, so we curate the actions we
care about explicitly.

For everything not in this map, ``pdx run`` returns a clean JSON error
that points the LLM to ``pdx http <app_slug> <METHOD> <path>``, which
works for any proxy-enabled app on day one.

Schema
------
Each entry is a dict with the following fields:

``app_slug``
    The Pipedream app slug, e.g. ``"github"``. Used to resolve the
    user's connected account.

``method``
    HTTP method.

``path_template``
    Upstream URL template. Use ``{prop}`` placeholders for path props.
    Use a relative path (starting with ``/``) when the app has a static
    or dynamic ``base_proxy_target_url``; otherwise use a fully
    qualified URL.

``required_props``
    List of prop names that must be present.

``path_props``
    Names of props that fill ``{...}`` placeholders in ``path_template``.

``query_props``
    Props that should be sent as query string parameters.

``body_props``
    Props that should be sent in the JSON body. If empty, no body is
    sent.

``static_headers``
    Headers always added to the request (e.g. ``Notion-Version``).
    Will be forwarded to upstream because most upstream APIs accept
    these headers directly; Pipedream's Connect Proxy passes them
    through for non-restricted headers.

``body_extra``
    Static body fields merged into the rendered body (rare).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional


@dataclass(frozen=True)
class ActionSignature:
    app_slug: str
    method: str
    path_template: str
    required_props: tuple = ()
    path_props: tuple = ()
    query_props: tuple = ()
    body_props: tuple = ()
    static_headers: Mapping[str, str] = field(default_factory=dict)
    body_extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RenderedRequest:
    app_slug: str
    method: str
    url: str
    headers: Dict[str, str]
    query: Dict[str, str]
    json_body: Optional[Dict[str, Any]]


# ---------------------------------------------------------------------------
# Curated map (intentionally small, expand over time)
# ---------------------------------------------------------------------------

ACTION_MAP: Dict[str, ActionSignature] = {
    # GitHub --------------------------------------------------------------
    "github-get-current-user": ActionSignature(
        app_slug="github",
        method="GET",
        path_template="https://api.github.com/user",
    ),
    "github-create-issue": ActionSignature(
        app_slug="github",
        method="POST",
        path_template="https://api.github.com/repos/{repoFullname}/issues",
        required_props=("repoFullname", "title"),
        path_props=("repoFullname",),
        body_props=("title", "body", "labels", "assignees", "milestone"),
    ),
    "github-create-issue-comment": ActionSignature(
        app_slug="github",
        method="POST",
        path_template=(
            "https://api.github.com/repos/{repoFullname}/issues/"
            "{issueNumber}/comments"
        ),
        required_props=("repoFullname", "issueNumber", "body"),
        path_props=("repoFullname", "issueNumber"),
        body_props=("body",),
    ),
    # Gmail ---------------------------------------------------------------
    "gmail-get-profile": ActionSignature(
        app_slug="gmail",
        method="GET",
        path_template="https://www.googleapis.com/gmail/v1/users/me/profile",
    ),
    "gmail-list-labels": ActionSignature(
        app_slug="gmail",
        method="GET",
        path_template="https://www.googleapis.com/gmail/v1/users/me/labels",
    ),
    # Notion --------------------------------------------------------------
    "notion-retrieve-self": ActionSignature(
        app_slug="notion",
        method="GET",
        path_template="https://api.notion.com/v1/users/me",
        static_headers={"Notion-Version": "2022-06-28"},
    ),
    # Resend (API-key app) ------------------------------------------------
    "resend-send-email": ActionSignature(
        app_slug="resend",
        method="POST",
        path_template="https://api.resend.com/emails",
        required_props=("from", "to", "subject"),
        body_props=(
            "from",
            "to",
            "subject",
            "html",
            "text",
            "cc",
            "bcc",
            "reply_to",
            "tags",
            "headers",
        ),
    ),
    "resend-list-domains": ActionSignature(
        app_slug="resend",
        method="GET",
        path_template="https://api.resend.com/domains",
    ),
}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class ActionRenderError(ValueError):
    pass


def render_request(
    action_key: str,
    configured_props: Optional[Mapping[str, Any]] = None,
) -> RenderedRequest:
    """Translate a configured action invocation into a concrete HTTP request."""
    if action_key not in ACTION_MAP:
        raise KeyError(action_key)

    sig = ACTION_MAP[action_key]
    props: Dict[str, Any] = dict(configured_props or {})

    # 1. Required prop check
    missing = [
        p for p in sig.required_props if p not in props or props[p] in (None, "")
    ]
    if missing:
        raise ActionRenderError(
            f"action {action_key!r} is missing required props: {missing}"
        )

    # 2. Path interpolation
    try:
        url = sig.path_template.format(**{p: props[p] for p in sig.path_props})
    except KeyError as e:
        raise ActionRenderError(
            f"action {action_key!r} path template needs prop {e!s} which was not provided"
        ) from None

    # 3. Query string
    query = {
        p: _stringify(props[p])
        for p in sig.query_props
        if p in props and props[p] is not None
    }

    # 4. Body
    body: Optional[Dict[str, Any]] = None
    if sig.body_props or sig.body_extra:
        body = {
            p: props[p] for p in sig.body_props if p in props and props[p] is not None
        }
        if sig.body_extra:
            for k, v in sig.body_extra.items():
                body.setdefault(k, v)
        if not body:
            body = None

    return RenderedRequest(
        app_slug=sig.app_slug,
        method=sig.method,
        url=url,
        headers=dict(sig.static_headers),
        query=query,
        json_body=body,
    )


def list_supported_actions() -> List[str]:
    return sorted(ACTION_MAP.keys())


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


__all__ = [
    "ACTION_MAP",
    "ActionRenderError",
    "ActionSignature",
    "RenderedRequest",
    "render_request",
    "list_supported_actions",
]
