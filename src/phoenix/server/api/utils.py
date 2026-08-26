from sqlalchemy import delete

from phoenix.db import models
from phoenix.server.access.schema_provisioning import deprovision_project_schemas
from phoenix.server.types import DbSessionFactory


async def delete_projects(
    db: DbSessionFactory,
    *project_names: str,
) -> list[int]:
    if not project_names:
        return []
    stmt = (
        delete(models.Project)
        .where(models.Project.name.in_(set(project_names)))
        .returning(models.Project.id)
    )
    async with db() as session:
        project_ids = list(await session.scalars(stmt))
    await deprovision_project_schemas(db, project_ids)
    return project_ids


async def delete_traces(
    db: DbSessionFactory,
    *trace_ids: str,
) -> list[int]:
    # Stage 4b-2h: investigated and deliberately left unchanged. The traces
    # this is ever asked to delete already belong to a project that's being
    # deleted alongside it at every call site (Experiment.project_name is
    # unconditionally set at experiment creation, never left None in real
    # usage) -- deleting that Project row already cascades to remove them
    # (see deprovision_project_schemas's docstring), making this call
    # redundant for the case that matters in practice, in both flag states.
    # The one case it could uniquely still reach -- eval traces with no
    # owning project at all -- is the same OTel-ID-index gap already
    # tracked for Stage 4b-3 (no project_id to route to without one); not
    # worth solving here for what's likely an empty edge case in any real
    # database.
    if not trace_ids:
        return []
    stmt = (
        delete(models.Trace)
        .where(models.Trace.trace_id.in_(set(trace_ids)))
        .returning(models.Trace.id)
    )
    async with db() as session:
        return list(await session.scalars(stmt))
