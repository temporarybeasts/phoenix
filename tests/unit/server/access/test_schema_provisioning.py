from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from phoenix.db import models
from phoenix.db.facilitator import _ensure_project_schemas_provisioned
from phoenix.db.insertion.span import insert_span
from phoenix.server.access.schema_provisioning import (
    _project_role_name,
    _project_schema_name,
    deprovision_project_schema,
    provision_project_schema,
    schema_scoped_connection,
)
from phoenix.server.types import DbSessionFactory
from phoenix.trace.schemas import Span, SpanContext, SpanKind, SpanStatusCode

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


async def test_ingest_auto_created_project_schema_provisioned_inline(
    db: DbSessionFactory,
) -> None:
    """Regression test for Stage 4b-2b's chicken-and-egg timing fix:
    `insert_span`'s brand-new-project branch must provision that project's
    schema in the SAME connection/transaction as the Project insert, not
    rely solely on the post-commit hook (`app.py`'s `_db()` factory), which
    runs too late for the Trace/Span this same call is about to persist
    once real per-project write routing lands (Stage 4b-2d). Checked via
    the *same*, still-uncommitted session's own connection -- a separate
    connection would never see uncommitted DDL regardless of timing, so
    seeing it here specifically confirms the inline call happened.
    """
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    project_name = "inline-provisioning-test"
    async with db() as session:
        event = await insert_span(
            session,
            Span(
                name="root",
                context=SpanContext(trace_id="trace-1", span_id="span-1"),
                span_kind=SpanKind.CHAIN,
                parent_id=None,
                start_time=start,
                end_time=start + timedelta(seconds=1),
                status_code=SpanStatusCode.OK,
                status_message="",
                attributes={},
                events=[],
                conversation=None,
            ),
            project_name,
        )
        assert event is not None
        schema_name = _project_schema_name(event.project_rowid)
        exists = await session.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :name)"),
            {"name": schema_name},
        )
        assert exists is True


async def test_reconciliation_pass_provisions_unprovisioned_projects(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
) -> None:
    """Regression test for Stage 4b-2b's bootstrap-project-and-safety-net
    fix: a `Project` row that exists without a schema (e.g. the bootstrap
    `default` project, seeded by the Alembic init migration on its own
    disposable engine before `schema_provisioning.py`'s `after_execute`
    hook exists) must get provisioned by `_ensure_project_schemas_provisioned`,
    the reconciliation pass wired into `Facilitator`.

    Inserts the Project row directly against the raw `postgresql_engine`
    connection rather than through the `db` fixture: `db` for this dialect
    *is* `app.py`'s real `_db()` factory (imported directly in conftest.py),
    which already drains `new_project_ids_var` and auto-provisions on
    commit -- the same hook production uses. Going through it here would
    provision the schema before this test ever calls the reconciliation
    pass, defeating the point. Inserting via the bare engine instead means
    the `after_execute` capture hook still fires (it's engine-level,
    unconditional) but has no `_db()`-scoped contextvar to drain into, so
    nothing auto-provisions -- the row genuinely has no schema until
    `_ensure_project_schemas_provisioned` runs.
    """
    async with postgresql_engine.begin() as conn:
        project_id = await conn.scalar(
            insert(models.Project)
            .values(name="unprovisioned-until-reconciled")
            .returning(models.Project.id)
        )
    assert project_id is not None
    schema_name = _project_schema_name(project_id)

    async with db() as session:
        exists_before = await session.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :name)"),
            {"name": schema_name},
        )
    assert exists_before is False

    await _ensure_project_schemas_provisioned(db)

    async with db() as session:
        exists_after = await session.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :name)"),
            {"name": schema_name},
        )
    assert exists_after is True


async def test_deprovision_drops_schema_and_role(
    postgresql_engine: AsyncEngine,
) -> None:
    """Regression test for Stage 4b-2b's Task #4: `deprovision_project_schema`
    must drop both the project's schema (and everything in it, via CASCADE)
    and its dedicated role, mirroring what `provision_project_schema` created.
    The role is also granted to `current_user` (see `provision_project_schema`'s
    final `GRANT ... TO current_user`) -- confirming `DROP ROLE` still
    succeeds proves that plain membership grant doesn't block the drop.
    """
    project_id = await _create_project(postgresql_engine, "deprovision-test")
    schema_name = _project_schema_name(project_id)
    role_name = _project_role_name(project_id)

    async with postgresql_engine.connect() as conn:
        assert await conn.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :name)"),
            {"name": schema_name},
        )
        assert await conn.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :name)"),
            {"name": role_name},
        )

    async with postgresql_engine.connect() as conn:
        await deprovision_project_schema(conn, project_id)
        await conn.commit()

    async with postgresql_engine.connect() as conn:
        schema_exists = await conn.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :name)"),
            {"name": schema_name},
        )
        role_exists = await conn.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :name)"),
            {"name": role_name},
        )
    assert schema_exists is False
    assert role_exists is False


async def test_deprovision_is_idempotent_for_never_provisioned_project(
    postgresql_engine: AsyncEngine,
) -> None:
    """A project deleted before it was ever provisioned (or whose
    provisioning previously failed) must not make deletion itself fail --
    `deprovision_project_schema` uses `IF EXISTS` throughout specifically
    so calling it for a schema/role that were never created is a no-op,
    not an error.
    """
    async with postgresql_engine.begin() as conn:
        project_id = await conn.scalar(
            insert(models.Project).values(name="never-provisioned").returning(models.Project.id)
        )
    assert project_id is not None

    async with postgresql_engine.connect() as conn:
        await deprovision_project_schema(conn, project_id)
        await conn.commit()
