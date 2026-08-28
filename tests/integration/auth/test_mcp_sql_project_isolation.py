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
from typing import Any, AsyncIterator

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


async def _grant_project_access(
    app: _AppInfo, *, user_id: int, project_group_ids: list[int]
) -> None:
    """Grants access to each of ``project_group_ids`` via a freshly-minted
    external role mapped to it (an ``ExternalRoleProjectGroupMapping`` row),
    and seeds ``users.idp_groups`` with those roles -- standing in for what
    a real OIDC login plus an external onboarding process populating the
    mapping table would do, without standing up a live IdP. Wholesale
    replace on ``idp_groups``, matching ``sync_idp_groups``'s own semantics.
    """
    sql_database_url = app.env[ENV_PHOENIX_SQL_DATABASE_URL]
    schema = app.env.get(ENV_PHOENIX_SQL_DATABASE_SCHEMA)
    engine = create_async_engine(get_async_db_url(sql_database_url), poolclass=NullPool)
    external_roles = [f"role-{token_hex(4)}" for _ in project_group_ids]
    try:
        async with engine.begin() as conn:
            if schema:
                # Each package-scoped app run gets its own randomized Postgres
                # schema (see _helpers.py's _random_schema) -- without this,
                # the update would silently target `public` instead.
                await conn.execute(text(f"SET search_path TO {schema}"))
            for role, group_id in zip(external_roles, project_group_ids):
                await conn.execute(
                    text(
                        "INSERT INTO external_role_project_group_mappings "
                        "(external_role, project_group_id, role) "
                        "VALUES (:role, :group_id, 'VIEWER')"
                    ),
                    {"role": role, "group_id": group_id},
                )
            await conn.execute(
                text("UPDATE users SET idp_groups = CAST(:groups AS JSONB) WHERE id = :user_id"),
                {"groups": json.dumps(external_roles), "user_id": user_id},
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


async def _assign_distinct_groups(app: _AppInfo, project_ids: list[int]) -> list[int]:
    """OTLP ingest auto-creates projects with no group-selection mechanism
    of its own -- they always land in the well-known default project group
    (see `phoenix.db.insertion.span.insert_span`). Reassigns each of
    ``project_ids`` to its own freshly-created, otherwise-empty group, so
    granting access to one project's group here can't accidentally also
    grant the other (which sharing the default group would do)."""
    sql_database_url = app.env[ENV_PHOENIX_SQL_DATABASE_URL]
    schema = app.env.get(ENV_PHOENIX_SQL_DATABASE_SCHEMA)
    engine = create_async_engine(get_async_db_url(sql_database_url), poolclass=NullPool)
    group_ids = []
    try:
        async with engine.begin() as conn:
            if schema:
                await conn.execute(text(f"SET search_path TO {schema}"))
            for project_id in project_ids:
                group_id = await conn.scalar(
                    text("INSERT INTO project_groups (name) VALUES (:name) RETURNING id"),
                    {"name": f"mcp-sql-test-group-{token_hex(8)}"},
                )
                await conn.execute(
                    text("UPDATE projects SET project_group_id = :group_id WHERE id = :id"),
                    {"group_id": group_id, "id": project_id},
                )
                group_ids.append(group_id)
    finally:
        await engine.dispose()
    return group_ids


@pytest.fixture
def _two_projects(_app: _AppInfo) -> tuple[int, int, int, int]:
    """Two distinct projects (each with one trace) and their own dedicated
    project groups, as plain numeric ids: ``(project_a_id, project_b_id,
    group_a_id, group_b_id)``.

    ``_insert_spans`` calls ``asyncio.run`` internally, so it must run from a
    sync fixture, not from inside an already-running async test -- calling it
    directly in an ``async def`` test raises "asyncio.run() cannot be called
    from a running event loop" (found by actually running this, not assumed).
    The group reassignment below has the same constraint, so it also runs
    via `asyncio.run` from this sync fixture.
    """
    import asyncio

    project_a = _insert_spans(_app, 1, project_name=f"mcp-sql-test-project-a-{token_hex(8)}")[
        0
    ].trace.project
    project_b = _insert_spans(_app, 1, project_name=f"mcp-sql-test-project-b-{token_hex(8)}")[
        0
    ].trace.project
    project_a_id, project_b_id = int(project_a.id.node_id), int(project_b.id.node_id)
    group_a_id, group_b_id = asyncio.run(
        _assign_distinct_groups(_app, [project_a_id, project_b_id])
    )
    return project_a_id, project_b_id, group_a_id, group_b_id


@pytest.fixture(autouse=True)
async def _cleanup_project_group_mappings(_app: _AppInfo) -> AsyncIterator[None]:
    """This file is the only one in the shared package-scoped ``_app``'s
    Postgres schema that ever inserts into
    ``external_role_project_group_mappings`` -- once any row exists there,
    project-group RBAC is "in use" for the rest of that shared app's
    lifetime (see ``resolution._project_group_rbac_in_use``), which would
    otherwise silently restrict every other, unrelated test's group-less
    MEMBER users (elsewhere in this package) for the remainder of the whole
    suite's run, well past this file's own tests. Delete whatever this
    test inserted so later, unrelated tests see an RBAC-inactive
    deployment again, matching their own assumptions."""
    yield
    sql_database_url = _app.env[ENV_PHOENIX_SQL_DATABASE_URL]
    schema = _app.env.get(ENV_PHOENIX_SQL_DATABASE_SCHEMA)
    engine = create_async_engine(get_async_db_url(sql_database_url), poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            if schema:
                await conn.execute(text(f"SET search_path TO {schema}"))
            await conn.execute(text("DELETE FROM external_role_project_group_mappings"))
    finally:
        await engine.dispose()


class TestMcpSqlProjectIsolation:
    async def test_member_sees_only_the_granted_project(
        self,
        _app: _AppInfo,
        _get_user: _GetUser,
        _two_projects: tuple[int, int, int, int],
    ) -> None:
        project_a_id, project_b_id, group_a_id, _group_b_id = _two_projects

        member = _get_user(_app, _MEMBER)
        member_id = int(GlobalID.from_id(member.gid).node_id)
        await _grant_project_access(_app, user_id=member_id, project_group_ids=[group_a_id])
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
        _two_projects: tuple[int, int, int, int],
    ) -> None:
        project_a_id, project_b_id, _group_a_id, _group_b_id = _two_projects

        admin = _get_user(_app, _ADMIN)
        token = await _mint_token_for_user(_app, admin)

        envelope = await _call_execute_sql(_app, token, _PROBE_SQL)
        visible_project_ids = {row[0] for row in envelope["rows"]}

        assert project_a_id in visible_project_ids
        assert project_b_id in visible_project_ids

    async def test_member_in_two_groups_with_no_active_selection_sees_neither(
        self,
        _app: _AppInfo,
        _get_user: _GetUser,
        _two_projects: tuple[int, int, int, int],
    ) -> None:
        """A user who's a member of 2+ project groups must explicitly pick
        which one they're viewing (see `phoenix.server.access.resolution`).
        A bearer-token/MCP caller has no browser to show that picker in, and
        sends no active-project-group cookie -- so it resolves to no access
        at all, fail-closed, rather than the union of every held group. This
        replaces the old model's `test_member_granted_both_projects_sees_both`,
        whose premise (holding two groups grants the union) no longer holds
        under the new one-group-at-a-time viewing model.
        """
        project_a_id, project_b_id, group_a_id, group_b_id = _two_projects

        member = _get_user(_app, _MEMBER)
        member_id = int(GlobalID.from_id(member.gid).node_id)
        await _grant_project_access(
            _app,
            user_id=member_id,
            project_group_ids=[group_a_id, group_b_id],
        )
        token = await _mint_token_for_user(_app, member)

        envelope = await _call_execute_sql(_app, token, _PROBE_SQL)
        visible_project_ids = {row[0] for row in envelope["rows"]}

        assert project_a_id not in visible_project_ids
        assert project_b_id not in visible_project_ids
