# /// script
# dependencies = [
#   "arize-phoenix[pg]",
# ]
# ///
"""
Stage 4b-2c: back-fill existing rows into each project's own Postgres
schema, ahead of Stage 4b-2d's read/write cutover.

For every row in `projects`, this script:
  1. Ensures that project's schema/role exist (`provision_project_schema`
     -- idempotent, safe to call even if a project's schema is already
     provisioned).
  2. Copies that project's rows, across all 9 project-scoped tables, from
     the shared schema into the project's own schema, in FK dependency
     order, preserving primary keys exactly (`INSERT INTO ... (id, ...)
     SELECT id, ... ... ON CONFLICT (id) DO NOTHING` -- a conflict means
     "already migrated," never "different row, same id," since ids are
     copied verbatim from the shared table).
  3. Advances each copied table's own per-project sequence (`setval`,
     resolved via `pg_get_serial_sequence` rather than assuming a naming
     convention) to the copied max id, so that once Stage 4b-2d starts
     writing directly into these schemas, new rows continue the same id
     space instead of colliding with the historical ones just copied in.

This is **copy-only** -- it never deletes or modifies shared-schema rows.
Both copies coexist until the cutover has soaked in production; cleaning
up the shared-schema rows is a later, separate, explicit step.

Resumable and safe to re-run: each project is migrated in its own
transaction (so an interrupted run leaves already-migrated projects
committed and picks up cleanly on the next run), and every INSERT is
idempotent via `ON CONFLICT (id) DO NOTHING`. Intended to be run
repeatedly in the run-up to the Stage 4b-2d cutover (to catch rows
ingested since the last pass), with a final run immediately before the
flag flip.

On a run that successfully processes every current project, writes a
completion marker (a singleton row in
`<shared schema>.project_scoped_storage_migration_status`) that Stage
4b-2d's startup path checks before allowing
`PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED` to come up -- see that stage's
plan for the exact gate.

Environment variables.

- `PHOENIX_SQL_DATABASE_URL` must be set to the database connection string.
- (optional) Postgresql schema can be set via `PHOENIX_SQL_DATABASE_SCHEMA`.

Postgres-only: schema-per-project storage doesn't exist for SQLite
deployments, so this script refuses to run against anything else.
"""

import asyncio
from time import perf_counter
from typing import Optional

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from phoenix.config import get_env_database_connection_str, get_env_database_schema
from phoenix.db.engines import get_async_db_url
from phoenix.server.access.schema_provisioning import (
    _PROJECT_SCOPED_MODELS_IN_DEPENDENCY_ORDER,
    _project_schema_name,
    provision_project_schema,
)

#: Per project-scoped table, how to restrict a copy to one project's rows.
#: Each fragment filters an aliased source row `s` (the shared-schema
#: table being read from) via a subquery keyed on `:project_id` --
#: subqueries rather than JOINs so every fragment is self-contained (no
#: risk of duplicate rows from a fan-out join) and slots into the same
#: template uniformly. The `{shared_*}` placeholders are substituted with
#: schema-qualified shared-table references at format time.
_PROJECT_FILTERS: dict[str, str] = {
    "project_sessions": "s.project_id = :project_id",
    "traces": "s.project_rowid = :project_id",
    "spans": (
        "s.trace_rowid IN ("
        "SELECT t.id FROM {shared_traces} AS t WHERE t.project_rowid = :project_id)"
    ),
    "span_annotations": (
        "s.span_rowid IN ("
        "SELECT sp.id FROM {shared_spans} AS sp "
        "JOIN {shared_traces} AS t ON sp.trace_rowid = t.id "
        "WHERE t.project_rowid = :project_id)"
    ),
    "trace_annotations": (
        "s.trace_rowid IN ("
        "SELECT t.id FROM {shared_traces} AS t WHERE t.project_rowid = :project_id)"
    ),
    "project_session_annotations": (
        "s.project_session_id IN ("
        "SELECT ps.id FROM {shared_project_sessions} AS ps WHERE ps.project_id = :project_id)"
    ),
    "document_annotations": (
        "s.span_rowid IN ("
        "SELECT sp.id FROM {shared_spans} AS sp "
        "JOIN {shared_traces} AS t ON sp.trace_rowid = t.id "
        "WHERE t.project_rowid = :project_id)"
    ),
    "span_costs": (
        "s.trace_rowid IN ("
        "SELECT t.id FROM {shared_traces} AS t WHERE t.project_rowid = :project_id)"
    ),
    "span_cost_details": (
        "s.span_cost_id IN ("
        "SELECT sc.id FROM {shared_span_costs} AS sc "
        "JOIN {shared_traces} AS t ON sc.trace_rowid = t.id "
        "WHERE t.project_rowid = :project_id)"
    ),
}


def _shared_ref(table_name: str, shared_schema: Optional[str]) -> str:
    return f'"{shared_schema}"."{table_name}"' if shared_schema else f'"{table_name}"'


async def _migrate_one_project(
    connection: AsyncConnection,
    project_id: int,
    shared_schema: Optional[str],
) -> dict[str, int]:
    project_schema = _project_schema_name(project_id)
    counts: dict[str, int] = {}
    for model in _PROJECT_SCOPED_MODELS_IN_DEPENDENCY_ORDER:
        table_name = model.__tablename__
        cols = list(model.__table__.columns.keys())
        col_list = ", ".join(f'"{c}"' for c in cols)
        filter_template = _PROJECT_FILTERS[table_name]
        filter_sql = filter_template.format(
            shared_traces=_shared_ref("traces", shared_schema),
            shared_spans=_shared_ref("spans", shared_schema),
            shared_span_costs=_shared_ref("span_costs", shared_schema),
            shared_project_sessions=_shared_ref("project_sessions", shared_schema),
        )
        insert_stmt = text(
            f'INSERT INTO "{project_schema}"."{table_name}" ({col_list}) '
            f"SELECT {col_list} "
            f"FROM {_shared_ref(table_name, shared_schema)} AS s "
            f"WHERE {filter_sql} "
            f'ON CONFLICT ("id") DO NOTHING'
        )
        result = await connection.execute(insert_stmt, {"project_id": project_id})
        counts[table_name] = result.rowcount or 0

        await connection.execute(
            text(
                f"""
                SELECT setval(
                    pg_get_serial_sequence(:qualified_table, 'id'),
                    COALESCE((SELECT MAX("id") FROM "{project_schema}"."{table_name}"), 1),
                    (SELECT MAX("id") FROM "{project_schema}"."{table_name}") IS NOT NULL
                )
                """
            ),
            {"qualified_table": f'"{project_schema}"."{table_name}"'},
        )
    return counts


async def _ensure_status_table(connection: AsyncConnection, shared_schema: Optional[str]) -> None:
    ref = _shared_ref("project_scoped_storage_migration_status", shared_schema)
    await connection.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {ref} (
                id boolean PRIMARY KEY DEFAULT TRUE,
                completed_at timestamptz NOT NULL,
                CONSTRAINT project_scoped_storage_migration_status_singleton CHECK (id)
            )
            """
        )
    )


async def _mark_complete(connection: AsyncConnection, shared_schema: Optional[str]) -> None:
    ref = _shared_ref("project_scoped_storage_migration_status", shared_schema)
    await connection.execute(
        text(
            f"""
            INSERT INTO {ref} (id, completed_at)
            VALUES (TRUE, now())
            ON CONFLICT (id) DO UPDATE SET completed_at = EXCLUDED.completed_at
            """
        )
    )


async def migrate_to_project_scoped_schemas(engine: AsyncEngine) -> None:
    if engine.dialect.name != "postgresql":
        raise ValueError(
            "Schema-per-project storage is Postgres-only; refusing to run "
            f"against dialect {engine.dialect.name!r}."
        )
    start_time = perf_counter()
    shared_schema = get_env_database_schema()

    async with engine.connect() as conn:
        await _ensure_status_table(conn, shared_schema)
        await conn.commit()
        project_ids = [
            row[0]
            for row in (
                await conn.execute(text(f"SELECT id FROM {_shared_ref('projects', shared_schema)}"))
            ).all()
        ]

    print(f"Found {len(project_ids)} project(s) to migrate.")
    for i, project_id in enumerate(project_ids, start=1):
        async with engine.begin() as conn:
            await provision_project_schema(conn, project_id)
            counts = await _migrate_one_project(conn, project_id, shared_schema)
        total = sum(counts.values())
        print(f"  [{i}/{len(project_ids)}] project {project_id}: copied {total} row(s) {counts}")

    async with engine.begin() as conn:
        await _mark_complete(conn, shared_schema)

    elapsed_time = perf_counter() - start_time
    print(
        f"✅ Migrated {len(project_ids)} project(s) to project-scoped schemas "
        f"in {elapsed_time:.3f} seconds."
    )


if __name__ == "__main__":
    sql_database_url = make_url(get_env_database_connection_str())
    print(f"Using database URL: {sql_database_url}")
    ans = input("Is that correct? [y]/n: ")
    if ans.lower().startswith("n"):
        url = input("Please enter the correct database URL: ")
        sql_database_url = make_url(url)

    backend = sql_database_url.get_backend_name()
    if backend != "postgresql":
        raise ValueError(f"Schema-per-project storage is Postgres-only; got backend {backend!r}.")

    async_url = get_async_db_url(sql_database_url.render_as_string(hide_password=False))
    async_engine = create_async_engine(url=async_url, echo=False)

    async def _run() -> None:
        try:
            await migrate_to_project_scoped_schemas(async_engine)
        finally:
            await async_engine.dispose()

    asyncio.run(_run())
