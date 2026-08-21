"""The project-access permission catalog.

Fixed, code-owned set of permission strings, following the
``entity:operation`` convention from upstream's accepted RBAC spec
(``internal_docs/specs/rbac.md``). Deliberately small: this fork builds
project-level resource scoping only (see the SSO/RBAC fork plan, Stage 4),
not upstream's full general permission system. New permissions can be added
here without a migration, since they're never stored as rows -- only
``ProjectGrant.permission`` values reference them, by string.
"""

PROJECT_READ = "project:read"
PROJECT_MANAGE_ACCESS = "project:manage-access"

ALL_PROJECT_PERMISSIONS = frozenset({PROJECT_READ, PROJECT_MANAGE_ACCESS})
