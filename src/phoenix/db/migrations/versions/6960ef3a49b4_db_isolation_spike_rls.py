"""DB-isolation spike: RLS + role-switching on spans/traces/projects

Revision ID: 6960ef3a49b4
Revises: 3419314c84b7
Create Date: 2026-08-21 00:00:00.000000

PROVISIONAL -- see the SSO/RBAC fork plan's "DB-isolation spike (B + A)"
section. This is Postgres-only (SQLite has no ROW LEVEL SECURITY / roles
concept, so this migration no-ops there) and lives on the
`db-isolation-spike` branch, not the main `rbac-fork` patch stack, until
architects confirm the isolation-mechanism decision.

Two independent layers, both fed by the same two session-scoped GUCs
(`app.bypass_rls`, `app.readable_project_ids`) that the modified session
factory in `app.py` sets via SET LOCAL on every transaction:

- A (RLS): FORCE ROW LEVEL SECURITY on projects/traces/spans, with a
  fail-closed policy -- a transaction that sets neither GUC sees nothing,
  not everything. `spans` has no direct project column (it's one hop via
  trace_rowid -> traces.project_rowid), so its policy uses a correlated
  EXISTS against traces.id/traces.project_rowid, both already indexed.
- B (role): a `phoenix_scoped` role, SET LOCAL ROLE'd for regular users.
  Honest limitation (documented in the plan): on this single-shared-table
  schema, GRANT is table-grained, so this role's grants can't be any
  narrower than what RLS already restricts -- its value here is
  defense-in-depth (an independent Postgres mechanism), not additional
  row-level restriction. That would change if this were ever paired with
  schema-per-project instead.

Simplification specific to this spike: GRANT phoenix_scoped TO current_user
assumes migrations run as the same role the app connects with (true for
this spike's single-connection-string setup). A real deployment with a
distinct migration role would need this granted explicitly to the app's
configured role name instead.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6960ef3a49b4"
down_revision: Union[str, None] = "3419314c84b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SCOPED_ROLE = "phoenix_scoped"

# Shared USING clause fragment: a transaction must explicitly set one of
# the two GUCs (bypass_rls, or a non-empty readable_project_ids) or it sees
# no rows -- fail-closed, not fail-open.
_BYPASS = "current_setting('app.bypass_rls', true) = 'true'"
_RESTRICTED = (
    "current_setting('app.readable_project_ids', true) IS NOT NULL "
    "AND current_setting('app.readable_project_ids', true) <> ''"
)
_READABLE_IDS = "string_to_array(current_setting('app.readable_project_ids', true), ',')::bigint[]"


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return

    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{_SCOPED_ROLE}') THEN
                CREATE ROLE {_SCOPED_ROLE} NOLOGIN;
            END IF;
        END
        $$;
        """
    )
    op.execute(f"GRANT {_SCOPED_ROLE} TO current_user")
    op.execute(f"GRANT SELECT ON projects, traces, spans TO {_SCOPED_ROLE}")

    for table in ("projects", "traces", "spans"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        f"""
        CREATE POLICY projects_isolation ON projects
        USING ({_BYPASS} OR ({_RESTRICTED} AND id = ANY({_READABLE_IDS})))
        """
    )
    op.execute(
        f"""
        CREATE POLICY traces_isolation ON traces
        USING ({_BYPASS} OR ({_RESTRICTED} AND project_rowid = ANY({_READABLE_IDS})))
        """
    )
    op.execute(
        f"""
        CREATE POLICY spans_isolation ON spans
        USING (
            {_BYPASS}
            OR (
                {_RESTRICTED}
                AND EXISTS (
                    SELECT 1 FROM traces
                    WHERE traces.id = spans.trace_rowid
                    AND traces.project_rowid = ANY({_READABLE_IDS})
                )
            )
        )
        """
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return

    op.execute("DROP POLICY IF EXISTS spans_isolation ON spans")
    op.execute("DROP POLICY IF EXISTS traces_isolation ON traces")
    op.execute("DROP POLICY IF EXISTS projects_isolation ON projects")
    for table in ("spans", "traces", "projects"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE SELECT ON projects, traces, spans FROM {_SCOPED_ROLE}")
    op.execute(f"REVOKE {_SCOPED_ROLE} FROM current_user")
    op.execute(f"DROP ROLE IF EXISTS {_SCOPED_ROLE}")
