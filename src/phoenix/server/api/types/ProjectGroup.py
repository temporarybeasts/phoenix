from typing import Optional

import strawberry
from strawberry.relay import Node, NodeID

from phoenix.db import models
from phoenix.server.access.permissions import ProjectGroupRole


@strawberry.type
class ProjectGroup(Node):
    id: NodeID[int]
    db_record: strawberry.Private[models.ProjectGroup]
    # The caller's own role in this group -- set by the resolver that
    # builds the list (see `phoenix.server.api.types.User.project_groups`),
    # not derived from `db_record`, since that carries no per-caller data.
    caller_role: strawberry.Private[Optional[ProjectGroupRole]] = None

    def __post_init__(self) -> None:
        if self.id != self.db_record.id:
            raise ValueError("ProjectGroup ID mismatch")

    @strawberry.field
    def name(self) -> str:
        return self.db_record.name

    @strawberry.field
    def description(self) -> Optional[str]:
        return self.db_record.description

    @strawberry.field
    def role(self) -> Optional[str]:
        """The caller's own role in this group (``VIEWER``/``MEMBER``/
        ``ADMIN``), or ``None`` if this ``ProjectGroup`` wasn't built via
        the caller-scoped listing that populates it."""
        return self.caller_role


def to_gql_project_group(
    project_group: models.ProjectGroup, *, caller_role: Optional[ProjectGroupRole] = None
) -> ProjectGroup:
    return ProjectGroup(id=project_group.id, db_record=project_group, caller_role=caller_role)
