"""Stage 4b-2e: flag-on regression tests for the REST trace write paths that
had to change for project-scoped storage -- POST /v1/traces/transfer and
DELETE /v1/traces/{trace_identifier}. The flag-off behavior for both is
already covered exhaustively in test_traces.py and is untouched by this
stage; these tests only exercise the new flag-on branches.
"""

from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine
from strawberry.relay import GlobalID

from phoenix.db import models
from phoenix.server.access.schema_provisioning import (
    project_scoped_session,
    provision_project_schema,
)
from phoenix.server.types import DbSessionFactory

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
        span = models.Span(
            trace_rowid=trace.id,
            span_id=f"span-{suffix}",
            parent_id=None,
            name="n",
            span_kind="CHAIN",
            start_time=now,
            end_time=now,
            attributes={},
            events=[],
            status_code="OK",
            status_message="",
            cumulative_error_count=0,
            cumulative_llm_token_count_prompt=0,
            cumulative_llm_token_count_completion=0,
        )
        session.add(span)
        await session.flush()
        return trace.id


async def _trace_exists_in_schema(engine: AsyncEngine, project_id: int, trace_rowid: int) -> bool:
    async with engine.connect() as conn:
        count = await conn.scalar(
            text(f'SELECT count(*) FROM "project_{project_id}".traces WHERE id = :id'),
            {"id": trace_rowid},
        )
    return bool(count)


async def test_transfer_traces_rest_moves_subtree_between_schemas(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    httpx_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_a = await _create_project(postgresql_engine, "rest-transfer-src")
    project_b = await _create_project(postgresql_engine, "rest-transfer-dest")
    trace_rowid = await _seed_trace(db, project_a, "rest")

    response = await httpx_client.post(
        "v1/traces/transfer",
        json={
            "trace_identifiers": [str(GlobalID("Trace", f"{project_a}:{trace_rowid}"))],
            "destination_project_identifier": "rest-transfer-dest",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["transferred_trace_count"] == 1
    assert data["destination_project_id"] == str(GlobalID("Project", str(project_b)))

    assert not await _trace_exists_in_schema(postgresql_engine, project_a, trace_rowid)
    async with postgresql_engine.connect() as conn:
        moved_trace_id = await conn.scalar(
            text(f'SELECT id FROM "project_{project_b}".traces WHERE trace_id = :tid'),
            {"tid": "trace-rest"},
        )
    assert moved_trace_id is not None


async def test_transfer_traces_rest_rejects_otel_trace_id_when_flag_on(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    httpx_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    await _create_project(postgresql_engine, "rest-transfer-otel-dest")

    response = await httpx_client.post(
        "v1/traces/transfer",
        json={
            "trace_identifiers": ["82c6c9c33ccc586e0d3bdf46b20db309"],
            "destination_project_identifier": "rest-transfer-otel-dest",
        },
    )
    assert response.status_code == 422
    # HTTPException responses in this app are plain text, not JSON --
    # see plain_text_http_exception_handler in server/app.py.
    assert "OpenTelemetry trace_id" in response.text


async def test_transfer_traces_rest_unknown_trace_flag_on(
    postgresql_engine: AsyncEngine,
    httpx_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_a = await _create_project(postgresql_engine, "rest-transfer-missing-src")
    await _create_project(postgresql_engine, "rest-transfer-missing-dest")

    response = await httpx_client.post(
        "v1/traces/transfer",
        json={
            "trace_identifiers": [str(GlobalID("Trace", f"{project_a}:999999"))],
            "destination_project_identifier": "rest-transfer-missing-dest",
        },
    )
    assert response.status_code == 404


async def test_delete_trace_rest_flag_on_by_global_id(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    httpx_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "rest-delete-test")
    trace_rowid = await _seed_trace(db, project_id, "del")

    response = await httpx_client.delete(
        f"v1/traces/{GlobalID('Trace', f'{project_id}:{trace_rowid}')}"
    )
    assert response.status_code == 204
    assert not await _trace_exists_in_schema(postgresql_engine, project_id, trace_rowid)


async def test_delete_trace_rest_flag_on_rejects_otel_trace_id(
    postgresql_engine: AsyncEngine,
    httpx_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    response = await httpx_client.delete("v1/traces/82c6c9c33ccc586e0d3bdf46b20db309")
    assert response.status_code == 422
    assert "OpenTelemetry trace_id" in response.text


async def test_delete_trace_rest_flag_on_not_found(
    postgresql_engine: AsyncEngine,
    httpx_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "rest-delete-missing")
    response = await httpx_client.delete(f"v1/traces/{GlobalID('Trace', f'{project_id}:999999')}")
    assert response.status_code == 404
