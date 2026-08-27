import pytest
import yaml
from sqlalchemy import insert, select, update

from phoenix.db import models
from phoenix.server.access import resolution
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
    resolution._project_permissions_cache.clear()


@pytest.fixture(autouse=True)
def _reset_mapping_cache() -> None:
    """`_load_group_mapping` caches its result at module-scope after first
    load -- reset it per test so `monkeypatch.setenv` in each test actually
    takes effect instead of returning a previous test's cached config."""
    resolution._mapping_cache = None


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


async def _set_idp_groups(db: DbSessionFactory, user_id: int, groups: list[str]) -> None:
    async with db() as session:
        await session.execute(
            update(models.User).where(models.User.id == user_id).values(idp_groups=groups)
        )


def _write_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "object", entries: list[dict]
) -> None:
    mapping_file = tmp_path / "mapping.yaml"  # type: ignore[operator]
    mapping_file.write_text(yaml.dump(entries))
    monkeypatch.setenv("PHOENIX_ACCESS_CONTROL_GROUP_MAPPING_FILE", str(mapping_file))
    resolution._mapping_cache = None


async def test_admin_sees_all_projects(db: DbSessionFactory) -> None:
    admin = _user(await _create_user(db, role="ADMIN"), role="ADMIN")
    async with db() as session:
        assert await get_readable_project_ids(session, admin) is None


async def test_system_user_sees_all_projects(db: DbSessionFactory) -> None:
    async with db() as session:
        assert await get_readable_project_ids(session, PhoenixSystemUser(UserId(1))) is None


async def test_no_groups_means_no_readable_projects(db: DbSessionFactory) -> None:
    user_id = await _create_user(db)
    project_id = await _create_project(db, "project-no-groups")
    user = _user(user_id)
    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset()
        assert not await user_can(session, user, project_id, PROJECT_READ)


async def test_group_derived_grant(
    db: DbSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: "object"
) -> None:
    _write_mapping(
        monkeypatch,
        tmp_path,
        [{"idp_group": "phoenix-proj-fraud-users", "projects": ["fraud-*"], "role": "viewer"}],
    )
    user_id = await _create_user(db)
    project_id = await _create_project(db, "fraud-detection")
    other_project_id = await _create_project(db, "unrelated-project")
    await _set_idp_groups(db, user_id, ["phoenix-proj-fraud-users"])

    user = _user(user_id)
    async with db() as session:
        readable = await get_readable_project_ids(session, user)
        assert readable == frozenset({project_id})
        assert other_project_id not in readable
        assert await user_can(session, user, project_id, PROJECT_READ)
        assert not await user_can(session, user, project_id, PROJECT_MANAGE_ACCESS)


async def test_admin_role_grants_manage_access(
    db: DbSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: "object"
) -> None:
    _write_mapping(
        monkeypatch,
        tmp_path,
        [{"idp_group": "phoenix-proj-fraud-admins", "projects": ["fraud-*"], "role": "admin"}],
    )
    user_id = await _create_user(db)
    project_id = await _create_project(db, "fraud-detection")
    await _set_idp_groups(db, user_id, ["phoenix-proj-fraud-admins"])

    user = _user(user_id)
    async with db() as session:
        assert await user_can(session, user, project_id, PROJECT_READ)
        assert await user_can(session, user, project_id, PROJECT_MANAGE_ACCESS)


async def test_editor_role_matches_viewer_permissions(
    db: DbSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: "object"
) -> None:
    """Documented gap: no write-distinct permission exists yet, so editor
    grants the same permission set as viewer."""
    _write_mapping(
        monkeypatch,
        tmp_path,
        [{"idp_group": "editors", "projects": ["proj"], "role": "editor"}],
    )
    user_id = await _create_user(db)
    project_id = await _create_project(db, "proj")
    await _set_idp_groups(db, user_id, ["editors"])

    user = _user(user_id)
    async with db() as session:
        assert await user_can(session, user, project_id, PROJECT_READ)
        assert not await user_can(session, user, project_id, PROJECT_MANAGE_ACCESS)


async def test_multiple_groups_union_permissions(
    db: DbSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: "object"
) -> None:
    """A project reachable via two groups with different roles gets the
    union of both roles' permissions."""
    _write_mapping(
        monkeypatch,
        tmp_path,
        [
            {"idp_group": "viewers", "projects": ["proj"], "role": "viewer"},
            {"idp_group": "managers", "projects": ["proj"], "role": "admin"},
        ],
    )
    user_id = await _create_user(db)
    project_id = await _create_project(db, "proj")
    await _set_idp_groups(db, user_id, ["viewers", "managers"])

    user = _user(user_id)
    async with db() as session:
        assert await user_can(session, user, project_id, PROJECT_READ)
        assert await user_can(session, user, project_id, PROJECT_MANAGE_ACCESS)


async def test_cache_invalidation(
    db: DbSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: "object"
) -> None:
    _write_mapping(
        monkeypatch,
        tmp_path,
        [{"idp_group": "g", "projects": ["project-cache-test"], "role": "viewer"}],
    )
    user_id = await _create_user(db)
    project_id = await _create_project(db, "project-cache-test")
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


async def test_narrowed_mapping_revokes_without_new_login(
    db: DbSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: "object"
) -> None:
    """Fixes the old additive-only sync bug: narrowing/removing a mapping
    entry takes effect on the next cache refresh, with no new login."""
    _write_mapping(
        monkeypatch,
        tmp_path,
        [{"idp_group": "g", "projects": ["fraud-*"], "role": "viewer"}],
    )
    user_id = await _create_user(db)
    project_id = await _create_project(db, "fraud-detection")
    await _set_idp_groups(db, user_id, ["g"])
    user = _user(user_id)

    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset({project_id})

    # Narrow the mapping (remove the entry entirely) -- no re-login, just
    # cache invalidation (standing in for the TTL expiring).
    _write_mapping(monkeypatch, tmp_path, [])
    invalidate_readable_project_ids_cache(user_id)
    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset()


async def test_new_project_matching_held_group_glob_appears_without_new_login(
    db: DbSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: "object"
) -> None:
    """Fixes glob staleness: a project created after the user already holds
    a matching group becomes visible on the next cache refresh, with no new
    login."""
    _write_mapping(
        monkeypatch,
        tmp_path,
        [{"idp_group": "g", "projects": ["fraud-*"], "role": "viewer"}],
    )
    user_id = await _create_user(db)
    await _set_idp_groups(db, user_id, ["g"])
    user = _user(user_id)

    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset()

    new_project_id = await _create_project(db, "fraud-newproject")
    invalidate_readable_project_ids_cache(user_id)
    async with db() as session:
        assert await get_readable_project_ids(session, user) == frozenset({new_project_id})


async def test_invalid_role_in_mapping_raises_clear_error(
    db: DbSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: "object"
) -> None:
    _write_mapping(
        monkeypatch,
        tmp_path,
        [{"idp_group": "g", "projects": ["proj"], "role": "not-a-real-role"}],
    )
    user_id = await _create_user(db)
    await _create_project(db, "proj")
    await _set_idp_groups(db, user_id, ["g"])
    user = _user(user_id)

    with pytest.raises(ValueError, match="not-a-real-role"):
        async with db() as session:
            await get_readable_project_ids(session, user)
