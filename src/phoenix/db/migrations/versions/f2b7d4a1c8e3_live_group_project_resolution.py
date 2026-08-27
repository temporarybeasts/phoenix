"""live group-project resolution -- replace idp group/project tables with users.idp_groups

Revision ID: f2b7d4a1c8e3
Revises: 21344763fd8b
Create Date: 2026-08-27 00:00:00.000000

Replaces the three-table structure from `3419314c84b7` (`idp_groups`,
`user_idp_group_memberships`, `project_grants`) with a single JSON column on
`users` holding the raw OIDC `groups` claim as of the user's most-recent
login. Project access is now computed live at resolution time (see
`phoenix.server.access.resolution`) from that list against the declarative
group->project mapping config, instead of being pre-materialized into
`project_grants` rows -- which fixes that table's additive-only sync (a
narrowed/removed mapping entry now takes effect without a new login) and its
staleness against newly created projects matching an already-held group's
glob.

Manual (non-IdP) per-user grants (`project_grants.user_id`, `source='manual'`)
are dropped along with the table: nothing in the codebase ever wrote a
`source='manual'` row (no mutation, endpoint, or UI exists for it), so this
removes unused schema, not working functionality. All project access now
flows only through IdP group claims.

Verified none of the three RLS migrations (`6960ef3a49b4`, `225b4cdcd01a`,
`21344763fd8b`) reference these three tables in any `CREATE POLICY`/`GRANT`
statement -- RLS only touches `projects`/`traces`/`spans` + the 7
annotation/session/cost tables, and the baseline-grants migration's blanket
`GRANT`/`ALTER DEFAULT PRIVILEGES` is table-name-agnostic. No RLS policy
edits are needed here. `DROP TABLE` auto-revokes any Postgres-granted
privileges on that table, so no explicit `REVOKE` either.
"""

from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import JSON
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.compiler import compiles

# revision identifiers, used by Alembic.
revision: str = "f2b7d4a1c8e3"
down_revision: Union[str, None] = "21344763fd8b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Migrations never import application code (`phoenix.db.models`) -- this
# reproduces the same cross-dialect JSON type inline, matching the existing
# precedent in e.g. `132d988c5bef_add_oauth2_authorization_server_tables.py`
# and `8a3764fe7f1a_change_jsonb_to_json_for_prompts.py`.
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


def upgrade() -> None:
    op.drop_table("project_grants")
    op.drop_table("user_idp_group_memberships")
    op.drop_table("idp_groups")
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("idp_groups", JSON_, server_default="[]", nullable=False))


def downgrade() -> None:
    # Lossy: raw group-name lists don't reconstruct into well-defined
    # idp_groups/membership rows, and no project_grants rows exist to
    # restore -- this redesign never persists them in the first place.
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("idp_groups")

    op.create_table(
        "idp_groups",
        sa.Column("id", _Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False, unique=True, index=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "user_idp_group_memberships",
        sa.Column(
            "user_id",
            _Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "idp_group_id",
            _Integer,
            sa.ForeignKey("idp_groups.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "idp_group_id",
        ),
    )

    op.create_table(
        "project_grants",
        sa.Column("id", _Integer, primary_key=True),
        sa.Column(
            "project_id",
            _Integer,
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            _Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "idp_group_id",
            _Integer,
            sa.ForeignKey("idp_groups.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("permission", sa.String, nullable=False),
        sa.Column(
            "source",
            sa.String,
            sa.CheckConstraint("source IN ('config', 'manual')", name="valid_project_grant_source"),
            nullable=False,
        ),
        sa.Column(
            "granted_by",
            _Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "(user_id IS NOT NULL) != (idp_group_id IS NOT NULL)",
            name="exactly_one_grant_subject",
        ),
    )
    op.create_index(
        "uq_project_grants_user",
        "project_grants",
        ["project_id", "user_id", "permission"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
        sqlite_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_project_grants_idp_group",
        "project_grants",
        ["project_id", "idp_group_id", "permission"],
        unique=True,
        postgresql_where=sa.text("idp_group_id IS NOT NULL"),
        sqlite_where=sa.text("idp_group_id IS NOT NULL"),
    )
