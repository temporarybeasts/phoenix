from typing import Any

import strawberry
from sqlalchemy.orm import QueryableAttribute
from strawberry import UNSET, Info
from typing_extensions import TypeAlias

from phoenix.server.api.context import Context
from phoenix.server.api.types.MimeType import MimeType

SpanRowId: TypeAlias = int


@strawberry.type
class SpanIOValue:
    span_rowid: strawberry.Private[SpanRowId] = UNSET
    # Schema-per-project (Stage 4b-1): only needed when `span_rowid`/`attr`
    # are used (the deferred-load path below); the `cached_value` path never
    # touches the loader, so it's not required there.
    project_id: strawberry.Private[int] = UNSET
    attr: strawberry.Private[QueryableAttribute[Any]] = UNSET
    cached_value: strawberry.Private[str] = UNSET
    mime_type: MimeType
    truncated_value: str = strawberry.field(
        default=UNSET,
        description="Truncated value up to 100 characters, appending '...' if truncated.",
    )

    def __post_init__(self) -> None:
        if self.cached_value is not UNSET:
            self.truncated_value = truncate_value(self.cached_value)
        elif (
            self.span_rowid is UNSET
            or self.attr is UNSET
            or self.project_id is UNSET
            or self.truncated_value is UNSET
        ):
            raise ValueError(
                "SpanIOValue must be initialized with either 'cached_value' or "
                "'truncated_value', 'span_rowid', 'attr', and 'project_id'."
            )

    @strawberry.field
    async def value(
        self,
        info: Info[Context, None],
    ) -> str:
        if self.cached_value is not UNSET:
            return self.cached_value
        if not self.truncated_value:
            return ""
        io_value = await info.context.data_loaders.span_fields.load(
            (self.span_rowid, self.attr, self.project_id)
        )
        return "" if io_value is None else str(io_value)


def truncate_value(value: str, chars: int = 100) -> str:
    return f"{value[: max(0, chars - 3)]}..." if len(value) > chars else value
