from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from typing import Literal, Optional, cast

from openinference.semconv.trace import SpanAttributes
from sqlalchemy import Select, func, select
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias, assert_never

from phoenix.db import models
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.types import DbSessionFactory
from phoenix.trace.schemas import MimeType

SessionRowId: TypeAlias = int
ProjectId: TypeAlias = int
Key: TypeAlias = tuple[SessionRowId, ProjectId]


@dataclass(frozen=True)
class SessionIOValue:
    span_rowid: int
    truncated_value: str
    mime_type: MimeType


Result: TypeAlias = Optional[SessionIOValue]

Kind = Literal["first_input", "last_output"]


class SessionIODataLoader(DataLoader[Key, Result]):
    def __init__(self, db: DbSessionFactory, kind: Kind) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db
        self._kind = kind

    @cached_property
    def _subq(self) -> Select[tuple[Optional[int], int, str, str, int]]:
        stmt = (
            select(
                models.Trace.project_session_rowid.label("id_"),
                models.Span.id.label("span_rowid"),
            )
            .join_from(models.Span, models.Trace)
            .where(models.Span.parent_id.is_(None))
        )
        if self._kind == "first_input":
            stmt = stmt.add_columns(
                models.Span.input_value_first_101_chars.label("truncated_value"),
                models.Span.attributes[INPUT_MIME_TYPE].as_string().label("mime_type"),
                func.row_number()
                .over(
                    partition_by=models.Trace.project_session_rowid,
                    # Span.id tie-break keeps this in lockstep with session_aggregates' window.
                    order_by=[
                        models.Trace.start_time.asc(),
                        models.Trace.id.asc(),
                        models.Span.id.asc(),
                    ],
                )
                .label("rank"),
            )
        elif self._kind == "last_output":
            stmt = stmt.add_columns(
                models.Span.output_value_first_101_chars.label("truncated_value"),
                models.Span.attributes[OUTPUT_MIME_TYPE].as_string().label("mime_type"),
                func.row_number()
                .over(
                    partition_by=models.Trace.project_session_rowid,
                    # Span.id tie-break keeps this in lockstep with session_aggregates' window.
                    order_by=[
                        models.Trace.start_time.desc(),
                        models.Trace.id.desc(),
                        models.Span.id.desc(),
                    ],
                )
                .label("rank"),
            )
        else:
            assert_never(self._kind)
        return cast(Select[tuple[Optional[int], int, str, str, int]], stmt)

    def _stmt(self, *keys: SessionRowId) -> Select[tuple[int, int, str, str]]:
        subq = self._subq.where(models.Trace.project_session_rowid.in_(keys)).subquery()
        return (
            select(
                subq.c.id_,
                subq.c.span_rowid,
                subq.c.truncated_value,
                subq.c.mime_type,
            )
            .filter_by(rank=1)
            .where(subq.c.truncated_value.isnot(None))
        )

    async def _load_fn(self, keys: list[Key]) -> list[Result]:
        by_project: dict[ProjectId, list[SessionRowId]] = defaultdict(list)
        for session_rowid, project_id in keys:
            by_project[project_id].append(session_rowid)
        result: dict[Key, SessionIOValue] = {}
        for project_id, session_rowids in by_project.items():
            async with project_scoped_read_connection(self._db, project_id) as session:
                async for id_, span_rowid, truncated_value, mime_type in await session.stream(
                    self._stmt(*session_rowids)
                ):
                    if id_ is not None:
                        result[id_, project_id] = SessionIOValue(
                            span_rowid=span_rowid,
                            truncated_value=truncated_value,
                            mime_type=MimeType(mime_type),
                        )
        return [result.get(key) for key in keys]


INPUT_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE.split(".")
OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE.split(".")
