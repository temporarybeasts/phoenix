from datetime import datetime, timezone

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from phoenix.db import models
from phoenix.server.access.schema_provisioning import (
    project_scoped_session,
    provision_project_schema,
)
from phoenix.server.api.dataloaders.document_evaluations import DocumentEvaluationsDataLoader
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
        project = models.Project(name="doc-eval-flag-off-test")
        session.add(project)
        await session.flush()
        trace = models.Trace(
            project_rowid=project.id, trace_id="doc-eval-off-trace", start_time=now, end_time=now
        )
        session.add(trace)
        await session.flush()
        span = models.Span(
            trace_rowid=trace.id,
            span_id="doc-eval-off-span",
            parent_id=None,
            name="n",
            span_kind="RETRIEVER",
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
        anno = models.DocumentAnnotation(
            span_rowid=span.id,
            document_position=0,
            name="relevance",
            label="relevant",
            score=1.0,
            metadata_={},
            annotator_kind="HUMAN",
            source="APP",
        )
        session.add(anno)
        await session.flush()
        span_id, project_id = span.id, project.id

    loader = DocumentEvaluationsDataLoader(db)
    results = await loader._load_fn([(span_id, project_id)])
    assert len(results) == 1
    assert len(results[0]) == 1
    assert results[0][0].name == "relevance"


@pytest.mark.postgres_only
async def test_flag_on_reads_from_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "doc-eval-flag-on-test")
    now = datetime.now(timezone.utc)

    async with project_scoped_session(db, project_id) as session:
        trace = models.Trace(
            project_rowid=project_id, trace_id="doc-eval-on-trace", start_time=now, end_time=now
        )
        session.add(trace)
        await session.flush()
        span = models.Span(
            trace_rowid=trace.id,
            span_id="doc-eval-on-span",
            parent_id=None,
            name="n",
            span_kind="RETRIEVER",
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
        anno = models.DocumentAnnotation(
            span_rowid=span.id,
            document_position=0,
            name="scoped-relevance",
            label="relevant",
            score=1.0,
            metadata_={},
            annotator_kind="HUMAN",
            source="APP",
        )
        session.add(anno)
        await session.flush()
        span_id = span.id

    loader = DocumentEvaluationsDataLoader(db)
    results = await loader._load_fn([(span_id, project_id)])
    assert len(results) == 1
    assert len(results[0]) == 1
    assert results[0][0].name == "scoped-relevance"
