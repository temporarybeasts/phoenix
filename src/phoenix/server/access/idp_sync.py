"""Syncs OIDC group claims into the fork's access-control tables at login.

Two things happen here, both at login time (see
``phoenix.server.api.routers.oauth2``, right after ``_process_oauth2_user``
returns):

1. ``sync_idp_group_memberships`` -- replace semantics, mirrors the
   existing OIDC role-resync behavior: a user's ``user_idp_group_memberships``
   rows are deleted and reinserted from their current claims on every
   login, so removal from a group takes effect on the user's next login,
   not instantly.
2. ``sync_config_driven_project_grants`` -- reconciles ``project_grants``
   rows (source="config") from the declarative
   PHOENIX_ACCESS_CONTROL_GROUP_MAPPING_FILE mapping, for the groups the
   logging-in user currently belongs to. **Known limitation for this
   foundation stage**: this is additive-only -- it upserts grants implied
   by the user's current groups, but doesn't retroactively revoke grants
   for *other* groups whose mapping entry was removed or narrowed, since
   no single login has visibility into every group that's ever been
   configured. Full reconciliation (sweeping all configured groups against
   current project names) is a natural follow-up, e.g. as a scheduled job,
   once this foundation is in place.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phoenix.config import get_env_access_control_group_mapping_file
from phoenix.db import models
from phoenix.db.helpers import SupportedSQLDialect
from phoenix.server.access.resolution import invalidate_readable_project_ids_cache


@dataclass(frozen=True)
class _GroupProjectMappingEntry:
    idp_group: str
    project_globs: list[str]
    permission: str


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
    _mapping_cache = [
        _GroupProjectMappingEntry(
            idp_group=entry["idp_group"],
            project_globs=list(entry["projects"]),
            permission=entry["permission"],
        )
        for entry in raw
    ]
    return _mapping_cache


async def sync_idp_group_memberships(
    session: AsyncSession, user_id: int, groups: list[str]
) -> dict[str, int]:
    """Replace `user_id`'s IdP-group memberships with `groups`, upserting
    any new group names. Returns {group name: idp_group_id}, which the
    caller passes straight to `sync_config_driven_project_grants` to avoid
    re-resolving the same names."""
    idp_group_ids_by_name = {name: await _get_or_create_idp_group(session, name) for name in groups}
    idp_group_ids = list(idp_group_ids_by_name.values())

    if idp_group_ids:
        await session.execute(
            delete(models.UserIdpGroupMembership).where(
                models.UserIdpGroupMembership.user_id == user_id,
                models.UserIdpGroupMembership.idp_group_id.notin_(idp_group_ids),
            )
        )
    else:
        await session.execute(
            delete(models.UserIdpGroupMembership).where(
                models.UserIdpGroupMembership.user_id == user_id
            )
        )

    dialect = SupportedSQLDialect(session.bind.dialect.name)
    for idp_group_id in idp_group_ids:
        record = {"user_id": user_id, "idp_group_id": idp_group_id}
        if dialect is SupportedSQLDialect.POSTGRESQL:
            stmt = pg_insert(models.UserIdpGroupMembership).values(record)
            await session.execute(
                stmt.on_conflict_do_nothing(index_elements=["user_id", "idp_group_id"])
            )
        else:
            stmt = sqlite_insert(models.UserIdpGroupMembership).values(record)
            await session.execute(
                stmt.on_conflict_do_nothing(index_elements=["user_id", "idp_group_id"])
            )

    invalidate_readable_project_ids_cache(user_id)
    return idp_group_ids_by_name


async def sync_config_driven_project_grants(
    session: AsyncSession, idp_group_ids_by_name: dict[str, int]
) -> None:
    """For each configured mapping entry whose idp_group the user currently
    belongs to, glob-match project names and upsert (source="config")
    project_grants rows keyed by idp_group_id -- so the grant is shared by
    every member of the group, not duplicated per user. See the module
    docstring for this function's additive-only limitation."""
    mapping = _load_group_mapping()
    if not mapping:
        return
    relevant = [entry for entry in mapping if entry.idp_group in idp_group_ids_by_name]
    if not relevant:
        return

    project_names = (await session.execute(select(models.Project.id, models.Project.name))).all()

    dialect = SupportedSQLDialect(session.bind.dialect.name)
    for entry in relevant:
        idp_group_id = idp_group_ids_by_name[entry.idp_group]
        matching_project_ids = [
            project_id
            for project_id, name in project_names
            if any(fnmatch.fnmatch(name, pattern) for pattern in entry.project_globs)
        ]
        for project_id in matching_project_ids:
            record = {
                "project_id": project_id,
                "idp_group_id": idp_group_id,
                "user_id": None,
                "permission": entry.permission,
                "source": "config",
            }
            if dialect is SupportedSQLDialect.POSTGRESQL:
                # uq_project_grants_idp_group is a PARTIAL unique index
                # (WHERE idp_group_id IS NOT NULL), not a named constraint --
                # Postgres's ON CONFLICT ON CONSTRAINT only works for actual
                # constraints, so this must match it via index_elements +
                # index_where instead of referencing it by name.
                stmt = pg_insert(models.ProjectGrant).values(record)
                await session.execute(
                    stmt.on_conflict_do_nothing(
                        index_elements=["project_id", "idp_group_id", "permission"],
                        index_where=text("idp_group_id IS NOT NULL"),
                    )
                )
            else:
                # SQLite also requires index_where to match the partial
                # index's predicate exactly for ON CONFLICT inference.
                stmt = sqlite_insert(models.ProjectGrant).values(record)
                await session.execute(
                    stmt.on_conflict_do_nothing(
                        index_elements=["project_id", "idp_group_id", "permission"],
                        index_where=text("idp_group_id IS NOT NULL"),
                    )
                )


async def _get_or_create_idp_group(session: AsyncSession, name: str) -> int:
    if (
        idp_group_id := await session.scalar(
            select(models.IdpGroup.id).where(models.IdpGroup.name == name)
        )
    ) is not None:
        return idp_group_id
    dialect = SupportedSQLDialect(session.bind.dialect.name)
    record = {"name": name}
    if dialect is SupportedSQLDialect.POSTGRESQL:
        stmt = pg_insert(models.IdpGroup).values(record)
        await session.execute(stmt.on_conflict_do_nothing(index_elements=["name"]))
    else:
        stmt = sqlite_insert(models.IdpGroup).values(record)
        await session.execute(stmt.on_conflict_do_nothing(index_elements=["name"]))
    idp_group_id = await session.scalar(
        select(models.IdpGroup.id).where(models.IdpGroup.name == name)
    )
    assert idp_group_id is not None
    return idp_group_id
