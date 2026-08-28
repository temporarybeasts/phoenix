"""Active-project-group cookie logic shared by every login code path (local,
LDAP, OAuth2) and the ``setActiveProjectGroup`` mid-session-switch mutation.

Deliberately *not* invoked from token refresh (``auth.py``'s
``_refresh_tokens``, which reuses the same ``_create_auth_response`` helper
as a genuine login) -- refreshing an access token should never reset or
re-prompt for the active group. The active-group cookie has its own
lifecycle (a true browser-session cookie, see
``phoenix.auth.set_active_project_group_cookie``), independent of the
access/refresh token cookies.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from phoenix.auth import ResponseType, set_active_project_group_cookie
from phoenix.server.access.resolution import get_user_project_group_memberships


async def apply_login_active_project_group_cookie(
    *, session: AsyncSession, response: ResponseType, user_id: int
) -> ResponseType:
    """Auto-selects the active-project-group cookie for a user who holds
    exactly one project group. Leaves it unset for a user with zero groups
    (nothing to select) or several (the frontend must show a picker before
    the app shell loads -- see ``requires_group_selection``)."""
    memberships = await get_user_project_group_memberships(session, user_id)
    if len(memberships) == 1:
        (only_group_id,) = memberships.keys()
        response = set_active_project_group_cookie(
            response=response, project_group_id=only_group_id
        )
    return response


async def requires_group_selection(session: AsyncSession, user_id: int) -> bool:
    """Whether a fresh login for ``user_id`` must be followed by an explicit
    group-selection step before the app shell loads (2+ held groups)."""
    memberships = await get_user_project_group_memberships(session, user_id)
    return len(memberships) > 1


async def resolve_active_project_group_switch(
    session: AsyncSession, user_id: int, requested_project_group_id: int
) -> Optional[int]:
    """Validates that ``user_id`` currently holds a role in
    ``requested_project_group_id`` before switching -- returns the group id
    to set the cookie to, or ``None`` if the user isn't a member (the
    mutation should reject rather than set the cookie in that case)."""
    memberships = await get_user_project_group_memberships(session, user_id)
    if requested_project_group_id in memberships:
        return requested_project_group_id
    return None
