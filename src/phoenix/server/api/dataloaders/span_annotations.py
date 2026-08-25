from collections import defaultdict

from sqlalchemy import select
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias

from phoenix.db.models import SpanAnnotation as ORMSpanAnnotation
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.types import DbSessionFactory

SpanRowId: TypeAlias = int
ProjectId: TypeAlias = int
Key: TypeAlias = tuple[SpanRowId, ProjectId]
Result: TypeAlias = list[ORMSpanAnnotation]


class SpanAnnotationsDataLoader(DataLoader[Key, Result]):
    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(self, keys: list[Key]) -> list[Result]:
        by_project: dict[ProjectId, list[SpanRowId]] = defaultdict(list)
        for span_rowid, project_id in keys:
            by_project[project_id].append(span_rowid)
        span_annotations_by_key: defaultdict[Key, Result] = defaultdict(list)
        for project_id, span_rowids in by_project.items():
            async with project_scoped_read_connection(self._db, project_id) as session:
                async for span_annotation in await session.stream_scalars(
                    select(ORMSpanAnnotation).where(ORMSpanAnnotation.span_rowid.in_(span_rowids))
                ):
                    span_annotations_by_key[span_annotation.span_rowid, project_id].append(
                        span_annotation
                    )
        return [span_annotations_by_key[key] for key in keys]
