from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import contains_eager
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias

from phoenix.db import models
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.api.dataloaders.types import (
    CostBreakdown,
    SpanCostDetailSummaryEntry,
)
from phoenix.server.types import DbSessionFactory

SpanRowID: TypeAlias = int
ProjectId: TypeAlias = int
Key: TypeAlias = tuple[SpanRowID, ProjectId]
Result: TypeAlias = list[SpanCostDetailSummaryEntry]


class SpanCostDetailSummaryEntriesBySpanDataLoader(DataLoader[Key, Result]):
    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(self, keys: list[Key]) -> list[Result]:
        by_project: dict[ProjectId, list[SpanRowID]] = defaultdict(list)
        for span_rowid, project_id in keys:
            by_project[project_id].append(span_rowid)
        results: defaultdict[Key, Result] = defaultdict(list)
        for project_id, span_rowids in by_project.items():
            async with project_scoped_read_connection(self._db, project_id) as session:
                async for span_cost_detail in await session.stream_scalars(
                    select(models.SpanCostDetail)
                    .join(models.SpanCost, models.SpanCostDetail.span_cost_id == models.SpanCost.id)
                    .where(models.SpanCost.span_rowid.in_(span_rowids))
                    .options(contains_eager(models.SpanCostDetail.span_cost))
                ):
                    entry = SpanCostDetailSummaryEntry(
                        token_type=span_cost_detail.token_type,
                        is_prompt=span_cost_detail.is_prompt,
                        value=CostBreakdown(
                            tokens=span_cost_detail.tokens,
                            cost=span_cost_detail.cost,
                        ),
                    )
                    results[span_cost_detail.span_cost.span_rowid, project_id].append(entry)
        return [list(results[key]) for key in keys]
