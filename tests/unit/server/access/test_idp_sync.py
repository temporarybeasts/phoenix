import pytest
from sqlalchemy import insert, select

from phoenix.db import models
from phoenix.server.access.idp_sync import sync_idp_groups
from phoenix.server.access.resolution import _project_permissions_cache
from phoenix.server.types import DbSessionFactory


@pytest.fixture(autouse=True)
async def _seed_user_roles(db: DbSessionFactory) -> None:
    """See test_resolution.py::_seed_user_roles for why this is needed."""
    async with db() as session:
        existing = set(await session.scalars(select(models.UserRole.name)))
        if missing := ({"ADMIN", "MEMBER", "VIEWER", "SYSTEM"} - existing):
            await session.execute(insert(models.UserRole), [{"name": n} for n in missing])


async def _create_user(db: DbSessionFactory) -> int:
    async with db() as session:
        role_id = await session.scalar(
            select(models.UserRole.id).where(models.UserRole.name == "MEMBER")
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


async def _get_idp_groups(db: DbSessionFactory, user_id: int) -> list[str]:
    async with db() as session:
        groups = await session.scalar(
            select(models.User.idp_groups).where(models.User.id == user_id)
        )
        assert groups is not None
        return list(groups)


async def test_replace_semantics(db: DbSessionFactory) -> None:
    user_id = await _create_user(db)

    async with db() as session:
        await sync_idp_groups(session, user_id, ["group-a", "group-b"])
    assert set(await _get_idp_groups(db, user_id)) == {"group-a", "group-b"}

    # Re-sync with only group-b: group-a should be gone, wholesale replaced
    # not merged.
    async with db() as session:
        await sync_idp_groups(session, user_id, ["group-b"])
    assert await _get_idp_groups(db, user_id) == ["group-b"]


async def test_empty_groups_clears_list(db: DbSessionFactory) -> None:
    user_id = await _create_user(db)
    async with db() as session:
        await sync_idp_groups(session, user_id, ["group-a"])
    async with db() as session:
        await sync_idp_groups(session, user_id, [])
    assert await _get_idp_groups(db, user_id) == []


async def test_sync_invalidates_cache(db: DbSessionFactory) -> None:
    user_id = await _create_user(db)
    _project_permissions_cache[user_id] = {123: frozenset({"project:read"})}
    async with db() as session:
        await sync_idp_groups(session, user_id, ["group-a"])
    assert user_id not in _project_permissions_cache
