from collections import defaultdict
from typing import Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import InstrumentedAttribute
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias, assert_never

from phoenix.db import models
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.types import DbSessionFactory

RowId: TypeAlias = int
ProjectId: TypeAlias = int
Key: TypeAlias = tuple[RowId, ProjectId]
Result: TypeAlias = Optional[str]

Kind = Literal["session", "trace"]


class UserIdsDataLoader(DataLoader[Key, Result]):
    """Loads the first non-null `user.id` span attribute for each session or trace,
    ordered by span start time."""

    def __init__(self, db: DbSessionFactory, kind: Kind) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db
        self._kind = kind

    async def _load_fn(self, keys: list[Key]) -> list[Result]:
        by_project: dict[ProjectId, list[RowId]] = defaultdict(list)
        for row_id, project_id in keys:
            by_project[project_id].append(row_id)

        id_col: InstrumentedAttribute[Optional[int]]
        if self._kind == "session":
            id_col = models.Trace.project_session_rowid
        elif self._kind == "trace":
            id_col = models.Span.trace_rowid
        else:
            assert_never(self._kind)
        user_id = models.Span.attributes[models.USER_ID].as_string()

        result: dict[Key, str] = {}
        for project_id, row_ids in by_project.items():
            stmt = (
                select(
                    id_col.label("id_"),
                    user_id.label("user_id"),
                    func.row_number()
                    .over(
                        partition_by=id_col,
                        order_by=[models.Span.start_time.asc(), models.Span.id.asc()],
                    )
                    .label("rank"),
                )
                .where(id_col.in_(row_ids))
                .where(user_id.is_not(None))
            )
            if self._kind == "session":
                stmt = stmt.join_from(models.Span, models.Trace)
            subq = stmt.subquery()
            async with project_scoped_read_connection(self._db, project_id) as session:
                async for id_, value in await session.stream(
                    select(subq.c.id_, subq.c.user_id).filter_by(rank=1)
                ):
                    if id_ is not None:
                        result[id_, project_id] = value
        return [result.get(key) for key in keys]
