"""Regression coverage for a real bug found while wiring Stage 4b-2d's read
path: `project_scoped_read_connection`'s flag-on branch originally yielded
a bare `AsyncConnection`, which does not hydrate ORM instances for a
whole-entity `select(SomeORMModel)` -- only dataloaders selecting individual
columns happened to work. Fixed by binding a real `AsyncSession` to the
connection instead (see `schema_provisioning.py`). This file exercises a
representative sample of the *other* dataloaders that do whole-entity
selects (beyond `document_evaluations`, covered in its own test file) to
confirm the fix generalizes rather than trusting a single case.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from phoenix.db import models
from phoenix.server.access.schema_provisioning import (
    project_scoped_session,
    provision_project_schema,
)
from phoenix.server.api.dataloaders.span_annotations import SpanAnnotationsDataLoader
from phoenix.server.api.dataloaders.span_by_id import SpanByIdDataLoader
from phoenix.server.api.dataloaders.span_cost_by_span import SpanCostBySpanDataLoader
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


async def test_span_by_id_hydrates_orm_instance_under_flag_on(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "orm-hydration-span-by-id-test")
    now = datetime.now(timezone.utc)
    async with project_scoped_session(db, project_id) as session:
        trace = models.Trace(
            project_rowid=project_id, trace_id="hydration-trace", start_time=now, end_time=now
        )
        session.add(trace)
        await session.flush()
        span = models.Span(
            trace_rowid=trace.id,
            span_id="hydration-span",
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
        session.add(span)
        await session.flush()
        span_id = span.id

    loader = SpanByIdDataLoader(db)
    results = await loader._load_fn([(span_id, project_id)])
    assert len(results) == 1
    result = results[0]
    assert isinstance(result, models.Span)
    assert result.span_id == "hydration-span"


async def test_span_annotations_hydrates_orm_instances_under_flag_on(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "orm-hydration-span-annotations-test")
    now = datetime.now(timezone.utc)
    async with project_scoped_session(db, project_id) as session:
        trace = models.Trace(
            project_rowid=project_id, trace_id="hydration-anno-trace", start_time=now, end_time=now
        )
        session.add(trace)
        await session.flush()
        span = models.Span(
            trace_rowid=trace.id,
            span_id="hydration-anno-span",
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
        session.add(span)
        await session.flush()
        anno = models.SpanAnnotation(
            span_rowid=span.id,
            name="hydration-check",
            metadata_={},
            annotator_kind="HUMAN",
            source="APP",
        )
        session.add(anno)
        await session.flush()
        span_id = span.id

    loader = SpanAnnotationsDataLoader(db)
    results = await loader._load_fn([(span_id, project_id)])
    assert len(results) == 1
    assert len(results[0]) == 1
    result = results[0][0]
    assert isinstance(result, models.SpanAnnotation)
    assert result.name == "hydration-check"


async def test_span_cost_by_span_hydrates_orm_instance_under_flag_on(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "orm-hydration-span-cost-test")
    now = datetime.now(timezone.utc)
    async with project_scoped_session(db, project_id) as session:
        trace = models.Trace(
            project_rowid=project_id, trace_id="hydration-cost-trace", start_time=now, end_time=now
        )
        session.add(trace)
        await session.flush()
        span = models.Span(
            trace_rowid=trace.id,
            span_id="hydration-cost-span",
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
        session.add(span)
        await session.flush()
        span_cost = models.SpanCost(
            span_rowid=span.id, trace_rowid=trace.id, span_start_time=now, total_cost=1.5
        )
        session.add(span_cost)
        await session.flush()
        span_id = span.id

    loader = SpanCostBySpanDataLoader(db)
    results = await loader._load_fn([(span_id, project_id)])
    assert len(results) == 1
    result = results[0]
    assert isinstance(result, models.SpanCost)
    assert result.total_cost == 1.5
