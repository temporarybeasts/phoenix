"""Stage 4b-2e: flag-on regression test for TraceDataSweeper's per-project
fan-out. Before this stage, `_apply` issued a single
`DELETE FROM traces WHERE project_rowid IN (:many)` spanning every project
covered by a policy -- impossible once those projects' traces live in
separate schemas. This confirms the per-project loop correctly enforces
the same retention rule independently in each project's own schema.

Stage 4b-2g extends the same fan-out treatment to `_delete_orphan_sessions`,
which (unlike `_apply`) has no retention-policy cohort to key off -- every
project in the database is enumerated and swept individually.
"""

from datetime import datetime, timedelta, timezone
from secrets import token_hex
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine

from phoenix.db import models
from phoenix.db.types.trace_retention import (
    MaxCountRule,
    TraceRetentionCronExpression,
    TraceRetentionRule,
)
from phoenix.server.access.schema_provisioning import (
    project_scoped_session,
    provision_project_schema,
)
from phoenix.server.retention import TraceDataSweeper
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


async def _seed_traces(db: DbSessionFactory, project_id: int, count: int, base_name: str) -> None:
    now = datetime.now(timezone.utc)
    async with project_scoped_session(db, project_id) as session:
        for i in range(count):
            session.add(
                models.Trace(
                    project_rowid=project_id,
                    trace_id=f"{base_name}-{i}",
                    start_time=now - timedelta(seconds=count - i),
                    end_time=now - timedelta(seconds=count - i) + timedelta(milliseconds=1),
                )
            )


async def _trace_names_in_schema(engine: AsyncEngine, project_id: int) -> set[str]:
    async with engine.connect() as conn:
        names = set(await conn.scalars(text(f'SELECT trace_id FROM "project_{project_id}".traces')))
    return names


async def test_apply_fans_out_per_project(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_a = await _create_project(postgresql_engine, "retention-fanout-a")
    project_b = await _create_project(postgresql_engine, "retention-fanout-b")

    max_count = 2
    # Project A gets 5 traces (start_time ascending -- the last 2 are newest),
    # project B gets 3, both well over the max_count=2 policy -- if fan-out
    # leaked across projects (e.g. ranked together instead of per-project),
    # one project would keep too few/many relative to its own trace count.
    await _seed_traces(db, project_a, 5, "a")
    await _seed_traces(db, project_b, 3, "b")

    async with db() as session:
        project_a_obj = await session.get(models.Project, project_a)
        project_b_obj = await session.get(models.Project, project_b)
        assert project_a_obj is not None and project_b_obj is not None
        policy = models.ProjectTraceRetentionPolicy(
            name=token_hex(8),
            projects=[project_a_obj, project_b_obj],
        )
        policy.rule = TraceRetentionRule(root=MaxCountRule(max_count=max_count))
        policy.cron_expression = TraceRetentionCronExpression(root="0 * * * *")
        session.add(policy)
        await session.flush()
        await session.refresh(policy, attribute_names=["projects"])

    sweeper = TraceDataSweeper(db=db, dml_event_handler=MagicMock())
    await sweeper._apply(policy)

    remaining_a = await _trace_names_in_schema(postgresql_engine, project_a)
    remaining_b = await _trace_names_in_schema(postgresql_engine, project_b)
    assert remaining_a == {"a-3", "a-4"}, "project A should keep only its own 2 newest traces"
    assert remaining_b == {"b-1", "b-2"}, "project B should keep only its own 2 newest traces"


async def _add_project_session(
    db: DbSessionFactory,
    project_id: int,
    end_time: datetime,
    with_trace: bool,
) -> int:
    async with project_scoped_session(db, project_id) as session:
        project_session = models.ProjectSession(
            session_id=token_hex(8),
            project_id=project_id,
            start_time=end_time - timedelta(minutes=5),
            end_time=end_time,
        )
        session.add(project_session)
        await session.flush()
        if with_trace:
            session.add(
                models.Trace(
                    trace_id=token_hex(16),
                    project_rowid=project_id,
                    project_session_rowid=project_session.id,
                    start_time=end_time - timedelta(minutes=5),
                    end_time=end_time,
                )
            )
            await session.flush()
        return int(project_session.id)


async def _project_session_ids_in_schema(engine: AsyncEngine, project_id: int) -> set[int]:
    async with engine.connect() as conn:
        ids = set(
            await conn.scalars(text(f'SELECT id FROM "project_{project_id}".project_sessions'))
        )
    return ids


def _sweeper(db: DbSessionFactory) -> TraceDataSweeper:
    sweeper = TraceDataSweeper(db=db, dml_event_handler=MagicMock())
    # The batch loop checks self._running; start() is deliberately not called
    # so the sweep can be driven synchronously, mirroring
    # TestOrphanSessionSweep._sweeper in test_retention.py.
    sweeper._running = True
    return sweeper


async def test_delete_orphan_sessions_fans_out_per_project(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_a = await _create_project(postgresql_engine, "orphan-fanout-a")
    project_b = await _create_project(postgresql_engine, "orphan-fanout-b")

    now = datetime.now(timezone.utc)
    old, recent = now - timedelta(hours=2), now - timedelta(minutes=10)

    # Each project gets an old orphan (should be deleted), a recent orphan,
    # and an old session that still has a trace (both of the latter two
    # should survive) -- if the fan-out leaked across projects (e.g. ran
    # only against one project's schema, or used the wrong project's
    # connection for another project's delete), one project's old orphan
    # would survive or the other project's non-orphans would be wrongly
    # swept.
    old_orphan_a = await _add_project_session(db, project_a, old, with_trace=False)
    recent_orphan_a = await _add_project_session(db, project_a, recent, with_trace=False)
    old_with_trace_a = await _add_project_session(db, project_a, old, with_trace=True)
    old_orphan_b = await _add_project_session(db, project_b, old, with_trace=False)
    recent_orphan_b = await _add_project_session(db, project_b, recent, with_trace=False)
    old_with_trace_b = await _add_project_session(db, project_b, old, with_trace=True)

    await _sweeper(db)._delete_orphan_sessions()

    remaining_a = await _project_session_ids_in_schema(postgresql_engine, project_a)
    remaining_b = await _project_session_ids_in_schema(postgresql_engine, project_b)
    assert old_orphan_a not in remaining_a, "project A's old orphan should be deleted"
    assert {recent_orphan_a, old_with_trace_a} <= remaining_a, (
        "project A's recent orphan and session-with-trace should be kept"
    )
    assert old_orphan_b not in remaining_b, "project B's old orphan should be deleted"
    assert {recent_orphan_b, old_with_trace_b} <= remaining_b, (
        "project B's recent orphan and session-with-trace should be kept"
    )


async def test_delete_orphan_sessions_batches_within_project(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "orphan-fanout-batches")
    now = datetime.now(timezone.utc)
    num_orphans = 3
    orphan_ids = {
        await _add_project_session(db, project_id, now - timedelta(hours=2), with_trace=False)
        for _ in range(num_orphans)
    }

    with patch("phoenix.server.retention._ORPHAN_SESSION_DELETE_BATCH_SIZE", 1):
        await _sweeper(db)._delete_orphan_sessions()

    remaining = await _project_session_ids_in_schema(postgresql_engine, project_id)
    assert not (orphan_ids & remaining), (
        "all orphans in the project should be deleted across multiple batches"
    )
