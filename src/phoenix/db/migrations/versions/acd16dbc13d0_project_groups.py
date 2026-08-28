"""project groups -- persisted external-role-to-group config, project grouping

Revision ID: acd16dbc13d0
Revises: 21344763fd8b
Create Date: 2026-08-27 00:00:00.000000

Replaces the (removed) YAML-file/glob-name-matching config with two tables:

- ``project_groups``: the grouping unit projects belong to (every project
  belongs to exactly one group -- no "ungrouped" state).
- ``external_role_project_group_mappings``: declarative config, persisted in
  the database (not naming convention), mapping a raw external role (an IdP
  ``groups`` claim value) to a ``(project_group, role)`` pair. Maintained by
  an onboarding process external to Phoenix.

Also re-adds ``users.idp_groups`` (previously introduced, then dropped
along with this branch's now-deleted YAML-based design) and fixes two gaps
in the write-side RLS policy on ``projects``, both stemming from the same
root cause -- a not-yet-existing row's own ``id`` can never already be a
member of the pre-computed ``app.readable_project_ids`` GUC:

1. ``WITH CHECK`` checked the new row's own id against
   ``app.readable_project_ids``, so any non-admin ``INSERT INTO projects``
   under ``phoenix_scoped`` would always be rejected. Replaced with a check
   against the new row's ``project_group_id`` and a new
   ``app.writable_project_group_ids`` GUC (set by `app.py`'s
   ``_set_db_isolation_guards`` alongside the existing read-side GUC).
2. Even with (1) fixed, ``INSERT ... RETURNING id`` (what the SQLAlchemy
   ORM issues) still failed: Postgres also enforces the policy's ``USING``
   (SELECT-side) clause before returning an inserted row, and the same
   not-yet-existing-id problem applies there too. Fixed by OR-ing the same
   group-based check into ``USING`` as well -- not a widening of access,
   since every writable role (``MEMBER``/``ADMIN``) already includes
   ``PROJECT_READ`` in ``PROJECT_ROLE_PERMISSIONS``; this only makes that
   read access available one transaction earlier than the id-based check
   alone would.

Since this branch was never deployed, this migration also serves as the
replacement for two deleted migrations (`3419314c84b7_project_grants.py`,
`f2b7d4a1c8e3_live_group_project_resolution.py`) that built and then
discarded an earlier, YAML/glob-based design -- no data from either exists
anywhere, so there is nothing to migrate forward from them.
"""

from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import JSON
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.compiler import compiles

# revision identifiers, used by Alembic.
revision: str = "acd16dbc13d0"
down_revision: Union[str, None] = "21344763fd8b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Migrations never import application code (`phoenix.db.models`) -- this
# reproduces the same cross-dialect JSON type inline, matching the existing
# precedent in e.g. `132d988c5bef_add_oauth2_authorization_server_tables.py`.
class JSONB(JSON):
    __visit_name__ = "JSONB"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(*args: Any, **kwargs: Any) -> str:
    return "JSONB"


JSON_ = (
    JSON()
    .with_variant(
        postgresql.JSONB(),
        "postgresql",
    )
    .with_variant(
        JSONB(),
        "sqlite",
    )
)

_Integer = sa.Integer().with_variant(
    sa.BigInteger(),
    "postgresql",
)

_TIMESTAMP = sa.TIMESTAMP(timezone=True)

# Mirrors `phoenix.config.DEFAULT_PROJECT_GROUP_NAME` -- migrations never
# import application code, so this is a duplicated literal, not a shared
# constant. Every pre-existing/auto-created project is backfilled into this
# group.
_DEFAULT_PROJECT_GROUP_NAME = "default"

_SCOPED_ROLE = "phoenix_scoped"
_BYPASS = "current_setting('app.bypass_rls', true) = 'true'"
_READ_RESTRICTED = (
    "current_setting('app.readable_project_ids', true) IS NOT NULL "
    "AND current_setting('app.readable_project_ids', true) <> ''"
)
_READABLE_IDS = "string_to_array(current_setting('app.readable_project_ids', true), ',')::bigint[]"
_WRITE_RESTRICTED = (
    "current_setting('app.writable_project_group_ids', true) IS NOT NULL "
    "AND current_setting('app.writable_project_group_ids', true) <> ''"
)
_WRITABLE_GROUP_IDS = (
    "string_to_array(current_setting('app.writable_project_group_ids', true), ',')::bigint[]"
)


def upgrade() -> None:
    connection = op.get_bind()
    is_postgresql = connection.dialect.name == "postgresql"

    op.create_table(
        "project_groups",
        sa.Column("id", _Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False, unique=True, index=True),
        sa.Column("description", sa.String, nullable=True),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            _TIMESTAMP,
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_table(
        "external_role_project_group_mappings",
        sa.Column("id", _Integer, primary_key=True),
        sa.Column("external_role", sa.String, nullable=False, unique=True, index=True),
        sa.Column(
            "project_group_id",
            _Integer,
            sa.ForeignKey("project_groups.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "role",
            sa.String,
            sa.CheckConstraint(
                "role IN ('VIEWER', 'MEMBER', 'ADMIN')", name="valid_project_group_role"
            ),
            nullable=False,
        ),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            _TIMESTAMP,
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )

    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("idp_groups", JSON_, server_default="[]", nullable=False))

    connection.execute(
        sa.text("INSERT INTO project_groups (name) VALUES (:name)"),
        {"name": _DEFAULT_PROJECT_GROUP_NAME},
    )
    default_group_id = connection.execute(
        sa.text("SELECT id FROM project_groups WHERE name = :name"),
        {"name": _DEFAULT_PROJECT_GROUP_NAME},
    ).scalar_one()

    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "project_group_id",
                _Integer,
                sa.ForeignKey("project_groups.id", ondelete="RESTRICT"),
                nullable=True,
            ),
        )
    connection.execute(
        sa.text("UPDATE projects SET project_group_id = :id WHERE project_group_id IS NULL"),
        {"id": default_group_id},
    )
    with op.batch_alter_table("projects") as batch_op:
        batch_op.alter_column("project_group_id", nullable=False)
    op.create_index("ix_projects_project_group_id", "projects", ["project_group_id"])

    if not is_postgresql:
        return

    # Fix the projects_isolation WITH CHECK (finding: a new row's own id
    # can never already be in app.readable_project_ids, so this previously
    # rejected every non-admin INSERT). Postgres-only; must run after the
    # project_group_id column above exists.
    op.execute(
        f"""
        ALTER POLICY projects_isolation ON projects
        USING (
            {_BYPASS}
            OR ({_READ_RESTRICTED} AND id = ANY({_READABLE_IDS}))
            OR ({_WRITE_RESTRICTED} AND project_group_id = ANY({_WRITABLE_GROUP_IDS}))
        )
        WITH CHECK (
            {_BYPASS}
            OR ({_WRITE_RESTRICTED} AND project_group_id = ANY({_WRITABLE_GROUP_IDS}))
        )
        """
    )
    # The USING widening above (an OR'd-in group-based clause) is required
    # for `INSERT ... RETURNING id` to work under `phoenix_scoped`: Postgres
    # enforces a row's SELECT-visibility (the policy's USING clause, not
    # just WITH CHECK) before returning it, and `app.readable_project_ids`
    # is a snapshot taken once at the start of the transaction -- a
    # brand-new row's own id can never already be in it. Granting SELECT
    # visibility into a project group the caller can already *write* to is
    # not a widening of access: every writable role (MEMBER/ADMIN) already
    # includes PROJECT_READ in `PROJECT_ROLE_PERMISSIONS`, so this only
    # makes read access available a transaction earlier than the
    # id-based check alone would.


def downgrade() -> None:
    connection = op.get_bind()
    is_postgresql = connection.dialect.name == "postgresql"

    if is_postgresql:
        op.execute(
            f"""
            ALTER POLICY projects_isolation ON projects
            USING ({_BYPASS} OR ({_READ_RESTRICTED} AND id = ANY({_READABLE_IDS})))
            WITH CHECK ({_BYPASS} OR ({_READ_RESTRICTED} AND id = ANY({_READABLE_IDS})))
            """
        )

    op.drop_index("ix_projects_project_group_id", table_name="projects")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("project_group_id")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("idp_groups")
    op.drop_table("external_role_project_group_mappings")
    op.drop_table("project_groups")
