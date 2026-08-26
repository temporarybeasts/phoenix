"""Verifies migration 21344763fd8b's baseline grants: phoenix_scoped can
reach non-project-scoped tables (previously zero access to anything outside
the 10 RLS-covered tables -- found by an integration test that actually drove
a full authenticated request and hit "permission denied for table
oauth2_clients" at login, before ever reaching MCP SQL).

Runs against a database migrated by real Alembic, same reasoning as
test_write_side_rls.py: the GRANT/ALTER DEFAULT PRIVILEGES DDL only exists in
migrations.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

import phoenix.db as db_pkg
from tests.unit.server.access.conftest import _run_alembic_downgrade, _run_alembic_upgrade

# `migrated_postgresql_engine` is a fixture defined in this package's
# conftest.py -- pytest discovers it automatically, no import needed.

pytestmark = pytest.mark.postgres_only


async def _restricted(conn: AsyncConnection) -> None:
    await conn.execute(text("SET LOCAL ROLE phoenix_scoped"))


async def test_phoenix_scoped_can_read_a_non_project_scoped_table(
    migrated_postgresql_engine: AsyncEngine,
) -> None:
    """`users` was never one of the 10 RLS-covered tables and has no project
    concept at all -- before this migration, phoenix_scoped had zero grants
    on it, so even an ordinary login-adjacent lookup by a non-admin user
    failed outright.
    """
    async with migrated_postgresql_engine.begin() as conn:
        await _restricted(conn)
        # Doesn't matter whether any rows exist -- a permission-denied error
        # is what this migration fixes, not row content.
        result = await conn.execute(text("SELECT count(*) FROM users"))
        assert result.scalar() is not None


async def test_downgrade_removes_blanket_access_but_keeps_project_scoped_tables_working(
    migrated_postgresql_engine: AsyncEngine,
) -> None:
    config_path = str(Path(db_pkg.__file__).parent / "alembic.ini")
    scripts_location = str(Path(db_pkg.__file__).parent / "migrations")
    alembic_cfg = Config(config_path)
    alembic_cfg.set_main_option("script_location", scripts_location)

    async with migrated_postgresql_engine.connect() as conn:
        await conn.run_sync(_run_alembic_downgrade, alembic_cfg, "225b4cdcd01a")
        await conn.commit()

    async with migrated_postgresql_engine.connect() as conn:
        async with conn.begin():
            await _restricted(conn)
            with pytest.raises(Exception, match="permission denied"):
                await conn.execute(text("SELECT count(*) FROM users"))

        # The narrower grants 225b4cdcd01a established must still work --
        # downgrading this migration alone must not also undo that one.
        async with conn.begin():
            await _restricted(conn)
            result = await conn.execute(text("SELECT count(*) FROM traces"))
            assert result.scalar() is not None

    async with migrated_postgresql_engine.connect() as conn:
        await conn.run_sync(_run_alembic_upgrade, alembic_cfg)
        await conn.commit()

    async with migrated_postgresql_engine.begin() as conn:
        await _restricted(conn)
        result = await conn.execute(text("SELECT count(*) FROM users"))
        assert result.scalar() is not None
