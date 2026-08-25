from collections import defaultdict
from typing import Iterable, Union

from sqlalchemy import select
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias

from phoenix.db import models
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.types import DbSessionFactory

SpanRowId: TypeAlias = int
ProjectId: TypeAlias = int

Key: TypeAlias = tuple[SpanRowId, ProjectId]
Result: TypeAlias = models.Span


class SpanByIdDataLoader(DataLoader[Key, Result]):
    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(self, keys: Iterable[Key]) -> list[Union[Result, ValueError]]:
        keys = list(keys)
        by_project: dict[ProjectId, set[SpanRowId]] = defaultdict(set)
        for span_rowid, project_id in keys:
            by_project[project_id].add(span_rowid)
        spans: dict[Key, Result] = {}
        for project_id, span_rowids in by_project.items():
            stmt = select(models.Span).where(models.Span.id.in_(span_rowids))
            async with project_scoped_read_connection(self._db, project_id) as session:
                data = await session.stream_scalars(stmt)
                async for span in data:
                    spans[span.id, project_id] = span
        return [
            spans.get((span_rowid, project_id), ValueError("Invalid span row id"))
            for span_rowid, project_id in keys
        ]
