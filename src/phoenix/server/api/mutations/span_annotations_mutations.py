from collections import defaultdict
from typing import Any, Optional, cast

import strawberry
from sqlalchemy import delete, insert, select
from starlette.requests import Request
from strawberry import UNSET, Info

from phoenix.db import models
from phoenix.server.access.schema_provisioning import project_scoped_session
from phoenix.server.api.auth import IsLocked, IsNotReadOnly, IsNotViewer
from phoenix.server.api.context import Context
from phoenix.server.api.exceptions import BadRequest, NotFound, Unauthorized
from phoenix.server.api.helpers.annotations import get_note_identifier, get_user_identifier
from phoenix.server.api.input_types.CreateSpanAnnotationInput import (
    CreateSpanAnnotationInput,
    CreateSpanNoteInput,
)
from phoenix.server.api.input_types.DeleteAnnotationsInput import DeleteAnnotationsInput
from phoenix.server.api.input_types.PatchAnnotationInput import PatchAnnotationInput
from phoenix.server.api.queries import Query
from phoenix.server.api.types.AnnotationSource import AnnotationSource
from phoenix.server.api.types.AnnotatorKind import AnnotatorKind
from phoenix.server.api.types.node import (
    from_global_id_with_expected_type,
    parse_project_scoped_node_id,
)
from phoenix.server.api.types.SpanAnnotation import SpanAnnotation
from phoenix.server.bearer_auth import PhoenixUser
from phoenix.server.dml_event import SpanAnnotationDeleteEvent, SpanAnnotationInsertEvent


@strawberry.type
class SpanAnnotationMutationPayload:
    span_annotations: list[SpanAnnotation]
    query: Query


@strawberry.type
class SpanAnnotationMutationMixin:
    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsNotViewer, IsLocked])  # type: ignore
    async def create_span_annotations(
        self, info: Info[Context, None], input: list[CreateSpanAnnotationInput]
    ) -> SpanAnnotationMutationPayload:
        if not input:
            raise BadRequest("No span annotations provided.")

        if any(d.name == "note" for d in input):
            raise BadRequest(
                "The name 'note' is reserved for trace and span notes. "
                "Use the createSpanNote mutation or POST /v1/span_notes instead."
            )

        assert isinstance(request := info.context.request, Request)
        user_id: Optional[int] = None
        if "user" in request.scope and isinstance((user := info.context.user), PhoenixUser):
            user_id = int(user.identity)

        # Span's node id is compound "<project_id>:<row_id>" (Stage 4b-1).
        # The project_id is server-issued -- this fork embedded it when it
        # originally returned the Span's GlobalID to the client, not
        # arbitrary client input -- so it's trusted directly here rather
        # than re-derived via a Span -> Trace join, which stops being
        # reliable once ingest writes exclusively per-project (Stage
        # 4b-2d): a span annotated shortly after being created wouldn't be
        # found by a shared-schema lookup at all.
        project_ids: list[int] = []
        span_rowids: list[int] = []
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
            project_ids.append(project_id)
            span_rowids.append(span_rowid)

        indices_by_project: dict[int, list[int]] = defaultdict(list)
        for idx, project_id in enumerate(project_ids):
            indices_by_project[project_id].append(idx)

        processed_annotations_map: dict[int, models.SpanAnnotation] = {}
        project_id_by_idx: dict[int, int] = dict(enumerate(project_ids))

        for project_id, indices in indices_by_project.items():
            async with project_scoped_session(info.context.db, project_id) as session:
                group_span_rowids = {span_rowids[idx] for idx in indices}
                existing_span_rowids = set(
                    await session.scalars(
                        select(models.Span.id).where(models.Span.id.in_(group_span_rowids))
                    )
                )
                missing_span_ids = [
                    str(input[idx].span_id)
                    for idx in indices
                    if span_rowids[idx] not in existing_span_rowids
                ]
                if missing_span_ids:
                    raise NotFound(f"Could not find spans with IDs: {missing_span_ids}")

                for idx in indices:
                    span_rowid = span_rowids[idx]
                    annotation_input = input[idx]
                    resolved_identifier = ""
                    if isinstance(annotation_input.identifier, str):
                        resolved_identifier = annotation_input.identifier
                    elif annotation_input.source == AnnotationSource.APP and user_id is not None:
                        resolved_identifier = get_user_identifier(user_id)
                    values = {
                        "span_rowid": span_rowid,
                        "name": annotation_input.name,
                        "label": annotation_input.label,
                        "score": annotation_input.score,
                        "explanation": annotation_input.explanation,
                        "annotator_kind": annotation_input.annotator_kind.value,
                        "metadata_": annotation_input.metadata,
                        "identifier": resolved_identifier,
                        "source": annotation_input.source.value,
                        "user_id": user_id,
                    }

                    processed_annotation: Optional[models.SpanAnnotation] = None

                    q = select(models.SpanAnnotation).where(
                        models.SpanAnnotation.span_rowid == span_rowid,
                        models.SpanAnnotation.name == annotation_input.name,
                        models.SpanAnnotation.identifier == resolved_identifier,
                    )
                    existing_annotation = await session.scalar(q)

                    if existing_annotation:
                        existing_annotation.name = annotation_input.name
                        existing_annotation.label = annotation_input.label
                        existing_annotation.score = annotation_input.score
                        existing_annotation.explanation = annotation_input.explanation
                        existing_annotation.metadata_ = cast(
                            dict[str, Any], annotation_input.metadata
                        )
                        existing_annotation.annotator_kind = annotation_input.annotator_kind.value
                        existing_annotation.source = annotation_input.source.value
                        existing_annotation.user_id = user_id
                        session.add(existing_annotation)
                        processed_annotation = existing_annotation

                    if processed_annotation is None:
                        stmt = insert(models.SpanAnnotation).values(**values)
                        stmt = stmt.returning(models.SpanAnnotation)
                        result = await session.scalars(stmt)
                        processed_annotation = result.one()

                    processed_annotations_map[idx] = processed_annotation

                await session.flush()

                # Re-fetch this project's annotations to get the final
                # state including DB defaults, still within this project's
                # own scoped session.
                group_annotation_ids = [processed_annotations_map[idx].id for idx in indices]
                final_annotations_result = await session.scalars(
                    select(models.SpanAnnotation).where(
                        models.SpanAnnotation.id.in_(group_annotation_ids)
                    )
                )
                final_annotations_by_id = {anno.id: anno for anno in final_annotations_result.all()}
                for idx in indices:
                    processed_annotations_map[idx] = final_annotations_by_id[
                        processed_annotations_map[idx].id
                    ]

        # Order the final annotations according to the input order.
        ordered_final_annotations = [processed_annotations_map[idx] for idx in range(len(input))]
        processed_annotation_ids = [anno.id for anno in ordered_final_annotations]

        if processed_annotation_ids:
            info.context.event_queue.put(SpanAnnotationInsertEvent(tuple(processed_annotation_ids)))

        returned_annotations = [
            SpanAnnotation(
                id=anno.id,
                project_id=project_id_by_idx[idx],
                db_record=anno,
            )
            for idx, anno in enumerate(ordered_final_annotations)
        ]

        return SpanAnnotationMutationPayload(
            span_annotations=returned_annotations,
            query=Query(),
        )

    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsNotViewer, IsLocked])  # type: ignore
    async def create_span_note(
        self, info: Info[Context, None], annotation_input: CreateSpanNoteInput
    ) -> SpanAnnotationMutationPayload:
        assert isinstance(request := info.context.request, Request)
        user_id: Optional[int] = None
        if "user" in request.scope and isinstance((user := info.context.user), PhoenixUser):
            user_id = int(user.identity)

        try:
            if annotation_input.span_id.type_name != "Span":
                raise ValueError("not a Span global id")
            # Trusted directly from the compound GlobalID -- see the
            # comment in create_span_annotations for why.
            project_id, span_rowid = parse_project_scoped_node_id(annotation_input.span_id.node_id)
        except ValueError:
            raise BadRequest(f"Invalid span ID: {annotation_input.span_id}")

        async with project_scoped_session(info.context.db, project_id) as session:
            span_exists = await session.scalar(
                select(models.Span.id).where(models.Span.id == span_rowid)
            )
            if span_exists is None:
                raise NotFound(f"Could not find span with ID: {annotation_input.span_id}")
            note_identifier = get_note_identifier("px-span-note")
            values = {
                "span_rowid": span_rowid,
                "name": "note",
                "label": None,
                "score": None,
                "explanation": annotation_input.note,
                "annotator_kind": AnnotatorKind.HUMAN.value,
                "metadata_": dict(),
                "identifier": note_identifier,
                "source": AnnotationSource.APP.value,
                "user_id": user_id,
            }

            stmt = insert(models.SpanAnnotation).values(**values)
            stmt = stmt.returning(models.SpanAnnotation)
            result = await session.scalars(stmt)
            processed_annotation = result.one()

            info.context.event_queue.put(SpanAnnotationInsertEvent((processed_annotation.id,)))
            returned_annotation = SpanAnnotation(
                id=processed_annotation.id, project_id=project_id, db_record=processed_annotation
            )
        return SpanAnnotationMutationPayload(
            span_annotations=[returned_annotation],
            query=Query(),
        )

    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsNotViewer, IsLocked])  # type: ignore
    async def patch_span_annotations(
        self, info: Info[Context, None], input: list[PatchAnnotationInput]
    ) -> SpanAnnotationMutationPayload:
        if not input:
            raise BadRequest("No span annotations provided.")

        assert isinstance(request := info.context.request, Request)
        user_id: Optional[int] = None
        if "user" in request.scope and isinstance((user := info.context.user), PhoenixUser):
            user_id = int(user.identity)

        patch_by_id = {}
        for patch in input:
            try:
                span_annotation_id = from_global_id_with_expected_type(
                    patch.annotation_id, SpanAnnotation.__name__
                )
            except ValueError:
                raise BadRequest(f"Invalid span annotation ID: {patch.annotation_id}")
            if span_annotation_id in patch_by_id:
                raise BadRequest(f"Duplicate patch for span annotation ID: {span_annotation_id}")
            patch_by_id[span_annotation_id] = patch

        async with info.context.db() as session:
            span_annotations_by_id = {}
            for span_annotation in await session.scalars(
                select(models.SpanAnnotation).where(
                    models.SpanAnnotation.id.in_(patch_by_id.keys())
                )
            ):
                if span_annotation.user_id != user_id:
                    raise Unauthorized(
                        "At least one span annotation is not associated with the current user."
                    )
                span_annotations_by_id[span_annotation.id] = span_annotation
            missing_span_annotation_ids = set(patch_by_id.keys()) - set(
                span_annotations_by_id.keys()
            )
            if missing_span_annotation_ids:
                raise NotFound(
                    f"Could not find span annotations with IDs: {missing_span_annotation_ids}"
                )
            for span_annotation_id, patch in patch_by_id.items():
                span_annotation = span_annotations_by_id[span_annotation_id]
                if patch.name:
                    span_annotation.name = patch.name
                if patch.annotator_kind:
                    span_annotation.annotator_kind = patch.annotator_kind.value
                if patch.label is not UNSET:
                    span_annotation.label = patch.label
                if patch.score is not UNSET:
                    span_annotation.score = patch.score
                if patch.explanation is not UNSET:
                    span_annotation.explanation = patch.explanation
                if patch.metadata is not UNSET:
                    assert isinstance(patch.metadata, dict)
                    span_annotation.metadata_ = patch.metadata
                if patch.identifier is not UNSET:
                    span_annotation.identifier = patch.identifier or ""
                if patch.source:
                    span_annotation.source = patch.source.value
                session.add(span_annotation)

            project_id_by_span_rowid = {
                row.id: row.project_rowid
                for row in await session.execute(
                    select(models.Span.id, models.Trace.project_rowid)
                    .join(models.Trace, models.Span.trace_rowid == models.Trace.id)
                    .where(
                        models.Span.id.in_(
                            {sa.span_rowid for sa in span_annotations_by_id.values()}
                        )
                    )
                )
            }
            patched_annotations = [
                SpanAnnotation(
                    id=span_annotation.id,
                    project_id=project_id_by_span_rowid[span_annotation.span_rowid],
                    db_record=span_annotation,
                )
                for span_annotation in span_annotations_by_id.values()
            ]

        info.context.event_queue.put(
            SpanAnnotationInsertEvent(tuple(span_annotations_by_id.keys()))
        )
        return SpanAnnotationMutationPayload(
            span_annotations=patched_annotations,
            query=Query(),
        )

    @strawberry.mutation(permission_classes=[IsNotReadOnly, IsNotViewer])  # type: ignore
    async def delete_span_annotations(
        self, info: Info[Context, None], input: DeleteAnnotationsInput
    ) -> SpanAnnotationMutationPayload:
        if not input.annotation_ids:
            raise BadRequest("No span annotation IDs provided.")

        assert isinstance(request := info.context.request, Request)
        user_id: Optional[int] = None
        user_is_admin = False
        if "user" in request.scope and isinstance((user := info.context.user), PhoenixUser):
            user_id = int(user.identity)
            user_is_admin = user.is_admin

        span_annotation_ids: dict[int, None] = {}  # use a dict to preserve ordering
        for annotation_gid in input.annotation_ids:
            try:
                span_annotation_id = from_global_id_with_expected_type(
                    annotation_gid, SpanAnnotation.__name__
                )
            except ValueError:
                raise BadRequest(f"Invalid span annotation ID: {annotation_gid}")
            if span_annotation_id in span_annotation_ids:
                raise BadRequest(f"Duplicate span annotation ID: {span_annotation_id}")
            span_annotation_ids[span_annotation_id] = None

        async with info.context.db() as session:
            stmt = (
                delete(models.SpanAnnotation)
                .where(models.SpanAnnotation.id.in_(span_annotation_ids.keys()))
                .returning(models.SpanAnnotation)
            )
            result = await session.scalars(stmt)
            deleted_annotations_by_id = {annotation.id: annotation for annotation in result.all()}

            if not user_is_admin and any(
                annotation.user_id != user_id for annotation in deleted_annotations_by_id.values()
            ):
                await session.rollback()
                raise Unauthorized(
                    "At least one span annotation is not associated with the current user."
                )

            missing_span_annotation_ids = set(span_annotation_ids.keys()) - set(
                deleted_annotations_by_id.keys()
            )
            if missing_span_annotation_ids:
                raise NotFound(
                    f"Could not find span annotations with IDs: {missing_span_annotation_ids}"
                )

            project_id_by_span_rowid = {
                row.id: row.project_rowid
                for row in await session.execute(
                    select(models.Span.id, models.Trace.project_rowid)
                    .join(models.Trace, models.Span.trace_rowid == models.Trace.id)
                    .where(
                        models.Span.id.in_(
                            {a.span_rowid for a in deleted_annotations_by_id.values()}
                        )
                    )
                )
            }

        deleted_annotations_gql = [
            SpanAnnotation(
                id=deleted_annotations_by_id[id].id,
                project_id=project_id_by_span_rowid[deleted_annotations_by_id[id].span_rowid],
                db_record=deleted_annotations_by_id[id],
            )
            for id in span_annotation_ids
        ]
        info.context.event_queue.put(
            SpanAnnotationDeleteEvent(tuple(deleted_annotations_by_id.keys()))
        )
        return SpanAnnotationMutationPayload(
            span_annotations=deleted_annotations_gql, query=Query()
        )
