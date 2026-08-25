from collections import defaultdict
from typing import Iterable, Optional

from sqlalchemy import select
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias

from phoenix.db import models
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.types import DbSessionFactory

TraceRowId: TypeAlias = int
SpanRowId: TypeAlias = int
ProjectId: TypeAlias = int

Key: TypeAlias = tuple[TraceRowId, ProjectId]
Result: TypeAlias = Optional[SpanRowId]


class TraceRootSpansDataLoader(DataLoader[Key, Result]):
    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(self, keys: Iterable[Key]) -> list[Result]:
        keys = list(keys)
        by_project: dict[ProjectId, set[TraceRowId]] = defaultdict(set)
        for trace_rowid, project_id in keys:
            by_project[project_id].add(trace_rowid)
        result: dict[Key, int] = {}
        for project_id, trace_rowids in by_project.items():
            stmt = (
                select(models.Trace.id, models.Span.id)
                .join(models.Trace)
                .where(models.Span.parent_id.is_(None))
                .where(models.Trace.id.in_(trace_rowids))
            )
            async with project_scoped_read_connection(self._db, project_id) as session:
                async for trace_rowid, span_rowid in await session.stream(stmt):
                    result[trace_rowid, project_id] = span_rowid
        return [result.get(key) for key in keys]
