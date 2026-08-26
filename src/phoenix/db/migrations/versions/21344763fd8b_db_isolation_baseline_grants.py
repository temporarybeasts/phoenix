"""DB-isolation: baseline grants for phoenix_scoped on non-project tables

Revision ID: 21344763fd8b
Revises: 225b4cdcd01a
Create Date: 2026-08-26 00:00:00.000000

Closes a gap found by actually driving a full authenticated request through
the real middleware stack, not by inspection: `_set_db_isolation_guards`
(`app.py`) runs `SET LOCAL ROLE phoenix_scoped` once for the *entire*
transaction the moment any non-admin user's session opens -- not scoped to
just the 10 project-scoped tables `6960ef3a49b4`/`225b4cdcd01a` granted.
Postgres defaults to deny, so any non-admin request touching any of the
other ~58 tables in the schema (confirmed concretely with `oauth2_clients`,
needed just to log in) got `permission denied` -- no non-admin user could do
anything at all under row-level isolation, independent of project scope.

Not an enumerated per-table audit, deliberately: GRANTs and RLS policies are
orthogonal in Postgres. GRANT decides whether a role can touch a table at
all; RLS policies (already in force on the 10 project-scoped tables from the
two migrations above) decide which rows, regardless of how broad the GRANT
is. Widening `phoenix_scoped`'s grants everywhere else does not loosen
project isolation on the tables that actually enforce it. The real
who-can-do-what boundary was always meant to live at the app layer
(`IsAdmin`/`IsNotViewer` in `server/api/auth.py`) and, for the one surface
that bypasses app-layer checks, MCP SQL's own independent table/column
allowlist (`server/mcp/sql/allowlist.py`) -- neither changes based on how
broad `phoenix_scoped`'s Postgres grants are. Hand-picking "which other
tables a member needs" is exactly the kind of enumeration that produced this
gap in the first place.

`ALTER DEFAULT PRIVILEGES` covers tables/sequences created by future
migrations (run as the same connecting role), so a new table doesn't
silently reopen this same gap.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "21344763fd8b"
down_revision: Union[str, None] = "225b4cdcd01a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SCOPED_ROLE = "phoenix_scoped"

# The narrower grants 225b4cdcd01a already established, reproduced here only
# so downgrade() can restore them after this migration's blanket REVOKE ALL
# removes everything -- not duplicated logic, just enough to undo cleanly.
_EXISTING_TABLES = ("projects", "traces", "spans")
_NEW_TABLES_FROM_PRIOR_MIGRATION = (
    "project_sessions",
    "trace_annotations",
    "span_costs",
    "project_session_annotations",
    "span_annotations",
    "document_annotations",
    "span_cost_details",
)


def _current_schema(connection: sa.engine.Connection) -> str:
    schema = connection.execute(sa.text("SELECT current_schema()")).scalar()
    assert schema is not None
    return str(schema)


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return

    schema = _current_schema(connection)
    # USAGE on the schema itself is a hard prerequisite in Postgres for
    # reaching anything inside it, independent of table-level grants -- the
    # public schema grants this to PUBLIC by default (Postgres <15, what
    # this project's CI runs), which is why this was never hit against the
    # default schema; a deployment configuring PHOENIX_SQL_DATABASE_SCHEMA
    # to a non-public schema has no such default. Found only by driving a
    # real request through a real custom-schema deployment, not by reading
    # the code -- the same class of gap as the table grants above.
    op.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {_SCOPED_ROLE}')
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" TO {_SCOPED_ROLE}'
    )
    op.execute(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT SELECT, INSERT, UPDATE, DELETE '
        f"ON TABLES TO {_SCOPED_ROLE}"
    )
    op.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" TO {_SCOPED_ROLE}')
    op.execute(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT USAGE, SELECT ON SEQUENCES '
        f"TO {_SCOPED_ROLE}"
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return

    schema = _current_schema(connection)
    # Schema USAGE is deliberately not revoked here -- the narrower grants
    # restored below (225b4cdcd01a's) need it to be reachable at all, the
    # same way this migration's own upgrade() needed it. Only public-schema
    # deployments ever had it for free; anywhere else, revoking it would
    # silently reintroduce the exact gap 225b4cdcd01a's grants rely on not
    # having.
    op.execute(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" REVOKE SELECT, INSERT, UPDATE, DELETE '
        f"ON TABLES FROM {_SCOPED_ROLE}"
    )
    op.execute(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" REVOKE USAGE, SELECT ON SEQUENCES '
        f"FROM {_SCOPED_ROLE}"
    )
    op.execute(
        f'REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" '
        f"FROM {_SCOPED_ROLE}"
    )
    op.execute(f'REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" FROM {_SCOPED_ROLE}')

    # The blanket REVOKE above also removes the narrower grants
    # 225b4cdcd01a established -- restore them so downgrading this
    # migration alone doesn't also undo that one.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON projects, traces, spans TO {_SCOPED_ROLE}")
    op.execute(
        f"GRANT USAGE, SELECT ON SEQUENCE projects_id_seq, traces_id_seq, spans_id_seq "
        f"TO {_SCOPED_ROLE}"
    )
    for table in _NEW_TABLES_FROM_PRIOR_MIGRATION:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_SCOPED_ROLE}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {_SCOPED_ROLE}")
