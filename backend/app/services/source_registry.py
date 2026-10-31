from __future__ import annotations

from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
import hashlib
import json
from typing import Any
from xml.etree import ElementTree

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Source, SourceImportRun, SourceItemOccurrence
from app.core.safety import OPML_IMPORT_ENTRY_MAX_COUNT, OPML_UPLOAD_MAX_BYTES, XMLSafetyError, parse_xml_document
from app.schemas.source import (
    OpmlImportCandidate,
    OpmlImportCommitRequest,
    OpmlImportPreviewResponse,
    SourceCategoryMutationResponse,
    SourceCategoryRead,
    SourceCreate,
    SourceImportResponse,
    SourceImportResult,
    SourceProvenance,
    SourceUpdate,
)
from app.services.source_resolver import (
    SourceValidationError,
    canonicalize_source_url,
    detect_source_url,
    normalize_http_url,
)
from app.services.source_icons import (
    apply_source_icon,
    now_utc,
    resolve_opml_site_icon,
    resolve_source_site_icon,
    resolve_supplied_icon,
)

normalize_url = normalize_http_url


def normalize_category_path(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise SourceValidationError("Source category path must not be empty.")
    return normalized


class SourceConflictError(ValueError):
    def __init__(self, source: Source) -> None:
        self.source = source
        super().__init__(f"Source already exists with id={source.id}")


class SourceImportRunNotFoundError(ValueError):
    pass


@dataclass(slots=True)
class ParsedOpmlEntry:
    name: str
    url: str | None
    site_url: str | None
    folder_path: str | None
    metadata: dict[str, Any]
    has_children: bool


def list_sources(session: Session) -> list[Source]:
    statement = select(Source).order_by(Source.is_active.desc(), Source.folder_path.is_(None), Source.folder_path, Source.name)
    return list(session.scalars(statement))


def export_sources_as_opml(session: Session) -> bytes:
    opml = ElementTree.Element("opml", {"version": "2.0"})
    head = ElementTree.SubElement(opml, "head")
    ElementTree.SubElement(head, "title").text = "InfoFlow Aggregator Sources"
    body = ElementTree.SubElement(opml, "body")

    folder_nodes: dict[tuple[str, ...], ElementTree.Element] = {}
    for source in list_sources(session):
        parent = body
        folder_segments = _split_folder_path(source.folder_path)
        for index, segment in enumerate(folder_segments, start=1):
            folder_key = tuple(folder_segments[:index])
            folder = folder_nodes.get(folder_key)
            if folder is None:
                folder = ElementTree.SubElement(parent, "outline", {"text": segment, "title": segment})
                folder_nodes[folder_key] = folder
            parent = folder
        ElementTree.SubElement(parent, "outline", _build_source_opml_attributes(source))

    ElementTree.indent(opml, space="  ")
    return ElementTree.tostring(opml, encoding="utf-8", xml_declaration=True)


def list_source_item_counts(session: Session, source_ids: list[int]) -> dict[int, int]:
    unique_source_ids = sorted(set(source_ids))
    if not unique_source_ids:
        return {}

    statement = (
        select(SourceItemOccurrence.source_id, func.count(func.distinct(SourceItemOccurrence.feed_item_id)))
        .where(SourceItemOccurrence.source_id.in_(unique_source_ids))
        .group_by(SourceItemOccurrence.source_id)
    )
    return {source_id: item_count for source_id, item_count in session.execute(statement)}


def get_source_or_none(session: Session, source_id: int) -> Source | None:
    return session.get(Source, source_id)


def list_source_categories(session: Session) -> list[SourceCategoryRead]:
    statement = (
        select(Source.folder_path, func.count(Source.id))
        .where(Source.folder_path.is_not(None))
        .group_by(Source.folder_path)
        .order_by(Source.folder_path)
    )
    return [
        SourceCategoryRead(path=folder_path, source_count=source_count)
        for folder_path, source_count in session.execute(statement)
        if folder_path
    ]


def rename_source_category(
    session: Session,
    *,
    old_path: str,
    new_path: str,
) -> SourceCategoryMutationResponse:
    old_path = normalize_category_path(old_path)
    new_path = normalize_category_path(new_path)
    if old_path == new_path:
        raise SourceValidationError("New source category path must be different from the current path.")

    sources = list(session.scalars(select(Source).where(Source.folder_path == old_path).order_by(Source.id)))
    if not sources:
        raise SourceValidationError("Source category not found.")

    for source in sources:
        source.folder_path = new_path
        session.add(source)

    session.flush()
    return SourceCategoryMutationResponse(
        updated_source_count=len(sources),
        categories=list_source_categories(session),
    )


def delete_source_category(session: Session, *, path: str) -> SourceCategoryMutationResponse:
    path = normalize_category_path(path)
    sources = list(session.scalars(select(Source).where(Source.folder_path == path).order_by(Source.id)))
    if not sources:
        raise SourceValidationError("Source category not found.")

    for source in sources:
        source.folder_path = None
        session.add(source)

    session.flush()
    return SourceCategoryMutationResponse(
        updated_source_count=len(sources),
        categories=list_source_categories(session),
    )


def create_source(
    session: Session,
    payload: SourceCreate,
    provenance: SourceProvenance = "manual",
    created_from_import_run_id: int | None = None,
    import_metadata_json: dict[str, Any] | None = None,
) -> Source:
    normalized_url = canonicalize_source_url(payload.kind, payload.url, session)
    normalized_site_url = normalize_url(payload.site_url)
    source_url_for_favicon = normalized_url if payload.kind == "rss" else None
    supplied_icon = resolve_supplied_icon(
        payload.icon_url,
        payload.icon_source,
        base_url=normalized_site_url or source_url_for_favicon,
    )
    source_icon = supplied_icon or resolve_source_site_icon(normalized_site_url, source_url_for_favicon)
    existing = find_existing_source(session, payload.kind, normalized_url)
    if existing is not None:
        raise SourceConflictError(existing)

    source = Source(
        name=payload.name,
        kind=payload.kind,
        url=normalized_url,
        site_url=normalized_site_url,
        folder_path=payload.folder_path,
        is_active=payload.is_active,
        ai_route=payload.ai_route,
        icon_url=source_icon.icon_url,
        icon_source=source_icon.icon_source,
        icon_updated_at=now_utc(),
        provenance=provenance,
        created_from_import_run_id=created_from_import_run_id,
        import_metadata_json=import_metadata_json,
    )
    session.add(source)
    session.flush()
    session.refresh(source)
    return source


def update_source(session: Session, source: Source, payload: SourceUpdate) -> Source:
    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates:
        source.name = updates["name"]
    if "site_url" in updates:
        source.site_url = normalize_url(updates["site_url"])
    if "folder_path" in updates:
        source.folder_path = updates["folder_path"]
    if "is_active" in updates:
        source.is_active = updates["is_active"]
    if "ai_route" in updates:
        source.ai_route = updates["ai_route"]
    if "url" in updates:
        normalized_url = canonicalize_source_url(source.kind, updates["url"], session)
        existing = find_existing_source(session, source.kind, normalized_url)
        if existing is not None and existing.id != source.id:
            raise SourceConflictError(existing)
        source.url = normalized_url

    session.add(source)
    session.flush()
    session.refresh(source)
    return source


def preview_opml(session: Session, filename: str | None, raw_payload: bytes) -> OpmlImportPreviewResponse:
    text = decode_opml_payload(raw_payload)
    entries = parse_opml(text)
    import_run = SourceImportRun(
        import_type="opml",
        filename=filename,
        raw_payload=text,
    )
    session.add(import_run)
    session.flush()

    response = build_opml_preview_response(session, import_run, entries)
    import_run.summary_json = {"preview": response.model_dump(mode="json")}
    session.add(import_run)
    session.flush()
    return response


def commit_opml_import(session: Session, payload: OpmlImportCommitRequest) -> SourceImportResponse:
    import_run = session.get(SourceImportRun, payload.import_run_id)
    if import_run is None or import_run.import_type != "opml":
        raise SourceImportRunNotFoundError("OPML import run not found")

    entries = parse_opml(import_run.raw_payload)
    preview_response = build_opml_preview_response(session, import_run, entries)
    candidates_by_id = {candidate.candidate_id: candidate for candidate in preview_response.candidates}
    entries_by_candidate_id = {
        candidate.candidate_id: entries[candidate.entry_index] for candidate in preview_response.candidates
    }

    selected_ids = [selection.candidate_id for selection in payload.selected_candidates]
    if len(selected_ids) != len(set(selected_ids)):
        raise SourceValidationError("OPML commit selected_candidates must not include duplicate candidate_id values.")

    missing_ids = [candidate_id for candidate_id in selected_ids if candidate_id not in candidates_by_id]
    if missing_ids:
        raise SourceValidationError(f"Selected OPML candidate was not found: {missing_ids[0]}")

    results: list[SourceImportResult] = []
    created_count = 0
    matched_existing_count = 0
    invalid_count = 0

    for selection in payload.selected_candidates:
        candidate = candidates_by_id[selection.candidate_id]
        entry = entries_by_candidate_id[selection.candidate_id]
        selected_ai_route = selection.ai_route or payload.default_ai_route
        selected_folder_path = selection.folder_path if "folder_path" in selection.model_fields_set else candidate.folder_path

        if candidate.kind is None or candidate.url is None:
            invalid_count += 1
            results.append(
                SourceImportResult(
                    status="invalid",
                    candidate_id=candidate.candidate_id,
                    name=candidate.name,
                    kind=candidate.kind,
                    input_url=candidate.input_url,
                    url=candidate.url,
                    folder_path=selected_folder_path,
                    ai_route=selected_ai_route,
                    icon_url=candidate.icon_url,
                    icon_source=candidate.icon_source,
                    message=candidate.message,
                )
            )
            continue

        try:
            existing = find_existing_source(session, candidate.kind, candidate.url)
            if existing is not None:
                if not existing.folder_path and selected_folder_path:
                    existing.folder_path = selected_folder_path
                    session.add(existing)
                    session.flush()
                if candidate.icon_source:
                    candidate_icon = resolve_supplied_icon(
                        candidate.icon_url,
                        candidate.icon_source,
                        base_url=existing.site_url or (existing.url if existing.kind == "rss" else None),
                    )
                    if candidate_icon and apply_source_icon(existing, candidate_icon, only_if_missing=True):
                        session.add(existing)
                        session.flush()

                matched_existing_count += 1
                results.append(
                    SourceImportResult(
                        status="matched_existing",
                        candidate_id=candidate.candidate_id,
                        source_id=existing.id,
                        name=existing.name,
                        kind=existing.kind,
                        input_url=entry.url,
                        url=existing.url,
                        folder_path=existing.folder_path,
                        ai_route=existing.ai_route,
                        icon_url=existing.icon_url,
                        icon_source=existing.icon_source,
                        icon_updated_at=existing.icon_updated_at,
                        message="Matched existing source by canonical source URL.",
                    )
                )
                continue

            source = create_source(
                session=session,
                payload=SourceCreate(
                    name=entry.name,
                    kind=candidate.kind,
                    url=candidate.url,
                    site_url=entry.site_url,
                    folder_path=selected_folder_path,
                    is_active=True,
                    ai_route=selected_ai_route,
                    icon_url=candidate.icon_url,
                    icon_source=candidate.icon_source,
                ),
                provenance="opml_import",
                created_from_import_run_id=import_run.id,
                import_metadata_json={
                    **entry.metadata,
                    "candidate_id": candidate.candidate_id,
                    "input_url": entry.url,
                    "detected_kind": candidate.kind,
                    "canonical_url": candidate.url,
                    "selected_ai_route": selected_ai_route,
                    "selected_folder_path": selected_folder_path,
                    "selected_icon_url": candidate.icon_url,
                    "selected_icon_source": candidate.icon_source,
                },
            )
        except SourceValidationError as exc:
            invalid_count += 1
            results.append(
                SourceImportResult(
                    status="invalid",
                    candidate_id=candidate.candidate_id,
                    name=entry.name,
                    input_url=entry.url,
                    folder_path=selected_folder_path,
                    ai_route=selected_ai_route,
                    icon_url=None,
                    icon_source=None,
                    message=str(exc),
                )
            )
            continue

        created_count += 1
        results.append(
            SourceImportResult(
                status="created",
                candidate_id=candidate.candidate_id,
                source_id=source.id,
                name=source.name,
                kind=source.kind,
                input_url=entry.url,
                url=source.url,
                folder_path=source.folder_path,
                ai_route=source.ai_route,
                icon_url=source.icon_url,
                icon_source=source.icon_source,
                icon_updated_at=source.icon_updated_at,
                message="Created source from OPML entry.",
            )
        )

    commit_summary = {
        "total_entries": len(entries),
        "selected_count": len(payload.selected_candidates),
        "skipped_count": len(entries) - len(payload.selected_candidates),
        "created_count": created_count,
        "matched_existing_count": matched_existing_count,
        "invalid_count": invalid_count,
        "results": [result.model_dump(mode="json") for result in results],
    }
    import_run.summary_json = {
        **(import_run.summary_json or {}),
        "preview": preview_response.model_dump(mode="json"),
        "last_commit": commit_summary,
    }
    session.add(import_run)
    session.flush()

    return SourceImportResponse(
        import_run_id=import_run.id,
        filename=import_run.filename,
        total_entries=len(entries),
        selected_count=len(payload.selected_candidates),
        skipped_count=len(entries) - len(payload.selected_candidates),
        created_count=created_count,
        matched_existing_count=matched_existing_count,
        invalid_count=invalid_count,
        results=results,
    )


def build_opml_preview_response(
    session: Session,
    import_run: SourceImportRun,
    entries: list[ParsedOpmlEntry],
) -> OpmlImportPreviewResponse:
    candidates = build_opml_candidates(session, entries)
    return OpmlImportPreviewResponse(
        import_run_id=import_run.id,
        filename=import_run.filename,
        total_entries=len(entries),
        ready_count=sum(1 for candidate in candidates if candidate.status == "ready"),
        matched_existing_count=sum(1 for candidate in candidates if candidate.status == "matched_existing"),
        duplicate_in_file_count=sum(1 for candidate in candidates if candidate.status == "duplicate_in_file"),
        invalid_count=sum(1 for candidate in candidates if candidate.status == "invalid"),
        candidates=candidates,
    )


def build_opml_candidates(session: Session, entries: list[ParsedOpmlEntry]) -> list[OpmlImportCandidate]:
    candidates: list[OpmlImportCandidate] = []
    first_candidate_by_identity: dict[tuple[str, str], str] = {}

    for entry_index, entry in enumerate(entries):
        if not entry.url:
            icon = resolve_opml_site_icon(entry.site_url, entry.url)
            candidates.append(
                OpmlImportCandidate(
                    candidate_id=build_opml_candidate_id(entry_index, entry, kind=None, canonical_url=None),
                    entry_index=entry_index,
                    status="invalid",
                    name=entry.name,
                    input_url=None,
                    site_url=entry.site_url,
                    folder_path=entry.folder_path,
                    icon_url=icon.icon_url,
                    icon_source=icon.icon_source,
                    message="Outline is missing xmlUrl and cannot be imported as a source.",
                    selected_by_default=False,
                )
            )
            continue

        try:
            resolved_url = detect_source_url(entry.url, session)
            normalized_site_url = normalize_url(entry.site_url)
            opml_icon = resolve_opml_site_icon(normalized_site_url, entry.url)
        except SourceValidationError as exc:
            icon = resolve_opml_site_icon(entry.site_url, entry.url)
            candidates.append(
                OpmlImportCandidate(
                    candidate_id=build_opml_candidate_id(entry_index, entry, kind=None, canonical_url=None),
                    entry_index=entry_index,
                    status="invalid",
                    name=entry.name,
                    input_url=entry.url,
                    site_url=entry.site_url,
                    folder_path=entry.folder_path,
                    icon_url=icon.icon_url,
                    icon_source=icon.icon_source,
                    message=str(exc),
                    selected_by_default=False,
                )
            )
            continue

        candidate_id = build_opml_candidate_id(
            entry_index,
            entry,
            kind=resolved_url.detected_kind,
            canonical_url=resolved_url.canonical_url,
        )
        identity = (resolved_url.detected_kind, resolved_url.canonical_url)
        duplicate_of_candidate_id = first_candidate_by_identity.get(identity)
        existing = find_existing_source(session, resolved_url.detected_kind, resolved_url.canonical_url)
        candidate_icon_url = existing.icon_url if existing and existing.icon_source else opml_icon.icon_url
        candidate_icon_source = existing.icon_source if existing and existing.icon_source else opml_icon.icon_source
        if duplicate_of_candidate_id is not None:
            candidates.append(
                OpmlImportCandidate(
                    candidate_id=candidate_id,
                    entry_index=entry_index,
                    status="duplicate_in_file",
                    source_id=existing.id if existing is not None else None,
                    name=entry.name,
                    kind=resolved_url.detected_kind,
                    input_url=entry.url,
                    url=resolved_url.canonical_url,
                    site_url=normalized_site_url,
                    folder_path=entry.folder_path,
                    duplicate_of_candidate_id=duplicate_of_candidate_id,
                    icon_url=candidate_icon_url,
                    icon_source=candidate_icon_source,
                    message="Duplicate OPML entry with the same canonical source URL.",
                    suggested_ai_route=existing.ai_route if existing is not None else "general",
                    selected_by_default=False,
                )
            )
            continue

        first_candidate_by_identity[identity] = candidate_id
        if existing is not None:
            candidates.append(
                OpmlImportCandidate(
                    candidate_id=candidate_id,
                    entry_index=entry_index,
                    status="matched_existing",
                    source_id=existing.id,
                    name=existing.name,
                    kind=existing.kind,
                    input_url=entry.url,
                    url=existing.url,
                    site_url=existing.site_url,
                    folder_path=existing.folder_path or entry.folder_path,
                    icon_url=candidate_icon_url,
                    icon_source=candidate_icon_source,
                    message="Matched existing source by canonical source URL.",
                    suggested_ai_route=existing.ai_route,
                    selected_by_default=False,
                )
            )
            continue

        candidates.append(
            OpmlImportCandidate(
                candidate_id=candidate_id,
                entry_index=entry_index,
                status="ready",
                name=entry.name,
                kind=resolved_url.detected_kind,
                input_url=entry.url,
                url=resolved_url.canonical_url,
                site_url=normalized_site_url,
                folder_path=entry.folder_path,
                icon_url=opml_icon.icon_url,
                icon_source=opml_icon.icon_source,
                message="Ready to create source from OPML entry.",
                suggested_ai_route="general",
                selected_by_default=True,
            )
        )

    return candidates


def build_opml_candidate_id(
    entry_index: int,
    entry: ParsedOpmlEntry,
    *,
    kind: str | None,
    canonical_url: str | None,
) -> str:
    stable_payload = {
        "name": entry.name,
        "kind": kind,
        "canonical_url": canonical_url,
        "input_url": entry.url,
        "site_url": entry.site_url,
        "folder_path": entry.folder_path,
    }
    encoded = json.dumps(stable_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    return f"opml:{entry_index:04d}:{digest}"


def _split_folder_path(folder_path: str | None) -> list[str]:
    if not folder_path:
        return []
    return [segment.strip() for segment in folder_path.split(" / ") if segment.strip()]


def _build_source_opml_attributes(source: Source) -> dict[str, str]:
    attrs = {
        "text": source.name,
        "title": source.name,
        "type": "rss",
        "xmlUrl": source.url,
        "infoflowSourceId": str(source.id),
        "infoflowKind": source.kind,
        "infoflowAiRoute": source.ai_route,
        "infoflowActive": _bool_attr(source.is_active),
        "infoflowFlowPolicy": source.flow_policy or "normal",
        "infoflowProvenance": source.provenance,
    }
    if source.site_url:
        attrs["htmlUrl"] = source.site_url
    if source.icon_url:
        attrs["infoflowIconUrl"] = source.icon_url
    if source.icon_source:
        attrs["infoflowIconSource"] = source.icon_source
    if source.created_from_import_run_id is not None:
        attrs["infoflowCreatedFromImportRunId"] = str(source.created_from_import_run_id)
    return attrs


def _bool_attr(value: bool) -> str:
    return "true" if value else "false"


def decode_opml_payload(raw_payload: bytes) -> str:
    if not raw_payload:
        raise SourceValidationError("OPML payload is empty")
    if len(raw_payload) > OPML_UPLOAD_MAX_BYTES:
        raise SourceValidationError("OPML payload exceeds the 10MB limit")

    try:
        return raw_payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw_payload.decode("utf-8", errors="replace")


def extract_uploaded_file_from_multipart(raw_body: bytes, content_type: str) -> tuple[str | None, bytes]:
    if not raw_body:
        raise SourceValidationError("Multipart payload is empty")

    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + content_type.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw_body
    )
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue

        filename = part.get_filename()
        if not filename:
            continue

        payload = part.get_payload(decode=True) or b""
        return filename, payload

    raise SourceValidationError("Multipart payload is missing an uploaded file.")


def parse_opml(raw_payload_text: str) -> list[ParsedOpmlEntry]:
    try:
        root = parse_xml_document(raw_payload_text, error_message="OPML payload is not valid XML")
    except XMLSafetyError as exc:
        raise SourceValidationError("OPML payload is not valid XML") from exc

    body = root.find("body")
    if body is None:
        raise SourceValidationError("OPML payload is missing a body element")

    entries: list[ParsedOpmlEntry] = []
    walk_opml_outlines(body, [], entries)
    return entries


def walk_opml_outlines(node: ElementTree.Element, folder_segments: list[str], entries: list[ParsedOpmlEntry]) -> None:
    for outline in node.findall("outline"):
        outline_label = (
            outline.attrib.get("title")
            or outline.attrib.get("text")
            or outline.attrib.get("xmlUrl")
            or outline.attrib.get("htmlUrl")
            or "Untitled source"
        ).strip()
        xml_url = outline.attrib.get("xmlUrl")
        html_url = outline.attrib.get("htmlUrl")
        has_children = bool(outline.findall("outline"))
        folder_path = " / ".join(folder_segments) if folder_segments else None

        if xml_url:
            entries.append(
                ParsedOpmlEntry(
                    name=outline_label,
                    url=xml_url,
                    site_url=html_url,
                    folder_path=folder_path,
                    metadata={
                        "folder_segments": folder_segments,
                        "outline_label": outline_label,
                        "outline_attributes": dict(outline.attrib),
                    },
                    has_children=has_children,
                )
            )
            _ensure_opml_entry_limit(entries)
        elif not has_children:
            entries.append(
                ParsedOpmlEntry(
                    name=outline_label,
                    url=None,
                    site_url=html_url,
                    folder_path=folder_path,
                    metadata={
                        "folder_segments": folder_segments,
                        "outline_label": outline_label,
                        "outline_attributes": dict(outline.attrib),
                    },
                    has_children=has_children,
                )
            )
            _ensure_opml_entry_limit(entries)

        if has_children:
            walk_opml_outlines(outline, [*folder_segments, outline_label], entries)


def _ensure_opml_entry_limit(entries: list[ParsedOpmlEntry]) -> None:
    if len(entries) > OPML_IMPORT_ENTRY_MAX_COUNT:
        raise SourceValidationError(f"OPML payload exceeds the {OPML_IMPORT_ENTRY_MAX_COUNT} entry limit")


def find_existing_source(session: Session, kind: str, normalized_url: str) -> Source | None:
    statement = select(Source).where(Source.kind == kind, Source.url == normalized_url)
    return session.scalar(statement)
