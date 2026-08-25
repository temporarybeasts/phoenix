import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from phoenix.db import models
from phoenix.db.facilitator import _ensure_project_scoped_storage_migration_complete
from phoenix.server.access.schema_provisioning import (
    project_scoped_session,
    provision_project_schema,
)
from phoenix.server.types import DbSessionFactory

pytestmark = pytest.mark.postgres_only


async def _create_project(engine: AsyncEngine, name: str) -> int:
    async with engine.begin() as conn:
        project_id = await conn.scalar(
            insert(models.Project).values(name=name).returning(models.Project.id)
        )
    assert project_id is not None
    async with engine.connect() as conn:
        await provision_project_schema(conn, project_id)
        await conn.commit()
    return project_id


async def test_project_scoped_session_flag_off_behaves_like_db(
    db: DbSessionFactory,
) -> None:
    """Flag defaults to off (unset in the test environment); `project_scoped_session`
    must be indistinguishable from calling `db()` directly in that state.
    """
    async with project_scoped_session(db, project_id=1) as session:
        result = await session.scalar(select(1))
    assert result == 1


async def test_project_scoped_session_flag_on_routes_orm_writes_into_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "flag-on-orm-routing-test")
    now = datetime.now(timezone.utc)

    async with project_scoped_session(db, project_id) as session:
        trace = models.Trace(
            project_rowid=project_id, trace_id="flag-on-trace", start_time=now, end_time=now
        )
        session.add(trace)
        await session.flush()
        trace_id = trace.id
    assert trace_id is not None

    # Visible via the project's own schema...
    async with postgresql_engine.connect() as conn:
        found_scoped = await conn.scalar(
            text(f'SELECT id FROM "project_{project_id}".traces WHERE trace_id = :tid'),
            {"tid": "flag-on-trace"},
        )
    assert found_scoped == trace_id

    # ...but not via the shared schema.
    async with postgresql_engine.connect() as conn:
        found_shared = await conn.scalar(
            select(models.Trace.id).where(models.Trace.trace_id == "flag-on-trace")
        )
    assert found_shared is None


async def test_project_scoped_session_flag_on_rolls_back_on_exception(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "flag-on-rollback-test")
    now = datetime.now(timezone.utc)

    with pytest.raises(RuntimeError, match="boom"):
        async with project_scoped_session(db, project_id) as session:
            trace = models.Trace(
                project_rowid=project_id, trace_id="rolled-back", start_time=now, end_time=now
            )
            session.add(trace)
            await session.flush()
            raise RuntimeError("boom")

    async with postgresql_engine.connect() as conn:
        found = await conn.scalar(
            text(f'SELECT id FROM "project_{project_id}".traces WHERE trace_id = :tid'),
            {"tid": "rolled-back"},
        )
    assert found is None


async def test_startup_gate_blocks_boot_without_completion_marker(
    db: DbSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    with pytest.raises(RuntimeError, match="has not completed a full pass"):
        await _ensure_project_scoped_storage_migration_complete(db)


async def test_startup_gate_passes_with_completion_marker_present(
    db: DbSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db() as session:
        await session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS project_scoped_storage_migration_status (
                    id boolean PRIMARY KEY DEFAULT TRUE,
                    completed_at timestamptz NOT NULL,
                    CONSTRAINT project_scoped_storage_migration_status_singleton CHECK (id)
                )
                """
            )
        )
        await session.execute(
            text(
                "INSERT INTO project_scoped_storage_migration_status (id, completed_at) "
                "VALUES (TRUE, now())"
            )
        )
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    await _ensure_project_scoped_storage_migration_complete(db)


async def test_startup_gate_is_a_noop_when_flag_is_off(
    db: DbSessionFactory,
) -> None:
    await _ensure_project_scoped_storage_migration_complete(db)


async def test_project_scoped_session_allows_scoped_writes_and_shared_reads(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single `project_scoped_session` correctly handles a project-scoped
    write (`Trace`) alongside a *read* of a shared reference table
    (`GenerativeModel`, one of `_SHARED_REFERENCED_MODELS`) in the same
    transaction -- both are covered by the per-project role's grants
    (SELECT/INSERT/UPDATE/DELETE on its own 9 tables, SELECT-only on the 3
    shared reference tables).
    """
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "mixed-write-test")
    now = datetime.now(timezone.utc)
    async with postgresql_engine.begin() as conn:
        model_id = await conn.scalar(
            insert(models.GenerativeModel)
            .values(
                name="mixed-write-shared-model",
                provider="test",
                name_pattern=re.compile("mixed-write-shared-model"),
                is_built_in=False,
            )
            .returning(models.GenerativeModel.id)
        )
    assert model_id is not None

    async with project_scoped_session(db, project_id) as session:
        trace = models.Trace(
            project_rowid=project_id, trace_id="mixed-write-trace", start_time=now, end_time=now
        )
        session.add(trace)
        found_model_id = await session.scalar(
            select(models.GenerativeModel.id).where(models.GenerativeModel.id == model_id)
        )
        await session.flush()
        trace_id = trace.id
    assert trace_id is not None
    assert found_model_id == model_id

    async with postgresql_engine.connect() as conn:
        found_trace_scoped = await conn.scalar(
            text(f'SELECT id FROM "project_{project_id}".traces WHERE id = :tid'),
            {"tid": trace_id},
        )
    assert found_trace_scoped == trace_id


async def test_project_scoped_session_denies_writes_to_ungranted_shared_tables(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for a real bug found while wiring Stage 4b-2d's
    write paths: `project_scoped_session`'s `SET LOCAL ROLE` restricts
    *every* statement in the session to the per-project role's grants, not
    just the ones touching that project's own tables. A shared table
    outside `_SHARED_REFERENCED_MODELS` (here, `GenerativeModel`'s own
    `TokenPrice` child table has no grant either -- use the same
    `GenerativeModel` table but attempt an INSERT, which only has a
    SELECT grant) must fail closed with a permission error, not silently
    succeed. `experiment_runner.py`'s `_persist_run` originally tried to
    insert `ExperimentRun` (no grant at all) through a `project_scoped_
    session` and hit exactly this -- fixed by splitting it into two
    sessions, not by loosening the per-project role's grants.
    """
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "denied-write-test")

    with pytest.raises(Exception, match="permission denied"):
        async with project_scoped_session(db, project_id) as session:
            session.add(
                models.GenerativeModel(
                    name="denied-write-model",
                    provider="test",
                    name_pattern=re.compile("denied-write-model"),
                    is_built_in=False,
                )
            )
            await session.flush()
