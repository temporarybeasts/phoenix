from sqlalchemy import exists, select
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias

from phoenix.db import models
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.types import DbSessionFactory

Key: TypeAlias = int  # project rowid
Result: TypeAlias = bool


class ProjectHasTracesDataLoader(DataLoader[Key, Result]):
    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(self, keys: list[Key]) -> list[Result]:
        result: dict[Key, Result] = {}
        for project_id in set(keys):
            stmt = select(exists(select(1).select_from(models.Trace)))
            async with project_scoped_read_connection(self._db, project_id) as session:
                result[project_id] = bool(await session.scalar(stmt))
        return [result[key] for key in keys]
