"""Verifies how a batch/request mixing a readable and an unreadable project
is actually rejected by the annotation-creation endpoints, for real, end to
end -- not assumed from reading the code.

The original concern (scoped before this file existed): the 3 batch-create
mutations (span/trace/document annotations) and their REST equivalents share
one transaction across a loop of individual INSERTs with no savepoints, so a
`WITH CHECK` violation partway through would abort every other item in the
same batch too, surfacing as an opaque, masked error.

Running this against a real, RLS-migrated Postgres revealed that concern
doesn't actually materialize as a `WITH CHECK` violation in practice: `USING`
(read) and `WITH CHECK` (write) share the identical `readable_project_ids`
predicate on `traces`/`spans`/`project_sessions`, so a restricted session's
own `SELECT` -- the one every one of these mutations already runs first, to
check the target rows exist -- can't see an unreadable row at all. That
missing-row check (pre-existing, not something this file's fix added) then
raises a clean `NotFound` for the whole batch before any INSERT is ever
attempted. This file proves that's what actually happens: found by running
the original version of these tests (which asserted an `Unauthorized`/403
that never fires) and getting a `NotFound`/404 instead.

The one place this reasoning doesn't hold is `create_project_session_annotations`,
a single-item mutation with no pre-existing existence check at all -- that
gap is real and is what this file's production-code change actually fixes:
`project_id is None` (session doesn't exist, or exists but is RLS-invisible
to this session -- indistinguishable, same fail-closed shape as the other
3 types) now raises `NotFound` before the INSERT, instead of silently
proceeding into an unhandled `WITH CHECK` violation.

Drives real HTTP requests (GraphQL and REST) through a real authenticated
app (`authentication_enabled=True`, a real JWT minted via `JwtStore`) against
a database migrated by real Alembic (RLS only exists there), rather than
calling resolvers directly -- the behavior under test is what a restricted
session's own queries can and can't see, which only a real request exercises
faithfully.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from typing import Any, AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp

from phoenix.db import models
from phoenix.server.app import _db, create_app
from phoenix.server.jwt_store import JwtStore
from phoenix.server.types import (
    AccessTokenAttributes,
    AccessTokenClaims,
    DbSessionFactory,
    RefreshTokenAttributes,
    RefreshTokenClaims,
    UserId,
)
from tests.unit.conftest import TestBulkInserter, patch_batched_caller, patch_grpc_server
from tests.unit.graphql import AsyncGraphQLClient
from tests.unit.server.access.conftest import migrated_postgresql_engine  # noqa: F401

pytestmark = pytest.mark.postgres_only

_SECRET = SecretStr("test-secret-at-least-32-chars-long!!")


@pytest.fixture
def db(migrated_postgresql_engine: AsyncEngine) -> DbSessionFactory:  # noqa: F811
    # Overrides the package's default `db` fixture (`create_all`-based) --
    # `authentication_enabled=True` below means every non-admin request goes
    # through `_set_db_isolation_guards`, which does `SET LOCAL ROLE
    # phoenix_scoped`; that role only exists on a database migrated by real
    # Alembic, not a `create_all` one. Confirmed by running this against the
    # default `db` fixture first: `role "phoenix_scoped" does not exist`.
    return DbSessionFactory(db=_db(migrated_postgresql_engine), dialect="postgresql")


@pytest.fixture
async def app(db: DbSessionFactory) -> AsyncIterator[FastAPI]:
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(patch_batched_caller())
        await stack.enter_async_context(patch_grpc_server())
        yield create_app(
            db=db,
            authentication_enabled=True,
            serve_ui=False,
            bulk_inserter_factory=TestBulkInserter,
            secret=_SECRET,
        )


@pytest.fixture
async def asgi_app(app: FastAPI) -> AsyncIterator[ASGIApp]:
    async with LifespanManager(app) as manager:
        yield manager.app


async def _create_member_with_token(db: DbSessionFactory) -> tuple[int, str]:
    async with db() as session:
        role_id = await session.scalar(
            select(models.UserRole.id).where(models.UserRole.name == "MEMBER")
        )
        assert role_id is not None
        user = models.User(
            user_role_id=role_id,
            username=f"member-{token_hex(4)}",
            email=f"member-{token_hex(4)}@example.com",
            password_hash=b"hash",
            password_salt=b"salt",
            reset_password=False,
            auth_method="LOCAL",
        )
        session.add(user)
        await session.flush()
        user_id = user.id

    token_store = JwtStore(db, _SECRET)
    now = datetime.now(timezone.utc)
    _refresh_token, refresh_token_id = await token_store.create_refresh_token(
        RefreshTokenClaims(
            subject=UserId(user_id),
            issued_at=now,
            expiration_time=now + timedelta(days=1),
            attributes=RefreshTokenAttributes(user_role="MEMBER"),
        )
    )
    access_token, _access_token_id = await token_store.create_access_token(
        AccessTokenClaims(
            subject=UserId(user_id),
            issued_at=now,
            expiration_time=now + timedelta(hours=1),
            attributes=AccessTokenAttributes(user_role="MEMBER", refresh_token_id=refresh_token_id),
        )
    )
    return user_id, str(access_token)


async def _grant_project_read(
    db: DbSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    *,
    user_id: int,
    project_name: str,
) -> None:
    """Grants access via an external role mapped to the project's own group
    through a persisted `ExternalRoleProjectGroupMapping` row, with the user
    directly seeded as a holder of that external role -- standing in for
    what a real OIDC login would populate on `users.idp_groups`. The user
    ends up a member of exactly one group, so it's their implicit active
    group with no cookie/ContextVar needed."""
    external_role = f"role-{token_hex(4)}"
    async with db() as session:
        project_group_id = await session.scalar(
            select(models.Project.project_group_id).where(models.Project.name == project_name)
        )
        assert project_group_id is not None
        session.add(
            models.ExternalRoleProjectGroupMapping(
                external_role=external_role, project_group_id=project_group_id, role="VIEWER"
            )
        )
        await session.execute(
            update(models.User).where(models.User.id == user_id).values(idp_groups=[external_role])
        )


async def _seed_project_with_trace_and_span(db: DbSessionFactory, *, name: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    async with db() as session:
        # Each project gets its own dedicated group, not the shared
        # cross-test default -- `_grant_project_read` grants access to
        # exactly one project's group at a time, and the isolation
        # scenarios here depend on the "ungranted" project genuinely being
        # in a different group.
        group = models.ProjectGroup(name=f"group-for-{name}")
        session.add(group)
        await session.flush()
        project = models.Project(name=name, project_group_id=group.id)
        session.add(project)
        await session.flush()
        trace = models.Trace(
            project_rowid=project.id, trace_id=token_hex(16), start_time=now, end_time=now
        )
        session.add(trace)
        await session.flush()
        span = models.Span(
            trace_rowid=trace.id,
            span_id=token_hex(8),
            parent_id=None,
            name="span",
            span_kind="LLM",
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
        return {
            "project_id": project.id,
            "project_name": name,
            "trace_rowid": trace.id,
            "span_rowid": span.id,
        }


async def _seed_project_with_session(db: DbSessionFactory, *, name: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    async with db() as session:
        # Each project gets its own dedicated group, not the shared
        # cross-test default -- `_grant_project_read` grants access to
        # exactly one project's group at a time, and the isolation
        # scenarios here depend on the "ungranted" project genuinely being
        # in a different group.
        group = models.ProjectGroup(name=f"group-for-{name}")
        session.add(group)
        await session.flush()
        project = models.Project(name=name, project_group_id=group.id)
        session.add(project)
        await session.flush()
        project_session = models.ProjectSession(
            project_id=project.id, session_id=token_hex(8), start_time=now, end_time=now
        )
        session.add(project_session)
        await session.flush()
        return {
            "project_id": project.id,
            "project_name": name,
            "session_rowid": project_session.id,
        }


def _gid(type_name: str, rowid: int) -> str:
    from strawberry.relay import GlobalID

    return str(GlobalID(type_name, str(rowid)))


class TestBatchAnnotationMixedProjectRejection:
    """The pre-existing missing-row check, running through a restricted
    session, already rejects a mixed-project batch cleanly -- these prove
    that's real, not assumed, and that no partial writes leak through."""

    async def test_create_span_annotations_rejects_batch_with_unreadable_project(
        self,
        db: DbSessionFactory,
        asgi_app: ASGIApp,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        granted = await _seed_project_with_trace_and_span(db, name=f"granted-{token_hex(4)}")
        ungranted = await _seed_project_with_trace_and_span(db, name=f"ungranted-{token_hex(4)}")
        user_id, token = await _create_member_with_token(db)
        await _grant_project_read(
            db, monkeypatch, tmp_path, user_id=user_id, project_name=granted["project_name"]
        )

        httpx_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        )
        gql_client = AsyncGraphQLClient(httpx_client)

        mutation = """
        mutation ($input: [CreateSpanAnnotationInput!]!) {
          createSpanAnnotations(input: $input) {
            spanAnnotations { id }
          }
        }
        """
        variables = {
            "input": [
                {
                    "spanId": _gid("Span", granted["span_rowid"]),
                    "name": "quality",
                    "annotatorKind": "HUMAN",
                    "source": "APP",
                    "metadata": {},
                },
                {
                    "spanId": _gid("Span", ungranted["span_rowid"]),
                    "name": "quality",
                    "annotatorKind": "HUMAN",
                    "source": "APP",
                    "metadata": {},
                },
            ]
        }
        result = await gql_client.execute(mutation, variables)
        assert result.errors, "batch mixing a readable and unreadable project must be rejected"
        assert any("could not find" in e.message.lower() for e in result.errors), result.errors

        # Ground truth: neither annotation was written -- including the one
        # targeting the *readable* project, proving the whole batch was
        # rejected up front rather than partially applied.
        async with db() as session:
            count = await session.scalar(select(func.count()).select_from(models.SpanAnnotation))
        assert count == 0

    async def test_create_trace_annotations_rejects_batch_with_unreadable_project(
        self,
        db: DbSessionFactory,
        asgi_app: ASGIApp,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        granted = await _seed_project_with_trace_and_span(db, name=f"granted-{token_hex(4)}")
        ungranted = await _seed_project_with_trace_and_span(db, name=f"ungranted-{token_hex(4)}")
        user_id, token = await _create_member_with_token(db)
        await _grant_project_read(
            db, monkeypatch, tmp_path, user_id=user_id, project_name=granted["project_name"]
        )

        httpx_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        )
        gql_client = AsyncGraphQLClient(httpx_client)

        mutation = """
        mutation ($input: [CreateTraceAnnotationInput!]!) {
          createTraceAnnotations(input: $input) {
            traceAnnotations { id }
          }
        }
        """
        variables = {
            "input": [
                {
                    "traceId": _gid("Trace", granted["trace_rowid"]),
                    "name": "quality",
                    "annotatorKind": "HUMAN",
                    "source": "APP",
                    "metadata": {},
                },
                {
                    "traceId": _gid("Trace", ungranted["trace_rowid"]),
                    "name": "quality",
                    "annotatorKind": "HUMAN",
                    "source": "APP",
                    "metadata": {},
                },
            ]
        }
        result = await gql_client.execute(mutation, variables)
        assert result.errors, "batch mixing a readable and unreadable project must be rejected"
        assert any("could not find" in e.message.lower() for e in result.errors), result.errors

        async with db() as session:
            count = await session.scalar(select(func.count()).select_from(models.TraceAnnotation))
        assert count == 0

    async def test_annotate_spans_rest_sync_rejects_batch_with_unreadable_project(
        self,
        db: DbSessionFactory,
        asgi_app: ASGIApp,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        granted = await _seed_project_with_trace_and_span(db, name=f"granted-{token_hex(4)}")
        ungranted = await _seed_project_with_trace_and_span(db, name=f"ungranted-{token_hex(4)}")
        user_id, token = await _create_member_with_token(db)
        await _grant_project_read(
            db, monkeypatch, tmp_path, user_id=user_id, project_name=granted["project_name"]
        )

        async with db() as session:
            granted_span_id = await session.scalar(
                select(models.Span.span_id).where(models.Span.id == granted["span_rowid"])
            )
            ungranted_span_id = await session.scalar(
                select(models.Span.span_id).where(models.Span.id == ungranted["span_rowid"])
            )

        httpx_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        )
        response = await httpx_client.post(
            "/v1/span_annotations?sync=true",
            json={
                "data": [
                    {
                        "span_id": granted_span_id,
                        "name": "quality",
                        "annotator_kind": "HUMAN",
                        "result": {"label": "good"},
                    },
                    {
                        "span_id": ungranted_span_id,
                        "name": "quality",
                        "annotator_kind": "HUMAN",
                        "result": {"label": "good"},
                    },
                ]
            },
        )
        # RLS makes the ungranted span invisible to this restricted
        # session's own existence check -- the same "not found", not
        # "forbidden", shape as the GraphQL mutations above.
        assert response.status_code == 404, response.text

        async with db() as session:
            count = await session.scalar(select(func.count()).select_from(models.SpanAnnotation))
        assert count == 0

    async def test_batch_within_a_single_readable_project_still_succeeds(
        self,
        db: DbSessionFactory,
        asgi_app: ASGIApp,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        """Not a false-positive block on the common case: a batch entirely
        within one granted project."""
        granted = await _seed_project_with_trace_and_span(db, name=f"granted-{token_hex(4)}")
        user_id, token = await _create_member_with_token(db)
        await _grant_project_read(
            db, monkeypatch, tmp_path, user_id=user_id, project_name=granted["project_name"]
        )

        httpx_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        )
        gql_client = AsyncGraphQLClient(httpx_client)

        mutation = """
        mutation ($input: [CreateSpanAnnotationInput!]!) {
          createSpanAnnotations(input: $input) {
            spanAnnotations { id }
          }
        }
        """
        variables = {
            "input": [
                {
                    "spanId": _gid("Span", granted["span_rowid"]),
                    "name": "quality",
                    "annotatorKind": "HUMAN",
                    "source": "APP",
                    "metadata": {},
                },
            ]
        }
        result = await gql_client.execute(mutation, variables)
        assert not result.errors, result.errors
        assert result.data is not None
        assert len(result.data["createSpanAnnotations"]["spanAnnotations"]) == 1


class TestCreateProjectSessionAnnotationRejectsUnreadableProject:
    """The one real gap this file's production-code change fixes: a
    single-item mutation with no pre-existing existence check, which used to
    let an inaccessible session's annotation attempt proceed to an unhandled
    `WITH CHECK` violation instead of a clean rejection."""

    async def test_rejects_annotation_on_unreadable_session(
        self,
        db: DbSessionFactory,
        asgi_app: ASGIApp,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        granted = await _seed_project_with_session(db, name=f"granted-{token_hex(4)}")
        ungranted = await _seed_project_with_session(db, name=f"ungranted-{token_hex(4)}")
        user_id, token = await _create_member_with_token(db)
        await _grant_project_read(
            db, monkeypatch, tmp_path, user_id=user_id, project_name=granted["project_name"]
        )

        httpx_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        )
        gql_client = AsyncGraphQLClient(httpx_client)

        mutation = """
        mutation ($input: CreateProjectSessionAnnotationInput!) {
          createProjectSessionAnnotations(input: $input) {
            projectSessionAnnotation { id }
          }
        }
        """
        result = await gql_client.execute(
            mutation,
            {
                "input": {
                    "projectSessionId": _gid("ProjectSession", ungranted["session_rowid"]),
                    "name": "quality",
                    "annotatorKind": "HUMAN",
                    "source": "APP",
                    "metadata": {},
                    "label": "good",
                }
            },
        )
        assert result.errors, "annotating a session in an unreadable project must be rejected"
        assert any("could not find" in e.message.lower() for e in result.errors), result.errors

        async with db() as session:
            count = await session.scalar(
                select(func.count()).select_from(models.ProjectSessionAnnotation)
            )
        assert count == 0

    async def test_succeeds_on_readable_session(
        self,
        db: DbSessionFactory,
        asgi_app: ASGIApp,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        granted = await _seed_project_with_session(db, name=f"granted-{token_hex(4)}")
        user_id, token = await _create_member_with_token(db)
        await _grant_project_read(
            db, monkeypatch, tmp_path, user_id=user_id, project_name=granted["project_name"]
        )

        httpx_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        )
        gql_client = AsyncGraphQLClient(httpx_client)

        mutation = """
        mutation ($input: CreateProjectSessionAnnotationInput!) {
          createProjectSessionAnnotations(input: $input) {
            projectSessionAnnotation { id }
          }
        }
        """
        result = await gql_client.execute(
            mutation,
            {
                "input": {
                    "projectSessionId": _gid("ProjectSession", granted["session_rowid"]),
                    "name": "quality",
                    "annotatorKind": "HUMAN",
                    "source": "APP",
                    "metadata": {},
                    "label": "good",
                }
            },
        )
        assert not result.errors, result.errors
        assert result.data is not None
