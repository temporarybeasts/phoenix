"""Stage 4b-2e: regression tests for `transfer_trace_between_projects`/
`transfer_traces_between_projects` -- the copy-then-delete cross-schema
trace mover that replaces the pre-cutover single `UPDATE traces SET
project_rowid = ...`.

The central risk these tests target is id collision: unlike Stage 4b-2c's
bulk migration (which copies into an empty destination schema), a transfer
moves a trace into an *already-populated* destination whose id sequences
have advanced independently -- so every test deliberately seeds the
destination project with a full subtree first, forcing every id in the
transferred trace's own subtree to collide with an already-occupied id in
the destination. If the transfer preserved ids instead of remapping them,
these tests would fail loudly (an FK violation, or worse, a silent
`ON CONFLICT DO NOTHING` no-op immediately followed by data loss when the
source row is deleted).
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine

from phoenix.db import models
from phoenix.server.access.schema_provisioning import (
    project_scoped_session,
    provision_project_schema,
    transfer_trace_between_projects,
    transfer_traces_between_projects,
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


async def _seed_full_subtree(db: DbSessionFactory, project_id: int, suffix: str) -> dict[str, int]:
    """One row in every table `transfer_trace_between_projects` moves,
    linked exactly as a real trace's subtree would be: a trace with two
    spans (root + an OTel-parent_id-linked child), a trace annotation, a
    span annotation on the root, a document annotation on the child, and a
    span cost with one detail row.
    """
    now = datetime.now(timezone.utc)
    async with project_scoped_session(db, project_id) as session:
        trace = models.Trace(
            project_rowid=project_id, trace_id=f"trace-{suffix}", start_time=now, end_time=now
        )
        session.add(trace)
        await session.flush()

        root_span = models.Span(
            trace_rowid=trace.id,
            span_id=f"root-{suffix}",
            parent_id=None,
            name="root",
            span_kind="CHAIN",
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
        session.add(root_span)
        await session.flush()

        child_span = models.Span(
            trace_rowid=trace.id,
            span_id=f"child-{suffix}",
            parent_id=f"root-{suffix}",
            name="child",
            span_kind="LLM",
            start_time=now,
            end_time=now,
            attributes={"retrieval": {"documents": [{"document": {"content": "doc"}}]}},
            events=[],
            status_code="OK",
            status_message="",
            cumulative_error_count=0,
            cumulative_llm_token_count_prompt=0,
            cumulative_llm_token_count_completion=0,
        )
        session.add(child_span)
        await session.flush()

        span_annotation = models.SpanAnnotation(
            span_rowid=root_span.id,
            name=f"span-anno-{suffix}",
            label=None,
            score=1.0,
            explanation=None,
            metadata_={},
            annotator_kind="HUMAN",
            identifier="",
            source="API",
        )
        session.add(span_annotation)

        trace_annotation = models.TraceAnnotation(
            trace_rowid=trace.id,
            name=f"trace-anno-{suffix}",
            label=None,
            score=1.0,
            explanation=None,
            metadata_={},
            annotator_kind="HUMAN",
            identifier="",
            source="API",
        )
        session.add(trace_annotation)

        document_annotation = models.DocumentAnnotation(
            span_rowid=child_span.id,
            document_position=0,
            name=f"doc-anno-{suffix}",
            label=None,
            score=1.0,
            explanation=None,
            metadata_={},
            annotator_kind="HUMAN",
            identifier="",
            source="API",
        )
        session.add(document_annotation)

        span_cost = models.SpanCost(
            span_rowid=root_span.id,
            trace_rowid=trace.id,
            span_start_time=now,
            total_cost=1.0,
            total_tokens=10.0,
            prompt_cost=0.5,
            prompt_tokens=5.0,
            completion_cost=0.5,
            completion_tokens=5.0,
        )
        session.add(span_cost)
        await session.flush()

        span_cost_detail = models.SpanCostDetail(
            span_cost_id=span_cost.id,
            token_type="input",
            is_prompt=True,
            cost=0.5,
            tokens=5.0,
            cost_per_token=0.1,
        )
        session.add(span_cost_detail)
        await session.flush()

        return {
            "trace_id": trace.id,
            "root_span_id": root_span.id,
            "child_span_id": child_span.id,
            "span_annotation_id": span_annotation.id,
            "trace_annotation_id": trace_annotation.id,
            "document_annotation_id": document_annotation.id,
            "span_cost_id": span_cost.id,
            "span_cost_detail_id": span_cost_detail.id,
        }


async def test_transfer_remaps_colliding_ids_and_preserves_subtree(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seeding goes through project_scoped_session, which itself falls back to
    # the plain shared session unless the flag is on.
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_a = await _create_project(postgresql_engine, "transfer-src")
    project_b = await _create_project(postgresql_engine, "transfer-dest")

    # Seed the destination FIRST so every id in it is taken before the
    # source subtree (seeded next) claims the same ids in its own,
    # independent per-project sequence -- both start at 1, so this
    # guarantees every row the transfer moves collides on id with an
    # already-occupied row in the destination.
    await _seed_full_subtree(db, project_b, "dummy")
    real = await _seed_full_subtree(db, project_a, "real")

    await transfer_trace_between_projects(postgresql_engine, real["trace_id"], project_a, project_b)

    # Source: the whole subtree is gone (single DELETE FROM traces cascaded).
    async with postgresql_engine.connect() as conn:
        for table in (
            "traces",
            "spans",
            "span_annotations",
            "trace_annotations",
            "document_annotations",
            "span_costs",
            "span_cost_details",
        ):
            count = await conn.scalar(text(f'SELECT count(*) FROM "project_{project_a}".{table}'))
            assert count == 0, f"project_a.{table} still has rows after transfer"

    # Destination: the dummy row survives untouched, plus the newly-copied
    # subtree under fresh ids (never id=1, which the dummy already holds).
    async with postgresql_engine.connect() as conn:
        new_trace_id = await conn.scalar(
            text(f'SELECT id FROM "project_{project_b}".traces WHERE trace_id = :tid'),
            {"tid": "trace-real"},
        )
        assert new_trace_id is not None
        assert new_trace_id != real["trace_id"], "trace id was not remapped away from a collision"

        row = (
            await conn.execute(
                text(
                    f"SELECT project_rowid, project_session_rowid "
                    f'FROM "project_{project_b}".traces WHERE id = :id'
                ),
                {"id": new_trace_id},
            )
        ).one()
        assert row.project_rowid == project_b
        assert row.project_session_rowid is None, (
            "project_session_rowid must be nulled -- the destination schema has no "
            "row for the source project's session"
        )

        new_root_id = await conn.scalar(
            text(f'SELECT id FROM "project_{project_b}".spans WHERE span_id = :sid'),
            {"sid": "root-real"},
        )
        new_child_id = await conn.scalar(
            text(f'SELECT id FROM "project_{project_b}".spans WHERE span_id = :sid'),
            {"sid": "child-real"},
        )
        assert new_root_id is not None and new_child_id is not None
        assert new_root_id != real["root_span_id"]
        assert new_child_id != real["child_span_id"]

        root_trace_rowid, root_parent_id = (
            await conn.execute(
                text(
                    f'SELECT trace_rowid, parent_id FROM "project_{project_b}".spans WHERE id = :id'
                ),
                {"id": new_root_id},
            )
        ).one()
        assert root_trace_rowid == new_trace_id

        child_trace_rowid, child_parent_id = (
            await conn.execute(
                text(
                    f'SELECT trace_rowid, parent_id FROM "project_{project_b}".spans WHERE id = :id'
                ),
                {"id": new_child_id},
            )
        ).one()
        assert child_trace_rowid == new_trace_id
        # parent_id is the OTel string span_id, not an integer rowid -- it needs
        # no remapping, and must still point at the (also-copied) root span.
        assert child_parent_id == "root-real"

        span_annotation_span_rowid = await conn.scalar(
            text(
                f'SELECT span_rowid FROM "project_{project_b}".span_annotations WHERE name = :name'
            ),
            {"name": "span-anno-real"},
        )
        assert span_annotation_span_rowid == new_root_id

        trace_annotation_trace_rowid = await conn.scalar(
            text(
                f'SELECT trace_rowid FROM "project_{project_b}".trace_annotations '
                "WHERE name = :name"
            ),
            {"name": "trace-anno-real"},
        )
        assert trace_annotation_trace_rowid == new_trace_id

        document_annotation_span_rowid = await conn.scalar(
            text(
                f'SELECT span_rowid FROM "project_{project_b}".document_annotations '
                "WHERE name = :name"
            ),
            {"name": "doc-anno-real"},
        )
        assert document_annotation_span_rowid == new_child_id

        span_cost_row = (
            await conn.execute(
                text(
                    f"SELECT id, span_rowid, trace_rowid, total_cost "
                    f'FROM "project_{project_b}".span_costs WHERE trace_rowid = :tid'
                ),
                {"tid": new_trace_id},
            )
        ).one()
        assert span_cost_row.span_rowid == new_root_id
        assert span_cost_row.total_cost == 1.0

        # Filtered by span_cost_id, not just token_type='input' -- the dummy
        # subtree's own detail row also has token_type='input', so an
        # unscoped query would nondeterministically match either one.
        detail_token_type = await conn.scalar(
            text(
                f'SELECT token_type FROM "project_{project_b}".span_cost_details '
                "WHERE span_cost_id = :span_cost_id"
            ),
            {"span_cost_id": span_cost_row.id},
        )
        assert detail_token_type == "input"

        # The pre-existing dummy row is untouched (id=1 in every table),
        # proving the transfer didn't clobber or merge into it.
        dummy_trace_id = await conn.scalar(
            text(f'SELECT id FROM "project_{project_b}".traces WHERE trace_id = :tid'),
            {"tid": "trace-dummy"},
        )
        assert dummy_trace_id == 1


async def test_transfer_unknown_trace_raises(
    postgresql_engine: AsyncEngine,
) -> None:
    project_a = await _create_project(postgresql_engine, "transfer-missing-src")
    project_b = await _create_project(postgresql_engine, "transfer-missing-dest")
    with pytest.raises(ValueError):
        await transfer_trace_between_projects(postgresql_engine, 999_999, project_a, project_b)


async def test_batch_transfer_is_atomic(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_a = await _create_project(postgresql_engine, "transfer-batch-src")
    project_b = await _create_project(postgresql_engine, "transfer-batch-dest")
    real = await _seed_full_subtree(db, project_a, "batch-real")

    with pytest.raises(ValueError):
        await transfer_traces_between_projects(
            postgresql_engine,
            [
                (real["trace_id"], project_a, project_b),
                (999_999, project_a, project_b),  # doesn't exist -- fails the whole batch
            ],
        )

    # The first (valid) transfer in the batch must NOT have committed --
    # one transaction for the whole batch, matching the pre-cutover single
    # UPDATE statement's all-or-nothing semantics for a multi-trace request.
    async with postgresql_engine.connect() as conn:
        count_in_source_schema = await conn.scalar(
            text(f'SELECT count(*) FROM "project_{project_a}".traces WHERE id = :id'),
            {"id": real["trace_id"]},
        )
        count_in_dest_schema = await conn.scalar(
            text(f'SELECT count(*) FROM "project_{project_b}".traces')
        )
    assert count_in_source_schema == 1, "batch must roll back atomically on partial failure"
    assert count_in_dest_schema == 0
