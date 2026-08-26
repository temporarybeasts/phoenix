"""Verifies migration 225b4cdcd01a's write-side RLS: GRANT INSERT/UPDATE/
DELETE plus USING+WITH CHECK policies on all 9 project-scoped tables, not
just the original spike's 3 (projects/traces/spans, SELECT/USING only).

Runs against a database migrated by real Alembic (not `create_all` -- the
GRANT/RLS/POLICY DDL only exists in the migration, so `postgresql_engine`'s
template-based fixture, built via `create_all`, wouldn't have any of it).

Exercises the `SET LOCAL ROLE phoenix_scoped` + `set_config` sequence
directly (the same two calls `app.py`'s `_set_db_isolation_guards` makes),
rather than going through the ASGI middleware/ContextVar plumbing -- this
is a test of the migration's DDL, not of the request-scoped wiring that
sets those GUCs. That wiring (`context.py`'s `CurrentUserMiddleware`/
`current_user_var`, `app.py`'s `_set_db_isolation_guards`) has no
automated test coverage of its own yet -- a pre-existing gap from the
spike, unchanged by this migration, not something this file fills.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from secrets import token_hex
from typing import Any, AsyncIterator

import pytest
from pytest_postgresql.janitor import DatabaseJanitor
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from phoenix.db.engines import aio_postgresql_engine

pytestmark = pytest.mark.postgres_only

_NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="function")
async def migrated_postgresql_engine(postgresql_proc: Any) -> AsyncIterator[AsyncEngine]:
    """A freshly created Postgres database migrated via real Alembic
    (`aio_postgresql_engine(..., migrate=True)`), not `create_all` --
    needed here specifically because this suite tests DDL (GRANT/RLS/
    POLICY) that only migrations create, unlike `models.Base.metadata`.
    """
    dbname = f"phoenix_rls_test_{os.getpid()}_{token_hex(4)}"
    janitor = DatabaseJanitor(
        user=postgresql_proc.user,
        host=postgresql_proc.host,
        port=postgresql_proc.port,
        version=postgresql_proc.version,
        dbname=dbname,
        password=postgresql_proc.password or None,
    )
    janitor.init()
    url = URL.create(
        "postgresql+asyncpg",
        username=postgresql_proc.user,
        password=postgresql_proc.password or None,
        host=postgresql_proc.host,
        port=postgresql_proc.port,
        database=dbname,
    )
    engine = aio_postgresql_engine(url, migrate=True, log_migrations=False)
    yield engine
    await engine.dispose()
    janitor.drop()


async def _bypass(conn: AsyncConnection) -> None:
    await conn.execute(text("SELECT set_config('app.bypass_rls', 'true', true)"))


async def _restricted(conn: AsyncConnection, project_ids: list[int]) -> None:
    ids_csv = ",".join(str(i) for i in project_ids)
    await conn.execute(
        text("SELECT set_config('app.readable_project_ids', :ids, true)"), {"ids": ids_csv}
    )
    await conn.execute(text("SET LOCAL ROLE phoenix_scoped"))


async def _no_guc_but_scoped(conn: AsyncConnection) -> None:
    """Fail-closed case: switches into `phoenix_scoped` (so RLS actually
    applies -- the outer test connection is otherwise a superuser, which
    always bypasses RLS regardless of policy) without setting either GUC.
    """
    await conn.execute(text("SET LOCAL ROLE phoenix_scoped"))


class _Seed:
    def __init__(self, project_a: int, project_b: int, seeded: dict[str, dict[str, int]]) -> None:
        self.project_a = project_a
        self.project_b = project_b
        self.seeded = seeded  # table -> {"a": id_in_project_a, "b": id_in_project_b}


async def _seed(engine: AsyncEngine) -> _Seed:
    """Seeds one row per project across all 9 project-scoped tables plus
    their two join-depth categories, via a bypassing (superuser) session.
    """
    seeded: dict[str, dict[str, int]] = {}
    async with engine.begin() as conn:
        await _bypass(conn)

        project_ids = {}
        for label in ("a", "b"):
            project_ids[label] = await conn.scalar(
                text("INSERT INTO projects (name) VALUES (:name) RETURNING id"),
                {"name": f"project_{label}_{token_hex(4)}"},
            )

        for label in ("a", "b"):
            pid = project_ids[label]

            session_id = await conn.scalar(
                text(
                    "INSERT INTO project_sessions (session_id, project_id, start_time, end_time) "
                    "VALUES (:sid, :pid, :st, :et) RETURNING id"
                ),
                {"sid": f"sess-{label}-{token_hex(4)}", "pid": pid, "st": _NOW, "et": _NOW},
            )
            seeded.setdefault("project_sessions", {})[label] = session_id

            trace_id = await conn.scalar(
                text(
                    "INSERT INTO traces (project_rowid, trace_id, start_time, end_time) "
                    "VALUES (:pid, :tid, :st, :et) RETURNING id"
                ),
                {"pid": pid, "tid": f"trace-{label}-{token_hex(4)}", "st": _NOW, "et": _NOW},
            )
            seeded.setdefault("traces", {})[label] = trace_id

            span_id = await conn.scalar(
                text(
                    "INSERT INTO spans (trace_rowid, span_id, name, span_kind, start_time, "
                    "end_time, attributes, events, status_code, status_message, "
                    "cumulative_error_count, cumulative_llm_token_count_prompt, "
                    "cumulative_llm_token_count_completion) "
                    "VALUES (:trace_rowid, :sid, 'span', 'LLM', :st, :et, '{}', '[]', 'OK', '', "
                    "0, 0, 0) RETURNING id"
                ),
                {
                    "trace_rowid": trace_id,
                    "sid": f"span-{label}-{token_hex(4)}",
                    "st": _NOW,
                    "et": _NOW,
                },
            )
            seeded.setdefault("spans", {})[label] = span_id

            trace_annotation_id = await conn.scalar(
                text(
                    "INSERT INTO trace_annotations (trace_rowid, name, annotator_kind, "
                    "metadata, source) VALUES (:trace_rowid, 'note', 'HUMAN', '{}', 'APP') "
                    "RETURNING id"
                ),
                {"trace_rowid": trace_id},
            )
            seeded.setdefault("trace_annotations", {})[label] = trace_annotation_id

            span_annotation_id = await conn.scalar(
                text(
                    "INSERT INTO span_annotations (span_rowid, name, annotator_kind, "
                    "metadata, source) VALUES (:span_rowid, 'note', 'HUMAN', '{}', 'APP') "
                    "RETURNING id"
                ),
                {"span_rowid": span_id},
            )
            seeded.setdefault("span_annotations", {})[label] = span_annotation_id

            document_annotation_id = await conn.scalar(
                text(
                    "INSERT INTO document_annotations (span_rowid, document_position, name, "
                    "annotator_kind, metadata, source) VALUES (:span_rowid, 0, 'note', 'HUMAN', "
                    "'{}', 'APP') RETURNING id"
                ),
                {"span_rowid": span_id},
            )
            seeded.setdefault("document_annotations", {})[label] = document_annotation_id

            project_session_annotation_id = await conn.scalar(
                text(
                    "INSERT INTO project_session_annotations (project_session_id, name, "
                    "annotator_kind, metadata, source) VALUES (:sid, 'note', 'HUMAN', '{}', "
                    "'APP') RETURNING id"
                ),
                {"sid": session_id},
            )
            seeded.setdefault("project_session_annotations", {})[label] = (
                project_session_annotation_id
            )

            span_cost_id = await conn.scalar(
                text(
                    "INSERT INTO span_costs (span_rowid, trace_rowid, span_start_time) "
                    "VALUES (:span_rowid, :trace_rowid, :st) RETURNING id"
                ),
                {"span_rowid": span_id, "trace_rowid": trace_id, "st": _NOW},
            )
            seeded.setdefault("span_costs", {})[label] = span_cost_id

            span_cost_detail_id = await conn.scalar(
                text(
                    "INSERT INTO span_cost_details (span_cost_id, token_type, is_prompt) "
                    "VALUES (:span_cost_id, 'input', true) RETURNING id"
                ),
                {"span_cost_id": span_cost_id},
            )
            seeded.setdefault("span_cost_details", {})[label] = span_cost_detail_id

    return _Seed(project_ids["a"], project_ids["b"], seeded)


async def test_bypass_sees_and_writes_across_all_projects(
    migrated_postgresql_engine: AsyncEngine,
) -> None:
    seed = await _seed(migrated_postgresql_engine)
    async with migrated_postgresql_engine.begin() as conn:
        await _bypass(conn)
        count = await conn.scalar(text("SELECT count(*) FROM trace_annotations"))
        assert count == 2, "bypass_rls must see rows from every project, not just one"

        # A write that isn't in either project's own connection still succeeds under bypass.
        new_id = await conn.scalar(
            text(
                "INSERT INTO trace_annotations (trace_rowid, name, annotator_kind, metadata, "
                "source) VALUES (:trace_rowid, 'bypass-write', 'HUMAN', '{}', 'APP') "
                "RETURNING id"
            ),
            {"trace_rowid": seed.seeded["traces"]["b"]},
        )
        assert new_id is not None


async def test_restricted_role_writes_succeed_within_readable_project(
    migrated_postgresql_engine: AsyncEngine,
) -> None:
    seed = await _seed(migrated_postgresql_engine)
    async with migrated_postgresql_engine.begin() as conn:
        await _restricted(conn, [seed.project_a])

        # UPDATE (1 join: trace_annotations -> traces)
        result = await conn.execute(
            text("UPDATE trace_annotations SET label = 'updated' WHERE id = :id"),
            {"id": seed.seeded["trace_annotations"]["a"]},
        )
        assert result.rowcount == 1

        # DELETE (2 joins: document_annotations -> spans -> traces)
        result = await conn.execute(
            text("DELETE FROM document_annotations WHERE id = :id"),
            {"id": seed.seeded["document_annotations"]["a"]},
        )
        assert result.rowcount == 1

        # INSERT (direct column: project_sessions)
        new_session_id = await conn.scalar(
            text(
                "INSERT INTO project_sessions (session_id, project_id, start_time, end_time) "
                "VALUES (:sid, :pid, :st, :et) RETURNING id"
            ),
            {"sid": f"new-sess-{token_hex(4)}", "pid": seed.project_a, "st": _NOW, "et": _NOW},
        )
        assert new_session_id is not None


async def test_restricted_role_insert_outside_readable_project_rejected(
    migrated_postgresql_engine: AsyncEngine,
) -> None:
    seed = await _seed(migrated_postgresql_engine)
    async with migrated_postgresql_engine.connect() as conn:
        async with conn.begin():
            await _restricted(conn, [seed.project_a])
            with pytest.raises(Exception, match="row-level security"):
                await conn.execute(
                    text(
                        "INSERT INTO trace_annotations (trace_rowid, name, annotator_kind, "
                        "metadata, source) VALUES (:trace_rowid, 'illegal', 'HUMAN', '{}', "
                        "'APP')"
                    ),
                    {"trace_rowid": seed.seeded["traces"]["b"]},
                )


async def test_restricted_role_update_delete_outside_readable_project_is_noop(
    migrated_postgresql_engine: AsyncEngine,
) -> None:
    """Unlike INSERT, UPDATE/DELETE against a row outside the readable set
    isn't an error -- USING filters it out of the target set entirely, so
    it's silently a 0-row no-op. Confirmed the row is untouched afterward.
    """
    seed = await _seed(migrated_postgresql_engine)
    async with migrated_postgresql_engine.begin() as conn:
        await _restricted(conn, [seed.project_a])

        result = await conn.execute(
            text("UPDATE trace_annotations SET label = 'should-not-apply' WHERE id = :id"),
            {"id": seed.seeded["trace_annotations"]["b"]},
        )
        assert result.rowcount == 0

        result = await conn.execute(
            text("DELETE FROM span_annotations WHERE id = :id"),
            {"id": seed.seeded["span_annotations"]["b"]},
        )
        assert result.rowcount == 0

    async with migrated_postgresql_engine.begin() as conn:
        await _bypass(conn)
        label = await conn.scalar(
            text("SELECT label FROM trace_annotations WHERE id = :id"),
            {"id": seed.seeded["trace_annotations"]["b"]},
        )
        assert label is None, "project B's row must be untouched by project A's restricted session"
        still_exists = await conn.scalar(
            text("SELECT count(*) FROM span_annotations WHERE id = :id"),
            {"id": seed.seeded["span_annotations"]["b"]},
        )
        assert still_exists == 1


@pytest.mark.parametrize(
    "table",
    [
        "project_sessions",  # direct column
        "trace_annotations",  # 1 join, via traces
        "span_costs",  # 1 join, via traces
        "project_session_annotations",  # 1 join, via project_sessions
        "span_annotations",  # 2 joins, via spans -> traces
        "document_annotations",  # 2 joins, via spans -> traces
        "span_cost_details",  # 2 joins, via span_costs -> traces
    ],
)
async def test_restricted_role_select_isolates_across_every_join_depth(
    migrated_postgresql_engine: AsyncEngine, table: str
) -> None:
    seed = await _seed(migrated_postgresql_engine)
    async with migrated_postgresql_engine.begin() as conn:
        await _restricted(conn, [seed.project_a])
        rows = await conn.execute(text(f"SELECT id FROM {table}"))
        visible_ids = {row[0] for row in rows}
        assert visible_ids == {seed.seeded[table]["a"]}, (
            f"{table}: restricted role must see only project A's row, not project B's"
        )


async def test_no_guc_is_fail_closed_across_new_tables(
    migrated_postgresql_engine: AsyncEngine,
) -> None:
    seed = await _seed(migrated_postgresql_engine)
    async with migrated_postgresql_engine.connect() as conn:
        async with conn.begin():
            await _no_guc_but_scoped(conn)

            count = await conn.scalar(text("SELECT count(*) FROM project_sessions"))
            assert count == 0, "no GUC set at all must be fail-closed, not fail-open"

            count = await conn.scalar(text("SELECT count(*) FROM span_annotations"))
            assert count == 0

        async with conn.begin():
            await _no_guc_but_scoped(conn)
            with pytest.raises(Exception, match="row-level security"):
                await conn.execute(
                    text(
                        "INSERT INTO trace_annotations (trace_rowid, name, annotator_kind, "
                        "metadata, source) VALUES (:trace_rowid, 'illegal', 'HUMAN', '{}', "
                        "'APP')"
                    ),
                    {"trace_rowid": seed.seeded["traces"]["a"]},
                )


def _run_alembic_downgrade(connection: Any, alembic_cfg: Any, revision: str) -> None:
    from alembic import command

    alembic_cfg.attributes["connection"] = connection
    command.downgrade(alembic_cfg, revision)


def _run_alembic_upgrade(connection: Any, alembic_cfg: Any) -> None:
    from alembic import command

    alembic_cfg.attributes["connection"] = connection
    command.upgrade(alembic_cfg, "head")


async def test_migration_downgrade_upgrade_roundtrip(
    migrated_postgresql_engine: AsyncEngine,
) -> None:
    """`migrated_postgresql_engine` is already at head (upgrade is exercised
    by every other test in this module). This confirms `downgrade()` -- the
    one path nothing else here exercises -- actually undoes this migration
    cleanly (write grants gone, back to the spike's SELECT-only shape) and
    that re-upgrading restores exactly the working state proven above,
    rather than trusting `downgrade()` to be correct by symmetry with
    `upgrade()` alone.
    """
    from pathlib import Path

    from alembic.config import Config

    import phoenix.db as db_pkg

    config_path = str(Path(db_pkg.__file__).parent / "alembic.ini")
    scripts_location = str(Path(db_pkg.__file__).parent / "migrations")
    alembic_cfg = Config(config_path)
    alembic_cfg.set_main_option("script_location", scripts_location)

    seed = await _seed(migrated_postgresql_engine)

    async with migrated_postgresql_engine.connect() as conn:
        await conn.run_sync(_run_alembic_downgrade, alembic_cfg, "6960ef3a49b4")
        await conn.commit()

    # Downgraded: phoenix_scoped has no grants at all on the 7 new tables,
    # and no write grants on the original 3 -- every write, in or out of
    # the readable set, must now fail outright (a GRANT-level denial, not
    # an RLS USING/WITH CHECK filter).
    async with migrated_postgresql_engine.connect() as conn:
        async with conn.begin():
            await _restricted(conn, [seed.project_a])
            with pytest.raises(Exception, match="permission denied"):
                await conn.execute(
                    text("UPDATE trace_annotations SET label = 'x' WHERE id = :id"),
                    {"id": seed.seeded["trace_annotations"]["a"]},
                )
        async with conn.begin():
            await _restricted(conn, [seed.project_a])
            with pytest.raises(Exception, match="permission denied"):
                await conn.execute(
                    text("UPDATE traces SET trace_id = 'x' WHERE id = :id"),
                    {"id": seed.seeded["traces"]["a"]},
                )

    async with migrated_postgresql_engine.connect() as conn:
        await conn.run_sync(_run_alembic_upgrade, alembic_cfg)
        await conn.commit()

    # Re-upgraded: the exact scenario proven in
    # test_restricted_role_writes_succeed_within_readable_project works again.
    async with migrated_postgresql_engine.begin() as conn:
        await _restricted(conn, [seed.project_a])
        result = await conn.execute(
            text("UPDATE trace_annotations SET label = 'updated' WHERE id = :id"),
            {"id": seed.seeded["trace_annotations"]["a"]},
        )
        assert result.rowcount == 1
