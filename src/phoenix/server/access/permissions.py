"""The project-group access permission catalog.

Fixed, code-owned set of permission strings, following the
``entity:operation`` convention from upstream's accepted RBAC spec
(``internal_docs/specs/rbac.md``). Deliberately small: this fork builds
project-group-level resource scoping only, not upstream's full general
permission system. New permissions can be added here without a migration,
since they're never stored as rows -- only ``PROJECT_ROLE_PERMISSIONS``
values reference them, by string.

``ProjectGroupRole`` intentionally echoes the vocabulary of the existing
global ``UserRoleName`` (``phoenix.db.models``, ``SYSTEM``/``ADMIN``/
``MEMBER``/``VIEWER``) for naming consistency, but is a distinct concept: a
per-project-group role granted via an external-role mapping
(``ExternalRoleProjectGroupMapping``), not the global per-account role.
Stored as a plain ``Literal`` (not a runtime ``enum.Enum``), matching the
existing ``UserRoleName``/``AuthMethod`` house style for role-name columns.
"""

from typing import Literal

from typing_extensions import TypeAlias

PROJECT_READ = "project:read"
PROJECT_WRITE = "project:write"
PROJECT_MANAGE_ACCESS = "project:manage-access"

ALL_PROJECT_PERMISSIONS = frozenset({PROJECT_READ, PROJECT_WRITE, PROJECT_MANAGE_ACCESS})

ProjectGroupRole: TypeAlias = Literal["VIEWER", "MEMBER", "ADMIN"]

PROJECT_GROUP_ROLES: tuple[ProjectGroupRole, ...] = ("VIEWER", "MEMBER", "ADMIN")

# Used to pick the highest-privilege role when a user holds more than one
# external role mapped into the same project group.
PROJECT_GROUP_ROLE_RANK: dict[ProjectGroupRole, int] = {
    "VIEWER": 0,
    "MEMBER": 1,
    "ADMIN": 2,
}

PROJECT_ROLE_PERMISSIONS: dict[ProjectGroupRole, frozenset[str]] = {
    "VIEWER": frozenset({PROJECT_READ}),
    "MEMBER": frozenset({PROJECT_READ, PROJECT_WRITE}),
    "ADMIN": frozenset({PROJECT_READ, PROJECT_WRITE, PROJECT_MANAGE_ACCESS}),
}

# Roles that can write into (and thus create projects in) a project group --
# used to compute the `app.writable_project_group_ids` RLS GUC.
WRITE_CAPABLE_PROJECT_GROUP_ROLES = frozenset({"MEMBER", "ADMIN"})
