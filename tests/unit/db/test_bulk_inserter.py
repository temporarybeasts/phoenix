import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from phoenix.db import models
from phoenix.db.bulk_inserter import BulkInserter
from phoenix.server.daemons.generative_model_store import GenerativeModelStore
from phoenix.server.daemons.span_cost_calculator import SpanCostCalculator
from phoenix.server.dml_event import DmlEvent
from phoenix.server.types import DbSessionFactory
from phoenix.trace.schemas import Span, SpanContext, SpanKind, SpanStatusCode

pytestmark = pytest.mark.postgres_only


class _FakeEventQueue:
    def __init__(self) -> None:
        self.items: list[DmlEvent] = []

    def put(self, item: DmlEvent) -> None:
        self.items.append(item)


def _make_inserter(db: DbSessionFactory) -> BulkInserter:
    model_store = GenerativeModelStore(db=db)
    span_cost_calculator = SpanCostCalculator(db=db, model_store=model_store)
    return BulkInserter(
        db,
        event_queue=_FakeEventQueue(),
        span_cost_calculator=span_cost_calculator,
    )


def _span(trace_id: str, span_id: str, *, start: datetime) -> Span:
    return Span(
        name="root",
        context=SpanContext(trace_id=trace_id, span_id=span_id),
        span_kind=SpanKind.CHAIN,
        parent_id=None,
        start_time=start,
        end_time=start + timedelta(seconds=1),
        status_code=SpanStatusCode.OK,
        status_message="",
        attributes={},
        events=[],
        conversation=None,
    )


async def test_insert_batch_shared_writes_to_shared_schema(db: DbSessionFactory) -> None:
    """Flag off (the default): unchanged behavior -- spans land in the
    shared schema, exactly as before Stage 4b-2d.
    """
    inserter = _make_inserter(db)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    project_ids: set[int] = set()
    span_costs: list[Any] = []
    async with db() as session:
        await inserter._insert_batch_shared(
            [(_span("trace-shared-1", "span-shared-1", start=start), "shared-routing-test")],
            project_ids,
            span_costs,
        )
    assert len(project_ids) == 1
    async with db() as session:
        found = await session.scalar(
            select(models.Span.span_id).where(models.Span.span_id == "span-shared-1")
        )
    assert found == "span-shared-1"


async def test_insert_batch_project_scoped_routes_into_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    inserter = _make_inserter(db)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    project_ids: set[int] = set()
    span_costs: list[Any] = []
    await inserter._insert_batch_project_scoped(
        [(_span("trace-scoped-1", "span-scoped-1", start=start), "project-scoped-routing-test")],
        project_ids,
        span_costs,
    )
    assert len(project_ids) == 1
    project_id = next(iter(project_ids))

    async with postgresql_engine.connect() as conn:
        found_scoped = await conn.scalar(
            text(f'SELECT span_id FROM "project_{project_id}".spans WHERE span_id = :sid'),
            {"sid": "span-scoped-1"},
        )
    assert found_scoped == "span-scoped-1"

    async with postgresql_engine.connect() as conn:
        found_shared = await conn.scalar(
            select(models.Span.span_id).where(models.Span.span_id == "span-scoped-1")
        )
    assert found_shared is None


async def test_insert_batch_project_scoped_isolates_multiple_projects(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single ingest batch mixing spans from two different projects must
    route each span into its own project's schema, not cross-contaminate.
    """
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    inserter = _make_inserter(db)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    project_ids: set[int] = set()
    span_costs: list[Any] = []
    batch = [
        (_span("trace-multi-a", "span-multi-a", start=start), "multi-project-test-a"),
        (_span("trace-multi-b", "span-multi-b", start=start), "multi-project-test-b"),
    ]
    await inserter._insert_batch_project_scoped(batch, project_ids, span_costs)
    assert len(project_ids) == 2

    async with postgresql_engine.connect() as conn:
        project_a_id = await conn.scalar(
            select(models.Project.id).where(models.Project.name == "multi-project-test-a")
        )
        project_b_id = await conn.scalar(
            select(models.Project.id).where(models.Project.name == "multi-project-test-b")
        )
    assert project_a_id is not None and project_b_id is not None
    assert {project_a_id, project_b_id} == project_ids

    async with postgresql_engine.connect() as conn:
        a_has_a = await conn.scalar(
            text(f'SELECT span_id FROM "project_{project_a_id}".spans WHERE span_id = :sid'),
            {"sid": "span-multi-a"},
        )
        a_has_b = await conn.scalar(
            text(f'SELECT span_id FROM "project_{project_a_id}".spans WHERE span_id = :sid'),
            {"sid": "span-multi-b"},
        )
        b_has_b = await conn.scalar(
            text(f'SELECT span_id FROM "project_{project_b_id}".spans WHERE span_id = :sid'),
            {"sid": "span-multi-b"},
        )
    assert a_has_a == "span-multi-a"
    assert a_has_b is None
    assert b_has_b == "span-multi-b"


async def test_insert_batch_project_scoped_isolates_per_span_failures(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A savepoint-isolated bad span within a project group must not take
    down the rest of that group's spans -- same guarantee as flag-off,
    now scoped per project connection instead of the shared one.
    """
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    inserter = _make_inserter(db)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    project_ids: set[int] = set()
    span_costs: list[Any] = []
    # A span whose (shared, brand-new) trace would get a NULL end_time --
    # a NOT NULL violation -- forcing a real DB-level failure for this one
    # span while a sibling span for the same (still-uncreated) trace,
    # arriving right after it in the same project group, succeeds.
    bad_span = dataclasses.replace(_span("trace-savepoint", "span-bad", start=start), end_time=None)  # type: ignore[arg-type]
    good_span = _span("trace-savepoint", "span-good", start=start)
    batch = [
        (bad_span, "savepoint-isolation-test"),
        (good_span, "savepoint-isolation-test"),
    ]
    await inserter._insert_batch_project_scoped(batch, project_ids, span_costs)
    assert len(project_ids) == 1
    project_id = next(iter(project_ids))

    async with postgresql_engine.connect() as conn:
        good_found = await conn.scalar(
            text(f'SELECT span_id FROM "project_{project_id}".spans WHERE span_id = :sid'),
            {"sid": "span-good"},
        )
        bad_found = await conn.scalar(
            text(f'SELECT span_id FROM "project_{project_id}".spans WHERE span_id = :sid'),
            {"sid": "span-bad"},
        )
    assert good_found == "span-good"
    assert bad_found is None
