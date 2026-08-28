import strawberry
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError as PostgreSQLIntegrityError
from sqlalchemy.orm import load_only
from sqlean.dbapi2 import IntegrityError as SQLiteIntegrityError  # type: ignore[import-untyped]
from strawberry import UNSET
from strawberry.relay import GlobalID
from strawberry.types import Info

from phoenix.auth import set_active_project_group_cookie
from phoenix.config import DEFAULT_PROJECT_NAME
from phoenix.db import models
from phoenix.server.access.active_group import resolve_active_project_group_switch
from phoenix.server.access.resolution import (
    get_active_project_group_id_for_create,
    get_default_project_group_id,
)
from phoenix.server.api.auth import IsNotReadOnly, IsNotViewer
from phoenix.server.api.context import Context
from phoenix.server.api.exceptions import BadRequest, Conflict
from phoenix.server.api.input_types.ClearProjectInput import ClearProjectInput
from phoenix.server.api.input_types.CreateProjectInput import CreateProjectInput
from phoenix.server.api.input_types.PatchProjectInput import PatchProjectInput
from phoenix.server.api.queries import Query
from phoenix.server.api.types.node import from_global_id_with_expected_type
from phoenix.server.api.types.Project import Project, to_gql_project
from phoenix.server.bearer_auth import PhoenixSystemUser
from phoenix.server.dml_event import ProjectDeleteEvent, ProjectInsertEvent, SpanDeleteEvent


@strawberry.type
class ProjectMutationPayload:
    project: Project
    query: Query


@strawberry.type
class ProjectMutationMixin:
    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsNotViewer])  # type: ignore
    async def create_project(
        self,
        info: Info[Context, None],
        input: CreateProjectInput,
    ) -> ProjectMutationPayload:
        if not (name := input.name.strip()):
            raise BadRequest("Name cannot be empty")
        description = (input.description or "").strip() or None
        gradient_start_color = (input.gradient_start_color or "").strip() or None
        gradient_end_color = (input.gradient_end_color or "").strip() or None
        try:
            async with info.context.db() as session:
                if info.context.auth_enabled:
                    # Falls back to the default project group internally
                    # when project-group RBAC isn't in use in this
                    # deployment at all (see
                    # `resolution._project_group_rbac_in_use`) -- a `None`
                    # here means RBAC *is* in use but this caller has no
                    # writable active group right now.
                    project_group_id = await get_active_project_group_id_for_create(
                        session, info.context.user
                    )
                    if project_group_id is None:
                        raise BadRequest(
                            "You must be viewing a project group with write access to "
                            "create a project -- select or switch to one first."
                        )
                else:
                    # No user/session concept at all with auth disabled --
                    # same landing spot as other group-less creation paths
                    # (OTLP ingest, the admin-only REST endpoint).
                    project_group_id = await get_default_project_group_id(session)
                project = models.Project(
                    name=name,
                    description=description,
                    gradient_start_color=gradient_start_color,
                    gradient_end_color=gradient_end_color,
                    project_group_id=project_group_id,
                )
                session.add(project)
        except (PostgreSQLIntegrityError, SQLiteIntegrityError):
            raise Conflict(f"Project with name '{name}' already exists")
        info.context.event_queue.put(ProjectInsertEvent((project.id,)))
        return ProjectMutationPayload(project=to_gql_project(project), query=Query())

    @strawberry.mutation(permission_classes=[IsNotReadOnly])  # type: ignore
    async def set_active_project_group(
        self,
        info: Info[Context, None],
        project_group_id: GlobalID,
    ) -> bool:
        """Switches the caller's active/"viewing" project group for the
        rest of this browser session -- validates the caller currently
        holds a role in the target group before switching. Takes effect on
        the caller's very next request (the active-group cookie is read
        fresh every request, see ``phoenix.server.access.context``)."""
        if not info.context.auth_enabled:
            raise BadRequest("Project groups require authentication to be enabled")
        raw_project_group_id = from_global_id_with_expected_type(
            global_id=project_group_id, expected_type_name="ProjectGroup"
        )
        user = info.context.user
        if isinstance(user, PhoenixSystemUser):
            raise BadRequest("System users have no active project group to switch")
        async with info.context.db() as session:
            resolved = await resolve_active_project_group_switch(
                session, int(user.identity), raw_project_group_id
            )
        if resolved is None:
            raise BadRequest("You are not a member of that project group")
        set_active_project_group_cookie(
            response=info.context.get_response(), project_group_id=resolved
        )
        return True

    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsNotViewer])  # type: ignore
    async def delete_project(self, info: Info[Context, None], id: GlobalID) -> Query:
        project_id = from_global_id_with_expected_type(global_id=id, expected_type_name="Project")
        async with info.context.db() as session:
            project = await session.scalar(
                select(models.Project)
                .where(models.Project.id == project_id)
                .options(load_only(models.Project.name))
            )
            if project is None:
                raise ValueError(f"Unknown project: {id}")
            if project.name == DEFAULT_PROJECT_NAME:
                raise ValueError(f"Cannot delete the {DEFAULT_PROJECT_NAME} project")
            await session.delete(project)
        info.context.event_queue.put(ProjectDeleteEvent((project_id,)))
        return Query()

    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsNotViewer])  # type: ignore
    async def clear_project(self, info: Info[Context, None], input: ClearProjectInput) -> Query:
        project_id = from_global_id_with_expected_type(
            global_id=input.id, expected_type_name="Project"
        )
        delete_statement = (
            delete(models.Trace)
            .where(models.Trace.project_rowid == project_id)
            .returning(models.Trace.project_session_rowid)
        )
        if input.end_time:
            delete_statement = delete_statement.where(models.Trace.start_time < input.end_time)
        async with info.context.db() as session:
            deleted_trace_project_session_ids = await session.scalars(delete_statement)
            session_ids_to_delete = [
                id_ for id_ in set(deleted_trace_project_session_ids) if id_ is not None
            ]
            # Process deletions in chunks of 10000 to avoid PostgreSQL argument limit
            chunk_size = 10000
            stmt = delete(models.ProjectSession)
            for i in range(0, len(session_ids_to_delete), chunk_size):
                chunk = session_ids_to_delete[i : i + chunk_size]
                await session.execute(stmt.where(models.ProjectSession.id.in_(chunk)))
        info.context.event_queue.put(SpanDeleteEvent((project_id,)))
        return Query()

    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsNotViewer])  # type: ignore
    async def patch_project(
        self,
        info: Info[Context, None],
        input: PatchProjectInput,
    ) -> ProjectMutationPayload:
        project_id = from_global_id_with_expected_type(
            global_id=input.id, expected_type_name="Project"
        )
        async with info.context.db() as session:
            project = await session.scalar(
                select(models.Project).where(models.Project.id == project_id)
            )
            if project is None:
                raise ValueError(f"Unknown project: {input.id}")
            if input.description is not UNSET:
                project.description = (input.description or "").strip() or None
            if input.gradient_start_color is not UNSET:
                project.gradient_start_color = (
                    input.gradient_start_color or ""
                ).strip() or project.gradient_start_color
            if input.gradient_end_color is not UNSET:
                project.gradient_end_color = (
                    input.gradient_end_color or ""
                ).strip() or project.gradient_end_color
        return ProjectMutationPayload(project=to_gql_project(project), query=Query())
