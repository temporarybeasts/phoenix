from collections import defaultdict
from typing import Iterable

from sqlalchemy import func, select
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias

from phoenix.db.models import Span
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.types import DbSessionFactory

TraceRowId: TypeAlias = int
ProjectId: TypeAlias = int
SpanKindStr: TypeAlias = str

Key: TypeAlias = tuple[TraceRowId, ProjectId]
Result: TypeAlias = list[tuple[SpanKindStr, int]]


class TraceSpanCountsByKindDataLoader(DataLoader[Key, Result]):
    """Counts spans per `(trace_rowid, span_kind)` pair.

    Returns, per trace, a list of `(span_kind, count)` pairs in deterministic
    order (descending count, then ascending kind name). Absent kinds are
    omitted — callers can treat a missing kind as zero.
    """

    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(self, keys: Iterable[Key]) -> list[Result]:
        keys = list(keys)
        by_project: dict[ProjectId, list[TraceRowId]] = defaultdict(list)
        for trace_rowid, project_id in keys:
            by_project[project_id].append(trace_rowid)
        buckets: dict[Key, list[tuple[SpanKindStr, int]]] = defaultdict(list)
        for project_id, trace_rowids in by_project.items():
            stmt = (
                select(Span.trace_rowid, Span.span_kind, func.count().label("cnt"))
                .where(Span.trace_rowid.in_(trace_rowids))
                .group_by(Span.trace_rowid, Span.span_kind)
            )
            async with project_scoped_read_connection(self._db, project_id) as session:
                async for trace_rowid, span_kind, cnt in await session.stream(stmt):
                    buckets[trace_rowid, project_id].append((span_kind, cnt))
        # Sort each bucket deterministically: count desc, then kind asc.
        for key in buckets:
            buckets[key].sort(key=lambda row: (-row[1], row[0]))
        return [buckets.get(key, []) for key in keys]
