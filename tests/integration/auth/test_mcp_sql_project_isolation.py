"""End-to-end proof that row-level isolation (RLS) actually reaches MCP SQL.

The row-level spike (``db-isolation-spike``, commit ``dc49c4de9``) and its
write-side follow-up were validated at the unit level against the migration's
own DDL (``tests/unit/server/access/test_write_side_rls.py``), and by reading
the code path ``executeSql`` -> ``execute_analytics_sql`` -> ``db.read()`` ->
``app.py``'s ``_set_db_isolation_guards``. Neither of those actually drives a
real MCP tool call over the real protocol, through real OAuth, against a real
running server -- which is the specific claim this file checks: that a member
granted access to one project cannot see another project's rows through
``executeSql``, with zero extra code beyond the RLS migrations themselves.

Postgres-only (RLS does not exist on SQLite) -- skipped when the whole
integration run is on SQLite, the same way every other test in this suite
defers to ``CI_TEST_DB_BACKEND`` rather than forcing its own backend. CI's
``[sqlite, postgresql]`` matrix is what gives this real coverage.

Runs against the package-scoped ``_app`` used throughout this package (see
``test_mcp.py``), and reuses its OAuth/MCP-transport helpers rather than
duplicating them.
"""

from __future__ import annotations

import json
import os
from secrets import token_hex
from typing import Any

import pytest
from fastmcp import Client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from strawberry.relay import GlobalID

from phoenix.config import ENV_PHOENIX_SQL_DATABASE_SCHEMA, ENV_PHOENIX_SQL_DATABASE_URL
from phoenix.db.engines import get_async_db_url
from tests.integration._helpers import (
    _ADMIN,
    _MEMBER,
    _AppInfo,
    _GetUser,
    _insert_spans,
    _User,
)

from .test_mcp import _base_url, _mcp_transport, _register_public_client

pytestmark = pytest.mark.skipif(
    os.getenv("CI_TEST_DB_BACKEND", "sqlite").lower() != "postgresql",
    reason="row-level isolation (RLS) is Postgres-only",
)


async def _grant_project_access(app: _AppInfo, *, user_id: int, groups: list[str]) -> None:
    """Sets ``users.idp_groups`` directly against the running app's own
    database -- standing in for what a real OIDC login would populate,
    without standing up a live IdP. The package-scoped app's
    ``PHOENIX_ACCESS_CONTROL_GROUP_MAPPING_FILE`` (see
    ``_env_access_control`` in conftest.py) maps these group names to the
    ``mcp-sql-test-project-{a,b}-*`` project globs the ``_two_projects``
    fixture below names its projects with, so project access is resolved
    live from this list -- there is no ``project_grants`` row to insert
    anymore. Wholesale replace, matching ``sync_idp_groups``'s own
    semantics.
    """
    sql_database_url = app.env[ENV_PHOENIX_SQL_DATABASE_URL]
    schema = app.env.get(ENV_PHOENIX_SQL_DATABASE_SCHEMA)
    engine = create_async_engine(get_async_db_url(sql_database_url), poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            if schema:
                # Each package-scoped app run gets its own randomized Postgres
                # schema (see _helpers.py's _random_schema) -- without this,
                # the update would silently target `public` instead.
                await conn.execute(text(f"SET search_path TO {schema}"))
            await conn.execute(
                text("UPDATE users SET idp_groups = CAST(:groups AS JSONB) WHERE id = :user_id"),
                {"groups": json.dumps(groups), "user_id": user_id},
            )
    finally:
        await engine.dispose()


async def _call_execute_sql(app: _AppInfo, access_token: str, sql: str) -> dict[str, Any]:
    async with Client(_mcp_transport(app, access_token)) as mcp_client:
        result = await mcp_client.call_tool("executeSql", {"sql": sql})
    assert result.structured_content is not None
    assert "error" not in result.structured_content, result.structured_content
    return dict(result.structured_content)


async def _mint_token_for_user(app: _AppInfo, user: _User, /) -> str:
    """Like ``test_mcp.py``'s ``_mcp_token_for``, but takes an already-created
    ``_User`` instead of creating one -- needed here because the caller must
    grant project access using the user's numeric id *before* minting the
    token, not after.
    """
    oauth_client = _register_public_client(app, resource=f"{_base_url(app)}/mcp")
    logged_in = user.log_in(app)
    return str(oauth_client.complete_flow(logged_in)["access_token"])


_PROBE_SQL = "SELECT project_rowid, count(*) AS n FROM traces GROUP BY project_rowid"


@pytest.fixture
def _two_projects(_app: _AppInfo) -> tuple[int, int]:
    """Two distinct projects, each with one trace, as plain numeric ids.

    ``_insert_spans`` calls ``asyncio.run`` internally, so it must run from a
    sync fixture, not from inside an already-running async test -- calling it
    directly in an ``async def`` test raises "asyncio.run() cannot be called
    from a running event loop" (found by actually running this, not assumed).
    """
    # Deterministic prefixes, not the default random hex name -- these must
    # match the globs in `_env_access_control` (conftest.py) so
    # `_grant_project_access` (which sets `idp_groups`, not a per-project
    # grant row) can actually resolve to these specific projects.
    project_a = _insert_spans(_app, 1, project_name=f"mcp-sql-test-project-a-{token_hex(8)}")[
        0
    ].trace.project
    project_b = _insert_spans(_app, 1, project_name=f"mcp-sql-test-project-b-{token_hex(8)}")[
        0
    ].trace.project
    return int(project_a.id.node_id), int(project_b.id.node_id)


class TestMcpSqlProjectIsolation:
    async def test_member_sees_only_the_granted_project(
        self,
        _app: _AppInfo,
        _get_user: _GetUser,
        _two_projects: tuple[int, int],
    ) -> None:
        project_a_id, project_b_id = _two_projects

        member = _get_user(_app, _MEMBER)
        member_id = int(GlobalID.from_id(member.gid).node_id)
        await _grant_project_access(_app, user_id=member_id, groups=["mcp-sql-test-project-a"])
        token = await _mint_token_for_user(_app, member)

        envelope = await _call_execute_sql(_app, token, _PROBE_SQL)
        visible_project_ids = {row[0] for row in envelope["rows"]}

        assert project_a_id in visible_project_ids, (
            f"member was denied its own granted project: {envelope}"
        )
        assert project_b_id not in visible_project_ids, (
            f"member saw an ungranted project's rows through executeSql: {envelope}"
        )

    async def test_admin_sees_every_project(
        self,
        _app: _AppInfo,
        _get_user: _GetUser,
        _two_projects: tuple[int, int],
    ) -> None:
        project_a_id, project_b_id = _two_projects

        admin = _get_user(_app, _ADMIN)
        token = await _mint_token_for_user(_app, admin)

        envelope = await _call_execute_sql(_app, token, _PROBE_SQL)
        visible_project_ids = {row[0] for row in envelope["rows"]}

        assert project_a_id in visible_project_ids
        assert project_b_id in visible_project_ids

    async def test_member_granted_both_projects_sees_both(
        self,
        _app: _AppInfo,
        _get_user: _GetUser,
        _two_projects: tuple[int, int],
    ) -> None:
        """Isolation tracks the grant, not a blanket per-role restriction --
        the same member who was denied project B above sees it once granted.
        """
        project_a_id, project_b_id = _two_projects

        member = _get_user(_app, _MEMBER)
        member_id = int(GlobalID.from_id(member.gid).node_id)
        await _grant_project_access(
            _app,
            user_id=member_id,
            groups=["mcp-sql-test-project-a", "mcp-sql-test-project-b"],
        )
        token = await _mint_token_for_user(_app, member)

        envelope = await _call_execute_sql(_app, token, _PROBE_SQL)
        visible_project_ids = {row[0] for row in envelope["rows"]}

        assert project_a_id in visible_project_ids
        assert project_b_id in visible_project_ids
