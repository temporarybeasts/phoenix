from collections import defaultdict
from typing import Optional

from sqlalchemy import select
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias

from phoenix.db import models
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.types import DbSessionFactory

SpanRowId: TypeAlias = int
ProjectId: TypeAlias = int
Key: TypeAlias = tuple[SpanRowId, ProjectId]
Result: TypeAlias = Optional[models.SpanCost]


class SpanCostBySpanDataLoader(DataLoader[Key, Result]):
    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(self, keys: list[Key]) -> list[Result]:
        by_project: dict[ProjectId, list[SpanRowId]] = defaultdict(list)
        for span_rowid, project_id in keys:
            by_project[project_id].append(span_rowid)
        result: dict[Key, models.SpanCost] = {}
        for project_id, span_rowids in by_project.items():
            stmt = select(models.SpanCost).where(models.SpanCost.span_rowid.in_(span_rowids))
            async with project_scoped_read_connection(self._db, project_id) as session:
                async for sc in await session.stream_scalars(stmt):
                    result[sc.span_rowid, project_id] = sc
        return [result.get(key) for key in keys]
