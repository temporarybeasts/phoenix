import contextlib
from typing import Iterator, Optional

import pytest
from sqlalchemy import delete, insert, select, update

from phoenix.config import DEFAULT_PROJECT_GROUP_NAME
from phoenix.db import models
from phoenix.server.access import resolution
from phoenix.server.access.context import active_project_group_var
from phoenix.server.access.permissions import PROJECT_MANAGE_ACCESS, PROJECT_READ
from phoenix.server.access.resolution import (
    get_readable_project_ids,
    invalidate_readable_project_ids_cache,
    user_can,
)
from phoenix.server.bearer_auth import PhoenixSystemUser, PhoenixUser
from phoenix.server.types import (
    AccessTokenId,
    DbSessionFactory,
    UserClaimSet,
    UserId,
    UserTokenAttributes,
)


@pytest.fixture(autouse=True)
async def _seed_user_roles(db: DbSessionFactory) -> None:
    """UserRole rows are normally seeded by `Facilitator._ensure_enums` at
    real app startup (`phoenix serve`), which nothing in an isolated unit
    test run triggers -- seed them here, idempotently, so these tests don't
    depend on some other test happening to boot a full app first against
    the shared in-memory sqlite DB."""
    async with db() as session:
        existing = set(await session.scalars(select(models.UserRole.name)))
        if missing := ({"ADMIN", "MEMBER", "VIEWER", "SYSTEM"} - existing):
            await session.execute(insert(models.UserRole), [{"name": n} for n in missing])


@pytest.fixture(autouse=True)
def _clear_resolution_caches() -> None:
    """The membership cache is a process-global TTLCache. Each test's DB
    transaction rolls back and reuses the same small auto-incrementing ids
    (1, 2, ...), so without clearing it between tests, one test's cached
    result leaks into the next test's completely different rows with the
    same ids."""
    resolution._membership_cache.clear()


def _user(user_id: int, role: str = "MEMBER") -> PhoenixUser:
    return PhoenixUser(
        UserId(user_id),
        UserClaimSet(
            subject=UserId(user_id),
            token_id=AccessTokenId(1),
            attributes=UserTokenAttributes(user_role=role),  # type: ignore[arg-type]
        ),
    )


@contextlib.contextmanager
def _active_group(project_group_id: Optional[int]) -> Iterator[None]:
    token = active_project_group_var.set(project_group_id)
    try:
        yield
    finally:
        active_project_group_var.reset(token)


async def _create_user(db: DbSessionFactory, *, role: str = "MEMBER") -> int:
    async with db() as session:
        role_id = await session.scalar(
            select(models.UserRole.id).where(models.UserRole.name == role)
        )
        assert role_id is not None
        user = models.User(
            user_role_id=role_id,
            username=f"user-{id(object())}",
            email=f"user-{id(object())}@example.com",
            password_hash=b"hash",
            password_salt=b"salt",
            reset_password=False,
            auth_method="LOCAL",
        )
        session.add(user)
        await session.flush()
        return user.id


async def _create_project_group(db: DbSessionFactory, name: str) -> int:
    async with db() as session:
        group = models.ProjectGroup(name=name)
        session.add(group)
        await session.flush()
        return group.id


async def _create_project(db: DbSessionFactory, name: str, project_group_id: int) -> int:
    async with db() as session:
        project = models.Project(name=name, project_group_id=project_group_id)
        session.add(project)
        await session.flush()
        return project.id


async def _set_idp_groups(db: DbSessionFactory, user_id: int, groups: list[str]) -> None:
    async with db() as session:
        await session.execute(
            update(models.User).where(models.User.id == user_id).values(idp_groups=groups)
        )


async def _create_mapping(
    db: DbSessionFactory, external_role: str, project_group_id: int, role: str
) -> None:
    async with db() as session:
        session.add(
            models.ExternalRoleProjectGroupMapping(
                external_role=external_role,
                project_group_id=project_group_id,
                role=role,
            )
        )
        await session.flush()


async def test_admin_sees_all_projects(db: DbSessionFactory) -> None:
    admin = _user(await _create_user(db, role="ADMIN"), role="ADMIN")
    async with db() as session:
        assert await get_readable_project_ids(session, admin) is None


async def test_system_user_sees_all_projects(db: DbSessionFactory) -> None:
    async with db() as session:
        assert await get_readable_project_ids(session, PhoenixSystemUser(UserId(1))) is None


async def test_no_groups_means_no_readable_projects(db: DbSessionFactory) -> None:
    """This user holds no external roles at all. Project-group RBAC is
    still *in use* deployment-wide (an unrelated mapping row exists for
    some other role/group), so this correctly resolves to "no access"
    rather than the RBAC-not-in-use fallback of "all access" -- see
    `test_no_mapping_rows_at_all_means_full_access` for that other case."""
    user_id = await _create_user(db)
    group_id = await _create_project_group(db, "group-no-members")
    project_id = await _create_project(db, "project-no-groups", group_id)
    other_group_id = await _create_project_group(db, "unrelated-group")
    await _create_mapping(db, "some-other-role", other_group_id, "VIEWER")
    user = _user(user_id)
    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset()
        assert not await user_can(session, user, project_id, PROJECT_READ)


async def test_no_mapping_rows_at_all_means_full_access(db: DbSessionFactory) -> None:
    """When `external_role_project_group_mappings` has zero rows anywhere,
    project-group RBAC isn't in use in this deployment at all -- no
    external role could possibly grant anyone access, so every
    authenticated non-admin user gets full access, matching
    pre-project-group-RBAC behavior, rather than "nobody can see
    anything." This is the fix for a real regression: without it, a plain
    basic-auth-only deployment (Postgres + RLS, auth enabled, no OAuth2/
    IdP groups configured) would show every non-admin user zero projects
    and reject every write, including project creation -- caught via
    `tests/integration/auth/test_oauth2.py::TestGrantTokenAccess::test_grant_token_can_write_rest_resources`
    failing against real RLS."""
    # This test's `db` fixture is a bare session factory that doesn't run
    # the app-startup Facilitator step that normally seeds the well-known
    # default project group -- seed it explicitly under its real name so
    # `get_default_project_group_id` (exercised below) can find it.
    default_group_id = await _create_project_group(db, DEFAULT_PROJECT_GROUP_NAME)
    user_id = await _create_user(db)
    # Not the default group -- this user isn't a member of anything, on
    # purpose, to prove access is independent of membership once RBAC is
    # confirmed not-in-use.
    group_id = await _create_project_group(db, "only-group")
    project_id = await _create_project(db, "only-project", group_id)
    user = _user(user_id)
    async with db() as session:
        assert await get_readable_project_ids(session, user) is None
        assert await user_can(session, user, project_id, PROJECT_READ)
        assert await user_can(session, user, project_id, PROJECT_MANAGE_ACCESS)
        assert (
            await resolution.get_active_project_group_id_for_create(session, user)
            == default_group_id
        )


async def test_group_derived_grant(db: DbSessionFactory) -> None:
    group_id = await _create_project_group(db, "fraud-group")
    await _create_mapping(db, "phoenix-proj-fraud-users", group_id, "VIEWER")
    user_id = await _create_user(db)
    project_id = await _create_project(db, "fraud-detection", group_id)
    other_group_id = await _create_project_group(db, "unrelated-group")
    other_project_id = await _create_project(db, "unrelated-project", other_group_id)
    await _set_idp_groups(db, user_id, ["phoenix-proj-fraud-users"])

    user = _user(user_id)
    async with db() as session:
        readable = await get_readable_project_ids(session, user)
        assert readable == frozenset({project_id})
        assert other_project_id not in readable
        assert await user_can(session, user, project_id, PROJECT_READ)
        assert not await user_can(session, user, project_id, PROJECT_MANAGE_ACCESS)


async def test_admin_role_grants_manage_access(db: DbSessionFactory) -> None:
    group_id = await _create_project_group(db, "fraud-group")
    await _create_mapping(db, "phoenix-proj-fraud-admins", group_id, "ADMIN")
    user_id = await _create_user(db)
    project_id = await _create_project(db, "fraud-detection", group_id)
    await _set_idp_groups(db, user_id, ["phoenix-proj-fraud-admins"])

    user = _user(user_id)
    async with db() as session:
        assert await user_can(session, user, project_id, PROJECT_READ)
        assert await user_can(session, user, project_id, PROJECT_MANAGE_ACCESS)


async def test_single_group_is_implicit_active_group(db: DbSessionFactory) -> None:
    """A user who holds exactly one group needs no explicit selection --
    it's their implicit active group even with no cookie/ContextVar set."""
    group_id = await _create_project_group(db, "only-group")
    await _create_mapping(db, "g", group_id, "VIEWER")
    user_id = await _create_user(db)
    project_id = await _create_project(db, "proj", group_id)
    await _set_idp_groups(db, user_id, ["g"])

    user = _user(user_id)
    async with db() as session:
        # No _active_group() context manager -- active_project_group_var
        # defaults to None.
        assert await get_readable_project_ids(session, user) == frozenset({project_id})


async def test_active_group_scopes_visibility(db: DbSessionFactory) -> None:
    """A user in 2+ groups sees only whichever single group is currently
    active -- not the union of both, and switching the active group
    switches which one they see."""
    group_a = await _create_project_group(db, "group-a")
    group_b = await _create_project_group(db, "group-b")
    await _create_mapping(db, "a-role", group_a, "VIEWER")
    await _create_mapping(db, "b-role", group_b, "VIEWER")
    user_id = await _create_user(db)
    project_a = await _create_project(db, "proj-a", group_a)
    project_b = await _create_project(db, "proj-b", group_b)
    await _set_idp_groups(db, user_id, ["a-role", "b-role"])

    user = _user(user_id)
    with _active_group(group_a):
        async with db() as session:
            assert await get_readable_project_ids(session, user) == frozenset({project_a})

    resolution._membership_cache.clear()
    with _active_group(group_b):
        async with db() as session:
            assert await get_readable_project_ids(session, user) == frozenset({project_b})


async def test_multi_group_no_selection_fails_closed(db: DbSessionFactory) -> None:
    """A user in 2+ groups with no active-group selection made yet sees
    nothing -- fail closed, never guess/union."""
    group_a = await _create_project_group(db, "group-a")
    group_b = await _create_project_group(db, "group-b")
    await _create_mapping(db, "a-role", group_a, "ADMIN")
    await _create_mapping(db, "b-role", group_b, "ADMIN")
    user_id = await _create_user(db)
    await _create_project(db, "proj-a", group_a)
    await _create_project(db, "proj-b", group_b)
    await _set_idp_groups(db, user_id, ["a-role", "b-role"])

    user = _user(user_id)
    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset()


async def test_selected_group_no_longer_held_fails_closed(db: DbSessionFactory) -> None:
    """A stale/tampered active-group cookie pointing at a group the caller
    no longer holds a role in resolves to no access, not an error and not a
    fallback to another held group."""
    group_a = await _create_project_group(db, "group-a")
    other_group = await _create_project_group(db, "other-group")
    await _create_mapping(db, "a-role", group_a, "VIEWER")
    user_id = await _create_user(db)
    await _create_project(db, "proj-a", group_a)
    await _set_idp_groups(db, user_id, ["a-role"])

    user = _user(user_id)
    with _active_group(other_group):
        async with db() as session:
            assert await get_readable_project_ids(session, user) == frozenset()


async def test_cache_invalidation(db: DbSessionFactory) -> None:
    group_id = await _create_project_group(db, "cache-group")
    await _create_mapping(db, "g", group_id, "VIEWER")
    user_id = await _create_user(db)
    project_id = await _create_project(db, "project-cache-test", group_id)
    user = _user(user_id)

    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset()

    await _set_idp_groups(db, user_id, ["g"])

    # Still cached (empty) until explicitly invalidated -- documents the
    # accepted TTL-cache tradeoff from the Stage 4a plan.
    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset()

    invalidate_readable_project_ids_cache(user_id)
    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset({project_id})


async def test_narrowed_mapping_revokes_without_new_login(db: DbSessionFactory) -> None:
    """Fixes the old additive-only sync bug: narrowing/removing a mapping
    row takes effect on the next cache refresh, with no new login."""
    group_id = await _create_project_group(db, "fraud-group")
    await _create_mapping(db, "g", group_id, "VIEWER")
    # An unrelated mapping row that stays put -- proves this user's own
    # access is revoked without the whole deployment's mapping table
    # emptying out (which would instead trigger the separate
    # RBAC-not-in-use "full access" fallback -- see
    # `test_no_mapping_rows_at_all_means_full_access`).
    other_group_id = await _create_project_group(db, "unrelated-group")
    await _create_mapping(db, "some-other-role", other_group_id, "VIEWER")
    user_id = await _create_user(db)
    project_id = await _create_project(db, "fraud-detection", group_id)
    await _set_idp_groups(db, user_id, ["g"])
    user = _user(user_id)

    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset({project_id})

    # Narrow the mapping (delete the row entirely) -- no re-login, just
    # cache invalidation (standing in for the TTL expiring).
    async with db() as session:
        await session.execute(
            delete(models.ExternalRoleProjectGroupMapping).where(
                models.ExternalRoleProjectGroupMapping.external_role == "g"
            )
        )
    invalidate_readable_project_ids_cache(user_id)
    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset()


async def test_new_project_in_held_group_appears_without_new_login(db: DbSessionFactory) -> None:
    """Fixes staleness: a project created after the user already holds a
    mapped group becomes visible on the next cache refresh, with no new
    login."""
    group_id = await _create_project_group(db, "fraud-group")
    await _create_mapping(db, "g", group_id, "VIEWER")
    user_id = await _create_user(db)
    await _set_idp_groups(db, user_id, ["g"])
    user = _user(user_id)

    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset()

    new_project_id = await _create_project(db, "fraud-newproject", group_id)
    invalidate_readable_project_ids_cache(user_id)
    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset({new_project_id})


def test_invalid_role_in_mapping_rejected_by_db() -> None:
    """Unlike the old YAML config (validated at parse time in application
    code), an invalid role can't even be written to the table -- the
    `valid_project_group_role` CheckConstraint rejects it at the DB level.

    Asserts against the constraint's own SQL text rather than actually
    triggering the violation at runtime: on this repo's sqlite driver combo
    (aiosqlite + sqlean), a threaded cursor's teardown re-raises a
    CHECK-constraint failure a second time during ``Cursor.close()``,
    escaping ``pytest.raises`` as an unraisable-exception warning instead of
    propagating through the normal `await session.execute(...)` call --
    unrelated to whether the constraint itself works (confirmed separately
    against real Postgres by `tests/unit/server/access/test_write_side_rls.py`
    and by `make schema-ddl`'s schema validation, which counts it among the
    migrated CHECK constraints).
    """
    role_column = models.ExternalRoleProjectGroupMapping.__table__.c["role"]
    constraint_texts = {
        c.sqltext.text
        for c in role_column.constraints
        if "valid_project_group_role" in (c.name or "")
    }
    assert constraint_texts == {"role IN ('VIEWER', 'MEMBER', 'ADMIN')"}
