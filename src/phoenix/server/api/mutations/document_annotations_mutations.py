from collections import defaultdict
from typing import Optional, cast

import strawberry
from sqlalchemy import delete, select, tuple_
from starlette.requests import Request
from strawberry import UNSET, Info

from phoenix.db import models
from phoenix.db.insertion.helpers import OnConflict, insert_on_conflict
from phoenix.server.access.schema_provisioning import project_scoped_session
from phoenix.server.api.auth import IsLocked, IsNotReadOnly, IsNotViewer
from phoenix.server.api.context import Context
from phoenix.server.api.exceptions import BadRequest, NotFound, Unauthorized
from phoenix.server.api.helpers.annotations import get_user_identifier
from phoenix.server.api.input_types.CreateDocumentAnnotationInput import (
    CreateDocumentAnnotationInput,
)
from phoenix.server.api.input_types.DeleteAnnotationsInput import DeleteAnnotationsInput
from phoenix.server.api.input_types.PatchAnnotationInput import PatchAnnotationInput
from phoenix.server.api.queries import Query
from phoenix.server.api.types.AnnotationSource import AnnotationSource
from phoenix.server.api.types.DocumentAnnotation import DocumentAnnotation
from phoenix.server.api.types.node import (
    from_project_scoped_global_id_with_expected_type,
    parse_project_scoped_node_id,
)
from phoenix.server.bearer_auth import PhoenixUser
from phoenix.server.dml_event import DocumentAnnotationDeleteEvent, DocumentAnnotationInsertEvent


@strawberry.type
class DocumentAnnotationMutationPayload:
    document_annotations: list[DocumentAnnotation]
    query: Query


@strawberry.type
class DocumentAnnotationMutationMixin:
    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsNotViewer, IsLocked])  # type: ignore
    async def create_document_annotations(
        self, info: Info[Context, None], input: list[CreateDocumentAnnotationInput]
    ) -> DocumentAnnotationMutationPayload:
        if not input:
            raise BadRequest("No document annotations provided.")

        if not isinstance(request := info.context.request, Request):
            raise BadRequest("Invalid request context.")
        user_id: Optional[int] = None
        if "user" in request.scope and isinstance((user := info.context.user), PhoenixUser):
            user_id = int(user.identity)

        # Parse input and build records. Span's node id is compound
        # "<project_id>:<row_id>" (Stage 4b-1); the project_id is
        # server-issued -- see the equivalent comment in
        # span_annotations_mutations.py's create_span_annotations -- so
        # it's trusted directly rather than re-derived via a DB lookup.
        records: list[dict[str, object]] = []
        project_id_by_span: dict[int, int] = {}
        for idx, annotation_input in enumerate(input):
            try:
                if annotation_input.span_id.type_name != "Span":
                    raise ValueError("not a Span global id")
                project_id, span_rowid = parse_project_scoped_node_id(
                    annotation_input.span_id.node_id
                )
            except ValueError:
                raise BadRequest(
                    f"Invalid span ID for annotation at index {idx}: {annotation_input.span_id}"
                )
            project_id_by_span[span_rowid] = project_id

            resolved_identifier = ""
            if isinstance(annotation_input.identifier, str):
                resolved_identifier = annotation_input.identifier.strip()
            elif annotation_input.source == AnnotationSource.APP and user_id is not None:
                resolved_identifier = get_user_identifier(user_id)

            metadata = annotation_input.metadata
            if metadata is not None and not isinstance(metadata, dict):
                raise BadRequest(f"metadata must be a dict for annotation at index {idx}")

            name = annotation_input.name.strip()
            if not name:
                raise BadRequest(f"name cannot be empty for annotation at index {idx}")

            label = annotation_input.label
            explanation = annotation_input.explanation
            records.append(
                {
                    "span_rowid": span_rowid,
                    "document_position": annotation_input.document_position,
                    "name": name,
                    "label": (label.strip() or None) if label else None,
                    "score": annotation_input.score,
                    "explanation": (explanation.strip() or None) if explanation else None,
                    "annotator_kind": annotation_input.annotator_kind.value,
                    "metadata_": metadata,
                    "identifier": resolved_identifier,
                    "source": annotation_input.source.value,
                    "user_id": user_id,
                }
            )

        records_by_project: dict[int, list[dict[str, object]]] = defaultdict(list)
        for record in records:
            span_rowid = cast(int, record["span_rowid"])
            records_by_project[project_id_by_span[span_rowid]].append(record)

        dialect = info.context.db.dialect
        all_annotations: list[models.DocumentAnnotation] = []
        for project_id, project_records in records_by_project.items():
            span_rowids = {cast(int, r["span_rowid"]) for r in project_records}
            async with project_scoped_session(info.context.db, project_id) as session:
                # Fetch spans and validate document positions
                num_docs_by_span: dict[int, int] = {
                    rowid: num_docs
                    async for rowid, num_docs in await session.stream(
                        select(models.Span.id, models.Span.num_documents).where(
                            models.Span.id.in_(span_rowids)
                        )
                    )
                }

                missing = span_rowids - set(num_docs_by_span.keys())
                if missing:
                    raise NotFound(f"Spans with row IDs {missing} do not exist.")

                for idx, record in enumerate(project_records):
                    span_rowid = cast(int, record["span_rowid"])
                    doc_pos = cast(int, record["document_position"])
                    num_docs = num_docs_by_span[span_rowid]
                    if doc_pos not in range(num_docs):
                        raise BadRequest(
                            f"Document position {doc_pos} is out of bounds "
                            f"for span at index {idx} (num_documents: {num_docs})"
                        )

                # Check for existing annotations owned by other users
                unique_keys = [
                    (r["name"], r["span_rowid"], r["document_position"], r["identifier"])
                    for r in project_records
                ]
                existing_user_ids = (
                    await session.scalars(
                        select(models.DocumentAnnotation.user_id)
                        .where(
                            tuple_(
                                models.DocumentAnnotation.name,
                                models.DocumentAnnotation.span_rowid,
                                models.DocumentAnnotation.document_position,
                                models.DocumentAnnotation.identifier,
                            ).in_(unique_keys)
                        )
                        .distinct()
                    )
                ).all()
                for existing_user_id in existing_user_ids:
                    if existing_user_id != user_id:
                        raise Unauthorized(
                            "Cannot overwrite document annotation owned by another user."
                        )

                stmt = insert_on_conflict(
                    *project_records,
                    dialect=dialect,
                    table=models.DocumentAnnotation,
                    unique_by=("name", "span_rowid", "document_position", "identifier"),
                    on_conflict=OnConflict.DO_UPDATE,
                    constraint_name="uq_document_annotations_name_span_rowid_document_pos_identifier",
                ).returning(models.DocumentAnnotation)

                result = await session.scalars(stmt)
                all_annotations.extend(result.all())

        annotations = all_annotations
        annotation_ids = tuple(anno.id for anno in annotations)
        if annotation_ids:
            info.context.event_queue.put(DocumentAnnotationInsertEvent(annotation_ids))

        return DocumentAnnotationMutationPayload(
            document_annotations=[
                DocumentAnnotation(
                    id=anno.id, project_id=project_id_by_span[anno.span_rowid], db_record=anno
                )
                for anno in annotations
            ],
            query=Query(),
        )

    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsNotViewer, IsLocked])  # type: ignore
    async def patch_document_annotations(
        self, info: Info[Context, None], input: list[PatchAnnotationInput]
    ) -> DocumentAnnotationMutationPayload:
        if not input:
            raise BadRequest("No document annotations provided.")

        if not isinstance(request := info.context.request, Request):
            raise BadRequest("Invalid request context.")
        user_id: Optional[int] = None
        if "user" in request.scope and isinstance((user := info.context.user), PhoenixUser):
            user_id = int(user.identity)

        # DocumentAnnotation's node id is compound "<project_id>:<row_id>"
        # (Stage 4b-2f); trusted directly, grouped by project rather than
        # rejected -- same accepted cross-project-atomicity tradeoff as
        # span_annotations_mutations.py's patch_span_annotations.
        # Keyed by (project_id, row_id) -- see the matching comment in
        # span_annotations_mutations.py's patch_span_annotations for why a
        # bare-id key is unsafe once row ids are only unique per project.
        patch_by_key: dict[tuple[int, int], PatchAnnotationInput] = {}
        for patch in input:
            try:
                project_id, document_annotation_id = (
                    from_project_scoped_global_id_with_expected_type(
                        patch.annotation_id, DocumentAnnotation.__name__
                    )
                )
            except ValueError:
                raise BadRequest(f"Invalid document annotation ID: {patch.annotation_id}")
            key = (project_id, document_annotation_id)
            if key in patch_by_key:
                raise BadRequest(
                    f"Duplicate patch for document annotation ID: {patch.annotation_id}"
                )
            patch_by_key[key] = patch

        annotation_ids_by_project: dict[int, list[int]] = defaultdict(list)
        for project_id, annotation_id in patch_by_key:
            annotation_ids_by_project[project_id].append(annotation_id)

        patched_annotations: list[DocumentAnnotation] = []
        for project_id, group_ids in annotation_ids_by_project.items():
            async with project_scoped_session(info.context.db, project_id) as session:
                document_annotations_by_id: dict[int, models.DocumentAnnotation] = {}
                for document_annotation in await session.scalars(
                    select(models.DocumentAnnotation).where(
                        models.DocumentAnnotation.id.in_(group_ids)
                    )
                ):
                    if document_annotation.user_id != user_id:
                        raise Unauthorized(
                            "At least one document annotation is not associated with the "
                            "current user."
                        )
                    document_annotations_by_id[document_annotation.id] = document_annotation

                missing_ids = set(group_ids) - set(document_annotations_by_id.keys())
                if missing_ids:
                    raise NotFound(f"Could not find document annotations with IDs: {missing_ids}")

                for annotation_id in group_ids:
                    document_annotation = document_annotations_by_id[annotation_id]
                    patch = patch_by_key[(project_id, annotation_id)]
                    if patch.name and (name := patch.name.strip()):
                        document_annotation.name = name
                    if patch.annotator_kind:
                        document_annotation.annotator_kind = patch.annotator_kind.value
                    if patch.label is not UNSET:
                        document_annotation.label = (
                            (patch.label.strip() or None) if patch.label else None
                        )
                    if patch.score is not UNSET:
                        document_annotation.score = patch.score
                    if patch.explanation is not UNSET:
                        document_annotation.explanation = (
                            (patch.explanation.strip() or None) if patch.explanation else None
                        )
                    if patch.metadata is not UNSET:
                        if not isinstance(patch.metadata, dict):
                            raise BadRequest("metadata must be a dict")
                        document_annotation.metadata_ = patch.metadata
                    if patch.identifier is not UNSET:
                        document_annotation.identifier = (patch.identifier or "").strip()
                    if patch.source:
                        document_annotation.source = patch.source.value
                    patched_annotations.append(
                        DocumentAnnotation(
                            id=annotation_id,
                            project_id=project_id,
                            db_record=document_annotation,
                        )
                    )

        # Publish event after successful commit (context manager auto-commits)
        info.context.event_queue.put(
            DocumentAnnotationInsertEvent(tuple(a.id for a in patched_annotations))
        )
        return DocumentAnnotationMutationPayload(
            document_annotations=patched_annotations,
            query=Query(),
        )

    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsNotViewer])  # type: ignore
    async def delete_document_annotations(
        self, info: Info[Context, None], input: DeleteAnnotationsInput
    ) -> DocumentAnnotationMutationPayload:
        if not input.annotation_ids:
            raise BadRequest("No document annotation IDs provided.")

        if not isinstance(request := info.context.request, Request):
            raise BadRequest("Invalid request context.")
        user_id: Optional[int] = None
        user_is_admin = False
        if "user" in request.scope and isinstance((user := info.context.user), PhoenixUser):
            user_id = int(user.identity)
            user_is_admin = user.is_admin

        # DocumentAnnotation's node id is compound "<project_id>:<row_id>"
        # (Stage 4b-2f); trusted directly, grouped by project rather than
        # rejected -- same accepted cross-project-atomicity tradeoff as
        # patch_document_annotations above. Parse and deduplicate IDs while
        # preserving order. Keyed by (project_id, row_id) -- see the
        # matching comment in span_annotations_mutations.py's
        # patch_span_annotations for why a bare-id key is unsafe.
        ordered_keys: list[tuple[int, int]] = []
        seen_keys: set[tuple[int, int]] = set()
        for annotation_gid in input.annotation_ids:
            try:
                project_id, annotation_id = from_project_scoped_global_id_with_expected_type(
                    annotation_gid, DocumentAnnotation.__name__
                )
            except ValueError:
                raise BadRequest(f"Invalid document annotation ID: {annotation_gid}")
            key = (project_id, annotation_id)
            if key in seen_keys:
                raise BadRequest(f"Duplicate document annotation ID: {annotation_id}")
            seen_keys.add(key)
            ordered_keys.append(key)

        annotation_ids_by_project: dict[int, list[int]] = defaultdict(list)
        for project_id, annotation_id in ordered_keys:
            annotation_ids_by_project[project_id].append(annotation_id)

        annotations_by_key: dict[tuple[int, int], models.DocumentAnnotation] = {}
        for project_id, group_ids in annotation_ids_by_project.items():
            async with project_scoped_session(info.context.db, project_id) as session:
                # Fetch annotations first to check authorization
                group_annotations_by_id: dict[int, models.DocumentAnnotation] = {
                    anno.id: anno
                    for anno in await session.scalars(
                        select(models.DocumentAnnotation).where(
                            models.DocumentAnnotation.id.in_(group_ids)
                        )
                    )
                }

                # Check for missing annotations
                missing_ids = set(group_ids) - set(group_annotations_by_id.keys())
                if missing_ids:
                    raise NotFound(f"Could not find document annotations with IDs: {missing_ids}")

                # Check authorization before deleting
                if not user_is_admin:
                    unauthorized_ids = [
                        aid
                        for aid, anno in group_annotations_by_id.items()
                        if anno.user_id != user_id
                    ]
                    if unauthorized_ids:
                        raise Unauthorized(
                            "At least one document annotation is not associated with the "
                            "current user."
                        )

                # Now delete
                await session.execute(
                    delete(models.DocumentAnnotation).where(
                        models.DocumentAnnotation.id.in_(group_ids)
                    )
                )
                for annotation_id, annotation in group_annotations_by_id.items():
                    annotations_by_key[(project_id, annotation_id)] = annotation

        # Publish event after successful commit (context manager auto-commits)
        info.context.event_queue.put(
            DocumentAnnotationDeleteEvent(tuple(aid for _, aid in ordered_keys))
        )

        # Return annotations in original order
        return DocumentAnnotationMutationPayload(
            document_annotations=[
                DocumentAnnotation(
                    id=annotation_id,
                    project_id=project_id,
                    db_record=annotations_by_key[(project_id, annotation_id)],
                )
                for project_id, annotation_id in ordered_keys
            ],
            query=Query(),
        )
