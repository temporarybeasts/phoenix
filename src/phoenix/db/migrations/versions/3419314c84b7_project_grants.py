"""project grants (idp_groups, user_idp_group_memberships, project_grants)

Revision ID: 3419314c84b7
Revises: 4aad9107d196
Create Date: 2026-08-21 00:00:00.000000

Fork-only schema for the SSO/RBAC fork plan's Stage 4a: "which
users/IdP-groups can access which projects" -- no query enforcement reads
these tables yet (that's Stage 4b), so this migration is purely additive.

- idp_groups: an IdP-synced group (one value of an OIDC ``groups`` claim).
- user_idp_group_memberships: current membership, replaced wholesale on
  every OIDC login (mirrors existing role-resync semantics).
- project_grants: grants project-scoped access to a user or an IdP group
  (exactly one of user_id/idp_group_id set per row). Two partial unique
  indexes rather than one combined UniqueConstraint, since Postgres/SQLite
  both treat NULL as distinct-from-NULL for uniqueness purposes.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3419314c84b7"
down_revision: Union[str, None] = "4aad9107d196"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_Integer = sa.Integer().with_variant(
    sa.BigInteger(),
    "postgresql",
)


def upgrade() -> None:
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
            # index on the second element of the composite primary key
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


def downgrade() -> None:
    op.drop_table("project_grants")
    op.drop_table("user_idp_group_memberships")
    op.drop_table("idp_groups")
