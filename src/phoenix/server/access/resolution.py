"""Resolves which projects a user can access, computed live from the raw
IdP-group-name list persisted on ``users.idp_groups`` (set at login, see
``phoenix.server.access.idp_sync``) against the declarative group->project
mapping config (``PHOENIX_ACCESS_CONTROL_GROUP_MAPPING_FILE``) and the
*current* ``projects`` table -- nothing is pre-materialized into its own
grant table, so a narrowed/removed mapping entry or a newly created project
matching an already-held group's glob takes effect on the next cache
refresh, not only at the user's next login.

This is the single source of truth both the app-layer query filtering and
the DB-isolation RLS session-variable hook (`app.py`'s
`_set_db_isolation_guards`) read from.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import cachetools
import yaml
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from phoenix.config import get_env_access_control_group_mapping_file
from phoenix.db import models
from phoenix.server.access.permissions import PROJECT_ROLE_PERMISSIONS, ProjectRole
from phoenix.server.bearer_auth import PhoenixSystemUser, PhoenixUser

# project_id -> the union of permission strings the user holds on it.
_ProjectPermissions = dict[int, frozenset[str]]

# Keyed by user_id. ~30s TTL is a concrete answer to the accepted RBAC
# spec's own open question about cache invalidation strategy (rbac.md flags
# this as unresolved) -- the tradeoff (a revoked grant can take up to the
# TTL to take effect, stacked on top of the existing OIDC
# role-resync-on-login latency) mirrors one already accepted in Stage 1.
_READABLE_PROJECT_IDS_TTL_SECONDS = 30
_project_permissions_cache: "cachetools.TTLCache[int, _ProjectPermissions]" = cachetools.TTLCache(
    maxsize=10_000, ttl=_READABLE_PROJECT_IDS_TTL_SECONDS
)


@dataclass(frozen=True)
class _GroupProjectMappingEntry:
    idp_group: str
    project_globs: list[str]
    role: ProjectRole


_mapping_cache: Optional[list[_GroupProjectMappingEntry]] = None


def _load_group_mapping() -> list[_GroupProjectMappingEntry]:
    """Loaded once per process and cached -- this is fork-only, low-churn
    config, not something that needs live-reload."""
    global _mapping_cache
    if _mapping_cache is not None:
        return _mapping_cache
    path = get_env_access_control_group_mapping_file()
    if not path:
        _mapping_cache = []
        return _mapping_cache
    raw = yaml.safe_load(Path(path).read_text()) or []
    entries = []
    for entry in raw:
        try:
            role = ProjectRole(entry["role"])
        except ValueError:
            raise ValueError(
                f"Invalid role {entry.get('role')!r} for idp_group "
                f"{entry.get('idp_group')!r} in {path} -- must be one of "
                f"{[r.value for r in ProjectRole]}"
            ) from None
        entries.append(
            _GroupProjectMappingEntry(
                idp_group=entry["idp_group"],
                project_globs=list(entry["projects"]),
                role=role,
            )
        )
    _mapping_cache = entries
    return _mapping_cache


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
    permissions = await _get_project_permissions(session, user)
    return frozenset(permissions)


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
    """Whether ``user`` holds ``permission`` on ``project_id``."""
    if isinstance(user, PhoenixSystemUser) or user.is_admin:
        return True
    permissions = await _get_project_permissions(session, user)
    return permission in permissions.get(project_id, frozenset())


def invalidate_readable_project_ids_cache(user_id: int) -> None:
    """Drop a user's cached project permissions -- call right after their
    groups change (e.g. the group sync at login) so the change is visible
    immediately rather than waiting out the TTL."""
    _project_permissions_cache.pop(user_id, None)


async def _get_project_permissions(session: AsyncSession, user: PhoenixUser) -> _ProjectPermissions:
    user_id = int(user.identity)
    if user_id in _project_permissions_cache:
        return _project_permissions_cache[user_id]
    permissions = await _resolve_project_permissions(session, user_id)
    _project_permissions_cache[user_id] = permissions
    return permissions


async def _resolve_project_permissions(session: AsyncSession, user_id: int) -> _ProjectPermissions:
    groups = set(
        await session.scalar(select(models.User.idp_groups).where(models.User.id == user_id)) or ()
    )
    if not groups:
        return {}
    relevant = [entry for entry in _load_group_mapping() if entry.idp_group in groups]
    if not relevant:
        return {}
    projects = await _list_all_projects_bypassing_rls(session)
    result: dict[int, set[str]] = {}
    for entry in relevant:
        permissions = PROJECT_ROLE_PERMISSIONS[entry.role]
        for project_id, name in projects:
            if any(fnmatch.fnmatch(name, pattern) for pattern in entry.project_globs):
                result.setdefault(project_id, set()).update(permissions)
    return {project_id: frozenset(perms) for project_id, perms in result.items()}


async def _list_all_projects_bypassing_rls(session: AsyncSession) -> list[tuple[int, str]]:
    """Lists every project's (id, name), regardless of the ambient
    session's own RLS scope. This is trusted internal work computing what a
    user's *new* access set should be, not a query already scoped to what
    they currently see -- it must see the full catalog. Reached from
    `app.py`'s `_set_db_isolation_guards` before that function has set
    `app.readable_project_ids`/`SET ROLE` for the request, so
    `app.bypass_rls` must be explicitly reset to `'false'` -- not just left
    set or merely unset -- before returning, or the rest of *this* request's
    own transaction would silently keep running with RLS off. Postgres-only;
    a no-op guard on SQLite, which has no RLS at all."""
    is_postgresql = session.bind is not None and session.bind.dialect.name == "postgresql"
    if is_postgresql:
        await session.execute(text("SELECT set_config('app.bypass_rls', 'true', true)"))
    try:
        rows = (await session.execute(select(models.Project.id, models.Project.name))).all()
    finally:
        if is_postgresql:
            await session.execute(text("SELECT set_config('app.bypass_rls', 'false', true)"))
    return [(row[0], row[1]) for row in rows]
