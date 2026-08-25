from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from phoenix.db import models
from phoenix.db.migrations.data_migration_scripts.migrate_to_project_scoped_schemas import (
    migrate_to_project_scoped_schemas,
)
from phoenix.server.access.schema_provisioning import (
    _project_schema_name,
    schema_scoped_connection,
)

pytestmark = pytest.mark.postgres_only


async def _seed_project(engine: AsyncEngine, name: str) -> dict[str, Any]:
    """Inserts one project plus one row in each of the 9 project-scoped
    tables, all still in the shared schema -- exactly the pre-cutover state
    this migration script is meant to run against. No provisioning here:
    the script itself is responsible for provisioning as its first step.
    """
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        project_id = await conn.scalar(
            insert(models.Project).values(name=name).returning(models.Project.id)
        )
        assert project_id is not None
        session_id = await conn.scalar(
            insert(models.ProjectSession)
            .values(session_id=f"sess-{name}", project_id=project_id, start_time=now, end_time=now)
            .returning(models.ProjectSession.id)
        )
        trace_id = await conn.scalar(
            insert(models.Trace)
            .values(
                project_rowid=project_id,
                trace_id=f"trace-{name}",
                project_session_rowid=session_id,
                start_time=now,
                end_time=now,
            )
            .returning(models.Trace.id)
        )
        span_id = await conn.scalar(
            insert(models.Span)
            .values(
                trace_rowid=trace_id,
                span_id=f"span-{name}",
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
        span_annotation_id = await conn.scalar(
            insert(models.SpanAnnotation)
            .values(
                span_rowid=span_id, name="ann", metadata_={}, annotator_kind="HUMAN", source="APP"
            )
            .returning(models.SpanAnnotation.id)
        )
        trace_annotation_id = await conn.scalar(
            insert(models.TraceAnnotation)
            .values(
                trace_rowid=trace_id, name="ann", metadata_={}, annotator_kind="HUMAN", source="APP"
            )
            .returning(models.TraceAnnotation.id)
        )
        project_session_annotation_id = await conn.scalar(
            insert(models.ProjectSessionAnnotation)
            .values(
                project_session_id=session_id,
                name="ann",
                metadata_={},
                annotator_kind="HUMAN",
                source="APP",
            )
            .returning(models.ProjectSessionAnnotation.id)
        )
        document_annotation_id = await conn.scalar(
            insert(models.DocumentAnnotation)
            .values(
                span_rowid=span_id,
                document_position=0,
                name="ann",
                metadata_={},
                annotator_kind="HUMAN",
                source="APP",
            )
            .returning(models.DocumentAnnotation.id)
        )
        span_cost_id = await conn.scalar(
            insert(models.SpanCost)
            .values(span_rowid=span_id, trace_rowid=trace_id, span_start_time=now)
            .returning(models.SpanCost.id)
        )
        span_cost_detail_id = await conn.scalar(
            insert(models.SpanCostDetail)
            .values(span_cost_id=span_cost_id, token_type="input", is_prompt=True)
            .returning(models.SpanCostDetail.id)
        )
    return {
        "project_id": project_id,
        "project_sessions": session_id,
        "traces": trace_id,
        "spans": span_id,
        "span_annotations": span_annotation_id,
        "trace_annotations": trace_annotation_id,
        "project_session_annotations": project_session_annotation_id,
        "document_annotations": document_annotation_id,
        "span_costs": span_cost_id,
        "span_cost_details": span_cost_detail_id,
    }


async def test_migrate_copies_all_nine_tables_preserving_ids(
    postgresql_engine: AsyncEngine,
) -> None:
    seeded = await _seed_project(postgresql_engine, "copy-test")
    await migrate_to_project_scoped_schemas(postgresql_engine)

    project_id = seeded["project_id"]
    async with schema_scoped_connection(postgresql_engine, project_id) as conn:
        assert (
            await conn.scalar(
                select(models.ProjectSession.id).where(
                    models.ProjectSession.id == seeded["project_sessions"]
                )
            )
            == seeded["project_sessions"]
        )
        assert (
            await conn.scalar(select(models.Trace.id).where(models.Trace.id == seeded["traces"]))
            == seeded["traces"]
        )
        assert (
            await conn.scalar(select(models.Span.id).where(models.Span.id == seeded["spans"]))
            == seeded["spans"]
        )
        assert (
            await conn.scalar(
                select(models.SpanAnnotation.id).where(
                    models.SpanAnnotation.id == seeded["span_annotations"]
                )
            )
            == seeded["span_annotations"]
        )
        assert (
            await conn.scalar(
                select(models.TraceAnnotation.id).where(
                    models.TraceAnnotation.id == seeded["trace_annotations"]
                )
            )
            == seeded["trace_annotations"]
        )
        assert (
            await conn.scalar(
                select(models.ProjectSessionAnnotation.id).where(
                    models.ProjectSessionAnnotation.id == seeded["project_session_annotations"]
                )
            )
            == seeded["project_session_annotations"]
        )
        assert (
            await conn.scalar(
                select(models.DocumentAnnotation.id).where(
                    models.DocumentAnnotation.id == seeded["document_annotations"]
                )
            )
            == seeded["document_annotations"]
        )
        assert (
            await conn.scalar(
                select(models.SpanCost.id).where(models.SpanCost.id == seeded["span_costs"])
            )
            == seeded["span_costs"]
        )
        assert (
            await conn.scalar(
                select(models.SpanCostDetail.id).where(
                    models.SpanCostDetail.id == seeded["span_cost_details"]
                )
            )
            == seeded["span_cost_details"]
        )


async def test_migrate_only_copies_the_owning_projects_rows(
    postgresql_engine: AsyncEngine,
) -> None:
    """Regression coverage for the hand-written per-table filter subqueries
    -- each one must scope strictly to its own project, not leak another
    project's rows via a too-loose join.
    """
    project_a = await _seed_project(postgresql_engine, "filter-test-a")
    project_b = await _seed_project(postgresql_engine, "filter-test-b")
    await migrate_to_project_scoped_schemas(postgresql_engine)

    async with schema_scoped_connection(postgresql_engine, project_a["project_id"]) as conn:
        count = await conn.scalar(select(models.Span.id))
        assert count == project_a["spans"]
        assert (
            await conn.scalar(select(models.Span.id).where(models.Span.id == project_b["spans"]))
        ) is None

    async with schema_scoped_connection(postgresql_engine, project_b["project_id"]) as conn:
        assert (
            await conn.scalar(select(models.Span.id).where(models.Span.id == project_a["spans"]))
        ) is None


async def test_migrate_advances_sequences_so_new_rows_do_not_collide(
    postgresql_engine: AsyncEngine,
) -> None:
    seeded = await _seed_project(postgresql_engine, "sequence-test")
    await migrate_to_project_scoped_schemas(postgresql_engine)

    project_id = seeded["project_id"]
    now = datetime.now(timezone.utc)
    async with schema_scoped_connection(postgresql_engine, project_id) as conn:
        new_trace_id = await conn.scalar(
            insert(models.Trace)
            .values(
                project_rowid=project_id, trace_id="post-migration", start_time=now, end_time=now
            )
            .returning(models.Trace.id)
        )
        await conn.commit()
    assert new_trace_id is not None
    assert new_trace_id > seeded["traces"]


async def test_migrate_is_idempotent(postgresql_engine: AsyncEngine) -> None:
    seeded = await _seed_project(postgresql_engine, "idempotent-test")
    await migrate_to_project_scoped_schemas(postgresql_engine)
    await migrate_to_project_scoped_schemas(postgresql_engine)

    async with schema_scoped_connection(postgresql_engine, seeded["project_id"]) as conn:
        span_count = await conn.scalar(
            select(models.Span.id).where(models.Span.id == seeded["spans"])
        )
        trace_count = (await conn.execute(select(models.Trace.id))).all()
    assert span_count == seeded["spans"]
    assert len(trace_count) == 1


async def test_migrate_does_not_modify_shared_schema_rows(
    postgresql_engine: AsyncEngine,
) -> None:
    seeded = await _seed_project(postgresql_engine, "copy-only-test")
    await migrate_to_project_scoped_schemas(postgresql_engine)

    async with postgresql_engine.connect() as conn:
        trace_id = await conn.scalar(
            select(models.Trace.id).where(models.Trace.id == seeded["traces"])
        )
    assert trace_id == seeded["traces"]


async def test_migrate_writes_completion_marker(postgresql_engine: AsyncEngine) -> None:
    await _seed_project(postgresql_engine, "marker-test")
    await migrate_to_project_scoped_schemas(postgresql_engine)

    async with postgresql_engine.connect() as conn:
        completed_at = await conn.scalar(
            text("SELECT completed_at FROM project_scoped_storage_migration_status WHERE id = TRUE")
        )
    assert completed_at is not None


async def test_migrate_provisions_schema_for_unprovisioned_project(
    postgresql_engine: AsyncEngine,
) -> None:
    """The script must not assume a project is already provisioned -- it
    has to work for the common real case of historical rows belonging to a
    project whose schema was never touched (e.g. everything that existed
    before Stage 4b-2b's provisioning fixes landed).
    """
    seeded = await _seed_project(postgresql_engine, "unprovisioned-test")
    schema_name = _project_schema_name(seeded["project_id"])
    async with postgresql_engine.connect() as conn:
        exists_before = await conn.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :name)"),
            {"name": schema_name},
        )
    assert exists_before is False

    await migrate_to_project_scoped_schemas(postgresql_engine)

    async with postgresql_engine.connect() as conn:
        exists_after = await conn.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :name)"),
            {"name": schema_name},
        )
    assert exists_after is True
