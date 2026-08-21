import pytest
from sqlalchemy import insert, select

from phoenix.db import models
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
def _clear_readable_project_ids_cache() -> None:
    """The resolution cache is a process-global TTLCache keyed by user_id.
    Each test's DB transaction rolls back and reuses the same small
    auto-incrementing ids (1, 2, ...), so without clearing this between
    tests, one test's cached result for "user_id=1" leaks into the next
    test's completely different "user_id=1"."""
    from phoenix.server.access import resolution

    resolution._readable_project_ids_cache.clear()


def _user(user_id: int, role: str = "MEMBER") -> PhoenixUser:
    return PhoenixUser(
        UserId(user_id),
        UserClaimSet(
            subject=UserId(user_id),
            token_id=AccessTokenId(1),
            attributes=UserTokenAttributes(user_role=role),  # type: ignore[arg-type]
        ),
    )


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


async def _create_project(db: DbSessionFactory, name: str) -> int:
    async with db() as session:
        project = models.Project(name=name)
        session.add(project)
        await session.flush()
        return project.id


async def _create_idp_group(db: DbSessionFactory, name: str) -> int:
    async with db() as session:
        group = models.IdpGroup(name=name)
        session.add(group)
        await session.flush()
        return group.id


async def test_admin_sees_all_projects(db: DbSessionFactory) -> None:
    admin = _user(await _create_user(db, role="ADMIN"), role="ADMIN")
    async with db() as session:
        assert await get_readable_project_ids(session, admin) is None


async def test_system_user_sees_all_projects(db: DbSessionFactory) -> None:
    async with db() as session:
        assert await get_readable_project_ids(session, PhoenixSystemUser(UserId(1))) is None


async def test_no_grants_means_no_readable_projects(db: DbSessionFactory) -> None:
    user_id = await _create_user(db)
    project_id = await _create_project(db, "project-no-grant")
    user = _user(user_id)
    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset()
        assert not await user_can(session, user, project_id, PROJECT_READ)


async def test_direct_grant(db: DbSessionFactory) -> None:
    user_id = await _create_user(db)
    project_id = await _create_project(db, "project-direct-grant")
    other_project_id = await _create_project(db, "project-other")
    async with db() as session:
        session.add(
            models.ProjectGrant(
                project_id=project_id,
                user_id=user_id,
                permission=PROJECT_READ,
                source="manual",
            )
        )
        await session.flush()

    user = _user(user_id)
    async with db() as session:
        readable = await get_readable_project_ids(session, user)
        assert readable == frozenset({project_id})
        assert other_project_id not in readable
        assert await user_can(session, user, project_id, PROJECT_READ)
        assert not await user_can(session, user, project_id, PROJECT_MANAGE_ACCESS)


async def test_group_derived_grant(db: DbSessionFactory) -> None:
    user_id = await _create_user(db)
    project_id = await _create_project(db, "project-group-grant")
    idp_group_id = await _create_idp_group(db, "phoenix-proj-fraud-users")
    async with db() as session:
        session.add(models.UserIdpGroupMembership(user_id=user_id, idp_group_id=idp_group_id))
        session.add(
            models.ProjectGrant(
                project_id=project_id,
                idp_group_id=idp_group_id,
                permission=PROJECT_READ,
                source="config",
            )
        )
        await session.flush()

    user = _user(user_id)
    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset({project_id})


async def test_cache_invalidation(db: DbSessionFactory) -> None:
    user_id = await _create_user(db)
    project_id = await _create_project(db, "project-cache-test")
    user = _user(user_id)

    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset()

    async with db() as session:
        session.add(
            models.ProjectGrant(
                project_id=project_id, user_id=user_id, permission=PROJECT_READ, source="manual"
            )
        )
        await session.flush()

    # Still cached (empty) until explicitly invalidated -- documents the
    # accepted TTL-cache tradeoff from the Stage 4a plan.
    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset()

    invalidate_readable_project_ids_cache(user_id)
    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset({project_id})
