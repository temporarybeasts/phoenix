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

from datetime import datetime, timezone
from secrets import token_hex

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.unit.server.access.conftest import _run_alembic_downgrade, _run_alembic_upgrade

# `migrated_postgresql_engine` is a fixture defined in this package's
# conftest.py -- pytest discovers it automatically for every test file
# here, no import needed (importing it would also shadow it via every
# test's same-named parameter, tripping ruff's F811).

pytestmark = pytest.mark.postgres_only

_NOW = datetime.now(timezone.utc)


async def _bypass(conn: AsyncConnection) -> None:
    await conn.execute(text("SELECT set_config('app.bypass_rls', 'true', true)"))


async def _restricted(
    conn: AsyncConnection,
    project_ids: list[int],
    writable_project_group_ids: "list[int] | None" = None,
) -> None:
    ids_csv = ",".join(str(i) for i in project_ids)
    await conn.execute(
        text("SELECT set_config('app.readable_project_ids', :ids, true)"), {"ids": ids_csv}
    )
    group_ids_csv = ",".join(str(i) for i in (writable_project_group_ids or []))
    await conn.execute(
        text("SELECT set_config('app.writable_project_group_ids', :ids, true)"),
        {"ids": group_ids_csv},
    )
    await conn.execute(text("SET LOCAL ROLE phoenix_scoped"))


async def _no_guc_but_scoped(conn: AsyncConnection) -> None:
    """Fail-closed case: switches into `phoenix_scoped` (so RLS actually
    applies -- the outer test connection is otherwise a superuser, which
    always bypasses RLS regardless of policy) without setting either GUC.
    """
    await conn.execute(text("SET LOCAL ROLE phoenix_scoped"))


class _Seed:
    def __init__(
        self,
        project_a: int,
        project_b: int,
        group_a: int,
        group_b: int,
        seeded: dict[str, dict[str, int]],
    ) -> None:
        self.project_a = project_a
        self.project_b = project_b
        self.group_a = group_a
        self.group_b = group_b
        self.seeded = seeded  # table -> {"a": id_in_project_a, "b": id_in_project_b}


async def _seed(engine: AsyncEngine) -> _Seed:
    """Seeds one row per project across all 9 project-scoped tables plus
    their two join-depth categories, via a bypassing (superuser) session.
    Each project gets its own project group (project A -> group A, project
    B -> group B), so tests can exercise group-scoped writable checks
    without the two projects being interchangeable.
    """
    seeded: dict[str, dict[str, int]] = {}
    async with engine.begin() as conn:
        await _bypass(conn)

        group_ids = {}
        for label in ("a", "b"):
            group_ids[label] = await conn.scalar(
                text("INSERT INTO project_groups (name) VALUES (:name) RETURNING id"),
                {"name": f"group_{label}_{token_hex(4)}"},
            )

        project_ids = {}
        for label in ("a", "b"):
            project_ids[label] = await conn.scalar(
                text(
                    "INSERT INTO projects (name, project_group_id) "
                    "VALUES (:name, :group_id) RETURNING id"
                ),
                {"name": f"project_{label}_{token_hex(4)}", "group_id": group_ids[label]},
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

    return _Seed(project_ids["a"], project_ids["b"], group_ids["a"], group_ids["b"], seeded)


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


async def test_restricted_role_can_insert_new_project_into_writable_group(
    migrated_postgresql_engine: AsyncEngine,
) -> None:
    """Regression test for the bug the `projects_isolation` WITH CHECK fix
    (migration acd16dbc13d0) addresses: a brand-new project row's own `id`
    can never already be a member of `app.readable_project_ids` (that GUC
    reflects *pre-existing* readable projects), so the original WITH CHECK
    (`id = ANY(readable_project_ids)`) rejected every non-admin INSERT into
    `projects`, untested until now. The fixed policy checks the new row's
    `project_group_id` against `app.writable_project_group_ids` instead.
    """
    seed = await _seed(migrated_postgresql_engine)
    async with migrated_postgresql_engine.begin() as conn:
        await _restricted(conn, [seed.project_a], writable_project_group_ids=[seed.group_a])
        new_project_id = await conn.scalar(
            text(
                "INSERT INTO projects (name, project_group_id) "
                "VALUES (:name, :group_id) RETURNING id"
            ),
            {"name": f"new_project_{token_hex(4)}", "group_id": seed.group_a},
        )
        assert new_project_id is not None


async def test_restricted_role_insert_project_outside_writable_group_rejected(
    migrated_postgresql_engine: AsyncEngine,
) -> None:
    seed = await _seed(migrated_postgresql_engine)
    async with migrated_postgresql_engine.connect() as conn:
        async with conn.begin():
            # Readable (so this isn't conflated with the readable-ids gate)
            # but not in the writable-groups GUC -- e.g. a VIEWER-only
            # membership, which get_writable_project_group_ids never
            # includes.
            await _restricted(
                conn, [seed.project_a, seed.project_b], writable_project_group_ids=[seed.group_a]
            )
            with pytest.raises(Exception, match="row-level security"):
                await conn.execute(
                    text("INSERT INTO projects (name, project_group_id) VALUES (:name, :group_id)"),
                    {"name": f"illegal_project_{token_hex(4)}", "group_id": seed.group_b},
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
