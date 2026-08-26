"""Stage 4b-2h: verifies the two claims the fix rests on.

1. Deleting a shared-schema `Project` row already cascades to remove that
   project's rows in its own per-project schema, via a real cross-schema
   Postgres FK constraint -- with no extra code, in both flag states. This
   is the load-bearing fact that made the originally-scoped `delete_traces`
   fix unnecessary; it's a pre-existing Postgres/SQLAlchemy behavior, not
   new code, but worth nailing down directly rather than trusting the
   reasoning that led to skipping the `delete_traces` change.
2. `deprovision_project_schemas` (the new batched, best-effort sibling to
   `deprovision_project_schema`) actually drops the schema/role for every
   project it's given, and `api/utils.py`'s `delete_projects` -- the
   function 3 of the 4 gap call sites either call directly or were
   simplified to call -- now does this automatically after its row delete.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine

from phoenix.db import models
from phoenix.server.access.schema_provisioning import (
    _project_role_name,
    _project_schema_name,
    deprovision_project_schemas,
    project_scoped_session,
    provision_project_schema,
)
from phoenix.server.api.utils import delete_projects
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


async def _schema_and_role_exist(engine: AsyncEngine, project_id: int) -> tuple[bool, bool]:
    schema_name = _project_schema_name(project_id)
    role_name = _project_role_name(project_id)
    async with engine.connect() as conn:
        schema_exists = bool(
            await conn.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :name)"),
                {"name": schema_name},
            )
        )
        role_exists = bool(
            await conn.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :name)"),
                {"name": role_name},
            )
        )
    return schema_exists, role_exists


async def test_project_row_delete_cascades_to_project_scoped_traces(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core claim 4b-2h's scoping rests on: `Trace.project_rowid`'s FK
    to the shared `projects.id` survives being cloned into the project's
    own schema (with its `ondelete="CASCADE"` intact), so a plain,
    unscoped `DELETE FROM projects WHERE id = ...` against the shared
    table already removes that project's `traces` rows in its own schema
    -- no `project_scoped_session` routing needed for this to happen.
    """
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "cascade-claim-test")
    now = datetime.now(timezone.utc)
    async with project_scoped_session(db, project_id) as session:
        session.add(
            models.Trace(
                project_rowid=project_id,
                trace_id="cascade-claim-trace",
                start_time=now,
                end_time=now,
            )
        )

    async with postgresql_engine.connect() as conn:
        count_before = await conn.scalar(
            text(f'SELECT count(*) FROM "project_{project_id}".traces')
        )
    assert count_before == 1, "sanity check: the trace should exist before the project is deleted"

    # Plain, unscoped delete of the shared Project row -- no deprovisioning,
    # no project_scoped_session, exactly what every one of the 5 delete-
    # project call sites in src/phoenix/server/ does for the row itself.
    async with postgresql_engine.begin() as conn:
        await conn.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_id})

    async with postgresql_engine.connect() as conn:
        count_after = await conn.scalar(text(f'SELECT count(*) FROM "project_{project_id}".traces'))
    assert count_after == 0, (
        "deleting the Project row should cascade to remove its per-schema traces "
        "via the real cross-schema FK constraint, with no extra code"
    )


async def test_deprovision_project_schemas_drops_every_project(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
) -> None:
    project_a = await _create_project(postgresql_engine, "batch-deprovision-a")
    project_b = await _create_project(postgresql_engine, "batch-deprovision-b")

    async with postgresql_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM projects WHERE id IN (:a, :b)"),
            {"a": project_a, "b": project_b},
        )

    await deprovision_project_schemas(db, [project_a, project_b])

    for project_id in (project_a, project_b):
        schema_exists, role_exists = await _schema_and_role_exist(postgresql_engine, project_id)
        assert not schema_exists, f"project {project_id}'s schema should be dropped"
        assert not role_exists, f"project {project_id}'s role should be dropped"


async def test_deprovision_project_schemas_is_best_effort(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
) -> None:
    """One project's deprovisioning failure (here, simulated by passing an
    id with no provisioned schema/role at all, which `deprovision_project_schema`
    handles via `IF EXISTS`) must not prevent another project's from
    succeeding -- each is dropped in its own transaction.
    """
    project_a = await _create_project(postgresql_engine, "best-effort-a")
    never_provisioned_id = 999_999_999

    await deprovision_project_schemas(db, [never_provisioned_id, project_a])

    schema_exists, role_exists = await _schema_and_role_exist(postgresql_engine, project_a)
    assert not schema_exists
    assert not role_exists


async def test_delete_projects_deprovisions_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
) -> None:
    """`api/utils.py`'s `delete_projects` -- called directly by
    `datasets.py`/`dataset_mutations.py`/`experiment_mutations.py`, and by
    the simplified `routers/v1/experiments.py` delete-experiment endpoint
    -- must deprovision the schema/role after its row delete, not just
    delete the row.
    """
    project_id = await _create_project(postgresql_engine, "delete-projects-test")

    deleted_ids = await delete_projects(db, "delete-projects-test")
    assert deleted_ids == [project_id]

    schema_exists, role_exists = await _schema_and_role_exist(postgresql_engine, project_id)
    assert not schema_exists
    assert not role_exists
