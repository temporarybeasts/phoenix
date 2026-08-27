"""Syncs OIDC group claims into ``users.idp_groups`` at login time (see
``phoenix.server.api.routers.oauth2``, right after ``_process_oauth2_user``
returns).

Wholesale-replace semantics, mirroring the existing OIDC role-resync
behavior: the user's raw group-name list is overwritten (not merged) from
their current claims on every login, so removal from a group takes effect on
the user's next login, not instantly. No project access is materialized
here -- it's computed live from this list at resolution time, see
``phoenix.server.access.resolution``.
"""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from phoenix.db import models
from phoenix.server.access.resolution import invalidate_readable_project_ids_cache


async def sync_idp_groups(session: AsyncSession, user_id: int, groups: list[str]) -> None:
    """Wholesale-replace `user_id`'s persisted raw group list with `groups`
    (the caller's already-extracted OIDC `groups` claim)."""
    await session.execute(
        update(models.User).where(models.User.id == user_id).values(idp_groups=groups)
    )
    invalidate_readable_project_ids_cache(user_id)
