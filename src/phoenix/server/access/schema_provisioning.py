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
import logging
from collections.abc import AsyncIterator, Iterable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import BLANK_SCHEMA, Insert, MetaData, Table, delete, event, insert, select, text
from sqlalchemy.engine import Connection, CursorResult, Engine
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from phoenix.config import get_env_database_schema, get_env_project_scoped_storage_enabled
from phoenix.db import models

if TYPE_CHECKING:
    from phoenix.server.types import DbSessionFactory

logger = logging.getLogger(__name__)

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

#: Stage 4b-2e: the subset of `_PROJECT_SCOPED_MODELS_IN_DEPENDENCY_ORDER`
#: that's actually owned by a single trace and moves with it on transfer.
#: Excludes `ProjectSession`/`ProjectSessionAnnotation` -- a session is
#: inherently multi-trace, so moving one trace out of it can't sensibly
#: carry the whole session along (see `transfer_trace_between_projects`).
_TRANSFERABLE_TRACE_SUBTREE_MODELS = tuple(
    model
    for model in _PROJECT_SCOPED_MODELS_IN_DEPENDENCY_ORDER
    if model not in (models.ProjectSession, models.ProjectSessionAnnotation)
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


async def deprovision_project_schemas(db: "DbSessionFactory", project_ids: list[int]) -> None:
    """Stage 4b-2h: batched, best-effort counterpart to
    `deprovision_project_schema` for callers that delete more than one
    `Project` row at once (`api/utils.py`'s `delete_projects`/
    `delete_projects_by_id`, `daemons/experiment_sweeper.py`'s ephemeral-
    experiment cleanup). Called after the shared-schema `Project` row
    deletes have already committed -- each project's schema/role is
    dropped independently, in its own transaction, so one project's
    deprovisioning failure doesn't block or roll back another's, and a
    failure never surfaces as a failed delete: the row data is already
    gone either way (Postgres's own cross-schema FK cascade already
    removed it -- see `_project_scoped_metadata`'s `referred_schema_fn`
    above), so this step only reclaims the now-empty schema/role.
    """
    if db.engine is None:
        return
    for project_id in project_ids:
        try:
            async with db.engine.begin() as connection:
                await deprovision_project_schema(connection, project_id)
        except Exception:
            logger.exception(f"Failed to deprovision schema for deleted project {project_id}")


async def transfer_trace_between_projects(
    engine: AsyncEngine,
    trace_rowid: int,
    source_project_id: int,
    dest_project_id: int,
) -> None:
    """Single-trace convenience wrapper around
    `transfer_traces_between_projects` -- opens its own transaction. Prefer
    `transfer_traces_between_projects` directly when a caller is moving more
    than one trace in the same request, so the whole batch commits or rolls
    back atomically instead of one trace at a time (matching the pre-cutover
    single-`UPDATE`-statement transfer's all-or-nothing semantics).
    """
    if engine.dialect.name != "postgresql":
        raise RuntimeError("transfer_trace_between_projects requires PostgreSQL.")
    async with engine.begin() as connection:
        await _transfer_trace_subtree_on_connection(
            connection, trace_rowid, source_project_id, dest_project_id
        )


async def transfer_traces_between_projects(
    engine: AsyncEngine,
    transfers: Iterable[tuple[int, int, int]],
) -> None:
    """Stage 4b-2e: moves each `(trace_rowid, source_project_id,
    dest_project_id)` triple's trace, and the subtree it owns (spans,
    span/trace/document annotations, span costs and their detail rows),
    from its source project's Postgres schema to its destination's, all in
    one transaction -- the whole batch commits or rolls back together,
    matching the pre-cutover single `UPDATE traces SET project_rowid = ...`
    transfer's all-or-nothing semantics for a multi-trace request.

    Physically impossible to express as that single UPDATE once traces live
    in separate per-project schemas -- a row can't move between two
    different physical tables via an UPDATE. Copy-then-delete instead,
    mirroring Stage 4b-2c's data migration
    (`migrate_to_project_scoped_schemas.py`) in spirit, but NOT in its
    id-preserving `INSERT ... ON CONFLICT (id) DO NOTHING` mechanics: that
    pattern is only safe when the destination schema starts empty (the
    one-time historical backfill's case). Here the destination project's
    schema is already live, with its own independently-advancing id
    sequences -- a moderately active destination project will very likely
    already have used the source trace's numeric id for an unrelated row,
    so preserving ids risks either a silent `ON CONFLICT DO NOTHING`
    no-op (the row appears copied but isn't -- silent data loss once the
    source row is deleted after) or, worse, an accidental collision with an
    unrelated row that happens to already exist at that id. Every row in
    the subtree is therefore inserted *without* specifying `id` (the
    destination's own sequence assigns a fresh one), and every in-subtree
    foreign key (`spans.trace_rowid`, `span_annotations.span_rowid`,
    `document_annotations.span_rowid`, `span_costs.trace_rowid`/
    `span_rowid`, `span_cost_details.span_cost_id`) is rewritten in Python
    to the newly-assigned id it now points to. `spans.parent_id` needs no
    rewriting -- it's keyed on the OTel string `span_id`, not the integer
    row id, and `span_id` itself is copied verbatim.

    `project_sessions`/`project_session_annotations` are deliberately NOT
    part of the moved subtree: a session is inherently multi-trace, so
    moving one trace out of it can't sensibly move the whole session along.
    `traces.project_session_rowid` is set to `NULL` on the copied row
    instead of carried over, since the destination schema's own
    `project_sessions` table has no row for the source session (copying the
    value unmodified would violate the FK outright, or -- worse -- silently
    attach to an unrelated session that happens to reuse the same row id in
    the destination project's independent id sequence). This is a real
    behavior difference from the flag-off path, where transferring a trace
    has always left `project_session_rowid` pointed at the original
    (shared-table) session unconditionally -- including when that leaves
    the trace's session technically owned by a different project than the
    trace itself, a pre-existing inconsistency, left unchanged there since
    flag-off behavior must stay provably identical to before this stage.

    Source-side cleanup is a single `DELETE FROM <source>.traces WHERE id =
    :trace_rowid` per transfer -- every table in the subtree has `ON DELETE
    CASCADE` on its FK into `traces` (directly, or transitively via
    `spans`/`span_costs`), confirmed directly from the model definitions,
    so deleting the trace row alone removes its entire subtree at the
    database level; no separate per-table deletes are needed.
    """
    if engine.dialect.name != "postgresql":
        raise RuntimeError("transfer_traces_between_projects requires PostgreSQL.")
    async with engine.begin() as connection:
        for trace_rowid, source_project_id, dest_project_id in transfers:
            await _transfer_trace_subtree_on_connection(
                connection, trace_rowid, source_project_id, dest_project_id
            )


async def _transfer_trace_subtree_on_connection(
    connection: AsyncConnection,
    trace_rowid: int,
    source_project_id: int,
    dest_project_id: int,
) -> None:
    source_schema = _project_schema_name(source_project_id)
    dest_schema = _project_schema_name(dest_project_id)
    source_metadata = _project_scoped_metadata(source_schema)
    dest_metadata = _project_scoped_metadata(dest_schema)

    def _table(metadata: MetaData, schema: str, model: Any) -> Table:
        return metadata.tables[f"{schema}.{model.__tablename__}"]

    src = {
        model: _table(source_metadata, source_schema, model)
        for model in _TRANSFERABLE_TRACE_SUBTREE_MODELS
    }
    dst = {
        model: _table(dest_metadata, dest_schema, model)
        for model in _TRANSFERABLE_TRACE_SUBTREE_MODELS
    }

    trace_row = (
        (
            await connection.execute(
                select(src[models.Trace]).where(src[models.Trace].c.id == trace_rowid)
            )
        )
        .mappings()
        .first()
    )
    if trace_row is None:
        raise ValueError(f"Trace {trace_rowid} not found in project {source_project_id}'s schema.")
    trace_values = dict(trace_row)
    trace_values.pop("id")
    trace_values["project_rowid"] = dest_project_id
    trace_values["project_session_rowid"] = None
    new_trace_id = (
        await connection.execute(
            insert(dst[models.Trace]).values(**trace_values).returning(dst[models.Trace].c.id)
        )
    ).scalar_one()

    span_rows = (
        (
            await connection.execute(
                select(src[models.Span]).where(src[models.Span].c.trace_rowid == trace_rowid)
            )
        )
        .mappings()
        .all()
    )
    span_id_map: dict[int, int] = {}
    for row in span_rows:
        values = dict(row)
        old_id = values.pop("id")
        values["trace_rowid"] = new_trace_id
        new_id = (
            await connection.execute(
                insert(dst[models.Span]).values(**values).returning(dst[models.Span].c.id)
            )
        ).scalar_one()
        span_id_map[old_id] = new_id

    trace_annotation_rows = (
        (
            await connection.execute(
                select(src[models.TraceAnnotation]).where(
                    src[models.TraceAnnotation].c.trace_rowid == trace_rowid
                )
            )
        )
        .mappings()
        .all()
    )
    for row in trace_annotation_rows:
        values = dict(row)
        values.pop("id")
        values["trace_rowid"] = new_trace_id
        await connection.execute(insert(dst[models.TraceAnnotation]).values(**values))

    for model in (models.SpanAnnotation, models.DocumentAnnotation):
        rows = (
            (
                (
                    await connection.execute(
                        select(src[model]).where(src[model].c.span_rowid.in_(span_id_map.keys()))
                    )
                )
                .mappings()
                .all()
            )
            if span_id_map
            else []
        )
        for row in rows:
            values = dict(row)
            values.pop("id")
            values["span_rowid"] = span_id_map[values["span_rowid"]]
            await connection.execute(insert(dst[model]).values(**values))

    span_cost_rows = (
        (
            await connection.execute(
                select(src[models.SpanCost]).where(
                    src[models.SpanCost].c.trace_rowid == trace_rowid
                )
            )
        )
        .mappings()
        .all()
    )
    span_cost_id_map: dict[int, int] = {}
    for row in span_cost_rows:
        values = dict(row)
        old_id = values.pop("id")
        values["trace_rowid"] = new_trace_id
        values["span_rowid"] = span_id_map[values["span_rowid"]]
        new_id = (
            await connection.execute(
                insert(dst[models.SpanCost]).values(**values).returning(dst[models.SpanCost].c.id)
            )
        ).scalar_one()
        span_cost_id_map[old_id] = new_id

    if span_cost_id_map:
        detail_rows = (
            (
                await connection.execute(
                    select(src[models.SpanCostDetail]).where(
                        src[models.SpanCostDetail].c.span_cost_id.in_(span_cost_id_map.keys())
                    )
                )
            )
            .mappings()
            .all()
        )
        for row in detail_rows:
            values = dict(row)
            values.pop("id")
            values["span_cost_id"] = span_cost_id_map[values["span_cost_id"]]
            await connection.execute(insert(dst[models.SpanCostDetail]).values(**values))

    await connection.execute(delete(src[models.Trace]).where(src[models.Trace].c.id == trace_rowid))


@contextlib.asynccontextmanager
async def schema_scoped_connection(
    engine: AsyncEngine, project_id: int
) -> AsyncIterator[AsyncConnection]:
    """Explicit, not ambient: opens a connection routed at exactly one
    project's schema (via `schema_translate_map`) with that project's role
    switched in. Core-level -- for ORM-level access, see
    `project_scoped_session`, which binds a real `AsyncSession` to a
    connection opened this way.

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


@contextlib.asynccontextmanager
async def project_scoped_session(
    db: "DbSessionFactory", project_id: int
) -> AsyncIterator[AsyncSession]:
    """Stage 4b-2d: the single place that decides whether ORM access for
    `project_id` goes through the project's own schema or the shared one,
    gated on `PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED`. Flag off (the
    default, and the only valid state before Stage 4b-2c's migration has
    run): behaves exactly like `db()`. Flag on: opens a
    `schema_scoped_connection` for `project_id` and binds a real ORM
    `AsyncSession` directly to that already-schema-translated connection --
    confirmed directly against real Postgres that a Session bound this way
    inherits the connection's `schema_translate_map` for every statement it
    issues, so existing ORM code (`insert_span`, the annotation mutations,
    etc.) needs no changes beyond being handed a session opened here
    instead of `db()`'s.

    Commit/rollback semantics mirror `db()`'s `Session.begin()`-based
    factory in `app.py`: commits if the block exits cleanly, rolls back
    otherwise. Implemented explicitly (not via `AsyncSession(...).begin()`)
    because `schema_scoped_connection`'s connection already has its own
    transaction open (needed to scope `SET LOCAL ROLE` to it) -- a second,
    nested `Session.begin()` on top would conflict with it; a plain
    `AsyncSession(bind=connection)` instead joins that existing transaction,
    and `session.commit()`/`.rollback()` end it directly, same as this
    module's own regression tests already exercise.

    **Sharp edge, flag on**: every statement issued through the returned
    session runs as the per-project role (`SET LOCAL ROLE`, switched in by
    `schema_scoped_connection`), not the caller's normal privileges -- not
    just the ones this helper redirects into the project schema. That role
    only has grants on the 9 project-scoped tables plus `SELECT` on
    `projects`/`users`/`generative_models` (see `provision_project_schema`).
    Writing any *other* shared table (e.g. `ExperimentRun`) through a
    session opened here fails with "permission denied" -- confirmed
    directly, not a hypothetical: `experiment_runner.py`'s `_persist_run`
    originally did exactly this in one session and had to be split into
    two. Do the shared-table write through a plain `db()` session instead,
    same as that fix and `_persist_eval_results`'s existing traces/
    annotations split.
    """
    if not get_env_project_scoped_storage_enabled():
        async with db() as session:
            yield session
        return
    assert db.engine is not None
    async with schema_scoped_connection(db.engine, project_id) as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@contextlib.asynccontextmanager
async def project_scoped_read_connection(
    db: "DbSessionFactory", project_id: int
) -> AsyncIterator[AsyncSession]:
    """Read-only counterpart to `project_scoped_session`, for dataloaders
    (`ProjectScopedTableFieldsDataLoader` and friends).

    Always yields a real `AsyncSession` -- **not** a bare `AsyncConnection`
    -- even in the flag-on path. A bare Core `AsyncConnection` executing
    `select(SomeORMModel)` (a whole-entity select, as opposed to a
    column-level one) does not hydrate ORM instances; it returns a `Row` of
    raw column values, since ORM hydration is a `Session`-level concern,
    not a Core one. Confirmed directly, not a hypothetical: a first version
    of this helper yielded the bare `AsyncConnection` from
    `schema_scoped_connection` and broke every dataloader that does a
    whole-entity `select(...)` (`span_by_id`, `span_annotations`,
    `document_evaluations`, etc.) with `AttributeError` on the returned
    rows -- only dataloaders selecting individual columns (like
    `table_fields.py`'s `_get_stmt`) happened to work, which is what let
    the bug slip past those loaders' own tests. Binding a plain
    `AsyncSession` to the connection (same technique as
    `project_scoped_session`) fixes both cases uniformly.

    Flag off: `db.read()` -- the existing read-replica-aware path,
    unchanged. Flag on: `schema_scoped_connection(db.engine, project_id)`
    -- **always hits the primary**, never a configured read replica, since
    `DbSessionFactory` doesn't expose a read-replica `AsyncEngine`. This is
    Stage 4b-2's own documented, accepted tradeoff for this stage (closing
    it -- a `DbSessionFactory.read_engine`/`.scoped_read()` accessor -- is
    a fast-follow, not a blocker); logged so it's observable in a
    replica-configured deployment, not a silent surprise.
    """
    if not get_env_project_scoped_storage_enabled():
        async with db.read() as session:
            yield session
        return
    assert db.engine is not None
    logger.info(
        "Project-scoped read for project %s routed through the primary engine "
        "(schema_scoped_connection has no read-replica variant)",
        project_id,
    )
    async with schema_scoped_connection(db.engine, project_id) as connection:
        yield AsyncSession(bind=connection, expire_on_commit=False)


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
