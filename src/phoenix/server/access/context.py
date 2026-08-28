"""Ambient "current authenticated user" and "active project group" state
for the DB-isolation spike's RLS session-variable hook (see the SSO/RBAC
fork plan's "DB-isolation spike" section).

`DbSessionFactory` is a bare no-arg callable, built once at startup and
shared across ~109 call sites (GraphQL resolvers, REST handlers) -- there's
no per-request slot to carry the current user through explicitly without a
large, risky diff across every call site. A `ContextVar` set once per
request by `CurrentUserMiddleware`, read implicitly inside the session
factory itself (see `app.py`'s modified `_db`), gets the same effect with
zero changes to any existing call site. The active project group -- which
of the user's several groups they're currently "viewing", see
`phoenix.server.access.resolution` -- is threaded through the same way, from
a dedicated session cookie rather than the user's signed auth token (see
`phoenix.auth`'s `PHOENIX_ACTIVE_PROJECT_GROUP_COOKIE_NAME`).
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from phoenix.auth import PHOENIX_ACTIVE_PROJECT_GROUP_COOKIE_NAME
from phoenix.server.bearer_auth import PhoenixUser

current_user_var: ContextVar[Optional[PhoenixUser]] = ContextVar("current_user", default=None)

# The `ProjectGroup.id` from the caller's active-project-group cookie for
# the duration of the request, or `None` if absent/unparseable. Never
# trusted as a credential on its own -- every read re-validates it against
# the caller's live group memberships (see
# `phoenix.server.access.resolution._resolve_membership`), so a stale or
# tampered value only ever degrades to "not currently a member", never to
# elevated access.
active_project_group_var: ContextVar[Optional[int]] = ContextVar(
    "active_project_group", default=None
)


class CurrentUserMiddleware:
    """Populates `current_user_var` from `scope["user"]` for the duration
    of each HTTP/WebSocket request, then resets it.

    Pure ASGI middleware (not `BaseHTTPMiddleware`), reading `scope`
    directly: `scope.get("user")` is safe whether or not
    `AuthenticationMiddleware` is even installed (it isn't when auth is
    disabled) -- Starlette's `Request.user` property, by contrast, raises
    if `AuthenticationMiddleware` never ran.

    Must be registered *after* `AuthenticationMiddleware` in `app.py`'s
    `middlewares` list -- Starlette applies middleware in list order (first
    in the list is outermost and sees the request first), so this needs to
    be appended to the list following `AuthenticationMiddleware`'s own
    `.append()` call, not before it, so `scope["user"]` is already
    populated by the time this runs.

    Background/daemon DB access (experiment runner, retention sweeps, bulk
    span insertion, etc.) never passes through any ASGI middleware at all,
    so `current_user_var` stays at its default (`None`) for those code
    paths -- the session factory's "no user in context" branch treats that
    the same as admin/system access (full visibility), not as denied. See
    `app.py`'s modified `_db` for that branch.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        user = scope.get("user")
        user_token = current_user_var.set(user if isinstance(user, PhoenixUser) else None)
        group_token = active_project_group_var.set(_read_active_project_group_cookie(scope))
        try:
            await self.app(scope, receive, send)
        finally:
            current_user_var.reset(user_token)
            active_project_group_var.reset(group_token)


def _read_active_project_group_cookie(scope: Scope) -> Optional[int]:
    # `Request(scope)` (no `receive`) only reads headers, never the body --
    # safe here since cookies live in headers, and avoids the same
    # `Request.user` pitfall this middleware's docstring already calls out
    # for `scope["user"]`.
    raw = Request(scope).cookies.get(PHOENIX_ACTIVE_PROJECT_GROUP_COOKIE_NAME)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
