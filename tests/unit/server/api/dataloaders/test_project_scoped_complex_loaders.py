"""Stage 4b-2d: flag-on regression tests for the trickier dataloader
retrofits -- ones that originally batched a cross-project SQL construct
(a Postgres VALUES-join percentile query, a recursive CTE) and had to be
decomposed into a per-project loop rather than a mechanical Key-widening.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from phoenix.db import models
from phoenix.server.access.schema_provisioning import (
    project_scoped_session,
    provision_project_schema,
)
from phoenix.server.api.dataloaders.latency_ms_quantile import LatencyMsQuantileDataLoader
from phoenix.server.api.dataloaders.span_descendants import SpanDescendantsDataLoader
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


async def test_latency_ms_quantile_isolates_projects(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_a = await _create_project(postgresql_engine, "latency-quantile-a")
    project_b = await _create_project(postgresql_engine, "latency-quantile-b")

    # Project A: traces with latencies 100ms and 300ms (median ~200ms).
    # Project B: traces with latencies 900ms and 1100ms (median ~1000ms).
    # If routing leaked across projects, the medians would blend together.
    async def _seed(project_id: int, latencies_ms: list[int]) -> None:
        async with project_scoped_session(db, project_id) as session:
            for i, latency in enumerate(latencies_ms):
                start = datetime(2026, 1, 1, tzinfo=timezone.utc)
                end = start + timedelta(milliseconds=latency)
                session.add(
                    models.Trace(
                        project_rowid=project_id,
                        trace_id=f"trace-{project_id}-{i}",
                        start_time=start,
                        end_time=end,
                    )
                )

    await _seed(project_a, [100, 300])
    await _seed(project_b, [900, 1100])

    loader = LatencyMsQuantileDataLoader(db)
    result_a, result_b = await loader._load_fn(
        [
            ("trace", project_a, None, None, None, 0.5),
            ("trace", project_b, None, None, None, 0.5),
        ]
    )
    assert result_a is not None and result_b is not None
    assert result_a < 500
    assert result_b > 500


async def test_span_descendants_isolates_projects(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_a = await _create_project(postgresql_engine, "descendants-a")
    project_b = await _create_project(postgresql_engine, "descendants-b")

    async def _seed(project_id: int) -> int:
        now = datetime.now(timezone.utc)
        async with project_scoped_session(db, project_id) as session:
            trace = models.Trace(
                project_rowid=project_id,
                trace_id=f"trace-{project_id}",
                start_time=now,
                end_time=now,
            )
            session.add(trace)
            await session.flush()

            def _span(span_id: str, parent_id: str | None) -> models.Span:
                return models.Span(
                    trace_rowid=trace.id,
                    span_id=span_id,
                    parent_id=parent_id,
                    name="n",
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

            root = _span("root", None)
            session.add(root)
            await session.flush()
            child = _span("child", "root")
            session.add(child)
            await session.flush()
            return root.id

    root_a = await _seed(project_a)
    root_b = await _seed(project_b)

    loader = SpanDescendantsDataLoader(db)
    # max_depth=3, not None/unlimited: a pre-existing bug in this loader
    # (confirmed to reproduce identically on the flag-off/shared-schema
    # path, unrelated to project-scoped storage) makes the unlimited-depth
    # case raise a Postgres type error whenever any key in the batch omits
    # max_depth -- no existing test exercises it either (Span.descendants'
    # GraphQL field always passes a concrete maxDepth). Out of scope for
    # this stage; noted for the record, not fixed here.
    descendants_a, descendants_b = await loader._load_fn(
        [
            (root_a, project_a, 3),
            (root_b, project_b, 3),
        ]
    )
    # Both roots have exactly 1 descendant (their own project's "child"
    # span) -- if project routing leaked, root ids colliding numerically
    # across the two independent per-project sequences would surface
    # extra or wrong descendants.
    assert len(descendants_a) == 1
    assert len(descendants_b) == 1
