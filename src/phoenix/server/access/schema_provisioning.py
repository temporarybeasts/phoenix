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
#: FKs should follow the table's own new schema. Anything else referenced
#: (projects, users, generative_models) stays in the shared schema. Order
#: matters below in `_project_scoped_metadata()`: each table's own FK
#: targets must already be present in the target metadata (at whichever
#: schema they resolve to) before that table is cloned, or `tometadata()`
#: raises `NoReferencedTableError` trying to resolve them -- confirmed
#: directly, this isn't just a style preference.
_PROJECT_SCOPED_TABLES = {
    "project_sessions",
    "traces",
    "spans",
    "span_annotations",
    "trace_annotations",
    "project_session_annotations",
    "document_annotations",
    "span_costs",
    "span_cost_details",
}

#: Shared (never project-scoped) tables that at least one project-scoped
#: table has a real FK to -- must be present in the cloned target metadata,
#: at the shared schema, for those FKs to resolve.
_SHARED_REFERENCED_MODELS = (models.Project, models.User, models.GenerativeModel)

#: Project-scoped models, in FK dependency order (each entry's own FK
#: targets among this set precede it).
_PROJECT_SCOPED_MODELS_IN_DEPENDENCY_ORDER = (
    models.ProjectSession,  # -> projects
    models.Trace,  # -> projects, project_sessions
    models.Span,  # -> traces
    models.SpanAnnotation,  # -> spans, users
    models.TraceAnnotation,  # -> traces, users
    models.ProjectSessionAnnotation,  # -> project_sessions, users
    models.DocumentAnnotation,  # -> spans, users
    models.SpanCost,  # -> spans, traces, generative_models
    models.SpanCostDetail,  # -> span_costs
)


def _project_scoped_metadata(project_schema: str) -> MetaData:
    """Shared reference tables (`projects`/`users`/`generative_models`)
    copied at the shared (current) schema -- present only so the
    project-scoped tables' foreign keys into them compile against the
    right schema, not recreated. The 9 project-scoped tables themselves
    copied at the new project schema.

    `tometadata()`'s default FK-remapping assumes a table's *entire*
    foreign-key graph moves with it to the new schema -- fine for FKs
    within the moving set (e.g. Span -> Trace, both move together), wrong
    for FKs to shared tables (e.g. Trace -> `projects`, which never
    moves). `referred_schema_fn` (SQLAlchemy's documented hook for exactly
    this mixed case) decides, per FK, which schema the referenced table
    resolves to in the target metadata: the new project schema for FKs
    within the moving set, otherwise the original/shared schema.
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
    for shared_model in _SHARED_REFERENCED_MODELS:
        shared_model.__table__.tometadata(target, schema=shared_schema)
    for scoped_model in _PROJECT_SCOPED_MODELS_IN_DEPENDENCY_ORDER:
        scoped_model.__table__.tometadata(
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

    def _shared_ref(table_name: str) -> str:
        return f'"{shared_schema}".{table_name}' if shared_schema else table_name

    await connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))

    target_metadata = _project_scoped_metadata(schema_name)
    scoped_tables = [
        target_metadata.tables[f"{schema_name}.{model.__tablename__}"]
        for model in _PROJECT_SCOPED_MODELS_IN_DEPENDENCY_ORDER
    ]
    await connection.run_sync(
        lambda sync_conn: target_metadata.create_all(
            sync_conn, tables=scoped_tables, checkfirst=True
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
    scoped_table_refs = ", ".join(
        f'"{schema_name}".{model.__tablename__}'
        for model in _PROJECT_SCOPED_MODELS_IN_DEPENDENCY_ORDER
    )
    # UPDATE/DELETE (not just SELECT/INSERT): the cumulative-count updates
    # ingest already does today, retention sweeps, and annotation edits all
    # need them once real write-routing lands -- adding the grant now so
    # provisioning doesn't need touching again for that later, narrower
    # change.
    await connection.execute(
        text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {scoped_table_refs} TO "{role_name}"')
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
    # Read-only grants on the shared tables a project-scoped query can join
    # into (FK enforcement itself doesn't require this -- Postgres checks FK
    # constraints with the referenced table's owner's privileges, not the
    # inserting role's).
    shared_refs = ", ".join(_shared_ref(model.__tablename__) for model in _SHARED_REFERENCED_MODELS)
    await connection.execute(text(f'GRANT SELECT ON {shared_refs} TO "{role_name}"'))
    await connection.execute(text(f'GRANT "{role_name}" TO current_user'))


async def deprovision_project_schema(connection: AsyncConnection, project_id: int) -> None:
    """Idempotent -- safe to call even if provisioning never ran (e.g. a
    project created and deleted before the schema-per-project rollout, or
    one whose provisioning previously failed). Called after the shared-schema
    `Project` row delete has already succeeded; callers should log loudly on
    failure here rather than let it block the user-facing delete, since the
    project row is already gone either way.
    """
    if connection.dialect.name != "postgresql":
        return

    schema_name = _project_schema_name(project_id)
    role_name = _project_role_name(project_id)

    await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
    # `DROP ROLE` refuses outright if the role still holds privileges
    # anywhere else in the database -- and it does: `provision_project_schema`
    # grants it SELECT on the shared tables it can join into (projects,
    # users, generative_models), which live outside the schema just dropped
    # above. `DROP OWNED BY` revokes those (and drops anything the role
    # itself owns, though it owns nothing here) so the role has nothing left
    # to block the drop. Confirmed directly: omitting this step raises
    # `DependentObjectsStillExistError` on a real provisioned role.
    await connection.execute(
        text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{role_name}') THEN
                    EXECUTE 'DROP OWNED BY "{role_name}"';
                    EXECUTE 'DROP ROLE "{role_name}"';
                END IF;
            END
            $$;
            """
        )
    )


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
