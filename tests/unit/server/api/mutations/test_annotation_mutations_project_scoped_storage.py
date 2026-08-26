"""Stage 4b-2d: flag-on regression tests for the 4 annotation create-
mutations (span/trace/document/project_session), confirming they route
through project_scoped_session and land in the annotated row's own
project schema -- not the shared one -- by trusting the project_id
already embedded in the compound GlobalID of the row being annotated.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from strawberry.relay.types import GlobalID

from phoenix.db import models
from phoenix.server.access.schema_provisioning import (
    project_scoped_session,
    provision_project_schema,
)
from phoenix.server.api.types.AnnotationSource import AnnotationSource
from phoenix.server.api.types.AnnotatorKind import AnnotatorKind
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


async def _seed_span(db: DbSessionFactory, project_id: int, suffix: str) -> int:
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
            span_kind="RETRIEVER",
            start_time=now,
            end_time=now,
            # `num_documents` is a read-only hybrid property derived from
            # this attribute (see models.py); document annotations require
            # at least one retrieval document to pass position validation.
            attributes={"retrieval": {"documents": [{"document": {"content": "doc-0"}}]}},
            events=[],
            status_code="OK",
            status_message="",
            cumulative_error_count=0,
            cumulative_llm_token_count_prompt=0,
            cumulative_llm_token_count_completion=0,
        )
        session.add(span)
        await session.flush()
        return span.id


async def _seed_session(db: DbSessionFactory, project_id: int, suffix: str) -> int:
    now = datetime.now(timezone.utc)
    async with project_scoped_session(db, project_id) as session:
        project_session = models.ProjectSession(
            project_id=project_id, session_id=f"psess-{suffix}", start_time=now, end_time=now
        )
        session.add(project_session)
        await session.flush()
        return project_session.id


CREATE_SPAN_ANNOTATIONS_MUTATION = """
mutation CreateSpanAnnotations($input: [CreateSpanAnnotationInput!]!) {
  createSpanAnnotations(input: $input) {
    spanAnnotations { id name }
  }
}
"""

CREATE_TRACE_ANNOTATIONS_MUTATION = """
mutation CreateTraceAnnotations($input: [CreateTraceAnnotationInput!]!) {
  createTraceAnnotations(input: $input) {
    traceAnnotations { id name }
  }
}
"""

CREATE_DOCUMENT_ANNOTATIONS_MUTATION = """
mutation CreateDocumentAnnotations($input: [CreateDocumentAnnotationInput!]!) {
  createDocumentAnnotations(input: $input) {
    documentAnnotations { id name }
  }
}
"""

CREATE_PROJECT_SESSION_ANNOTATION_MUTATION = """
mutation CreateProjectSessionAnnotations($input: CreateProjectSessionAnnotationInput!) {
  createProjectSessionAnnotations(input: $input) {
    projectSessionAnnotation { id name }
  }
}
"""


async def test_create_span_annotation_lands_in_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "anno-mut-span-test")
    span_id = await _seed_span(db, project_id, "a")

    variables = {
        "input": [
            {
                "spanId": str(GlobalID("Span", f"{project_id}:{span_id}")),
                "name": "relevance",
                "label": "relevant",
                "score": 1.0,
                "explanation": None,
                "annotatorKind": AnnotatorKind.HUMAN.name,
                "metadata": {},
                "identifier": "",
                "source": AnnotationSource.API.name,
            }
        ]
    }
    result = await gql_client.execute(CREATE_SPAN_ANNOTATIONS_MUTATION, variables)
    assert not result.errors
    assert result.data is not None

    async with postgresql_engine.connect() as conn:
        found_scoped = await conn.scalar(
            text(
                f'SELECT name FROM "project_{project_id}".span_annotations WHERE span_rowid = :sid'
            ),
            {"sid": span_id},
        )
    assert found_scoped == "relevance"

    async with postgresql_engine.connect() as conn:
        found_shared = await conn.scalar(
            select(models.SpanAnnotation.id).where(models.SpanAnnotation.span_rowid == span_id)
        )
    assert found_shared is None


async def test_create_span_annotations_isolates_multiple_projects(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_a = await _create_project(postgresql_engine, "anno-mut-multi-a")
    project_b = await _create_project(postgresql_engine, "anno-mut-multi-b")
    span_a = await _seed_span(db, project_a, "multi-a")
    span_b = await _seed_span(db, project_b, "multi-b")

    variables = {
        "input": [
            {
                "spanId": str(GlobalID("Span", f"{project_a}:{span_a}")),
                "name": "anno-a",
                "label": None,
                "score": None,
                "explanation": None,
                "annotatorKind": AnnotatorKind.HUMAN.name,
                "metadata": {},
                "identifier": "",
                "source": AnnotationSource.API.name,
            },
            {
                "spanId": str(GlobalID("Span", f"{project_b}:{span_b}")),
                "name": "anno-b",
                "label": None,
                "score": None,
                "explanation": None,
                "annotatorKind": AnnotatorKind.HUMAN.name,
                "metadata": {},
                "identifier": "",
                "source": AnnotationSource.API.name,
            },
        ]
    }
    result = await gql_client.execute(CREATE_SPAN_ANNOTATIONS_MUTATION, variables)
    assert not result.errors
    assert result.data is not None
    names = {a["name"] for a in result.data["createSpanAnnotations"]["spanAnnotations"]}
    assert names == {"anno-a", "anno-b"}

    # `span_a`/`span_b` are both project-schema-local ids that can (and, in
    # this test, do) collide numerically across the two independent
    # per-project sequences -- filtering by name, not span_rowid, is what
    # actually proves isolation rather than coincidentally re-finding the
    # querying project's own row under the same numeric id.
    async with postgresql_engine.connect() as conn:
        a_names = set(
            await conn.scalars(text(f'SELECT name FROM "project_{project_a}".span_annotations'))
        )
        b_names = set(
            await conn.scalars(text(f'SELECT name FROM "project_{project_b}".span_annotations'))
        )
    assert a_names == {"anno-a"}
    assert b_names == {"anno-b"}


async def test_create_trace_annotation_lands_in_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "anno-mut-trace-test")
    now = datetime.now(timezone.utc)
    async with project_scoped_session(db, project_id) as session:
        trace = models.Trace(
            project_rowid=project_id, trace_id="trace-anno-test", start_time=now, end_time=now
        )
        session.add(trace)
        await session.flush()
        trace_id = trace.id

    variables = {
        "input": [
            {
                "traceId": str(GlobalID("Trace", f"{project_id}:{trace_id}")),
                "name": "quality",
                "label": "good",
                "score": 1.0,
                "explanation": None,
                "annotatorKind": AnnotatorKind.HUMAN.name,
                "metadata": {},
                "identifier": "",
                "source": AnnotationSource.API.name,
            }
        ]
    }
    result = await gql_client.execute(CREATE_TRACE_ANNOTATIONS_MUTATION, variables)
    assert not result.errors
    assert result.data is not None

    async with postgresql_engine.connect() as conn:
        found = await conn.scalar(
            text(
                f'SELECT name FROM "project_{project_id}".trace_annotations '
                "WHERE trace_rowid = :tid"
            ),
            {"tid": trace_id},
        )
    assert found == "quality"


async def test_create_document_annotation_lands_in_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "anno-mut-doc-test")
    span_id = await _seed_span(db, project_id, "doc")

    variables = {
        "input": [
            {
                "spanId": str(GlobalID("Span", f"{project_id}:{span_id}")),
                "documentPosition": 0,
                "name": "relevance",
                "label": "relevant",
                "score": 1.0,
                "explanation": None,
                "annotatorKind": AnnotatorKind.HUMAN.name,
                "metadata": {},
                "identifier": "",
                "source": AnnotationSource.API.name,
            }
        ]
    }
    result = await gql_client.execute(CREATE_DOCUMENT_ANNOTATIONS_MUTATION, variables)
    assert not result.errors
    assert result.data is not None

    async with postgresql_engine.connect() as conn:
        found = await conn.scalar(
            text(
                f'SELECT name FROM "project_{project_id}".document_annotations '
                "WHERE span_rowid = :sid"
            ),
            {"sid": span_id},
        )
    assert found == "relevance"


async def test_create_project_session_annotation_lands_in_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "anno-mut-session-test")
    session_id = await _seed_session(db, project_id, "sess")

    variables = {
        "input": {
            "projectSessionId": str(GlobalID("ProjectSession", f"{project_id}:{session_id}")),
            "name": "quality",
            "label": "good",
            "score": 1.0,
            "explanation": None,
            "annotatorKind": AnnotatorKind.HUMAN.name,
            "metadata": {},
            "identifier": "",
            "source": AnnotationSource.API.name,
        }
    }
    result = await gql_client.execute(CREATE_PROJECT_SESSION_ANNOTATION_MUTATION, variables)
    assert not result.errors
    assert result.data is not None

    async with postgresql_engine.connect() as conn:
        found = await conn.scalar(
            text(
                f'SELECT name FROM "project_{project_id}".project_session_annotations '
                "WHERE project_session_id = :sid"
            ),
            {"sid": session_id},
        )
    assert found == "quality"
