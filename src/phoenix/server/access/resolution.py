"""Resolves which projects a user can access, from ``project_grants``
(direct or via IdP-group membership).

This is the single source of truth both the app-layer query filtering
(Stage 4b) and the DB-isolation spike's RLS session-variable hook are meant
to read from -- see the SSO/RBAC fork plan.
"""

from __future__ import annotations

from typing import Iterable

import cachetools
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phoenix.db import models
from phoenix.server.bearer_auth import PhoenixSystemUser, PhoenixUser

# Keyed by user_id. ~30s TTL is a concrete answer to the accepted RBAC
# spec's own open question about cache invalidation strategy (rbac.md flags
# this as unresolved) -- the tradeoff (a revoked grant can take up to the
# TTL to take effect, stacked on top of the existing OIDC
# role-resync-on-login latency) mirrors one already accepted in Stage 1.
_READABLE_PROJECT_IDS_TTL_SECONDS = 30
_readable_project_ids_cache: "cachetools.TTLCache[int, frozenset[int] | None]" = (
    cachetools.TTLCache(maxsize=10_000, ttl=_READABLE_PROJECT_IDS_TTL_SECONDS)
)


async def get_readable_project_ids(
    session: AsyncSession, user: PhoenixUser
) -> frozenset[int] | None:
    """The set of project ids ``user`` can read, or ``None`` meaning "all
    projects" -- admin, system user, or (by construction, since callers only
    reach this when auth is enabled) never invoked for the auth-disabled
    case, which callers should treat as "all projects" without calling this
    at all."""
    if isinstance(user, PhoenixSystemUser) or user.is_admin:
        return None
    user_id = int(user.identity)
    if user_id in _readable_project_ids_cache:
        return _readable_project_ids_cache[user_id]
    ids = await _resolve_readable_project_ids(session, user_id)
    _readable_project_ids_cache[user_id] = ids
    return ids


async def get_unreadable_project_ids(
    session: AsyncSession, user: PhoenixUser, project_ids: Iterable[int]
) -> frozenset[int]:
    """The subset of ``project_ids`` ``user`` cannot read -- empty for
    admin/system users. Reused for write eligibility too (not just reads):
    the app layer draws no per-project read/write distinction today (a
    single global role gate, not project-scoped), so a project a user can
    read is one they can annotate. Callers should only invoke this when
    auth is enabled and ``user`` is a real ``PhoenixUser``, matching
    ``get_readable_project_ids``'s own contract."""
    readable = await get_readable_project_ids(session, user)
    if readable is None:
        return frozenset()
    return frozenset(project_ids) - readable


async def user_can(
    session: AsyncSession, user: PhoenixUser, project_id: int, permission: str
) -> bool:
    """Whether ``user`` holds ``permission`` on ``project_id``. For
    lower-volume checks (e.g. PROJECT_MANAGE_ACCESS mutations) that don't
    warrant going through the cached readable-project-ids path."""
    if isinstance(user, PhoenixSystemUser) or user.is_admin:
        return True
    user_id = int(user.identity)
    stmt = _grant_query(project_id=project_id, user_id=user_id, permission=permission).limit(1)
    result = await session.execute(stmt)
    return result.first() is not None


def invalidate_readable_project_ids_cache(user_id: int) -> None:
    """Drop a user's cached readable-project-ids -- call right after their
    grants change (e.g. the config-driven group sync at login) so the
    change is visible immediately rather than waiting out the TTL."""
    _readable_project_ids_cache.pop(user_id, None)


async def _resolve_readable_project_ids(session: AsyncSession, user_id: int) -> frozenset[int]:
    stmt = _grant_query(user_id=user_id)
    result = await session.execute(stmt)
    return frozenset(row[0] for row in result)


def _grant_query(
    *,
    user_id: int,
    project_id: "int | None" = None,
    permission: "str | None" = None,
):
    """A grant lookup covering both direct user grants and grants held via
    the user's current IdP-group memberships. Callers narrow with
    ``project_id``/``permission`` for a targeted check, or leave them unset
    to enumerate every readable project.

    Filters are applied to each branch *before* the UNION -- a
    ``CompoundSelect`` (what ``.union()`` returns) doesn't support
    ``.where()`` the way a plain ``Select`` does, so this can't be done
    after combining the two branches."""

    def _filtered(stmt):
        if project_id is not None:
            stmt = stmt.where(models.ProjectGrant.project_id == project_id)
        if permission is not None:
            stmt = stmt.where(models.ProjectGrant.permission == permission)
        return stmt

    direct = _filtered(
        select(models.ProjectGrant.project_id).where(models.ProjectGrant.user_id == user_id)
    )
    via_group = _filtered(
        select(models.ProjectGrant.project_id)
        .join(
            models.UserIdpGroupMembership,
            models.UserIdpGroupMembership.idp_group_id == models.ProjectGrant.idp_group_id,
        )
        .where(models.UserIdpGroupMembership.user_id == user_id)
    )
    return direct.union(via_group)
