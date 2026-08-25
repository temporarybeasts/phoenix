from collections import defaultdict

from sqlalchemy import select
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias

from phoenix.db.models import TraceAnnotation
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.types import DbSessionFactory

TraceRowId: TypeAlias = int
ProjectId: TypeAlias = int
Key: TypeAlias = tuple[TraceRowId, ProjectId]
Result: TypeAlias = list[TraceAnnotation]


class TraceAnnotationsByTraceDataLoader(DataLoader[Key, Result]):
    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(self, keys: list[Key]) -> list[Result]:
        by_project: dict[ProjectId, list[TraceRowId]] = defaultdict(list)
        for trace_rowid, project_id in keys:
            by_project[project_id].append(trace_rowid)
        annotations_by_key: defaultdict[Key, Result] = defaultdict(list)
        for project_id, trace_rowids in by_project.items():
            async with project_scoped_read_connection(self._db, project_id) as session:
                async for annotation in await session.stream_scalars(
                    select(TraceAnnotation).where(TraceAnnotation.trace_rowid.in_(trace_rowids))
                ):
                    annotations_by_key[annotation.trace_rowid, project_id].append(annotation)
        return [annotations_by_key[key] for key in keys]
