"""End-to-end proof that row-level isolation (RLS) reaches agent-assistant
tool execution -- the question left open when auditing the ~65 agent tools:
of the 4 genuinely server-side ("internal") tools, `write_span_note` writes
directly to the DB and `bash` executes arbitrary GraphQL in-process, both via
`request.app.state.db`/`build_graphql_context`. Both *should* inherit RLS via
`current_user_var` (the ContextVar `app.py`'s `_set_db_isolation_guards`
reads, populated once per HTTP request by `CurrentUserMiddleware`) -- but
`bash.py` explicitly threads the requesting user into `build_graphql_context`
rather than relying on that ContextVar being ambiently correct, which is
suspicious enough to verify rather than assume.

Drives a real HTTP request through the real ASGI middleware stack (real
`CurrentUserMiddleware`, real `AuthenticationMiddleware` validating a real
minted JWT, real `routers/agents.py` chat handler, real `Agent.run()`, real
tool execution, real Postgres with RLS active) -- the only mocked seam is the
literal network call to an LLM provider, replaced with a scripted
`FunctionModel` that deterministically calls the tool under test. This is an
in-process test (`httpx.ASGITransport`), not a subprocess integration test,
because the question under test is about asyncio/ASGI ContextVar propagation,
not about process or network boundaries -- monkeypatching the model call
would be impossible across a subprocess boundary, and isn't needed to
exercise the real mechanism.

Postgres-only: RLS does not exist on SQLite.
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from typing import Any, AsyncIterator

import httpx
import pytest
import yaml
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pytest_postgresql.janitor import DatabaseJanitor
from sqlalchemy import URL, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp

from phoenix.db import models
from phoenix.db.engines import aio_postgresql_engine
from phoenix.server.access import resolution
from phoenix.server.agents.model_selection import BuiltInProviderModelSelection
from phoenix.server.app import create_app
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

pytestmark = pytest.mark.postgres_only

_SECRET = SecretStr("test-secret-at-least-32-chars-long!!")
_BUILD_MODEL_PATCH_TARGET = "phoenix.server.api.routers.agents.build_model"
_MODEL_SELECTION = BuiltInProviderModelSelection(
    provider_type="builtin", provider="OPENAI", model_name="gpt-test"
)


@pytest.fixture(scope="function")
async def migrated_postgresql_engine(postgresql_proc: Any) -> AsyncIterator[AsyncEngine]:
    """A freshly created Postgres database migrated via real Alembic -- the
    GRANT/RLS/POLICY DDL that makes this test meaningful only exists in
    migrations, not in the `create_all`-based fixture other unit tests share.
    """
    dbname = f"phoenix_agent_isolation_test_{os.getpid()}_{token_hex(4)}"
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


@pytest.fixture
def db(migrated_postgresql_engine: AsyncEngine) -> DbSessionFactory:
    from phoenix.server.app import _db

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


@pytest.fixture
def httpx_client(asgi_app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app), base_url="http://test")


async def _create_member_with_token(db: DbSessionFactory) -> tuple[int, str]:
    """A real MEMBER user plus a real, validly-signed bearer token minted the
    same way login does (`JwtStore`) -- not a bypass of `AuthenticationMiddleware`.
    """
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


async def _seed_project_with_span(db: DbSessionFactory, *, name: str) -> tuple[int, str]:
    """One project with one trace and one span. Returns (project_id, span_id)
    -- `span_id` is the 16-hex-char OTel id `write_span_note` targets."""
    now = datetime.now(timezone.utc)
    otel_span_id = token_hex(8)
    async with db() as session:
        project = models.Project(name=name)
        session.add(project)
        await session.flush()
        trace = models.Trace(
            project_rowid=project.id,
            trace_id=token_hex(16),
            start_time=now,
            end_time=now,
        )
        session.add(trace)
        await session.flush()
        span = models.Span(
            trace_rowid=trace.id,
            span_id=otel_span_id,
            parent_id=None,
            name=f"span-in-{name}",
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
        return project.id, otel_span_id


async def _grant_project_read(
    db: DbSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    *,
    user_id: int,
    project_name: str,
) -> None:
    """Grants access the only way it's now possible: an IdP group mapped to
    the project via the declarative config file, with the user directly
    seeded as a member of that group -- standing in for what a real OIDC
    login would populate on `users.idp_groups`."""
    group_name = f"group-{token_hex(4)}"
    mapping_file = tmp_path / f"mapping-{token_hex(4)}.yaml"
    mapping_file.write_text(
        yaml.dump([{"idp_group": group_name, "projects": [project_name], "role": "viewer"}])
    )
    monkeypatch.setenv("PHOENIX_ACCESS_CONTROL_GROUP_MAPPING_FILE", str(mapping_file))
    resolution._mapping_cache = None
    async with db() as session:
        await session.execute(
            update(models.User).where(models.User.id == user_id).values(idp_groups=[group_name])
        )


async def _create_agent_session(db: DbSessionFactory, *, user_id: int) -> str:
    from strawberry.relay import GlobalID

    from phoenix.config import get_env_phoenix_agents_assistant_project_name

    async with db() as session:
        agent_session = models.AgentSession(
            user_id=user_id,
            title="",
            project_name=get_env_phoenix_agents_assistant_project_name(),
            is_ephemeral=False,
            model_provider=_MODEL_SELECTION.provider,
            model_name=_MODEL_SELECTION.model_name,
            custom_provider_id=None,
        )
        session.add(agent_session)
        await session.flush()
        return str(GlobalID("AgentSession", str(agent_session.id)))


def _chat_body(session_id: str, *, message_text: str) -> dict[str, Any]:
    return {
        "trigger": "submit-message",
        "id": session_id,
        "headless": False,
        "model": {
            "providerType": "builtin",
            "provider": "OPENAI",
            "modelName": "gpt-test",
        },
        "message": {
            "id": "11111111-1111-4111-8111-111111111111",
            "role": "user",
            "parts": [{"type": "text", "text": message_text}],
        },
    }


def _tool_call_once_model(*, tool_name: str, args: dict[str, Any]) -> FunctionModel:
    """Calls `tool_name` with `args` on the turn's first model round, then
    replies with plain text once the tool has returned (matching
    `test_agents_router.py`'s established `_scripted_model` shape)."""

    async def stream_function(
        messages: list[ModelMessage],
        agent_info: AgentInfo,
    ) -> AsyncIterator[str | DeltaToolCalls]:
        from pydantic_ai.messages import ToolReturnPart

        already_ran = any(
            isinstance(part, ToolReturnPart) and part.tool_name == tool_name
            for part in messages[-1].parts
        )
        if not already_ran:
            yield {1: DeltaToolCall(name=tool_name, json_args=json.dumps(args))}
        else:
            yield "done"

    def function(messages: list[ModelMessage], agent_info: AgentInfo) -> Any:
        # Only reached by the session-title auto-summarization request (a
        # separate, non-streamed call using the same model), not the chat
        # turn itself -- calling the expected "summary" tool keeps that call
        # from erroring out noisily in the logs; it isn't part of what this
        # test verifies.
        from pydantic_ai.messages import ModelResponse, ToolCallPart

        return ModelResponse(
            parts=[ToolCallPart(tool_name="summary", args={"summary": "isolation probe"})]
        )

    return FunctionModel(function=function, stream_function=stream_function)


class TestWriteSpanNoteDbIsolation:
    async def test_write_span_note_denied_for_ungranted_project(
        self,
        db: DbSessionFactory,
        httpx_client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        granted_project_name = f"granted-{token_hex(4)}"
        _granted_project_id, _granted_span_id = await _seed_project_with_span(
            db, name=granted_project_name
        )
        _ungranted_project_id, ungranted_span_id = await _seed_project_with_span(
            db, name=f"ungranted-{token_hex(4)}"
        )
        user_id, token = await _create_member_with_token(db)
        await _grant_project_read(
            db, monkeypatch, tmp_path, user_id=user_id, project_name=granted_project_name
        )
        agent_session_id = await _create_agent_session(db, user_id=user_id)

        async def _fake_build_model(*args: object, **kwargs: object) -> FunctionModel:
            return _tool_call_once_model(
                tool_name="write_span_note",
                args={"spanId": ungranted_span_id, "note": "isolation probe"},
            )

        monkeypatch.setattr(_BUILD_MODEL_PATCH_TARGET, _fake_build_model)

        response = await httpx_client.post(
            f"/v1/agent_sessions/{agent_session_id}/chat",
            headers={"Authorization": f"Bearer {token}"},
            json=_chat_body(agent_session_id, message_text="add a note to that span"),
        )
        assert response.status_code == 200, response.text

        # Bypass RLS (superuser-equivalent connecting role) to check ground
        # truth: did the write actually land, regardless of what the tool
        # call reported back to the model.
        async with db() as session:
            annotation_count = await session.scalar(
                select(models.SpanAnnotation)
                .join(models.Span, models.Span.id == models.SpanAnnotation.span_rowid)
                .where(
                    models.Span.span_id == ungranted_span_id,
                    models.SpanAnnotation.identifier == "pxi",
                )
                .with_only_columns(text("count(*)"))
            )
        assert annotation_count == 0, (
            "write_span_note wrote a note to a span in a project the member was never "
            "granted access to -- current_user_var did not correctly restrict the "
            "underlying DB session to the member's actual project access."
        )


class TestBashToolDbIsolation:
    async def test_bash_graphql_denied_for_ungranted_project(
        self,
        db: DbSessionFactory,
        httpx_client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        from strawberry.relay import GlobalID

        granted_project_name = f"granted-{token_hex(4)}"
        ungranted_project_name = f"ungranted-{token_hex(4)}"
        _granted_project_id, _granted_span_id = await _seed_project_with_span(
            db, name=granted_project_name
        )
        _ungranted_project_id, ungranted_span_id = await _seed_project_with_span(
            db, name=ungranted_project_name
        )
        async with db() as session:
            ungranted_span_rowid = await session.scalar(
                select(models.Span.id).where(models.Span.span_id == ungranted_span_id)
            )
        assert ungranted_span_rowid is not None
        span_gid = str(GlobalID("Span", str(ungranted_span_rowid)))
        # The distinctive marker to look for: `id` alone needs no DB fetch
        # (Query.node constructs `Span(id=node_id)` directly from the decoded
        # input, per queries.py -- confirmed by reading it after an earlier
        # version of this test passed for the wrong reason). `name` is a real
        # field resolver that has to hit the DB, which is what actually
        # exercises RLS.
        ungranted_span_name = f"span-in-{ungranted_project_name}"

        user_id, token = await _create_member_with_token(db)
        await _grant_project_read(
            db, monkeypatch, tmp_path, user_id=user_id, project_name=granted_project_name
        )
        agent_session_id = await _create_agent_session(db, user_id=user_id)

        # GraphQL string literals need double quotes; the whole query is then
        # single-quoted for the shell, matching test_bash.py's own convention
        # (`phoenix-gql '{ hello }'`) -- `span_gid` is base64 (+/=), none of
        # which need escaping inside either layer of quoting.
        query = f'{{ node(id: "{span_gid}") {{ id ... on Span {{ name }} }} }}'

        async def _fake_build_model(*args: object, **kwargs: object) -> FunctionModel:
            return _tool_call_once_model(
                tool_name="bash",
                args={
                    "summary": "look up the span",
                    "command": f"phoenix-gql --data-only '{query}'",
                },
            )

        monkeypatch.setattr(_BUILD_MODEL_PATCH_TARGET, _fake_build_model)

        response = await httpx_client.post(
            f"/v1/agent_sessions/{agent_session_id}/chat",
            headers={"Authorization": f"Bearer {token}"},
            json=_chat_body(agent_session_id, message_text="look up that span for me"),
        )
        assert response.status_code == 200, response.text
        # `span_gid` alone proves nothing -- `node(id:)` returns it as an echo
        # of the decoded input with no DB fetch either way (queries.py builds
        # `Span(id=node_id)` directly). `name` is a real field resolver that
        # has to hit the DB, so its presence in the tool's *output* (not the
        # echoed *input*, which the command string also legitimately embeds)
        # is the actual signal.
        output_chunks = [
            json.loads(line[len("data: ") :])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        tool_outputs = [
            chunk for chunk in output_chunks if chunk.get("type") == "tool-output-available"
        ]
        assert tool_outputs, f"bash tool never returned output: {response.text}"
        stdout = json.dumps(tool_outputs[-1]["output"])
        assert ungranted_span_name not in stdout, (
            "the bash tool's in-process GraphQL dispatch resolved a Span node's real data "
            "from a project the member was never granted access to -- build_graphql_context's "
            "explicit user threading fixed app-layer authorization but not "
            f"current_user_var, the separate ContextVar RLS actually reads. stdout={stdout}"
        )
