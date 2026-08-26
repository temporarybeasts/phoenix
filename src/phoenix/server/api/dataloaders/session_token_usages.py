from collections import defaultdict

from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias

from phoenix.db.session_aggregates import SESSION_ROWID, token_counts_by_session
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.types import DbSessionFactory
from phoenix.trace.schemas import TokenUsage

SessionRowId: TypeAlias = int
ProjectId: TypeAlias = int
Key: TypeAlias = tuple[SessionRowId, ProjectId]
Result: TypeAlias = TokenUsage


class SessionTokenUsagesDataLoader(DataLoader[Key, Result]):
    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(self, keys: list[Key]) -> list[Result]:
        by_project: dict[ProjectId, list[SessionRowId]] = defaultdict(list)
        for session_rowid, project_id in keys:
            by_project[project_id].append(session_rowid)
        result: dict[Key, TokenUsage] = {}
        for project_id, session_rowids in by_project.items():
            stmt = token_counts_by_session().as_grouped_subquery(session_rowids)
            async with project_scoped_read_connection(self._db, project_id) as session:
                async for row in await session.stream(stmt):
                    id_ = row._mapping[SESSION_ROWID]
                    if id_ is not None:
                        result[id_, project_id] = TokenUsage(
                            prompt=row.prompt, completion=row.completion
                        )
        return [result.get(key, TokenUsage()) for key in keys]
