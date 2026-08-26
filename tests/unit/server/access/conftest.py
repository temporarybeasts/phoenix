from __future__ import annotations

import os
from secrets import token_hex
from typing import Any, AsyncIterator

import pytest
from pytest_postgresql.janitor import DatabaseJanitor
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from phoenix.db.engines import aio_postgresql_engine


@pytest.fixture(scope="function")
async def migrated_postgresql_engine(postgresql_proc: Any) -> AsyncIterator[AsyncEngine]:
    """A freshly created Postgres database migrated via real Alembic
    (`aio_postgresql_engine(..., migrate=True)`), not `create_all` -- needed
    by any test in this package that exercises DDL (GRANT/RLS/POLICY) that
    only migrations create, unlike `models.Base.metadata`.
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


def _run_alembic_downgrade(connection: Any, alembic_cfg: Any, revision: str) -> None:
    from alembic import command

    alembic_cfg.attributes["connection"] = connection
    command.downgrade(alembic_cfg, revision)


def _run_alembic_upgrade(connection: Any, alembic_cfg: Any) -> None:
    from alembic import command

    alembic_cfg.attributes["connection"] = connection
    command.upgrade(alembic_cfg, "head")
