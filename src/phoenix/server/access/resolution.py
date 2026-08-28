"""Resolves which projects a user can access.

Projects are organized into project groups (`models.ProjectGroup`); access
to a group is granted via external roles (raw IdP `groups` claim values
persisted on `users.idp_groups`, set at login -- see
`phoenix.server.access.idp_sync`) mapped to a `(project group, role)` pair
in `models.ExternalRoleProjectGroupMapping`, a config table maintained by an
onboarding process external to Phoenix -- not derived by naming convention,
and not YAML/file-based.

A user viewing more than one group at once is not supported: visibility is
scoped to whichever single group is currently "active" for the request (see
`phoenix.server.access.context.active_project_group_var`), not a union
across every group the user is a member of. A user who belongs to exactly
one group has that group as their implicit active group; a user who belongs
to zero, or to several with no selection made yet, resolves to no access
(fail closed) rather than silently choosing for them.

Nothing is pre-materialized into its own grant table -- a narrowed/removed
mapping row or a newly created project in a held group takes effect on the
next cache refresh, not only at the user's next login.

This is the single source of truth both the app-layer query filtering and
the DB-isolation RLS session-variable hook (`app.py`'s
`_set_db_isolation_guards`) read from.
"""

from __future__ import annotations

from typing import Iterable, Optional

import cachetools
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from phoenix.config import DEFAULT_PROJECT_GROUP_NAME
from phoenix.db import models
from phoenix.server.access.context import active_project_group_var
from phoenix.server.access.permissions import (
    PROJECT_GROUP_ROLE_RANK,
    PROJECT_ROLE_PERMISSIONS,
    WRITE_CAPABLE_PROJECT_GROUP_ROLES,
    ProjectGroupRole,
)
from phoenix.server.bearer_auth import PhoenixSystemUser, PhoenixUser

# (project_group_id, role) the user currently has access through, or None if
# unresolvable (no groups, or 2+ groups with no active selection, or a
# selected group the user no longer holds a mapped role into).
_MembershipResolution = Optional[tuple[int, ProjectGroupRole]]

# Keyed by (user_id, active_project_group_id). ~30s TTL is a concrete answer
# to the accepted RBAC spec's own open question about cache invalidation
# strategy (rbac.md flags this as unresolved) -- the tradeoff (a revoked
# grant can take up to the TTL to take effect, stacked on top of the
# existing OIDC role-resync-on-login latency) mirrors one already accepted
# in Stage 1.
_READABLE_PROJECT_IDS_TTL_SECONDS = 30
_membership_cache: "cachetools.TTLCache[tuple[int, Optional[int]], _MembershipResolution]" = (
    cachetools.TTLCache(maxsize=10_000, ttl=_READABLE_PROJECT_IDS_TTL_SECONDS)
)


async def _project_group_rbac_in_use(session: AsyncSession) -> bool:
    """Whether project-group RBAC is actually in use in this deployment at
    all -- i.e. whether `external_role_project_group_mappings` has any rows.
    An empty table means no external role could possibly grant anyone
    project-group access, for *any* user, IdP-authenticated or local --
    matching the pre-project-group-RBAC behavior (every authenticated
    non-admin user has full access) is the correct fallback, not "everyone
    sees nothing." This also correctly covers the bring-up window where an
    operator has configured an IdP's groups claim but the onboarding
    process hasn't populated the mapping table yet.

    Deliberately uncached, unlike `_membership_cache` -- a stale "not in
    use" reading here means every authenticated user gets full,
    unrestricted access deployment-wide until the cache expires, which is a
    far more dangerous direction of staleness than the membership cache's
    (which only ever narrows or delays one user's own access). Same
    precedent as `_list_project_ids_in_group_bypassing_rls` below. A single
    indexed `LIMIT 1` lookup per request is cheap enough not to need it."""
    return (
        await session.scalar(select(models.ExternalRoleProjectGroupMapping.id).limit(1))
    ) is not None


async def get_readable_project_ids(
    session: AsyncSession, user: PhoenixUser
) -> frozenset[int] | None:
    """The set of project ids ``user`` can read, or ``None`` meaning "all
    projects" -- admin, system user, project-group RBAC not in use in this
    deployment at all (see `_project_group_rbac_in_use`), or (by
    construction, since callers only reach this when auth is enabled) never
    invoked for the auth-disabled case, which callers should treat as "all
    projects" without calling this at all."""
    if isinstance(user, PhoenixSystemUser) or user.is_admin:
        return None
    if not await _project_group_rbac_in_use(session):
        return None
    membership = await _resolve_membership(session, int(user.identity))
    if membership is None:
        return frozenset()
    project_group_id, _role = membership
    return await _list_project_ids_in_group_bypassing_rls(session, project_group_id)


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


async def get_writable_project_group_ids(
    session: AsyncSession, user: PhoenixUser
) -> frozenset[int]:
    """The set of project-group ids ``user`` can write new/existing project
    rows into, given their currently active group -- empty for admin/system
    users, since those bypass RLS entirely via `app.bypass_rls` rather than
    an explicit writable-group allowlist (see `app.py`'s
    `_set_db_isolation_guards`)."""
    if isinstance(user, PhoenixSystemUser) or user.is_admin:
        return frozenset()
    membership = await _resolve_membership(session, int(user.identity))
    if membership is None:
        return frozenset()
    project_group_id, role = membership
    if role in WRITE_CAPABLE_PROJECT_GROUP_ROLES:
        return frozenset({project_group_id})
    return frozenset()


async def get_active_project_group_id_for_create(
    session: AsyncSession, user: PhoenixUser
) -> Optional[int]:
    """The project group a new project created by ``user`` right now should
    land in, or ``None`` if there isn't one -- zero/unselected groups, or a
    resolved group the user only holds ``VIEWER`` on. Note this does *not*
    special-case a global-role admin the way read access does (see
    ``get_readable_project_ids``): creating a project is still scoped to
    whichever group the caller is currently viewing, admin or not -- the
    global ``ADMIN`` account role and a per-group ``ADMIN`` role are
    different things. Only the true system actor (background/daemon work,
    no UI session) has no "active group" concept of its own; callers
    creating a project on its behalf should pick a group explicitly rather
    than calling this. When project-group RBAC isn't in use in this
    deployment at all (see `_project_group_rbac_in_use`), every
    authenticated user's projects land in the well-known default group,
    the same as the auth-disabled and OTLP-ingest paths."""
    if isinstance(user, PhoenixSystemUser):
        return None
    if not await _project_group_rbac_in_use(session):
        return await get_default_project_group_id(session)
    membership = await _resolve_membership(session, int(user.identity))
    if membership is None:
        return None
    project_group_id, role = membership
    if role not in WRITE_CAPABLE_PROJECT_GROUP_ROLES:
        return None
    return project_group_id


async def get_active_project_group(session: AsyncSession, user_id: int) -> _MembershipResolution:
    """The ``(project_group_id, role)`` ``user_id`` is currently "viewing",
    or ``None`` if unresolved. Used by the GraphQL ``User.activeProjectGroup``
    field -- deliberately does *not* special-case a global-role admin the
    same way ``get_readable_project_ids`` does, for the same reason
    ``get_active_project_group_id_for_create`` doesn't: viewing/creating is
    scoped to a group either way, admin or not. Takes a raw ``user_id``
    rather than a ``PhoenixUser``, since the GraphQL ``User`` type has no
    notion of ``PhoenixSystemUser`` (a virtual identity, never a real
    ``models.User`` row)."""
    return await _resolve_membership(session, user_id)


async def user_can(
    session: AsyncSession, user: PhoenixUser, project_id: int, permission: str
) -> bool:
    """Whether ``user`` holds ``permission`` on ``project_id``."""
    if isinstance(user, PhoenixSystemUser) or user.is_admin:
        return True
    if not await _project_group_rbac_in_use(session):
        return True
    membership = await _resolve_membership(session, int(user.identity))
    if membership is None:
        return False
    project_group_id, role = membership
    if permission not in PROJECT_ROLE_PERMISSIONS[role]:
        return False
    actual_group_id = await _get_project_group_id_bypassing_rls(session, project_id)
    return actual_group_id == project_group_id


async def get_default_project_group_id(session: AsyncSession) -> int:
    """The well-known project group every migration backfills pre-existing
    projects into (see `phoenix.config.DEFAULT_PROJECT_GROUP_NAME`) -- the
    landing group for OTLP-ingest-auto-created projects and other
    system-managed projects created with no authenticated "active group"
    context of their own (e.g. dataset-evaluator trace-capture projects,
    the admin-only REST project-creation endpoint)."""
    group_id = await session.scalar(
        select(models.ProjectGroup.id).where(models.ProjectGroup.name == DEFAULT_PROJECT_GROUP_NAME)
    )
    assert group_id is not None, (
        f"Default project group {DEFAULT_PROJECT_GROUP_NAME!r} is missing -- "
        "migration acd16dbc13d0_project_groups.py not applied?"
    )
    return group_id


def invalidate_readable_project_ids_cache(user_id: int) -> None:
    """Drop a user's cached membership resolution -- call right after their
    groups change (e.g. the group sync at login) so the change is visible
    immediately rather than waiting out the TTL. Sweeps every active-group
    variant cached for this user, since the cache key includes the active
    group id."""
    for key in [k for k in list(_membership_cache) if k[0] == user_id]:
        _membership_cache.pop(key, None)


async def _resolve_membership(session: AsyncSession, user_id: int) -> _MembershipResolution:
    active_project_group_id = active_project_group_var.get(None)
    cache_key = (user_id, active_project_group_id)
    if cache_key in _membership_cache:
        return _membership_cache[cache_key]
    result = await _resolve_membership_uncached(session, user_id, active_project_group_id)
    _membership_cache[cache_key] = result
    return result


async def get_user_project_group_memberships(
    session: AsyncSession, user_id: int
) -> dict[int, ProjectGroupRole]:
    """Every project group ``user_id`` currently holds a role in, via their
    held external roles -- independent of any active-group selection.
    Used at login time (single-group auto-select; multi-group picker, see
    ``phoenix.server.access.active_group``) and by the active-group-switch
    mutation, to validate a requested target."""
    groups = set(
        await session.scalar(select(models.User.idp_groups).where(models.User.id == user_id)) or ()
    )
    if not groups:
        return {}
    rows = (
        await session.execute(
            select(
                models.ExternalRoleProjectGroupMapping.project_group_id,
                models.ExternalRoleProjectGroupMapping.role,
            ).where(models.ExternalRoleProjectGroupMapping.external_role.in_(groups))
        )
    ).all()
    memberships: dict[int, ProjectGroupRole] = {}
    for project_group_id, role in rows:
        if (
            project_group_id not in memberships
            or PROJECT_GROUP_ROLE_RANK[role]
            > PROJECT_GROUP_ROLE_RANK[memberships[project_group_id]]
        ):
            memberships[project_group_id] = role
    return memberships


async def _resolve_membership_uncached(
    session: AsyncSession, user_id: int, active_project_group_id: Optional[int]
) -> _MembershipResolution:
    memberships = await get_user_project_group_memberships(session, user_id)
    if not memberships:
        return None
    if active_project_group_id is not None:
        if active_project_group_id in memberships:
            return (active_project_group_id, memberships[active_project_group_id])
        # Selected group no longer held (revoked mid-session, or stale/
        # tampered cookie) -- fail closed rather than falling back to
        # another held group the user never chose.
        return None
    if len(memberships) == 1:
        ((only_group_id, only_role),) = memberships.items()
        return (only_group_id, only_role)
    # 2+ groups held, none selected yet -- fail closed rather than guessing.
    return None


async def _list_project_ids_in_group_bypassing_rls(
    session: AsyncSession, project_group_id: int
) -> frozenset[int]:
    """Deliberately uncached (unlike `_resolve_membership`'s TTL cache):
    caching this separately previously broke the "a new project in a held
    group appears without re-login" guarantee, since invalidating the
    membership cache for one user has no way to know which group-listing
    cache entries (shared across every member of that group) need dropping
    too. The membership cache already bounds how often this runs; a plain
    indexed query per call is cheap enough not to need its own cache on
    top."""
    is_postgresql = session.bind is not None and session.bind.dialect.name == "postgresql"
    if is_postgresql:
        await session.execute(text("SELECT set_config('app.bypass_rls', 'true', true)"))
    try:
        ids = (
            await session.scalars(
                select(models.Project.id).where(models.Project.project_group_id == project_group_id)
            )
        ).all()
    finally:
        if is_postgresql:
            await session.execute(text("SELECT set_config('app.bypass_rls', 'false', true)"))
    return frozenset(ids)


async def _get_project_group_id_bypassing_rls(
    session: AsyncSession, project_id: int
) -> Optional[int]:
    """Trusted internal lookup of a project's own group, regardless of the
    ambient session's own RLS scope -- see `_list_project_ids_in_group_bypassing_rls`
    for why `app.bypass_rls` must be explicitly reset before returning."""
    is_postgresql = session.bind is not None and session.bind.dialect.name == "postgresql"
    if is_postgresql:
        await session.execute(text("SELECT set_config('app.bypass_rls', 'true', true)"))
    try:
        project_group_id: Optional[int] = await session.scalar(
            select(models.Project.project_group_id).where(models.Project.id == project_id)
        )
        return project_group_id
    finally:
        if is_postgresql:
            await session.execute(text("SELECT set_config('app.bypass_rls', 'false', true)"))
