from collections import defaultdict

from sqlalchemy import select
from strawberry.dataloader import DataLoader
from typing_extensions import TypeAlias

from phoenix.db.models import ProjectSessionAnnotation
from phoenix.server.access.schema_provisioning import project_scoped_read_connection
from phoenix.server.types import DbSessionFactory

ProjectSessionId: TypeAlias = int
ProjectId: TypeAlias = int
Key: TypeAlias = tuple[ProjectSessionId, ProjectId]
Result: TypeAlias = list[ProjectSessionAnnotation]


class SessionAnnotationsBySessionDataLoader(DataLoader[Key, Result]):
    def __init__(self, db: DbSessionFactory) -> None:
        super().__init__(load_fn=self._load_fn)
        self._db = db

    async def _load_fn(self, keys: list[Key]) -> list[Result]:
        by_project: dict[ProjectId, list[ProjectSessionId]] = defaultdict(list)
        for project_session_id, project_id in keys:
            by_project[project_id].append(project_session_id)
        annotations_by_key: defaultdict[Key, Result] = defaultdict(list)
        for project_id, project_session_ids in by_project.items():
            async with project_scoped_read_connection(self._db, project_id) as session:
                async for annotation in await session.stream_scalars(
                    select(ProjectSessionAnnotation).where(
                        ProjectSessionAnnotation.project_session_id.in_(project_session_ids)
                    )
                ):
                    annotations_by_key[annotation.project_session_id, project_id].append(annotation)
        return [annotations_by_key[key] for key in keys]
