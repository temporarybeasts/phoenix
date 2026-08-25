from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.sql.functions import coalesce
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias

from phoenix.db import models
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.api.dataloaders.types import CostBreakdown, SpanCostSummary
from phoenix.server.types import DbSessionFactory

TraceRowId: TypeAlias = int
ProjectId: TypeAlias = int
Key: TypeAlias = tuple[TraceRowId, ProjectId]
Result: TypeAlias = SpanCostSummary


class SpanCostSummaryByTraceDataLoader(DataLoader[Key, Result]):
    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(self, keys: list[Key]) -> list[Result]:
        by_project: dict[ProjectId, list[TraceRowId]] = defaultdict(list)
        for trace_rowid, project_id in keys:
            by_project[project_id].append(trace_rowid)
        results: defaultdict[Key, Result] = defaultdict(SpanCostSummary)
        pk = models.SpanCost.trace_rowid
        for project_id, trace_rowids in by_project.items():
            stmt = (
                select(
                    pk,
                    coalesce(func.sum(models.SpanCost.prompt_cost), 0).label("prompt_cost"),
                    coalesce(func.sum(models.SpanCost.completion_cost), 0).label("completion_cost"),
                    coalesce(func.sum(models.SpanCost.total_cost), 0).label("total_cost"),
                    coalesce(func.sum(models.SpanCost.prompt_tokens), 0).label("prompt_tokens"),
                    coalesce(func.sum(models.SpanCost.completion_tokens), 0).label(
                        "completion_tokens"
                    ),
                    coalesce(func.sum(models.SpanCost.total_tokens), 0).label("total_tokens"),
                )
                .where(pk.in_(trace_rowids))
                .group_by(pk)
            )
            async with project_scoped_read_connection(self._db, project_id) as session:
                data = await session.stream(stmt)
                async for (
                    id_,
                    prompt_cost,
                    completion_cost,
                    total_cost,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                ) in data:
                    summary = SpanCostSummary(
                        prompt=CostBreakdown(tokens=prompt_tokens, cost=prompt_cost),
                        completion=CostBreakdown(tokens=completion_tokens, cost=completion_cost),
                        total=CostBreakdown(tokens=total_tokens, cost=total_cost),
                    )
                    results[id_, project_id] = summary
        return [results[key] for key in keys]
