from collections import defaultdict
from typing import Iterable

from sqlalchemy import func, select
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias

from phoenix.db.models import Span, Trace
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.types import DbSessionFactory

TraceRowId: TypeAlias = int
ProjectId: TypeAlias = int

Key: TypeAlias = tuple[TraceRowId, ProjectId]
Result: TypeAlias = int


class NumSpansPerTraceDataLoader(DataLoader[Key, Result]):
    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(self, keys: Iterable[Key]) -> list[Result]:
        keys = list(keys)
        by_project: dict[ProjectId, list[TraceRowId]] = defaultdict(list)
        for trace_rowid, project_id in keys:
            by_project[project_id].append(trace_rowid)
        result: dict[Key, Result] = {}
        for project_id, trace_rowids in by_project.items():
            stmt = (
                select(Trace.id, func.count())
                .join(Span)
                .where(Trace.id.in_(trace_rowids))
                .group_by(Trace.id)
            )
            async with project_scoped_read_connection(self._db, project_id) as session:
                async for id_, cnt in await session.stream(stmt):
                    result[id_, project_id] = cnt
        return [result.get(key, 0) for key in keys]
