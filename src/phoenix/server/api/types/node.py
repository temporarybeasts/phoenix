import re
from base64 import b64decode
from binascii import Error as BinasciiError
from typing import TYPE_CHECKING, cast

from strawberry.relay import GlobalID

_COMPOSITE_GLOBAL_ID_PATTERN = re.compile(r"[^:]+:[^:]+(:[^:]+)+")

if TYPE_CHECKING:
    from phoenix.db.models import SandboxBackendType


def is_composite_global_id(node_id: str) -> bool:
    try:
        decoded_node_id = b64decode(node_id).decode()
    except (BinasciiError, UnicodeDecodeError):
        return False
    return _COMPOSITE_GLOBAL_ID_PATTERN.match(decoded_node_id) is not None


def from_global_id(global_id: GlobalID) -> tuple[str, int]:
    """
    Decode the given global id into a type and id.

    :param global_id: The global id to decode.
    :return: A tuple of type and id.
    """
    return global_id.type_name, int(global_id.node_id)


def from_global_id_with_expected_type(global_id: GlobalID, expected_type_name: str) -> int:
    """
    Decodes the given global id and return the id, checking that the type
    matches the expected type.
    """
    type_name = global_id.type_name
    if type_name != expected_type_name:
        raise ValueError(
            f"The node id must correspond to a node of type {expected_type_name}, "
            f"but instead corresponds to a node of type: {type_name}"
        )
    try:
        return int(global_id.node_id)
    except ValueError as exc:
        raise ValueError(
            f"The node id must correspond to a node of type {expected_type_name}, "
            f"but the id is not a valid integer"
        ) from exc


def from_global_id_str_with_expected_type(global_id: GlobalID, expected_type_name: str) -> str:
    """Decode a GlobalID with a non-integer Relay node payload (type-checked)."""
    type_name = global_id.type_name
    if type_name != expected_type_name:
        raise ValueError(
            f"The node id must correspond to a node of type {expected_type_name}, "
            f"but instead corresponds to a node of type: {type_name}"
        )
    return str(global_id.node_id)


def get_sandbox_backend_type_from_global_id(global_id: GlobalID) -> "SandboxBackendType":
    return cast(
        "SandboxBackendType",
        from_global_id_str_with_expected_type(
            global_id,
            expected_type_name="SandboxProvider",
        ),
    )


def parse_project_scoped_node_id(node_id: str) -> tuple[int, int]:
    """Parses the compound "<project_id>:<row_id>" node id used by
    Trace/Span/ProjectSession (see Stage 4b-1 of the SSO/RBAC fork plan):
    once each project has its own Postgres schema, a bare row id is no
    longer globally unique, so these types encode both. `node_id` here is
    already the decoded remainder after the type name was split off (i.e.
    `GlobalID.node_id`, not the raw base64 string).
    """
    try:
        project_id_str, row_id_str = node_id.split(":", 1)
        return int(project_id_str), int(row_id_str)
    except ValueError:
        raise ValueError(f"Invalid project-scoped node id: {node_id}") from None


def from_project_scoped_global_id_with_expected_type(
    global_id: GlobalID, expected_type_name: str
) -> tuple[int, int]:
    """Decodes a compound "<project_id>:<row_id>" GlobalID (Trace/Span/
    ProjectSession from Stage 4b-1; the 4 annotation types from Stage
    4b-2f), checking that the type matches, and returns
    `(project_id, row_id)`. The `from_global_id_with_expected_type`
    counterpart above is for plain (non-project-scoped) node ids.
    """
    if global_id.type_name != expected_type_name:
        raise ValueError(
            f"The node id must correspond to a node of type {expected_type_name}, "
            f"but instead corresponds to a node of type: {global_id.type_name}"
        )
    return parse_project_scoped_node_id(global_id.node_id)
