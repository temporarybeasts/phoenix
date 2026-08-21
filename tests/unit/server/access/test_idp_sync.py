import pytest
from sqlalchemy import func, insert, select

from phoenix.db import models
from phoenix.server.access import idp_sync
from phoenix.server.access.idp_sync import (
    sync_config_driven_project_grants,
    sync_idp_group_memberships,
)
from phoenix.server.types import DbSessionFactory


@pytest.fixture(autouse=True)
async def _seed_user_roles(db: DbSessionFactory) -> None:
    """See test_resolution.py::_seed_user_roles for why this is needed."""
    async with db() as session:
        existing = set(await session.scalars(select(models.UserRole.name)))
        if missing := ({"ADMIN", "MEMBER", "VIEWER", "SYSTEM"} - existing):
            await session.execute(insert(models.UserRole), [{"name": n} for n in missing])


@pytest.fixture(autouse=True)
def _reset_mapping_cache() -> None:
    """`_load_group_mapping` caches its result at module-scope after first
    load -- reset it per test so `monkeypatch.setenv` in each test actually
    takes effect instead of returning a previous test's cached config."""
    idp_sync._mapping_cache = None


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


async def _create_project(db: DbSessionFactory, name: str) -> int:
    async with db() as session:
        project = models.Project(name=name)
        session.add(project)
        await session.flush()
        return project.id


async def test_group_membership_replace_semantics(db: DbSessionFactory) -> None:
    user_id = await _create_user(db)

    async with db() as session:
        ids_by_name = await sync_idp_group_memberships(session, user_id, ["group-a", "group-b"])
    assert set(ids_by_name) == {"group-a", "group-b"}

    async with db() as session:
        memberships = set(
            await session.scalars(
                select(models.UserIdpGroupMembership.idp_group_id).where(
                    models.UserIdpGroupMembership.user_id == user_id
                )
            )
        )
    assert memberships == set(ids_by_name.values())

    # Re-sync with only group-b: group-a's membership row should be gone,
    # group-b's should remain (not recreated with a new id).
    async with db() as session:
        ids_by_name_2 = await sync_idp_group_memberships(session, user_id, ["group-b"])
    assert ids_by_name_2 == {"group-b": ids_by_name["group-b"]}

    async with db() as session:
        memberships = set(
            await session.scalars(
                select(models.UserIdpGroupMembership.idp_group_id).where(
                    models.UserIdpGroupMembership.user_id == user_id
                )
            )
        )
    assert memberships == {ids_by_name["group-b"]}


async def test_group_membership_idempotent_reinsert(db: DbSessionFactory) -> None:
    """Re-syncing with the exact same groups shouldn't error (ON CONFLICT
    DO NOTHING on the already-present membership row)."""
    user_id = await _create_user(db)
    async with db() as session:
        await sync_idp_group_memberships(session, user_id, ["group-a"])
    async with db() as session:
        ids_by_name = await sync_idp_group_memberships(session, user_id, ["group-a"])
    assert set(ids_by_name) == {"group-a"}


async def test_config_driven_grants_glob_match(
    db: DbSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: "object"
) -> None:
    import yaml

    mapping_file = tmp_path / "mapping.yaml"  # type: ignore[operator]
    mapping_file.write_text(
        yaml.dump(
            [
                {
                    "idp_group": "phoenix-proj-fraud-admins",
                    "projects": ["fraud-*"],
                    "permission": "project:manage-access",
                },
                {
                    "idp_group": "phoenix-proj-fraud-admins",
                    "projects": ["unrelated-project"],
                    "permission": "project:read",
                },
            ]
        )
    )
    monkeypatch.setenv("PHOENIX_ACCESS_CONTROL_GROUP_MAPPING_FILE", str(mapping_file))
    idp_sync._mapping_cache = None

    fraud_project_id = await _create_project(db, "fraud-detection")
    await _create_project(db, "other-project")  # should NOT match "fraud-*"

    user_id = await _create_user(db)
    async with db() as session:
        ids_by_name = await sync_idp_group_memberships(
            session, user_id, ["phoenix-proj-fraud-admins"]
        )
        await sync_config_driven_project_grants(session, ids_by_name)

    async with db() as session:
        grants = (
            await session.execute(
                select(
                    models.ProjectGrant.project_id,
                    models.ProjectGrant.permission,
                    models.ProjectGrant.source,
                )
            )
        ).all()

    assert set(grants) == {
        (fraud_project_id, "project:manage-access", "config"),
    }


async def test_config_driven_grants_upsert_is_idempotent(
    db: DbSessionFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: "object"
) -> None:
    import yaml

    mapping_file = tmp_path / "mapping.yaml"  # type: ignore[operator]
    mapping_file.write_text(
        yaml.dump([{"idp_group": "g", "projects": ["proj"], "permission": "project:read"}])
    )
    monkeypatch.setenv("PHOENIX_ACCESS_CONTROL_GROUP_MAPPING_FILE", str(mapping_file))
    idp_sync._mapping_cache = None

    await _create_project(db, "proj")
    user_id = await _create_user(db)
    async with db() as session:
        ids_by_name = await sync_idp_group_memberships(session, user_id, ["g"])
        await sync_config_driven_project_grants(session, ids_by_name)
        # Second call (e.g. a second login) shouldn't error or duplicate.
        await sync_config_driven_project_grants(session, ids_by_name)

    async with db() as session:
        count = await session.scalar(select(func.count()).select_from(models.ProjectGrant))
    assert count == 1
