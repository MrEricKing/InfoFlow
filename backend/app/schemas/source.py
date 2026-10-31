from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SourceKind = Literal["rss", "rsshub"]
SourceProvenance = Literal["manual", "opml_import"]
SourceAIRoute = Literal["general", "sensitive", "disabled"]
SourceIconSource = Literal[
    "rss_channel_image",
    "atom_icon",
    "atom_logo",
    "opml_site_favicon",
    "source_site_favicon",
    "fallback_avatar",
]
ImportResultStatus = Literal["created", "matched_existing", "invalid"]
OpmlImportCandidateStatus = Literal["ready", "matched_existing", "duplicate_in_file", "invalid"]
SourceFetchStatus = Literal["pending", "success", "not_modified", "error"]
MatchRule = Literal["created", "external_id", "canonical_url", "title_published_at"]
SourceFlowPolicy = Literal["normal", "mixed_high_frequency", "high_frequency_brief"]
SourcePreferenceProfileStatus = Literal["draft", "active", "archived"]
SourcePreferenceProfileSourceType = Literal["user_edit", "codex_import", "future_suggestion"]


class SourceBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=255)
    kind: SourceKind
    url: str = Field(min_length=1)
    site_url: str | None = None
    folder_path: str | None = Field(default=None, max_length=512)
    is_active: bool = True
    ai_route: SourceAIRoute = "general"

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be empty")
        return normalized

    @field_validator("url")
    @classmethod
    def normalize_url_input(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("url must not be empty")
        return normalized

    @field_validator("site_url", "folder_path")
    @classmethod
    def empty_string_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None

        normalized = value.strip()
        return normalized or None


class SourceCreate(SourceBase):
    icon_url: str | None = None
    icon_source: SourceIconSource | None = None

    @field_validator("icon_url")
    @classmethod
    def empty_icon_url_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None

        normalized = value.strip()
        return normalized or None


class SourcePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: SourceKind | None = None
    url: str = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def normalize_url_input(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("url must not be empty")
        return normalized


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    url: str | None = Field(default=None, min_length=1)
    site_url: str | None = None
    folder_path: str | None = Field(default=None, max_length=512)
    is_active: bool | None = None
    ai_route: SourceAIRoute | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None

        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be empty")
        return normalized

    @field_validator("url")
    @classmethod
    def normalize_url_input(cls, value: str | None) -> str | None:
        if value is None:
            return None

        normalized = value.strip()
        if not normalized:
            raise ValueError("url must not be empty")
        return normalized

    @field_validator("site_url", "folder_path")
    @classmethod
    def empty_string_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None

        normalized = value.strip()
        return normalized or None


class SourceDeleteSourceRead(BaseModel):
    id: int
    name: str
    kind: SourceKind
    url: str
    site_url: str | None
    folder_path: str | None
    is_active: bool
    ai_route: SourceAIRoute
    item_count: int


class SourceDeletePreviewCounts(BaseModel):
    source_fetches: int
    source_item_occurrences: int
    exclusive_unsaved_feed_items: int
    retained_feed_items: int
    shared_feed_items_retained: int
    saved_items_retained: int
    active_saved_items_retained: int
    removed_saved_items_retained: int
    source_preference_profiles: int
    source_channel_tendencies: int
    homepage_read_models: int
    feed_item_insight_artifacts: int
    diagnostic_operations_to_detach: int
    diagnostic_attempts_to_detach: int


class SourceDeleteBlockerRead(BaseModel):
    operation_id: int
    operation_type: str
    operation_kind: str
    status: str
    source_id: int | None = None
    reason: str
    message: str | None = None


class SourceDeletePlanRead(BaseModel):
    deletes: list[str]
    retains: list[str]
    detaches: list[str]


class SourceDeletePreviewRead(BaseModel):
    source: SourceDeleteSourceRead
    can_delete: bool
    blockers: list[SourceDeleteBlockerRead]
    counts: SourceDeletePreviewCounts
    preview_token: str
    delete_plan: SourceDeletePlanRead


class SourceDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool
    preview_token: str = Field(min_length=1)


class SourceDeleteResult(BaseModel):
    source: SourceDeleteSourceRead
    deleted: bool
    counts: SourceDeletePreviewCounts
    retained_saved_items: int


class SourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: SourceKind
    url: str
    site_url: str | None
    folder_path: str | None
    is_active: bool
    ai_route: SourceAIRoute
    icon_url: str | None
    icon_source: SourceIconSource | None
    icon_updated_at: datetime | None
    provenance: SourceProvenance
    created_from_import_run_id: int | None
    item_count: int = 0
    latest_fetch_is_available: bool | None = None
    latest_fetch_status: SourceFetchStatus | None = None
    latest_fetch_http_status: int | None = None
    latest_fetch_error_message: str | None = None
    latest_fetch_items_seen_count: int | None = None
    latest_fetch_items_created_count: int | None = None
    latest_fetch_items_matched_count: int | None = None
    latest_fetch_items_invalid_count: int | None = None
    latest_fetch_started_at: datetime | None = None
    latest_fetch_finished_at: datetime | None = None
    source_profile_draft_source_type: str | None = None
    source_profile_pending_external_suggestion: bool = False
    created_at: datetime
    updated_at: datetime


class SourceCategoryRead(BaseModel):
    path: str
    source_count: int


class SourceCategoryRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    old_path: str = Field(min_length=1, max_length=512)
    new_path: str = Field(min_length=1, max_length=512)

    @field_validator("old_path", "new_path")
    @classmethod
    def normalize_category_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("category path must not be empty")
        return normalized


class SourceCategoryDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    path: str = Field(min_length=1, max_length=512)

    @field_validator("path")
    @classmethod
    def normalize_category_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("category path must not be empty")
        return normalized


class SourceCategoryMutationResponse(BaseModel):
    updated_source_count: int
    categories: list[SourceCategoryRead]


class SourceImportResult(BaseModel):
    status: ImportResultStatus
    candidate_id: str | None = None
    source_id: int | None = None
    name: str
    kind: SourceKind | None = None
    input_url: str | None = None
    url: str | None = None
    folder_path: str | None = None
    ai_route: SourceAIRoute | None = None
    icon_url: str | None = None
    icon_source: SourceIconSource | None = None
    icon_updated_at: datetime | None = None
    message: str


class SourceImportResponse(BaseModel):
    import_run_id: int
    filename: str | None
    total_entries: int
    selected_count: int | None = None
    skipped_count: int | None = None
    created_count: int
    matched_existing_count: int
    invalid_count: int
    results: list[SourceImportResult]


class OpmlImportCandidate(BaseModel):
    candidate_id: str
    entry_index: int
    status: OpmlImportCandidateStatus
    source_id: int | None = None
    name: str
    kind: SourceKind | None = None
    input_url: str | None = None
    url: str | None = None
    site_url: str | None = None
    folder_path: str | None = None
    duplicate_of_candidate_id: str | None = None
    icon_url: str | None = None
    icon_source: SourceIconSource | None = None
    message: str
    suggested_ai_route: SourceAIRoute = "general"
    selected_by_default: bool = False


class OpmlImportPreviewResponse(BaseModel):
    import_run_id: int
    filename: str | None
    total_entries: int
    ready_count: int
    matched_existing_count: int
    duplicate_in_file_count: int
    invalid_count: int
    candidates: list[OpmlImportCandidate]


class OpmlImportCommitSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    candidate_id: str = Field(min_length=1)
    ai_route: SourceAIRoute | None = None
    folder_path: str | None = Field(default=None, max_length=512)

    @field_validator("folder_path")
    @classmethod
    def empty_folder_path_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None

        normalized = value.strip()
        return normalized or None


class OpmlImportCommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    import_run_id: int
    default_ai_route: SourceAIRoute = "general"
    selected_candidates: list[OpmlImportCommitSelection]


class SourceFetchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    requested_url: str
    status: SourceFetchStatus
    http_status: int | None
    etag: str | None
    last_modified: str | None
    error_message: str | None
    items_seen_count: int
    items_created_count: int
    items_matched_count: int
    items_invalid_count: int
    fetch_started_at: datetime
    fetch_finished_at: datetime | None


class SourceFetchWithSourceRead(SourceFetchRead):
    source_name: str


class SourceChannelTendencyRead(BaseModel):
    channel_id: str
    tendency: float
    is_primary: bool
    notes: str


class SourceChannelTendencyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str
    tendency: float = Field(ge=0.0, le=1.0)
    is_primary: bool = False
    notes: str = ""


class SourceInsightRoutingRead(BaseModel):
    source_id: int
    flow_policy: SourceFlowPolicy
    channel_tendencies: list[SourceChannelTendencyRead]


class SourceInsightRoutingUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_policy: SourceFlowPolicy
    channel_tendencies: list[SourceChannelTendencyUpdate]


class SourcePreferenceProfileFieldError(BaseModel):
    field: str
    code: str
    message: str
    actual: int | None = None
    limit: int | None = None
    unit: str | None = None
    pattern_class: str | None = None


class SourcePreferenceProfileDraftUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    subscription_reason: str | None = None
    good_article_criteria: list[str] | None = None
    bad_article_patterns: list[str] | None = None
    preferred_topics: list[str] | None = None
    ignored_topics: list[str] | None = None
    preferred_content_types: list[str] | None = None
    positive_example_item_ids: list[int] | None = None
    negative_example_item_ids: list[int] | None = None
    positive_example_summaries: list[str] | None = None
    negative_example_summaries: list[str] | None = None
    quality_notes: str | None = None
    source_type: SourcePreferenceProfileSourceType | None = None
    edit_note: str | None = None


class SourcePreferenceProfileActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    edit_note: str | None = None


class SourcePreferenceProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    status: SourcePreferenceProfileStatus
    schema_version: str
    profile_version: str | None
    subscription_reason: str | None
    good_article_criteria: list[str]
    bad_article_patterns: list[str]
    preferred_topics: list[str]
    ignored_topics: list[str]
    preferred_content_types: list[str]
    positive_example_item_ids: list[int]
    negative_example_item_ids: list[int]
    positive_example_summaries: list[str]
    negative_example_summaries: list[str]
    quality_notes: str | None
    prompt_context: dict[str, Any]
    prompt_context_stats: dict[str, Any]
    source_type: str
    edit_note: str | None
    activated_at: datetime | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SourcePreferenceProfileStateRead(BaseModel):
    source_id: int
    active: SourcePreferenceProfileRead | None
    draft: SourcePreferenceProfileRead | None
    history: list[SourcePreferenceProfileRead]
    rendered_prompt_context: dict[str, Any]
    render_stats: dict[str, Any]
    validation_errors: list[SourcePreferenceProfileFieldError] = Field(default_factory=list)


class FeedItemInsightArtifactBuildStats(BaseModel):
    processed_feed_item_count: int
    artifact_rows_created: int
    failed_feed_item_count: int


class SourcePreviewSummary(BaseModel):
    detected_kind: SourceKind
    canonical_url: str
    resolved_url: str
    title: str | None
    site_url: str | None
    description: str | None
    language: str | None
    icon_url: str | None
    icon_source: SourceIconSource | None


class SourcePreviewEntry(BaseModel):
    title: str
    published_at: datetime | None
    author_name: str | None
    summary_text: str | None
    original_url: str


class SourcePreviewDiagnostics(BaseModel):
    requested_url: str
    final_url: str
    http_status: int
    content_type: str | None


class SourcePreviewResponse(BaseModel):
    source: SourcePreviewSummary
    entries: list[SourcePreviewEntry]
    diagnostics: SourcePreviewDiagnostics


class SingleSourceSyncResponse(BaseModel):
    source_id: int
    source_name: str
    fetch: SourceFetchRead
    created_feed_item_ids: list[int] = Field(default_factory=list)
    feed_item_insight_artifacts: FeedItemInsightArtifactBuildStats = Field(default_factory=lambda: _empty_artifact_stats())


class BulkSourceSyncResponse(BaseModel):
    source_count: int
    success_count: int
    error_count: int
    results: list[SingleSourceSyncResponse]


class ReaderContractResponse(BaseModel):
    canonical_item_fields: list[str]
    occurrence_fields: list[str]
    notes: list[str]


def _empty_artifact_stats() -> FeedItemInsightArtifactBuildStats:
    return FeedItemInsightArtifactBuildStats(
        processed_feed_item_count=0,
        artifact_rows_created=0,
        failed_feed_item_count=0,
    )
