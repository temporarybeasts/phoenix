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

Key: TypeAlias = tuple[TraceRowId, ProjectId]
Result: TypeAlias = int

_ERROR_STATUS = "ERROR"


class TraceErrorCountDataLoader(DataLoader[Key, Result]):
    """Counts spans under each trace with `status_code == 'ERROR'`.

    Cheap, backed by a single `COUNT(*)`. Missing keys return 0.

    We intentionally do not reuse the pre-aggregated `Span.cumulative_error_count`
    column. That column stores the error count of the subtree rooted at each
    span, so recovering the per-trace total would require either ``max()`` over
    a well-defined root or a sum across orphan-root candidates — both of which
    re-introduce the orphan-root handling complexity that `Trace.spans` already
    encodes for pagination. A direct `COUNT(*)` over `status_code == 'ERROR'`
    sidesteps that branch and is correct regardless of parent/child topology.
    """

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
                select(Span.trace_rowid, func.count())
                .where(Span.trace_rowid.in_(trace_rowids))
                .where(Span.status_code == _ERROR_STATUS)
                .group_by(Span.trace_rowid)
            )
            async with project_scoped_read_connection(self._db, project_id) as session:
                async for trace_rowid, cnt in await session.stream(stmt):
                    result[trace_rowid, project_id] = cnt
        return [result.get(key, 0) for key in keys]
