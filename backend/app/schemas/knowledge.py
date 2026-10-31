from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.reader import ReaderItemStateRead


CaptureStatus = Literal["pending", "succeeded", "partial_failed", "failed"]
MirrorStatus = Literal["pending", "succeeded", "partial_failed", "failed"]
ArchiveArtifactKind = Literal[
    "singlefile_html_v1",
    "crawl4ai_raw_markdown_v1",
    "crawl4ai_fit_markdown_v1",
    "crawl4ai_fit_image_reinserted_markdown_v1",
    "page_image_v1",
    "uploaded_source_material_v1",
    "uploaded_markdown_v1",
]
ArchiveArtifactStatus = Literal["available", "unavailable"]
SavedItemOriginType = Literal[
    "feed_saved",
    "manual_url",
    "external_saved",
    "automation_capture",
    "markdown_upload",
    "html_upload",
    "singlefile_html_upload",
]
SavedItemResolvedInsightSource = Literal["ai_generated", "curated"]
SavedItemAIRoute = Literal["general", "sensitive", "disabled"]


class SavedItemSnapshotStatusRead(BaseModel):
    has_canonical_snapshot: bool
    has_original_page_snapshot: bool
    has_singlefile_html: bool
    has_crawl4ai_raw_markdown: bool
    has_crawl4ai_fit_markdown: bool
    has_crawl4ai_fit_image_reinserted_markdown: bool
    image_asset_count: int


class SavedItemCaptureTitleMetadataRead(BaseModel):
    captured_at: datetime | None
    captured_page_title: str | None
    singlefile_title: str | None
    crawl4ai_title: str | None
    feed_title: str | None
    title_source: str | None
    user_title_present: bool | None
    title_updated: bool | None
    saved_item_title_before: str | None
    saved_item_title_after: str | None


class SavedItemCaptureRead(BaseModel):
    status: CaptureStatus
    latest_run_id: int | None
    latest_error_summary: str | None
    latest_started_at: datetime | None
    latest_finished_at: datetime | None
    retryable: bool
    title_metadata: SavedItemCaptureTitleMetadataRead | None


class SavedItemLocalMirrorPathsRead(BaseModel):
    knowledge_base_root: str
    article_dir: str
    raw_markdown: str
    fit_markdown: str
    fit_image_reinserted_markdown: str
    uploaded_markdown: str
    manifest: str
    markdown_assets_dir: str
    singlefile_html: str
    source_material: str | None = None


class SavedItemLocalMirrorRead(BaseModel):
    status: MirrorStatus
    latest_export_id: int | None
    latest_error_summary: str | None
    latest_started_at: datetime | None
    latest_finished_at: datetime | None
    source_capture_run_id: int | None
    written_file_count: int
    paths: SavedItemLocalMirrorPathsRead


class SavedItemCaptureToolCallRead(BaseModel):
    id: int
    tool_name: str
    tool_version: str | None
    status: str
    exit_code: int | None
    error_summary: str | None
    started_at: datetime
    finished_at: datetime | None


class SavedItemArchiveArtifactRead(BaseModel):
    artifact_kind: ArchiveArtifactKind
    status: ArchiveArtifactStatus
    snapshot_id: int | None
    asset_id: int | None
    asset_key: str | None
    logical_name: str | None
    mime_type: str | None
    byte_size: int | None
    created_at: datetime | None
    capture_run_id: int | None
    source_url: str | None
    preview_url: str | None
    download_url: str | None


class SavedItemInsightPayloadRead(BaseModel):
    summary: str | None = None
    long_term_value: str | None = None
    actionable_takeaways: list[str] = Field(default_factory=list)
    key_concepts: list[str] = Field(default_factory=list)
    suggested_tags: list[str] = Field(default_factory=list)
    topic_ids: list[str] = Field(default_factory=list)
    topic_labels: list[str] = Field(default_factory=list)
    content_type: str | None = None
    content_type_label: str | None = None
    source_kind: str | None = None
    ai_route: str | None = None
    input_snapshot_kind: str | None = None
    input_text_source: str | None = None
    input_char_count: int = 0
    input_text_was_truncated: bool = False


class SavedItemAIInsightRead(BaseModel):
    artifact_id: int
    artifact_kind: str
    model_name: str | None
    prompt_version: str | None
    payload: SavedItemInsightPayloadRead
    created_at: datetime


class SavedItemCuratedInsightRead(BaseModel):
    id: int
    saved_item_id: int
    base_insight_engine_artifact_id: int | None
    payload: SavedItemInsightPayloadRead
    diff_json: dict[str, Any] | None
    edit_note: str | None
    created_at: datetime
    updated_at: datetime


class SavedItemResolvedInsightRead(BaseModel):
    source: SavedItemResolvedInsightSource
    payload: SavedItemInsightPayloadRead
    insight_engine_artifact_id: int | None = None
    curated_insight_id: int | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    generated_at: datetime | None = None
    curated_updated_at: datetime | None = None


class SavedItemCuratedInsightEventRead(BaseModel):
    id: int
    saved_item_id: int
    curated_insight_id: int | None
    base_insight_engine_artifact_id: int | None
    event_type: str
    event_payload_json: dict[str, Any]
    created_at: datetime


class SavedItemListRead(BaseModel):
    id: int
    feed_item_id: int | None
    origin_type: str
    ai_route: SavedItemAIRoute
    title: str
    author_name: str | None
    published_at: datetime | None
    summary_text: str | None
    canonical_url: str | None
    original_url: str | None
    source_id: int | None
    source_name: str
    source_kind: str | None
    source_url: str | None
    source_site_url: str | None
    source_folder_path: str | None
    original_filename: str | None
    mime_type: str | None
    processing_status: str
    processing_error: str | None
    source_material_artifact: SavedItemArchiveArtifactRead | None = None
    saved_at: datetime
    user_note: str | None
    state: ReaderItemStateRead
    snapshots: SavedItemSnapshotStatusRead
    capture: SavedItemCaptureRead | None
    local_mirror: SavedItemLocalMirrorRead | None
    resolved_insight: SavedItemResolvedInsightRead | None = None


class SavedItemDetailRead(SavedItemListRead):
    content_html: str | None
    capture_tool_calls: list[SavedItemCaptureToolCallRead]
    archive_artifacts: list[SavedItemArchiveArtifactRead]
    ai_generated_insight: SavedItemAIInsightRead | None = None
    curated_insight: SavedItemCuratedInsightRead | None = None


class SavedItemRemoveResponse(BaseModel):
    saved_item_id: int
    feed_item_id: int | None
    removed_at: datetime
    state: ReaderItemStateRead | None


class ManualUrlCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    title: str | None = None
    user_note: str | None = None
    ai_route: SavedItemAIRoute = "sensitive"

    @field_validator("url")
    @classmethod
    def normalize_url_input(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        return normalized

    @field_validator("title", "user_note")
    @classmethod
    def empty_string_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None

        normalized = value.strip()
        return normalized or None


class SavedItemUpdateRequest(BaseModel):
    user_note: str | None

    @field_validator("user_note")
    @classmethod
    def normalize_user_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class SavedItemInsightRebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_route: SavedItemAIRoute | None = None


class SavedItemCuratedInsightUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]
    edit_note: str | None = None

    @field_validator("edit_note")
    @classmethod
    def normalize_edit_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None
