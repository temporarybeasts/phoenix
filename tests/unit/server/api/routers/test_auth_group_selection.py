"""Verifies that local login (`/auth/login`) signals when a multi-group
user must pick which project group they're viewing before entering the
app -- a `{"requiresGroupSelection": true}` JSON body (status 200) instead
of the usual empty 204, since a 204 response can't carry a body. See
`_create_auth_response` in `phoenix.server.api.routers.auth`.

Zero- and single-group logins are unaffected (still a bare 204): zero
groups has nothing to select, and a single group is the implicit active
group with no picker needed (see `phoenix.server.access.resolution`).
"""

from __future__ import annotations

import contextlib
import secrets
from datetime import timedelta
from typing import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select
from starlette.types import ASGIApp

from phoenix.auth import (
    DEFAULT_SECRET_LENGTH,
    PHOENIX_ACTIVE_PROJECT_GROUP_COOKIE_NAME,
    compute_password_hash,
)
from phoenix.db import models
from phoenix.server.app import create_app
from phoenix.server.types import DbSessionFactory
from tests.unit.conftest import TestBulkInserter, patch_batched_caller, patch_grpc_server

_SECRET = SecretStr("test-secret-at-least-32-chars-long!!")
_PASSWORD = "a-perfectly-fine-password-123"


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
            access_token_expiry=timedelta(minutes=10),
            refresh_token_expiry=timedelta(days=7),
        )


@pytest.fixture
async def asgi_app(app: FastAPI) -> AsyncIterator[ASGIApp]:
    async with LifespanManager(app) as manager:
        yield manager.app


@pytest.fixture
def httpx_client(asgi_app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app), base_url="http://test")


async def _create_local_user(db: DbSessionFactory, *, idp_groups: list[str]) -> str:
    """Creates a LOCAL user with a known password and the given
    (test-only) idp_groups already populated, standing in for what a real
    OIDC login would have set -- LOCAL auth normally never touches
    idp_groups, but nothing about the column is OIDC-specific. Returns the
    user's email."""
    email = f"user-{secrets.token_hex(4)}@example.com"
    async with db() as session:
        role_id = await session.scalar(
            select(models.UserRole.id).where(models.UserRole.name == "MEMBER")
        )
        assert role_id is not None
        salt = secrets.token_bytes(DEFAULT_SECRET_LENGTH)
        user = models.User(
            user_role_id=role_id,
            username=f"user-{secrets.token_hex(4)}",
            email=email,
            password_hash=compute_password_hash(password=SecretStr(_PASSWORD), salt=salt),
            password_salt=salt,
            reset_password=False,
            auth_method="LOCAL",
            idp_groups=idp_groups,
        )
        session.add(user)
    return email


async def _create_mapping(db: DbSessionFactory, external_role: str, group_name: str) -> int:
    async with db() as session:
        group = models.ProjectGroup(name=group_name)
        session.add(group)
        await session.flush()
        session.add(
            models.ExternalRoleProjectGroupMapping(
                external_role=external_role, project_group_id=group.id, role="VIEWER"
            )
        )
        return group.id


async def test_zero_groups_returns_bare_204(
    db: DbSessionFactory, httpx_client: httpx.AsyncClient
) -> None:
    email = await _create_local_user(db, idp_groups=[])
    response = await httpx_client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    assert response.status_code == 204
    assert not response.content
    assert PHOENIX_ACTIVE_PROJECT_GROUP_COOKIE_NAME not in response.cookies


async def test_single_group_returns_204_and_sets_active_group_cookie(
    db: DbSessionFactory, httpx_client: httpx.AsyncClient
) -> None:
    group_id = await _create_mapping(db, "role-a", "group-a")
    email = await _create_local_user(db, idp_groups=["role-a"])
    response = await httpx_client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    assert response.status_code == 204
    assert not response.content
    assert response.cookies[PHOENIX_ACTIVE_PROJECT_GROUP_COOKIE_NAME] == str(group_id)


async def test_multiple_groups_requires_selection(
    db: DbSessionFactory, httpx_client: httpx.AsyncClient
) -> None:
    await _create_mapping(db, "role-a", "group-a")
    await _create_mapping(db, "role-b", "group-b")
    email = await _create_local_user(db, idp_groups=["role-a", "role-b"])
    response = await httpx_client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    assert response.status_code == 200
    assert response.json() == {"requiresGroupSelection": True}
    # No active group can be chosen on the user's behalf -- the frontend
    # must show the picker, not silently land on one of the two groups.
    assert PHOENIX_ACTIVE_PROJECT_GROUP_COOKIE_NAME not in response.cookies
    # Still logged in -- access/refresh cookies are set the same as any
    # other successful login.
    assert "phoenix-access-token" in response.cookies
    assert "phoenix-refresh-token" in response.cookies


async def test_create_project_succeeds_when_no_oauth2_client_configures_groups(
    db: DbSessionFactory, httpx_client: httpx.AsyncClient
) -> None:
    """This app fixture is authenticated (`authentication_enabled=True`)
    but configures zero OAuth2 clients -- the `external_role_project_group_mappings`
    table is therefore necessarily empty, meaning project-group RBAC isn't
    in use in this deployment at all (see
    `resolution._project_group_rbac_in_use`). A local user, who never has
    `idp_groups` populated by anything, must still be able to create a
    project; it should land in the well-known default project group, the
    same fallback ingest and other group-less creation paths already use.
    This guards the fix in `get_active_project_group_id_for_create`:
    without the RBAC-in-use gate, *any* authenticated deployment --
    including a plain basic-auth-only one with no IdP/RBAC in use at all
    -- would incorrectly reject every local user's project creation."""
    email = await _create_local_user(db, idp_groups=[])
    login_response = await httpx_client.post(
        "/auth/login", json={"email": email, "password": _PASSWORD}
    )
    assert login_response.status_code == 204

    project_name = f"project-{secrets.token_hex(4)}"
    response = await httpx_client.post(
        "/graphql",
        json={
            "query": (
                "mutation($input: CreateProjectInput!) { "
                "createProject(input: $input) { project { id name } } }"
            ),
            "variables": {"input": {"name": project_name}},
        },
    )
    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["createProject"]["project"]["name"] == project_name

    async with db() as session:
        project_group_name = await session.scalar(
            select(models.ProjectGroup.name)
            .join(models.Project, models.Project.project_group_id == models.ProjectGroup.id)
            .where(models.Project.name == project_name)
        )
    assert project_group_name == "default"
