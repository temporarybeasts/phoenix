from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from phoenix.db import models
from phoenix.server.access.schema_provisioning import (
    provision_project_schema,
    schema_scoped_connection,
)

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


async def test_span_trace_project_join_through_schema_scoped_connection(
    postgresql_engine: AsyncEngine,
) -> None:
    """Regression test for the schema_translate_map bug found while scoping
    Stage 4b-2: before the schema-token fix (Stage 4b-2a), `schema_scoped_connection`
    keyed its translate map directly on `get_env_database_schema()`'s value,
    which every table shared -- including `projects`, never cloned into any
    project's schema. A query joining a project-scoped table to `projects`
    in one statement would try to resolve `projects` inside the project's
    own schema and fail. This confirms it now succeeds.
    """
    async with postgresql_engine.begin() as conn:
        project_id = await conn.scalar(
            insert(models.Project).values(name="join-test").returning(models.Project.id)
        )
    assert project_id is not None
    async with postgresql_engine.connect() as conn:
        await provision_project_schema(conn, project_id)
        await conn.commit()

    now = datetime.now(timezone.utc)
    async with schema_scoped_connection(postgresql_engine, project_id) as conn:
        trace_id = await conn.scalar(
            insert(models.Trace)
            .values(project_rowid=project_id, trace_id="abc123", start_time=now, end_time=now)
            .returning(models.Trace.id)
        )
        assert trace_id is not None
        span_id = await conn.scalar(
            insert(models.Span)
            .values(
                trace_rowid=trace_id,
                span_id="span1",
                parent_id=None,
                name="n",
                span_kind="LLM",
                start_time=now,
                end_time=now,
                attributes={},
                events=[],
                status_code="OK",
                status_message="",
                cumulative_error_count=0,
                cumulative_llm_token_count_prompt=0,
                cumulative_llm_token_count_completion=0,
            )
            .returning(models.Span.id)
        )
        assert span_id is not None
        await conn.commit()

    async with schema_scoped_connection(postgresql_engine, project_id) as conn:
        # The bug scenario: a single statement joining a project-scoped
        # table (Span, via Trace) to the shared `projects` table.
        row = (
            await conn.execute(
                select(models.Span.span_id, models.Trace.trace_id, models.Project.name)
                .select_from(models.Span)
                .join(models.Trace, models.Span.trace_rowid == models.Trace.id)
                .join(models.Project, models.Trace.project_rowid == models.Project.id)
                .where(models.Span.id == span_id)
            )
        ).one()
    assert row.span_id == "span1"
    assert row.trace_id == "abc123"
    assert row.name == "join-test"


async def test_unscoped_queries_still_hit_the_shared_schema(
    postgresql_engine: AsyncEngine,
) -> None:
    """A connection with no `schema_scoped_connection` routing (the normal
    case for every query until Stage 4b-2d's cutover) must keep hitting
    exactly where it hit before Stage 4b-2a's schema-token change -- the
    engine-default `schema_translate_map` added in `db/engines.py` is what
    makes that true; this exercises it directly against a real Postgres
    connection built the same way `aio_postgresql_engine` builds one.
    """
    project_id = await _create_project(postgresql_engine, "unscoped-test")
    now = datetime.now(timezone.utc)
    async with postgresql_engine.begin() as conn:
        trace_id = await conn.scalar(
            insert(models.Trace)
            .values(
                project_rowid=project_id, trace_id="unscoped-trace", start_time=now, end_time=now
            )
            .returning(models.Trace.id)
        )
    async with postgresql_engine.connect() as conn:
        # No schema qualifier in the query itself -- if the model-level
        # token leaked into physical DDL/DML without the engine-default
        # translate map applied, this would 42P01 ("relation does not
        # exist") instead of finding the row in the real shared schema.
        found = await conn.scalar(select(models.Trace.trace_id).where(models.Trace.id == trace_id))
    assert found == "unscoped-trace"


async def test_cross_project_role_still_denied_after_token_fix(
    postgresql_engine: AsyncEngine,
) -> None:
    """The schema-token fix (4b-2a) only changes *which* tables get
    redirected by `schema_translate_map` -- it must not weaken the
    per-project role's actual GRANTs. A role scoped to project A must
    still be refused touching project B's schema.
    """
    project_a = await _create_project(postgresql_engine, "role-test-a")
    project_b = await _create_project(postgresql_engine, "role-test-b")
    async with schema_scoped_connection(postgresql_engine, project_a) as conn:
        with pytest.raises(Exception, match="permission denied"):
            await conn.execute(text(f'SELECT * FROM "project_{project_b}".traces'))
