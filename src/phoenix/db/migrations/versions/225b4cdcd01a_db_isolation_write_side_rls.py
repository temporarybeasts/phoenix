"""DB-isolation: write-side RLS (WITH CHECK) + remaining project-scoped tables

Revision ID: 225b4cdcd01a
Revises: 6960ef3a49b4
Create Date: 2026-08-26 00:00:00.000000

Closes the write side the original DB-isolation spike (6960ef3a49b4) left
open -- see the SSO/RBAC fork plan's "Row-level isolation -- write-side RLS
(WITH CHECK)" section. Postgres-only, no-ops on SQLite, same as the spike
migration.

Two decisions made there, not guessed, both implemented here:

1. Table scope widens from the spike's 3 tables (projects/traces/spans) to
   all 9 project-scoped tables (matching the archived schema-per-project
   work's own scope decision): the spike's 2 project-scoped tables (traces,
   spans; `projects` is the root/catalog table, not itself "project-scoped")
   plus the 7 not yet covered -- project_sessions, span_annotations,
   trace_annotations, document_annotations, project_session_annotations,
   span_costs, span_cost_details. Without this, `phoenix_scoped` has zero
   grants on any of the 7, so a non-admin user can't even SELECT an
   annotation today, let alone write one.
2. Write permission reuses the same `readable_project_ids` GUC already set
   for reads (no new PROJECT_WRITE permission) -- matches how the app layer
   already draws no per-project read/write distinction (`IsNotViewer` is a
   single global MEMBER-tier gate, not project-scoped). `phoenix_scoped`
   gets GRANT INSERT/UPDATE/DELETE (not just SELECT) on all 9 tables, and
   every policy gets an explicit WITH CHECK mirroring its USING clause --
   for INSERT/UPDATE, WITH CHECK governs the *new* row; for DELETE, only
   USING applies (Postgres has no WITH CHECK concept for row removal).
   Explicit even though a FOR ALL policy with no WITH CHECK already
   defaults to reusing USING, for clarity and to make the intent undeniable
   in the DDL itself.

Each of the 7 new tables' project-derivation path was read directly off
`db/models.py`, not assumed -- direct column for project_sessions
(`project_id`), one join for spans/trace_annotations/span_costs (all carry
`trace_rowid` straight to `traces.id`), two joins for
span_annotations/document_annotations (`span_rowid` -> spans.trace_rowid ->
traces.id) and span_cost_details (`span_cost_id` -> span_costs.trace_rowid
-> traces.id), and project_session_annotations via `project_session_id` ->
project_sessions.project_id. The same per-table join shapes the archived
schema-per-project migration script (`migrate_to_project_scoped_schemas.py`)
already worked out and tested for its own project-scoping subqueries.

Cascading deletes (e.g. a trace delete cascading to its spans/annotations
via ON DELETE CASCADE) run under the referenced table's owner privileges,
not the deleting role's -- Postgres bypasses row security for FK-triggered
referential-integrity actions specifically, so this migration doesn't need
to do anything special for cascades to keep working; the new grants/policies
here are for *direct* writes to each table (e.g. a PATCH/DELETE annotation
mutation), which is the actual gap found while scoping this.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "225b4cdcd01a"
down_revision: Union[str, None] = "6960ef3a49b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SCOPED_ROLE = "phoenix_scoped"

_BYPASS = "current_setting('app.bypass_rls', true) = 'true'"
_RESTRICTED = (
    "current_setting('app.readable_project_ids', true) IS NOT NULL "
    "AND current_setting('app.readable_project_ids', true) <> ''"
)
_READABLE_IDS = "string_to_array(current_setting('app.readable_project_ids', true), ',')::bigint[]"

# table -> its own column to check against readable_project_ids directly.
# Covers the 2 pre-existing direct-column tables (projects, traces -- same
# predicates as migration 6960ef3a49b4, reproduced here so downgrade() can
# restore them exactly) plus the 1 new one (project_sessions).
_DIRECT_COLUMN_TABLES = {
    "projects": "id",
    "traces": "project_rowid",
    "project_sessions": "project_id",
}
# table -> EXISTS-subquery SQL (fully formed, references _BYPASS/_RESTRICTED
# /_READABLE_IDS directly so each is self-contained). Covers the 1
# pre-existing joined table (spans -- same predicate as 6960ef3a49b4) plus
# the 6 new ones.
_JOINED_TABLE_PREDICATES = {
    "spans": """
        EXISTS (
            SELECT 1 FROM traces
            WHERE traces.id = spans.trace_rowid
            AND traces.project_rowid = ANY({ids})
        )
    """,
    "trace_annotations": """
        EXISTS (
            SELECT 1 FROM traces
            WHERE traces.id = trace_annotations.trace_rowid
            AND traces.project_rowid = ANY({ids})
        )
    """,
    "span_costs": """
        EXISTS (
            SELECT 1 FROM traces
            WHERE traces.id = span_costs.trace_rowid
            AND traces.project_rowid = ANY({ids})
        )
    """,
    "project_session_annotations": """
        EXISTS (
            SELECT 1 FROM project_sessions
            WHERE project_sessions.id = project_session_annotations.project_session_id
            AND project_sessions.project_id = ANY({ids})
        )
    """,
    "span_annotations": """
        EXISTS (
            SELECT 1 FROM spans
            JOIN traces ON traces.id = spans.trace_rowid
            WHERE spans.id = span_annotations.span_rowid
            AND traces.project_rowid = ANY({ids})
        )
    """,
    "document_annotations": """
        EXISTS (
            SELECT 1 FROM spans
            JOIN traces ON traces.id = spans.trace_rowid
            WHERE spans.id = document_annotations.span_rowid
            AND traces.project_rowid = ANY({ids})
        )
    """,
    "span_cost_details": """
        EXISTS (
            SELECT 1 FROM span_costs
            JOIN traces ON traces.id = span_costs.trace_rowid
            WHERE span_costs.id = span_cost_details.span_cost_id
            AND traces.project_rowid = ANY({ids})
        )
    """,
}

_EXISTING_TABLES = ("projects", "traces", "spans")
# The 7 project-scoped tables not yet covered by 6960ef3a49b4 -- explicit,
# not derived from the predicate dicts above, since those dicts also carry
# projects/traces/spans' predicates (reused by downgrade() to restore the
# originals exactly).
_NEW_TABLES = (
    "project_sessions",
    "trace_annotations",
    "span_costs",
    "project_session_annotations",
    "span_annotations",
    "document_annotations",
    "span_cost_details",
)


def _policy_expr(table: str) -> str:
    if table in _DIRECT_COLUMN_TABLES:
        column = _DIRECT_COLUMN_TABLES[table]
        restricted = f"{_RESTRICTED} AND {table}.{column} = ANY({_READABLE_IDS})"
    else:
        restricted = f"{_RESTRICTED} AND " + _JOINED_TABLE_PREDICATES[table].format(
            ids=_READABLE_IDS
        )
    return f"({_BYPASS} OR ({restricted}))"


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return

    # 1. Existing 3 tables: add write grants + an explicit WITH CHECK
    #    (previously SELECT-only, USING-only). Sequence grants are required
    #    too, not just the table grants -- an INSERT relying on the `id`
    #    column's default (nextval on `<table>_id_seq`) fails with
    #    "permission denied for sequence" without USAGE on it, the same
    #    class of gap the archived schema-per-project work already hit and
    #    fixed once (its own Stage 4b-2b) -- found again here directly by
    #    running the new tests against real Postgres, not assumed away.
    op.execute(f"GRANT INSERT, UPDATE, DELETE ON projects, traces, spans TO {_SCOPED_ROLE}")
    op.execute(
        f"GRANT USAGE, SELECT ON SEQUENCE projects_id_seq, traces_id_seq, spans_id_seq "
        f"TO {_SCOPED_ROLE}"
    )
    op.execute(
        f"ALTER POLICY projects_isolation ON projects WITH CHECK ({_policy_expr('projects')})"
    )
    op.execute(f"ALTER POLICY traces_isolation ON traces WITH CHECK ({_policy_expr('traces')})")
    op.execute(f"ALTER POLICY spans_isolation ON spans WITH CHECK ({_policy_expr('spans')})")

    # 2. The 7 not-yet-covered project-scoped tables: full grants + RLS +
    #    USING/WITH CHECK policy, same fail-closed shape as the spike.
    for table in _NEW_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_SCOPED_ROLE}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {_SCOPED_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        expr = _policy_expr(table)
        op.execute(
            f"""
            CREATE POLICY {table}_isolation ON {table}
            USING ({expr})
            WITH CHECK ({expr})
            """
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return

    for table in reversed(_NEW_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE USAGE, SELECT ON SEQUENCE {table}_id_seq FROM {_SCOPED_ROLE}")
        op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {table} FROM {_SCOPED_ROLE}")

    # Restore the original USING-only policies (no WITH CHECK) and revoke
    # the write grants added above, back to the spike's original SELECT-only
    # shape. Postgres has no "drop the WITH CHECK clause" form of ALTER
    # POLICY -- recreate each policy from scratch instead, matching
    # 6960ef3a49b4's original USING-only definitions exactly.
    for table in _EXISTING_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY {table}_isolation ON {table}
            USING ({_policy_expr(table)})
            """
        )
    op.execute(
        f"REVOKE USAGE, SELECT ON SEQUENCE projects_id_seq, traces_id_seq, spans_id_seq "
        f"FROM {_SCOPED_ROLE}"
    )
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON projects, traces, spans FROM {_SCOPED_ROLE}")
