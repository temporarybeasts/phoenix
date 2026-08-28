"""Regression test for the RLS-bypass hazard in
`resolution._list_project_ids_in_group_bypassing_rls`: it must see every
project in a group regardless of the ambient session's own RLS scope, AND
must explicitly reset `app.bypass_rls` back to `'false'` before returning --
not just leave it set or merely unset it -- since it's reached from
`app.py`'s `_set_db_isolation_guards`, which runs at the start of *every*
authenticated request's transaction, before `app.readable_project_ids`/
`SET ROLE` are applied. If the bypass GUC were left `'true'`, the rest of
that same request's real queries would silently run with RLS off too -- a
security regression, not just a functional bug.

This is the same class of bug originally found and fixed in the old
`sync_config_driven_project_grants` (see git history / commit adding this
file's earlier version): for a user's first-ever login, `current_user_var`
is unset (`None`), which correctly bypasses RLS. But a user who
re-authenticates via SSO *while already holding a valid session* has
`current_user_var` resolve to *themselves* -- a real, non-admin
`PhoenixUser` -- so any "list all projects in this group" lookup that
doesn't explicitly bypass RLS would be silently scoped to that user's own
current readable projects instead of the full catalog. The resolver
reintroduces this same lookup (now on every access check, not just at
login), so the same hazard applies here, with higher stakes: the bypass now
runs inside the request's own transaction, not an isolated login-only one.

Runs against `migrated_postgresql_engine` (real Alembic DDL, same reasoning
as `test_write_side_rls.py`/`test_baseline_grants.py`: the RLS policies only
exist via migrations, not `create_all`) and the app's real `_db()` factory,
so this actually exercises the RLS policies plus the
`current_user_var`/`_set_db_isolation_guards` wiring.
"""

from __future__ import annotations

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from phoenix.db import models
from phoenix.server.access import resolution
from phoenix.server.access.context import current_user_var
from phoenix.server.access.resolution import get_readable_project_ids
from phoenix.server.app import _db
from phoenix.server.bearer_auth import PhoenixUser
from phoenix.server.types import (
    AccessTokenId,
    DbSessionFactory,
    UserClaimSet,
    UserId,
    UserTokenAttributes,
)

pytestmark = pytest.mark.postgres_only


def _phoenix_user(user_id: int, role: str = "MEMBER") -> PhoenixUser:
    return PhoenixUser(
        UserId(user_id),
        UserClaimSet(
            subject=UserId(user_id),
            token_id=AccessTokenId(1),
            attributes=UserTokenAttributes(user_role=role),  # type: ignore[arg-type]
        ),
    )


async def test_resolution_bypasses_rls_for_already_authenticated_user(
    migrated_postgresql_engine: AsyncEngine,
) -> None:
    resolution._membership_cache.clear()
    db = DbSessionFactory(db=_db(migrated_postgresql_engine), dialect="postgresql")

    # Set-up work (seeding roles/project-group/mapping/project/user) runs
    # with no ambient user, so it bypasses RLS the same way background/
    # daemon work does -- this mirrors how these rows actually get created
    # in production (migrations, an external onboarding process, JIT
    # provisioning) before the buggy request ever happens.
    async with db() as session:
        existing = set(await session.scalars(select(models.UserRole.name)))
        if missing := ({"ADMIN", "MEMBER", "VIEWER", "SYSTEM"} - existing):
            await session.execute(insert(models.UserRole), [{"name": n} for n in missing])
        role_id = await session.scalar(
            select(models.UserRole.id).where(models.UserRole.name == "MEMBER")
        )
        assert role_id is not None
        group = models.ProjectGroup(name="g-group")
        session.add(group)
        await session.flush()
        session.add(
            models.ExternalRoleProjectGroupMapping(
                external_role="g", project_group_id=group.id, role="VIEWER"
            )
        )
        project = models.Project(name="proj", project_group_id=group.id)
        session.add(project)
        user = models.User(
            user_role_id=role_id,
            username="user",
            email="user@example.com",
            password_hash=b"hash",
            password_salt=b"salt",
            reset_password=False,
            auth_method="LOCAL",
            idp_groups=["g"],
        )
        session.add(user)
        await session.flush()
        project_id, user_id = project.id, user.id

    # The bug only manifests once this user is the *ambient* current user --
    # i.e. they're re-authenticating with an existing, valid session, not
    # logging in for the first time (which leaves current_user_var unset).
    token = current_user_var.set(_phoenix_user(user_id))
    try:
        async with db() as session:
            readable = await get_readable_project_ids(session, _phoenix_user(user_id))
            assert readable == frozenset({project_id})

            # The load-bearing new assertion: bypass_rls must read back
            # 'false' within the *same* transaction afterward, or the rest
            # of this request's own queries would run with RLS off too.
            bypass = await session.scalar(text("SELECT current_setting('app.bypass_rls', true)"))
            assert bypass == "false", (
                "app.bypass_rls was left set after resolving readable project ids -- "
                "the rest of this request's transaction would run with RLS bypassed"
            )
    finally:
        current_user_var.reset(token)


async def test_resolution_sees_new_grant_without_stale_scoping(
    migrated_postgresql_engine: AsyncEngine,
) -> None:
    """The original failure mode: an already-authenticated user's project
    listing must see every project in a newly-mapped group (so it can be
    included once the mapping row exists), not be silently scoped to their
    own current readable projects."""
    resolution._membership_cache.clear()
    db = DbSessionFactory(db=_db(migrated_postgresql_engine), dialect="postgresql")

    async with db() as session:
        existing = set(await session.scalars(select(models.UserRole.name)))
        if missing := ({"ADMIN", "MEMBER", "VIEWER", "SYSTEM"} - existing):
            await session.execute(insert(models.UserRole), [{"name": n} for n in missing])
        role_id = await session.scalar(
            select(models.UserRole.id).where(models.UserRole.name == "MEMBER")
        )
        assert role_id is not None
        group = models.ProjectGroup(name="g-group-2")
        session.add(group)
        await session.flush()
        project = models.Project(name="proj2", project_group_id=group.id)
        session.add(project)
        # Starts with no groups -- current readable projects is empty.
        user = models.User(
            user_role_id=role_id,
            username="user2",
            email="user2@example.com",
            password_hash=b"hash",
            password_salt=b"salt",
            reset_password=False,
            auth_method="LOCAL",
        )
        session.add(user)
        await session.flush()
        project_id, user_id, group_id = project.id, user.id, group.id

    token = current_user_var.set(_phoenix_user(user_id))
    try:
        async with db() as session:
            await session.execute(
                update(models.User).where(models.User.id == user_id).values(idp_groups=["g"])
            )
            session.add(
                models.ExternalRoleProjectGroupMapping(
                    external_role="g", project_group_id=group_id, role="VIEWER"
                )
            )
        resolution.invalidate_readable_project_ids_cache(user_id)

        async with db() as session:
            readable = await get_readable_project_ids(session, _phoenix_user(user_id))
        assert readable == frozenset({project_id})
    finally:
        current_user_var.reset(token)
