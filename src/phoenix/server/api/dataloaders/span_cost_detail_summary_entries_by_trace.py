from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.sql.functions import coalesce
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias

from phoenix.db import models
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.api.dataloaders.types import (
    CostBreakdown,
    SpanCostDetailSummaryEntry,
)
from phoenix.server.types import DbSessionFactory

TraceRowId: TypeAlias = int
ProjectId: TypeAlias = int
Key: TypeAlias = tuple[TraceRowId, ProjectId]
Result: TypeAlias = list[SpanCostDetailSummaryEntry]


class SpanCostDetailSummaryEntriesByTraceDataLoader(DataLoader[Key, Result]):
    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(self, keys: list[Key]) -> list[Result]:
        by_project: dict[ProjectId, list[TraceRowId]] = defaultdict(list)
        for trace_rowid, project_id in keys:
            by_project[project_id].append(trace_rowid)
        pk = models.SpanCost.trace_rowid
        results: defaultdict[Key, Result] = defaultdict(list)
        for project_id, trace_rowids in by_project.items():
            stmt = (
                select(
                    pk,
                    models.SpanCostDetail.token_type,
                    models.SpanCostDetail.is_prompt,
                    coalesce(func.sum(models.SpanCostDetail.cost), 0).label("cost"),
                    coalesce(func.sum(models.SpanCostDetail.tokens), 0).label("tokens"),
                )
                .select_from(models.SpanCostDetail)
                .join(models.SpanCost, models.SpanCostDetail.span_cost_id == models.SpanCost.id)
                .where(pk.in_(trace_rowids))
                .group_by(pk, models.SpanCostDetail.token_type, models.SpanCostDetail.is_prompt)
            )
            async with project_scoped_read_connection(self._db, project_id) as session:
                data = await session.stream(stmt)
                async for (
                    id_,
                    token_type,
                    is_prompt,
                    cost,
                    tokens,
                ) in data:
                    entry = SpanCostDetailSummaryEntry(
                        token_type=token_type,
                        is_prompt=is_prompt,
                        value=CostBreakdown(tokens=tokens, cost=cost),
                    )
                    results[id_, project_id].append(entry)
        return [list(results[key]) for key in keys]
