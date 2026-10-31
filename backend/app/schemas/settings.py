from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


AIProfile = Literal["general", "sensitive"]
AIProvider = Literal["deepseek", "openai-compatible", "lm-studio"]
DeepSeekThinking = Literal["disabled", "enabled"]
InsightProfileSourceType = Literal["local_user", "codex_import", "manual_edit"]
InsightProfileEventType = Literal["created", "activated", "rolled_back"]
InsightEvidencePackageRange = Literal["7d", "30d", "90d", "180d", "365d", "all", "custom"]
InsightEvidencePackageArtifactMode = Literal["latest", "all_history"]
InsightProfileDraftChangeType = Literal["added", "removed", "modified", "unchanged"]
LowPriorityBucketReason = Literal["not_interested", "auto_score_threshold"]
InsightEngineAttemptStatus = Literal["failed", "skipped"]
ReadingDepth = Literal["light", "medium", "deep"]
InsightChannelDefaultFlowPolicy = Literal["normal", "mixed_high_frequency", "high_frequency_brief"]


class InsightChannelRead(BaseModel):
    id: str
    label: str
    description: str
    focus_prompt: str
    boundary_prompt: str
    reading_depth: ReadingDepth
    default_flow_policy: InsightChannelDefaultFlowPolicy
    sort_order: int
    enabled: bool
    is_system: bool = False
    retired_at: datetime | None = None
    channel_catalog_version: int = Field(default=1, ge=1)

    model_config = ConfigDict(from_attributes=True)


class InsightChannelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    description: str = ""
    focus_prompt: str = ""
    boundary_prompt: str = ""
    reading_depth: ReadingDepth = "medium"
    default_flow_policy: InsightChannelDefaultFlowPolicy = "normal"
    sort_order: int = 0
    enabled: bool = True
    expected_channel_catalog_version: int = Field(ge=1)


class InsightChannelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    description: str | None = None
    focus_prompt: str | None = None
    boundary_prompt: str | None = None
    reading_depth: ReadingDepth | None = None
    default_flow_policy: InsightChannelDefaultFlowPolicy | None = None
    sort_order: int | None = None
    enabled: bool | None = None
    expected_channel_catalog_version: int = Field(ge=1)

    @model_validator(mode="before")
    @classmethod
    def reject_null_editable_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            null_fields = [
                field
                for field in (
                    "label",
                    "description",
                    "focus_prompt",
                    "boundary_prompt",
                    "reading_depth",
                    "default_flow_policy",
                    "sort_order",
                    "enabled",
                    "expected_channel_catalog_version",
                )
                if field in data and data[field] is None
            ]
            if null_fields:
                raise ValueError(f"Insight channel fields cannot be null: {', '.join(null_fields)}")
        return data


class InsightChannelImpactRead(BaseModel):
    channel_id: str
    channel_label: str
    affected_feed_item_count: int = Field(ge=0)
    source_tendency_count: int = Field(ge=0)
    human_correction_count: int = Field(ge=0)
    channel_catalog_version: int = Field(ge=1)


class InsightChannelRetireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_retire: bool
    expected_channel_catalog_version: int = Field(ge=1)


class StoragePathRead(BaseModel):
    key: str
    value: str
    resolved_path: str
    source: str
    exists: bool
    is_dir: bool
    is_writable: bool
    is_inside_project: bool
    error: str | None = None


class StoragePathsRead(BaseModel):
    knowledge_base_root: StoragePathRead


class StoragePathsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_base_root: str | None = None


class StoragePathsValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_base_root: str | None = None


class RsshubSettingsRead(BaseModel):
    key: str
    value: str
    source: str


class RsshubSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rsshub_base_url: str | None = None


class AIProfileSettingsRead(BaseModel):
    profile: AIProfile
    provider: AIProvider
    enabled: bool
    base_url: str
    model: str
    deepseek_thinking: DeepSeekThinking
    artifact_concurrency: int = Field(ge=1, le=32)
    api_key_configured: bool
    api_key_masked: str | None = None


class AIProfileSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: AIProvider | None = None
    enabled: bool | None = None
    base_url: str | None = None
    model: str | None = None
    deepseek_thinking: str | None = None
    artifact_concurrency: int | None = Field(default=None, ge=1, le=32)
    api_key: str | None = None


class AISettingsRead(BaseModel):
    profiles: dict[AIProfile, AIProfileSettingsRead]


class AISettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: dict[AIProfile, AIProfileSettingsUpdateRequest] = Field(default_factory=dict)


class InsightEngineSettingsRead(BaseModel):
    auto_low_priority_archiving_enabled: bool
    low_priority_score_threshold: int = Field(ge=0, le=100)


class InsightEngineSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_low_priority_archiving_enabled: bool | None = None
    low_priority_score_threshold: int | None = Field(default=None, ge=0, le=100)


class InsightEngineAttemptRead(BaseModel):
    id: int
    feed_item_id: int | None = None
    feed_item_title: str | None = None
    source_id: int | None = None
    source_name: str | None = None
    ai_route: str | None = None
    profile: str
    provider: str
    model: str | None = None
    status: InsightEngineAttemptStatus
    stage: str
    error_type: str | None = None
    error_message: str | None = None
    duration_ms: int | None = None
    artifact_count: int
    created_at: datetime
    completed_at: datetime | None = None
    request_snapshot: dict[str, Any] | None = None


class InsightEngineAttemptListRead(BaseModel):
    items: list[InsightEngineAttemptRead]
    total: int
    limit: int
    offset: int


class InsightEngineCoverageReasonRead(BaseModel):
    code: str
    label: str
    count: int


class InsightEngineCoverageLatestAttemptRead(BaseModel):
    id: int
    status: InsightEngineAttemptStatus
    stage: str
    error_type: str | None = None
    error_message: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class InsightEngineCoverageItemRead(BaseModel):
    feed_item_id: int
    title: str
    source_id: int
    source_name: str
    ai_route: str
    profile: str
    provider: str
    model: str | None = None
    existing_artifact_kinds: list[str]
    missing_artifact_kinds: list[str]
    content_text_source: str
    content_extraction_status: str
    content_extraction_error: str | None = None
    possible_reason_code: str
    possible_reason_label: str
    coverage_category: str
    retryable: bool
    latest_attempt: InsightEngineCoverageLatestAttemptRead | None = None


class InsightEngineCoverageRead(BaseModel):
    total_item_count: int
    complete_item_count: int
    incomplete_item_count: int
    no_artifact_item_count: int
    partial_artifact_item_count: int
    retryable_item_count: int
    problem_item_count: int
    problem_retryable_item_count: int
    initial_generation_item_count: int
    initial_generation_retryable_item_count: int
    blocked_item_count: int
    reason_counts: list[InsightEngineCoverageReasonRead]
    items: list[InsightEngineCoverageItemRead]
    total: int
    limit: int
    offset: int


class AIConnectionTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: AIProfile = "general"
    settings: AIProfileSettingsUpdateRequest | None = None


class AIConnectionTestResponse(BaseModel):
    ok: bool
    message: str
    profile: AIProfile
    provider: AIProvider
    model: str
    latency_ms: int | None = None


class AIInsightRequestPreviewSampleRead(BaseModel):
    item_id: int
    title: str
    source_id: int
    source_name: str
    source_folder: str | None = None
    ai_route: str
    profile: AIProfile
    content_text_source: str
    content_extraction_status: str
    missing_artifact_kinds: list[str]


class AIInsightRequestPreviewRead(BaseModel):
    available: bool
    can_send: bool
    message: str
    candidates: list[AIInsightRequestPreviewSampleRead] = Field(default_factory=list)
    sample: AIInsightRequestPreviewSampleRead | None = None
    payload: dict[str, Any] | None = None


class InsightProfileVersionRead(BaseModel):
    id: int | None
    schema_version: str
    profile_name: str
    profile_version: str
    profile_json: dict[str, Any]
    source_type: str
    edit_note: str | None = None
    diff_summary: str | None = None
    previous_profile_id: int | None = None
    created_at: datetime | None = None


class InsightProfileEventRead(BaseModel):
    id: int | None
    profile_id: int | None
    event_type: str
    note: str | None = None
    event_json: dict[str, Any] | None = None
    created_at: datetime | None = None


class InsightProfileSettingsRead(BaseModel):
    active_profile: InsightProfileVersionRead
    versions: list[InsightProfileVersionRead] = Field(default_factory=list)
    events: list[InsightProfileEventRead] = Field(default_factory=list)
    template: dict[str, Any]
    active_prompt_context: dict[str, Any]


class InsightProfileValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_json: dict[str, Any]


class InsightProfileValidateResponse(BaseModel):
    normalized_profile: dict[str, Any]
    prompt_context: dict[str, Any]


class InsightProfileDraftInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_json: dict[str, Any]


class InsightProfileFieldDiffRead(BaseModel):
    field: str
    change_type: InsightProfileDraftChangeType
    before: Any = None
    after: Any = None


class InsightProfileDraftInspectResponse(BaseModel):
    normalized_profile: dict[str, Any]
    prompt_context: dict[str, Any]
    base_profile: InsightProfileVersionRead
    diff_summary: str
    field_diffs: list[InsightProfileFieldDiffRead]
    evidence_references: list[dict[str, Any]] = Field(default_factory=list)


class InsightProfileVersionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_json: dict[str, Any]
    source_type: InsightProfileSourceType = "manual_edit"
    edit_note: str | None = None
    activate: bool = False


class InsightProfileActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = None


class InsightEvidencePackagePreviewRead(BaseModel):
    package_version: str
    range: InsightEvidencePackageRange
    artifact_mode: InsightEvidencePackageArtifactMode = "latest"
    start_at: datetime | None = None
    end_at: datetime | None = None
    generated_at: datetime
    item_count: int
    excluded_unread_count: int
    content_text_file_count: int
    feedback_item_count: int
    structured_feedback_item_count: int = 0
    channel_correction_item_count: int = 0
    channel_corrected_item_count: int = 0
    strong_signal_count: int = 0
    medium_signal_count: int = 0
    weak_cleanup_signal_count: int = 0
    negative_signal_count: int = 0
    behavior_event_count: int
    feedback_clear_event_count: int = 0
    not_interested_clear_event_count: int = 0
    auto_low_priority_clear_event_count: int = 0
    artifact_coverage: dict[str, int]
    saved_item_count: int = 0
    saved_item_content_text_file_count: int = 0
    saved_item_display_markdown_file_count: int = 0
    saved_item_display_asset_count: int = 0
    saved_item_ai_insight_count: int = 0
    saved_item_curated_insight_count: int = 0
    saved_item_curated_event_count: int = 0
    external_saved_item_count: int = 0
    failed_attempt_count: int = 0
    skipped_attempt_count: int = 0


class InsightProfileCandidateInspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_json: dict[str, Any]


class InsightProfileCandidateSourceApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_json: dict[str, Any]
    source_ids: list[int] = Field(default_factory=list)


class InsightProfileCandidateSourceChangeRead(BaseModel):
    source_id: int
    source_name: str
    normalized_profile: dict[str, Any]
    prompt_context: dict[str, Any]
    diff_summary: str
    field_diffs: list[InsightProfileFieldDiffRead] = Field(default_factory=list)
    evidence_references: list[dict[str, Any]] = Field(default_factory=list)


class InsightProfileCandidateChannelSuggestionRead(BaseModel):
    channel_id: str
    channel_label: str
    current_focus_prompt: str
    suggested_focus_prompt: str | None = None
    current_boundary_prompt: str
    suggested_boundary_prompt: str | None = None
    rationale: str | None = None
    evidence_references: list[dict[str, Any]] = Field(default_factory=list)


class InsightProfileCandidateInspectResponse(BaseModel):
    candidate_version: str
    based_on_package: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = None
    normalized_candidate: dict[str, Any]
    global_profile_inspection: InsightProfileDraftInspectResponse | None = None
    source_profile_changes: list[InsightProfileCandidateSourceChangeRead] = Field(default_factory=list)
    channel_prompt_boundary_suggestions: list[InsightProfileCandidateChannelSuggestionRead] = Field(default_factory=list)
    scoring_policy_suggestions: list[dict[str, Any]] = Field(default_factory=list)
    evidence_references: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    dry_run: dict[str, Any] = Field(default_factory=dict)


class InsightProfileCandidateSourceApplyResponse(BaseModel):
    applied_source_ids: list[int]
    applied_count: int
    skipped_source_ids: list[int] = Field(default_factory=list)
    message: str
