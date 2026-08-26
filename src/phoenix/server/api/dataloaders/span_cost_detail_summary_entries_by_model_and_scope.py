"""Batch model token-detail summaries by project and time range."""

from collections import defaultdict
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
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


@dataclass(frozen=True)
class CostDetailSummaryScope:
    """Optional project and time filters narrowing a token-detail summary.

    Every key carrying the same scope is answered by a single query, so the
    scope is what the loader batches on. The default scope is unfiltered.
    """

    project_id: Optional[int] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None


@dataclass(frozen=True)
class GenerativeModelCostDetailSummaryKey:
    """Identifies one model's token details within a scope."""

    model_id: int
    scope: CostDetailSummaryScope = CostDetailSummaryScope()


CostDetailSummaryEntries: TypeAlias = list[SpanCostDetailSummaryEntry]


class SpanCostDetailSummaryEntriesByModelAndScopeDataLoader(
    DataLoader[GenerativeModelCostDetailSummaryKey, CostDetailSummaryEntries]
):
    """Loads model token details with one aggregate query per unique scope."""

    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(
        self, keys: list[GenerativeModelCostDetailSummaryKey]
    ) -> list[CostDetailSummaryEntries]:
        model_ids_by_scope: defaultdict[CostDetailSummaryScope, set[int]] = defaultdict(set)
        for key in keys:
            model_ids_by_scope[key.scope].add(key.model_id)

        summaries: defaultdict[tuple[CostDetailSummaryScope, int], CostDetailSummaryEntries] = (
            defaultdict(list)
        )
        for scope, model_ids in model_ids_by_scope.items():
            stmt = _build_cost_detail_summary_statement(model_ids=model_ids, scope=scope)
            # A scope with a project_id is a real per-project query and can
            # be routed into that project's own schema. A scope with no
            # project_id is an intentionally cross-project, org-wide
            # rollup (e.g. "total cost of this model across every
            # project") -- there's no single project schema to route it
            # into, so it stays on the shared engine, same as the other
            # intentionally-cross-project cost/model aggregate loaders.
            # Post-cutover this only reflects whatever historical data
            # Stage 4b-2c's copy-only migration left in the shared schema,
            # not new per-project writes -- an accepted, known limitation,
            # not silently wrong.
            cm: AbstractAsyncContextManager[AsyncSession]
            if scope.project_id is not None:
                cm = project_scoped_read_connection(self._db, scope.project_id)
            else:
                cm = self._db.read()
            async with cm as session:
                rows = await session.stream(stmt)
                async for model_id, token_type, is_prompt, cost, tokens in rows:
                    summaries[(scope, model_id)].append(
                        SpanCostDetailSummaryEntry(
                            token_type=token_type,
                            is_prompt=is_prompt,
                            value=CostBreakdown(tokens=tokens, cost=cost),
                        )
                    )

        return [summaries[(key.scope, key.model_id)] for key in keys]


def _build_cost_detail_summary_statement(
    *,
    model_ids: set[int],
    scope: CostDetailSummaryScope,
) -> Select[tuple[Optional[int], str, bool, Optional[float], Optional[float]]]:
    """Aggregate token counts and costs by model, token type, and prompt kind."""
    statement = (
        select(
            models.SpanCost.model_id,
            models.SpanCostDetail.token_type,
            models.SpanCostDetail.is_prompt,
            coalesce(func.sum(models.SpanCostDetail.cost), 0).label("cost"),
            coalesce(func.sum(models.SpanCostDetail.tokens), 0).label("tokens"),
        )
        .select_from(models.SpanCostDetail)
        .join(
            models.SpanCost,
            models.SpanCostDetail.span_cost_id == models.SpanCost.id,
        )
        .where(models.SpanCost.model_id.in_(model_ids))
        .group_by(
            models.SpanCost.model_id,
            models.SpanCostDetail.token_type,
            models.SpanCostDetail.is_prompt,
        )
    )
    if scope.project_id is not None:
        statement = statement.join(
            models.Trace,
            models.SpanCost.trace_rowid == models.Trace.id,
        ).where(models.Trace.project_rowid == scope.project_id)
    if scope.start_time is not None:
        statement = statement.where(models.SpanCost.span_start_time >= scope.start_time)
    if scope.end_time is not None:
        statement = statement.where(models.SpanCost.span_start_time < scope.end_time)
    return statement
