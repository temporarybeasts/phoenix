"""The project-access permission catalog.

Fixed, code-owned set of permission strings, following the
``entity:operation`` convention from upstream's accepted RBAC spec
(``internal_docs/specs/rbac.md``). Deliberately small: this fork builds
project-level resource scoping only (see the SSO/RBAC fork plan, Stage 4),
not upstream's full general permission system. New permissions can be added
here without a migration, since they're never stored as rows -- only
``PROJECT_ROLE_PERMISSIONS`` values reference them, by string.
"""

import enum

PROJECT_READ = "project:read"
PROJECT_MANAGE_ACCESS = "project:manage-access"

ALL_PROJECT_PERMISSIONS = frozenset({PROJECT_READ, PROJECT_MANAGE_ACCESS})


class ProjectRole(str, enum.Enum):
    """A named role a group->project mapping entry can grant. Config-time
    convenience over the flat permission strings above -- not a parallel
    enforcement mechanism; every role expands to a fixed permission set at
    resolution time, and enforcement (RLS GUCs, app-layer checks) still
    operates on permission strings underneath."""

    VIEWER = "viewer"
    EDITOR = "editor"
    ADMIN = "admin"


# EDITOR intentionally grants the same permissions as VIEWER: the app layer
# defines no write-distinct permission today (225b4cdcd01a's own docstring
# notes it "draws no per-project read/write distinction"), so there's nothing
# for EDITOR to hold beyond PROJECT_READ yet. It exists as a forward-looking
# config label, not a functional distinction -- introducing a real
# project:write permission is a separate, larger change (it would touch
# enforcement call sites outside this package).
PROJECT_ROLE_PERMISSIONS: dict[ProjectRole, frozenset[str]] = {
    ProjectRole.VIEWER: frozenset({PROJECT_READ}),
    ProjectRole.EDITOR: frozenset({PROJECT_READ}),
    ProjectRole.ADMIN: frozenset({PROJECT_READ, PROJECT_MANAGE_ACCESS}),
}
