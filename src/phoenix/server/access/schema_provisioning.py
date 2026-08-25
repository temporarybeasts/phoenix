"""Schema-per-project spike (B + C) -- see the SSO/RBAC fork plan's
"Schema-per-project spike (B + C)" section. PROVISIONAL, Postgres-only.

Each project gets its own Postgres schema holding its own `traces`/`spans`
tables (cross-schema-FK-correct clones of the real models, via SQLAlchemy's
documented multi-schema `tometadata()` pattern) plus a dedicated role
granted only on that schema. Provisioning is triggered centrally for all 6
code paths that can create a `Project` row, via an **engine-level
`after_execute`** event registered at import time -- not by editing each
call site.

Originally this used a `Session`-level `after_flush` hook watching
`session.new`, but empirical testing (booting a real server and checking
which projects actually got provisioned) caught a real bug: the
highest-volume creation path, `db/insertion/span.py`'s auto-creation on
ingest, uses a Core-level `insert(models.Project)` executed directly via
`session.execute(...)`, not `session.add(...)` -- so it never appears in
`session.new` at all, and `after_flush` silently missed it. ORM-flush
inserts and raw Core inserts both ultimately compile down to and execute
the same `Insert` construct against `models.Project.__table__`, so
watching execution at the engine level (`after_execute`) catches both
uniformly. The captured ids are threaded to the session factory via a
`ContextVar` (same mechanism as `current_user_var` in `context.py`) rather
than `session.info`, since this hook has no `Session` in scope, only a
raw `Connection`.

Query routing into a specific project's schema is a separate, explicit
helper (`schema_scoped_connection`), not ambient like `current_user_var`:
a single logical request can span multiple projects, which schema-per-project
fundamentally can't do in one query.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Optional

from sqlalchemy import BLANK_SCHEMA, Insert, MetaData, event, select, text
from sqlalchemy.engine import Connection, CursorResult, Engine
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from phoenix.config import get_env_database_schema
from phoenix.db import models

#: Populated by `_capture_new_project_ids` (an engine-level `after_execute`
#: hook) whenever an INSERT into `projects` executes; read and cleared by
#: the session factory in `app.py` after a transaction commits.
new_project_ids_var: ContextVar[Optional[list[int]]] = ContextVar("new_project_ids", default=None)


def _project_schema_name(project_id: int) -> str:
    return f"project_{project_id}"


def _project_role_name(project_id: int) -> str:
    return f"phoenix_scoped_project_{project_id}"


#: Tables that move together into the new project schema -- their mutual
#: FKs (Span -> Trace) should follow the table's own new schema. Anything
#: else referenced (projects, project_sessions) stays in the shared schema.
_PROJECT_SCOPED_TABLES = {"traces", "spans"}


def _project_scoped_metadata(project_schema: str) -> MetaData:
    """Project/ProjectSession copied at the shared (current) schema --
    present only so Trace/Span's foreign keys into them compile against the
    right schema, not recreated. Trace/Span copied at the new project
    schema.

    `tometadata()`'s default FK-remapping assumes a table's *entire*
    foreign-key graph moves with it to the new schema -- fine for Span's FK
    to Trace (both move together), wrong for Trace's FKs to
    `projects`/`project_sessions` (neither moves). `referred_schema_fn`
    (SQLAlchemy's documented hook for exactly this mixed case) decides,
    per FK, which schema the referenced table resolves to in the target
    metadata: the new project schema for FKs within the moving set,
    otherwise the original/shared schema.
    """
    shared_schema = get_env_database_schema()

    def _referred_schema_fn(
        table: object, to_schema: "str | None", constraint: object, referred_schema: "str | None"
    ) -> object:
        # Returning `None` here means "no override, keep the source's exact
        # referred schema" to `tometadata()`, not "explicit no schema" -- so
        # when `shared_schema` (a real Postgres schema, or `None` for the
        # common default-schema case) is `None`, `return shared_schema`
        # would silently no-op instead of clearing the schema, leaving the
        # clone's FK still pointed at `to_schema`/whatever the source had.
        # `BLANK_SCHEMA` is SQLAlchemy's sentinel for "explicitly no schema",
        # distinct from `None`'s "unspecified" -- confirmed directly against
        # this SQLAlchemy version, not just from docs.
        if constraint.referred_table.name in _PROJECT_SCOPED_TABLES:  # type: ignore[attr-defined]
            return to_schema
        return shared_schema if shared_schema is not None else BLANK_SCHEMA

    target = MetaData()
    models.Project.__table__.tometadata(target, schema=shared_schema)
    models.ProjectSession.__table__.tometadata(target, schema=shared_schema)
    models.Trace.__table__.tometadata(
        target, schema=project_schema, referred_schema_fn=_referred_schema_fn
    )
    models.Span.__table__.tometadata(
        target, schema=project_schema, referred_schema_fn=_referred_schema_fn
    )
    return target


async def provision_project_schema(connection: AsyncConnection, project_id: int) -> None:
    """Idempotent -- safe to call concurrently/repeatedly for the same
    project. Necessary because span-ingest auto-creation
    (`db/insertion/span.py`) has no upsert on the Project insert, so two
    concurrent ingests for a brand-new project name can both trigger
    provisioning for the same project_id.
    """
    if connection.dialect.name != "postgresql":
        return

    schema_name = _project_schema_name(project_id)
    role_name = _project_role_name(project_id)
    shared_schema = get_env_database_schema()
    shared_projects_ref = f'"{shared_schema}".projects' if shared_schema else "projects"

    await connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))

    target_metadata = _project_scoped_metadata(schema_name)
    trace_table = target_metadata.tables[f"{schema_name}.traces"]
    span_table = target_metadata.tables[f"{schema_name}.spans"]
    await connection.run_sync(
        lambda sync_conn: target_metadata.create_all(
            sync_conn, tables=[trace_table, span_table], checkfirst=True
        )
    )

    await connection.execute(
        text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role_name}') THEN
                    CREATE ROLE "{role_name}" NOLOGIN;
                END IF;
            END
            $$;
            """
        )
    )
    await connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema_name}" TO "{role_name}"'))
    await connection.execute(
        text(
            f'GRANT SELECT, INSERT ON "{schema_name}".traces, "{schema_name}".spans '
            f'TO "{role_name}"'
        )
    )
    # The `id` columns are Postgres SERIAL (an implicit sequence + a column
    # DEFAULT nextval(...)), and a sequence's privileges are independent of
    # its owning table's -- INSERT on the table alone isn't enough to call
    # nextval() on the sequence backing its own primary key. Confirmed
    # directly against real Postgres: this grant was missing before Stage
    # 4b-2a's regression test exercised an actual scoped-role INSERT for
    # the first time.
    await connection.execute(
        text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema_name}" TO "{role_name}"')
    )
    # Read-only grant on the shared catalog table so a single-project query
    # can still join in the project's own name/metadata (FK enforcement
    # itself doesn't require this -- Postgres checks FK constraints with
    # the referenced table owner's privileges, not the inserting role's).
    await connection.execute(text(f'GRANT SELECT ON {shared_projects_ref} TO "{role_name}"'))
    await connection.execute(text(f'GRANT "{role_name}" TO current_user'))


@contextlib.asynccontextmanager
async def schema_scoped_connection(
    engine: AsyncEngine, project_id: int
) -> AsyncIterator[AsyncConnection]:
    """Explicit, not ambient: opens a connection routed at exactly one
    project's schema (via `schema_translate_map`) with that project's role
    switched in. Demo/mechanism-proof helper for this spike -- not wired
    into the real GraphQL/REST query surface or the dataloader layer.

    Keys the translate map on `models.PROJECT_SCOPED_SCHEMA_TOKEN`, not on
    `get_env_database_schema()`'s value directly (Stage 4b-2a) -- every
    table in the app shares that same default schema, so mapping it
    directly would redirect *all* of them into the project's schema, not
    just the ones actually cloned there (e.g. a query joining a
    project-scoped table to `projects` would wrongly try to resolve
    `projects` inside `project_<id>`, where it doesn't exist). The token
    is a schema value only the project-scoped tables use, so the map only
    ever matches those.
    """
    schema_name = _project_schema_name(project_id)
    role_name = _project_role_name(project_id)
    async with engine.connect() as connection:
        connection = await connection.execution_options(
            schema_translate_map={models.PROJECT_SCOPED_SCHEMA_TOKEN: schema_name}
        )
        async with connection.begin():
            await connection.execute(text(f'SET LOCAL ROLE "{role_name}"'))
            yield connection


def _capture_new_project_ids(
    conn: Connection,
    clauseelement: object,
    multiparams: object,
    params: object,
    execution_options: object,
    result: CursorResult,
) -> None:
    """Engine-level `after_execute` hook, registered once at import time --
    fires for *every* statement execution app-wide, whether it originated
    from an ORM flush or a raw Core `session.execute(insert(...))` call
    (both compile down to the same `Insert` construct against the mapped
    table), catching all 6 code paths that can create a `Project` row
    without editing any of them.

    Appends into `new_project_ids_var` rather than returning/storing on the
    connection: this hook has no `Session` in scope, and the ContextVar
    correctly scopes the captured ids to whichever async task (i.e.
    whichever `_db()` factory invocation) is actually executing this
    statement, the same way `current_user_var` scopes per-request identity.
    """
    if not (isinstance(clauseelement, Insert) and clauseelement.table.name == "projects"):
        return
    try:
        new_ids = [row[0] for row in result.inserted_primary_key_rows]
    except InvalidRequestError:
        # `inserted_primary_key_rows` refuses outright when the caller's
        # own statement has an explicit `.returning()` (e.g.
        # db/insertion/span.py's ingest-time auto-creation does) -- and we
        # can't safely consume `result` ourselves to read it, since the
        # caller still needs to read that same result afterward. Recover
        # the inserted name from the compiled statement's bind parameters
        # instead (works whether the value came from `.values()` or
        # external params) and look the id up with a small
        # same-connection, same-transaction follow-up query -- sees its
        # own just-inserted, still-uncommitted row fine, and unambiguous
        # since `Project.name` is unique.
        name = clauseelement.compile().params.get("name")
        if name is None:
            return
        row = conn.execute(select(models.Project.id).where(models.Project.name == name)).first()
        new_ids = [row[0]] if row else []
    if not new_ids:
        return
    acc = new_project_ids_var.get(None)
    if acc is None:
        acc = []
        new_project_ids_var.set(acc)
    acc.extend(new_ids)


event.listen(Engine, "after_execute", _capture_new_project_ids)
