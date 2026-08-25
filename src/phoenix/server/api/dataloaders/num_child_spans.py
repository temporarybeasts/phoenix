from collections import defaultdict
from typing import Iterable

from sqlalchemy import func, select
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias

from phoenix.db import models
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.types import DbSessionFactory

SpanRowId: TypeAlias = int
ProjectId: TypeAlias = int

Key: TypeAlias = tuple[SpanRowId, ProjectId]
Result: TypeAlias = int


class NumChildSpansDataLoader(DataLoader[Key, Result]):
    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(self, keys: Iterable[Key]) -> list[Result]:
        keys = list(keys)
        by_project: dict[ProjectId, set[SpanRowId]] = defaultdict(set)
        for span_rowid, project_id in keys:
            by_project[project_id].add(span_rowid)
        result: dict[Key, Result] = {}
        for project_id, span_rowids in by_project.items():
            children = select(models.Span).alias("children")
            stmt = (
                select(models.Span.id, func.count())
                .where(models.Span.id.in_(span_rowids))
                .join(children, children.c.parent_id == models.Span.span_id)
                .group_by(models.Span.id)
            )
            async with project_scoped_read_connection(self._db, project_id) as session:
                data = await session.stream(stmt)
                async for span_rowid, num_child_spans in data:
                    result[span_rowid, project_id] = num_child_spans
        return [result.get(key, 0) for key in keys]
