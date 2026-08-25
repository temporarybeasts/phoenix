from datetime import datetime, timezone

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from phoenix.db import models
from phoenix.server.access.schema_provisioning import (
    project_scoped_session,
    provision_project_schema,
)
from phoenix.server.api.dataloaders.table_fields import ProjectScopedTableFieldsDataLoader
from phoenix.server.types import DbSessionFactory


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


async def test_flag_off_reads_from_shared_schema(db: DbSessionFactory) -> None:
    now = datetime.now(timezone.utc)
    async with db() as session:
        project = models.Project(name="table-fields-flag-off-test")
        session.add(project)
        await session.flush()
        trace = models.Trace(
            project_rowid=project.id, trace_id="tf-off-trace", start_time=now, end_time=now
        )
        session.add(trace)
        await session.flush()
        span = models.Span(
            trace_rowid=trace.id,
            span_id="tf-off-span",
            parent_id=None,
            name="my-span-name",
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
        session.add(span)
        await session.flush()
        span_id, project_id = span.id, project.id

    loader = ProjectScopedTableFieldsDataLoader(db, models.Span)
    results = await loader._load_fn([(span_id, models.Span.name, project_id)])
    assert results == ["my-span-name"]


@pytest.mark.postgres_only
async def test_flag_on_reads_from_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "table-fields-flag-on-test")
    now = datetime.now(timezone.utc)

    async with project_scoped_session(db, project_id) as session:
        trace = models.Trace(
            project_rowid=project_id, trace_id="tf-on-trace", start_time=now, end_time=now
        )
        session.add(trace)
        await session.flush()
        span = models.Span(
            trace_rowid=trace.id,
            span_id="tf-on-span",
            parent_id=None,
            name="scoped-span-name",
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
        session.add(span)
        await session.flush()
        span_id = span.id

    loader = ProjectScopedTableFieldsDataLoader(db, models.Span)
    results = await loader._load_fn([(span_id, models.Span.name, project_id)])
    assert results == ["scoped-span-name"]


@pytest.mark.postgres_only
async def test_flag_on_isolates_multiple_projects_in_one_batch(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_a = await _create_project(postgresql_engine, "table-fields-multi-a")
    project_b = await _create_project(postgresql_engine, "table-fields-multi-b")
    now = datetime.now(timezone.utc)

    async def _add_span(project_id: int, span_id_str: str, name: str) -> int:
        async with project_scoped_session(db, project_id) as session:
            trace = models.Trace(
                project_rowid=project_id,
                trace_id=f"trace-{span_id_str}",
                start_time=now,
                end_time=now,
            )
            session.add(trace)
            await session.flush()
            span = models.Span(
                trace_rowid=trace.id,
                span_id=span_id_str,
                parent_id=None,
                name=name,
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
            session.add(span)
            await session.flush()
            return span.id

    span_a_id = await _add_span(project_a, "multi-span-a", "name-a")
    span_b_id = await _add_span(project_b, "multi-span-b", "name-b")

    loader = ProjectScopedTableFieldsDataLoader(db, models.Span)
    results = await loader._load_fn(
        [
            (span_a_id, models.Span.name, project_a),
            (span_b_id, models.Span.name, project_b),
        ]
    )
    assert results == ["name-a", "name-b"]
