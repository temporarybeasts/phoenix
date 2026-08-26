"""Stage 4b-2f: flag-on regression tests for the annotation write paths that
were blocked by Stage 4b-2d -- the 8 per-id patch/delete mutations (now
unblocked by extending compound GlobalIDs to the 4 annotation types) and
the 6 already-project-scoped bulk-delete paths (REST filter-based delete,
GraphQL delete-by-name), plus the Query.node dispatcher fix for
SpanAnnotation/TraceAnnotation.
"""

from datetime import datetime, timezone

import httpx
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


async def _seed_trace(db: DbSessionFactory, project_id: int, suffix: str) -> int:
    now = datetime.now(timezone.utc)
    async with project_scoped_session(db, project_id) as session:
        trace = models.Trace(
            project_rowid=project_id, trace_id=f"trace-{suffix}", start_time=now, end_time=now
        )
        session.add(trace)
        await session.flush()
        return trace.id


async def _seed_session(db: DbSessionFactory, project_id: int, suffix: str) -> int:
    now = datetime.now(timezone.utc)
    async with project_scoped_session(db, project_id) as session:
        project_session = models.ProjectSession(
            project_id=project_id, session_id=f"psess-{suffix}", start_time=now, end_time=now
        )
        session.add(project_session)
        await session.flush()
        return project_session.id


async def _seed_span_annotation(
    db: DbSessionFactory, project_id: int, span_rowid: int, name: str
) -> int:
    async with project_scoped_session(db, project_id) as session:
        anno = models.SpanAnnotation(
            span_rowid=span_rowid,
            name=name,
            label="l",
            score=1.0,
            explanation=None,
            metadata_={},
            annotator_kind="HUMAN",
            identifier="",
            source="API",
        )
        session.add(anno)
        await session.flush()
        return anno.id


async def _seed_trace_annotation(
    db: DbSessionFactory, project_id: int, trace_rowid: int, name: str
) -> int:
    async with project_scoped_session(db, project_id) as session:
        anno = models.TraceAnnotation(
            trace_rowid=trace_rowid,
            name=name,
            label="l",
            score=1.0,
            explanation=None,
            metadata_={},
            annotator_kind="HUMAN",
            identifier="",
            source="API",
        )
        session.add(anno)
        await session.flush()
        return anno.id


async def _seed_document_annotation(
    db: DbSessionFactory, project_id: int, span_rowid: int, name: str
) -> int:
    async with project_scoped_session(db, project_id) as session:
        anno = models.DocumentAnnotation(
            span_rowid=span_rowid,
            document_position=0,
            name=name,
            label="l",
            score=1.0,
            explanation=None,
            metadata_={},
            annotator_kind="HUMAN",
            identifier="",
            source="API",
        )
        session.add(anno)
        await session.flush()
        return anno.id


PATCH_SPAN_MUTATION = """
mutation PatchSpanAnnotations($input: [PatchAnnotationInput!]!) {
  patchSpanAnnotations(input: $input) { spanAnnotations { id label } }
}
"""
DELETE_SPAN_MUTATION = """
mutation DeleteSpanAnnotations($input: DeleteAnnotationsInput!) {
  deleteSpanAnnotations(input: $input) { spanAnnotations { id } }
}
"""
PATCH_TRACE_MUTATION = """
mutation PatchTraceAnnotations($input: [PatchAnnotationInput!]!) {
  patchTraceAnnotations(input: $input) { traceAnnotations { id label } }
}
"""
DELETE_TRACE_MUTATION = """
mutation DeleteTraceAnnotations($input: DeleteAnnotationsInput!) {
  deleteTraceAnnotations(input: $input) { traceAnnotations { id } }
}
"""
PATCH_DOCUMENT_MUTATION = """
mutation PatchDocumentAnnotations($input: [PatchAnnotationInput!]!) {
  patchDocumentAnnotations(input: $input) { documentAnnotations { id label } }
}
"""
DELETE_DOCUMENT_MUTATION = """
mutation DeleteDocumentAnnotations($input: DeleteAnnotationsInput!) {
  deleteDocumentAnnotations(input: $input) { documentAnnotations { id } }
}
"""
UPDATE_SESSION_MUTATION = """
mutation UpdateProjectSessionAnnotations($input: UpdateAnnotationInput!) {
  updateProjectSessionAnnotations(input: $input) {
    projectSessionAnnotation { id label }
  }
}
"""
DELETE_SESSION_MUTATION = """
mutation DeleteProjectSessionAnnotation($id: ID!) {
  deleteProjectSessionAnnotation(id: $id) { projectSessionAnnotation { id } }
}
"""
DELETE_PROJECT_SPAN_ANNOTATIONS_MUTATION = """
mutation DeleteProjectSpanAnnotations($input: DeleteProjectAnnotationsInput!) {
  deleteProjectSpanAnnotations(input: $input) { deletedAnnotationCount }
}
"""
DELETE_PROJECT_TRACE_ANNOTATIONS_MUTATION = """
mutation DeleteProjectTraceAnnotations($input: DeleteProjectAnnotationsInput!) {
  deleteProjectTraceAnnotations(input: $input) { deletedAnnotationCount }
}
"""
NODE_QUERY = """
query Node($id: ID!) {
  node(id: $id) {
    __typename
    ... on SpanAnnotation { id label }
    ... on TraceAnnotation { id label }
  }
}
"""


async def test_patch_span_annotation_lands_in_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "patch-span-test")
    span_id = await _seed_span(db, project_id, "a")
    anno_id = await _seed_span_annotation(db, project_id, span_id, "relevance")

    result = await gql_client.execute(
        PATCH_SPAN_MUTATION,
        {
            "input": [
                {
                    "annotationId": str(GlobalID("SpanAnnotation", f"{project_id}:{anno_id}")),
                    "label": "patched",
                }
            ]
        },
    )
    assert not result.errors
    assert result.data is not None
    patched = result.data["patchSpanAnnotations"]["spanAnnotations"][0]
    assert patched["label"] == "patched"

    async with postgresql_engine.connect() as conn:
        label = await conn.scalar(
            text(f'SELECT label FROM "project_{project_id}".span_annotations WHERE id = :id'),
            {"id": anno_id},
        )
    assert label == "patched"


async def test_patch_span_annotations_isolates_multiple_projects(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_a = await _create_project(postgresql_engine, "patch-span-multi-a")
    project_b = await _create_project(postgresql_engine, "patch-span-multi-b")
    span_a = await _seed_span(db, project_a, "multi-a")
    span_b = await _seed_span(db, project_b, "multi-b")
    anno_a = await _seed_span_annotation(db, project_a, span_a, "anno-a")
    anno_b = await _seed_span_annotation(db, project_b, span_b, "anno-b")

    result = await gql_client.execute(
        PATCH_SPAN_MUTATION,
        {
            "input": [
                {
                    "annotationId": str(GlobalID("SpanAnnotation", f"{project_a}:{anno_a}")),
                    "label": "patched-a",
                },
                {
                    "annotationId": str(GlobalID("SpanAnnotation", f"{project_b}:{anno_b}")),
                    "label": "patched-b",
                },
            ]
        },
    )
    assert not result.errors
    assert result.data is not None
    labels = {a["label"] for a in result.data["patchSpanAnnotations"]["spanAnnotations"]}
    assert labels == {"patched-a", "patched-b"}

    async with postgresql_engine.connect() as conn:
        label_a = await conn.scalar(
            text(f'SELECT label FROM "project_{project_a}".span_annotations WHERE id = :id'),
            {"id": anno_a},
        )
        label_b = await conn.scalar(
            text(f'SELECT label FROM "project_{project_b}".span_annotations WHERE id = :id'),
            {"id": anno_b},
        )
    assert label_a == "patched-a"
    assert label_b == "patched-b"


async def test_delete_span_annotations_removes_from_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "delete-span-test")
    span_id = await _seed_span(db, project_id, "del")
    anno_id = await _seed_span_annotation(db, project_id, span_id, "to-delete")

    result = await gql_client.execute(
        DELETE_SPAN_MUTATION,
        {"input": {"annotationIds": [str(GlobalID("SpanAnnotation", f"{project_id}:{anno_id}"))]}},
    )
    assert not result.errors

    async with postgresql_engine.connect() as conn:
        count = await conn.scalar(
            text(f'SELECT count(*) FROM "project_{project_id}".span_annotations WHERE id = :id'),
            {"id": anno_id},
        )
    assert count == 0


async def test_patch_trace_annotation_lands_in_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "patch-trace-test")
    trace_id = await _seed_trace(db, project_id, "a")
    anno_id = await _seed_trace_annotation(db, project_id, trace_id, "quality")

    result = await gql_client.execute(
        PATCH_TRACE_MUTATION,
        {
            "input": [
                {
                    "annotationId": str(GlobalID("TraceAnnotation", f"{project_id}:{anno_id}")),
                    "label": "patched",
                }
            ]
        },
    )
    assert not result.errors
    async with postgresql_engine.connect() as conn:
        label = await conn.scalar(
            text(f'SELECT label FROM "project_{project_id}".trace_annotations WHERE id = :id'),
            {"id": anno_id},
        )
    assert label == "patched"


async def test_delete_trace_annotations_removes_from_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "delete-trace-test")
    trace_id = await _seed_trace(db, project_id, "del")
    anno_id = await _seed_trace_annotation(db, project_id, trace_id, "to-delete")

    result = await gql_client.execute(
        DELETE_TRACE_MUTATION,
        {"input": {"annotationIds": [str(GlobalID("TraceAnnotation", f"{project_id}:{anno_id}"))]}},
    )
    assert not result.errors
    async with postgresql_engine.connect() as conn:
        count = await conn.scalar(
            text(f'SELECT count(*) FROM "project_{project_id}".trace_annotations WHERE id = :id'),
            {"id": anno_id},
        )
    assert count == 0


async def test_patch_document_annotation_lands_in_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "patch-doc-test")
    span_id = await _seed_span(db, project_id, "a")
    anno_id = await _seed_document_annotation(db, project_id, span_id, "relevance")

    result = await gql_client.execute(
        PATCH_DOCUMENT_MUTATION,
        {
            "input": [
                {
                    "annotationId": str(GlobalID("DocumentAnnotation", f"{project_id}:{anno_id}")),
                    "label": "patched",
                }
            ]
        },
    )
    assert not result.errors
    async with postgresql_engine.connect() as conn:
        label = await conn.scalar(
            text(f'SELECT label FROM "project_{project_id}".document_annotations WHERE id = :id'),
            {"id": anno_id},
        )
    assert label == "patched"


async def test_delete_document_annotations_removes_from_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "delete-doc-test")
    span_id = await _seed_span(db, project_id, "del")
    anno_id = await _seed_document_annotation(db, project_id, span_id, "to-delete")

    result = await gql_client.execute(
        DELETE_DOCUMENT_MUTATION,
        {
            "input": {
                "annotationIds": [str(GlobalID("DocumentAnnotation", f"{project_id}:{anno_id}"))]
            }
        },
    )
    assert not result.errors
    async with postgresql_engine.connect() as conn:
        count = await conn.scalar(
            text(
                f'SELECT count(*) FROM "project_{project_id}".document_annotations WHERE id = :id'
            ),
            {"id": anno_id},
        )
    assert count == 0


async def test_update_project_session_annotation_lands_in_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "update-session-test")
    session_id = await _seed_session(db, project_id, "a")
    async with project_scoped_session(db, project_id) as session:
        anno = models.ProjectSessionAnnotation(
            project_session_id=session_id,
            name="quality",
            label="l",
            score=1.0,
            explanation=None,
            metadata_={},
            annotator_kind="HUMAN",
            identifier="",
            source="API",
        )
        session.add(anno)
        await session.flush()
        anno_id = anno.id

    result = await gql_client.execute(
        UPDATE_SESSION_MUTATION,
        {
            "input": {
                "id": str(GlobalID("ProjectSessionAnnotation", f"{project_id}:{anno_id}")),
                "name": "quality",
                "label": "patched",
                "score": 1.0,
                "explanation": None,
                "annotatorKind": "HUMAN",
                "metadata": {},
                "source": "API",
            }
        },
    )
    assert not result.errors
    async with postgresql_engine.connect() as conn:
        label = await conn.scalar(
            text(
                f'SELECT label FROM "project_{project_id}".project_session_annotations '
                "WHERE id = :id"
            ),
            {"id": anno_id},
        )
    assert label == "patched"


async def test_delete_project_session_annotation_removes_from_project_schema(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "delete-session-test")
    session_id = await _seed_session(db, project_id, "del")
    async with project_scoped_session(db, project_id) as session:
        anno = models.ProjectSessionAnnotation(
            project_session_id=session_id,
            name="to-delete",
            label="l",
            score=1.0,
            explanation=None,
            metadata_={},
            annotator_kind="HUMAN",
            identifier="",
            source="API",
        )
        session.add(anno)
        await session.flush()
        anno_id = anno.id

    result = await gql_client.execute(
        DELETE_SESSION_MUTATION,
        {"id": str(GlobalID("ProjectSessionAnnotation", f"{project_id}:{anno_id}"))},
    )
    assert not result.errors
    async with postgresql_engine.connect() as conn:
        count = await conn.scalar(
            text(
                f'SELECT count(*) FROM "project_{project_id}".project_session_annotations '
                "WHERE id = :id"
            ),
            {"id": anno_id},
        )
    assert count == 0


async def test_rest_delete_span_annotations_by_filter_flag_on(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    httpx_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "rest-delete-span-anno")
    span_id = await _seed_span(db, project_id, "a")
    await _seed_span_annotation(db, project_id, span_id, "filter-me")

    response = await httpx_client.delete(
        "v1/projects/rest-delete-span-anno/span_annotations",
        params={"name": "filter-me", "delete_all": "true"},
    )
    assert response.status_code == 204
    async with postgresql_engine.connect() as conn:
        count = await conn.scalar(
            text(f'SELECT count(*) FROM "project_{project_id}".span_annotations')
        )
    assert count == 0


async def test_rest_delete_trace_annotations_by_filter_flag_on(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    httpx_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "rest-delete-trace-anno")
    trace_id = await _seed_trace(db, project_id, "a")
    await _seed_trace_annotation(db, project_id, trace_id, "filter-me")

    response = await httpx_client.delete(
        "v1/projects/rest-delete-trace-anno/trace_annotations",
        params={"name": "filter-me", "delete_all": "true"},
    )
    assert response.status_code == 204
    async with postgresql_engine.connect() as conn:
        count = await conn.scalar(
            text(f'SELECT count(*) FROM "project_{project_id}".trace_annotations')
        )
    assert count == 0


async def test_graphql_delete_project_span_annotations_by_name_flag_on(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "gql-delete-project-span-anno")
    span_id = await _seed_span(db, project_id, "a")
    await _seed_span_annotation(db, project_id, span_id, "bulk-delete-me")

    result = await gql_client.execute(
        DELETE_PROJECT_SPAN_ANNOTATIONS_MUTATION,
        {
            "input": {
                "projectId": str(GlobalID("Project", str(project_id))),
                "annotationName": "bulk-delete-me",
            }
        },
    )
    assert not result.errors
    assert result.data is not None
    assert result.data["deleteProjectSpanAnnotations"]["deletedAnnotationCount"] == 1
    async with postgresql_engine.connect() as conn:
        count = await conn.scalar(
            text(f'SELECT count(*) FROM "project_{project_id}".span_annotations')
        )
    assert count == 0


async def test_graphql_delete_project_trace_annotations_by_name_flag_on(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "gql-delete-project-trace-anno")
    trace_id = await _seed_trace(db, project_id, "a")
    await _seed_trace_annotation(db, project_id, trace_id, "bulk-delete-me")

    result = await gql_client.execute(
        DELETE_PROJECT_TRACE_ANNOTATIONS_MUTATION,
        {
            "input": {
                "projectId": str(GlobalID("Project", str(project_id))),
                "annotationName": "bulk-delete-me",
            }
        },
    )
    assert not result.errors
    assert result.data is not None
    assert result.data["deleteProjectTraceAnnotations"]["deletedAnnotationCount"] == 1


async def test_node_query_resolves_span_annotation_by_compound_id(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "node-dispatch-span-anno")
    span_id = await _seed_span(db, project_id, "a")
    anno_id = await _seed_span_annotation(db, project_id, span_id, "findable")

    result = await gql_client.execute(
        NODE_QUERY,
        {"id": str(GlobalID("SpanAnnotation", f"{project_id}:{anno_id}"))},
    )
    assert not result.errors
    assert result.data is not None
    assert result.data["node"]["__typename"] == "SpanAnnotation"
    assert result.data["node"]["label"] == "l"


async def test_node_query_resolves_trace_annotation_by_compound_id(
    db: DbSessionFactory,
    postgresql_engine: AsyncEngine,
    gql_client: AsyncGraphQLClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PHOENIX_PROJECT_SCOPED_STORAGE_ENABLED", "true")
    project_id = await _create_project(postgresql_engine, "node-dispatch-trace-anno")
    trace_id = await _seed_trace(db, project_id, "a")
    anno_id = await _seed_trace_annotation(db, project_id, trace_id, "findable")

    result = await gql_client.execute(
        NODE_QUERY,
        {"id": str(GlobalID("TraceAnnotation", f"{project_id}:{anno_id}"))},
    )
    assert not result.errors
    assert result.data is not None
    assert result.data["node"]["__typename"] == "TraceAnnotation"
    assert result.data["node"]["label"] == "l"
