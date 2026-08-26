"""Stage 4b-2e: flag-on regression tests for the GraphQL trace/project write
paths retrofitted for project-scoped storage -- deleteTraces,
transferTracesToProject, and clearProject. Flag-off behavior for all three
is unchanged and already covered by the existing GraphQL mutation test
suites; these tests only exercise the new flag-on branches.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine
from strawberry.relay.types import GlobalID

from phoenix.db import models
from phoenix.server.access.schema_provisioning import (
    project_scoped_session,
    provision_project_schema,
)
from phoenix.server.types import DbSessionFactory
from tests.unit.graphql import AsyncGraphQLClient

pytestmark = pytest.mark.postgres_only


async def _create_project(engine: AsyncEngine, name: str) -> int:
    async with engine.begin() as conn:
        project_id = await conn.scalar(
            insert(models.Project).values(name=name).returning(models.Project.id)
        )
    assert project_id is not None
    async with engine.connect() as conn:
        await provision_project_schema(conn, project_id)
        await conn.commit()
    return project_id


async def _seed_trace(db: DbSessionFactory, project_id: int, suffix: str) -> int:
    now = datetime.now(timezone.utc)
    async with project_scoped_session(db, project_id) as session:
        trace = models.Trace(
            project_rowid=project_id, trace_id=f"trace-{suffix}", start_time=now, end_time=now
        )
        session.add(trace)
        await session.flush()
        return trace.id


async def _trace_count_in_schema(engine: AsyncEngine, project_id: int) -> int:
    async with engine.connect() as conn:
        count = await conn.scalar(text(f'SELECT count(*) FROM "project_{project_id}".traces'))
    return int(count or 0)


DELETE_TRACES_MUTATION = """
mutation DeleteTraces($traceIds: [ID!]!) {
  deleteTraces(traceIds: $traceIds) { __typename }
}
"""

TRANSFER_TRACES_TO_PROJECT_MUTATION = """
mutation TransferTracesToProject($traceIds: [ID!]!, $projectId: ID!) {
  transferTracesToProject(traceIds: $traceIds, projectId: $projectId) { __typename }
}
"""

CLEAR_PROJECT_MUTATION = """
mutation ClearProject($input: ClearProjectInput!) {
  clearProject(input: $input) { __typename }
}
"""


async def test_delete_traces_mutation_flag_on(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "gql-delete-traces")
    trace_rowid = await _seed_trace(db, project_id, "gql-del")

    result = await gql_client.execute(
        DELETE_TRACES_MUTATION,
        {"traceIds": [str(GlobalID("Trace", f"{project_id}:{trace_rowid}"))]},
    )
    assert not result.errors
    assert await _trace_count_in_schema(postgresql_engine, project_id) == 0


async def test_delete_traces_mutation_flag_on_rejects_multiple_projects(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_a = await _create_project(postgresql_engine, "gql-delete-multi-a")
    project_b = await _create_project(postgresql_engine, "gql-delete-multi-b")
    trace_a = await _seed_trace(db, project_a, "multi-a")
    trace_b = await _seed_trace(db, project_b, "multi-b")

    result = await gql_client.execute(
        DELETE_TRACES_MUTATION,
        {
            "traceIds": [
                str(GlobalID("Trace", f"{project_a}:{trace_a}")),
                str(GlobalID("Trace", f"{project_b}:{trace_b}")),
            ]
        },
    )
    assert result.errors
    assert "multiple projects" in result.errors[0].message.lower()
    # Neither project's trace should have been touched.
    assert await _trace_count_in_schema(postgresql_engine, project_a) == 1
    assert await _trace_count_in_schema(postgresql_engine, project_b) == 1


async def test_transfer_traces_to_project_mutation_flag_on(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_a = await _create_project(postgresql_engine, "gql-transfer-src")
    project_b = await _create_project(postgresql_engine, "gql-transfer-dest")
    trace_rowid = await _seed_trace(db, project_a, "gql-xfer")

    result = await gql_client.execute(
        TRANSFER_TRACES_TO_PROJECT_MUTATION,
        {
            "traceIds": [str(GlobalID("Trace", f"{project_a}:{trace_rowid}"))],
            "projectId": str(GlobalID("Project", str(project_b))),
        },
    )
    assert not result.errors
    assert await _trace_count_in_schema(postgresql_engine, project_a) == 0
    async with postgresql_engine.connect() as conn:
        moved = await conn.scalar(
            text(f'SELECT id FROM "project_{project_b}".traces WHERE trace_id = :tid'),
            {"tid": "trace-gql-xfer"},
        )
    assert moved is not None


async def test_clear_project_mutation_flag_on(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "gql-clear-project")
    await _seed_trace(db, project_id, "clear-1")
    await _seed_trace(db, project_id, "clear-2")
    assert await _trace_count_in_schema(postgresql_engine, project_id) == 2

    result = await gql_client.execute(
        CLEAR_PROJECT_MUTATION,
        {"input": {"id": str(GlobalID("Project", str(project_id)))}},
    )
    assert not result.errors
    assert await _trace_count_in_schema(postgresql_engine, project_id) == 0
