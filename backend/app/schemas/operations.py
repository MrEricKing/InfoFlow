from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.source import SourceFetchStatus


OperationType = Literal[
    "source_sync",
    "feed_item_insight_artifacts",
    "feed_item_insight_artifact",
    "knowledge_capture",
    "local_mirror_export",
    "feed_item_extract",
    "saved_item_insight",
    "channel_lifecycle",
]
SourceSyncOperationKind = Literal["all", "single", "auto_create", "selected"]
FeedItemInsightArtifactBatchOperationKind = Literal["global", "current_source", "failed_attempts", "retryable_coverage"]
FeedItemInsightArtifactCoverageReasonCode = Literal[
    "source_ai_route_disabled",
    "provider_disabled_or_unconfigured",
    "low_confidence_content",
    "previous_failed_attempt",
    "previous_skipped_attempt",
    "partial_artifacts",
    "no_attempt_history",
]
KnowledgeCaptureOperationKind = Literal[
    "manual_url",
    "uploaded_material",
    "manual_import",
    "retry_capture",
    "feed_saved_capture",
]
LocalMirrorOperationKind = Literal["manual_reexport"]
FeedItemExtractOperationKind = Literal["manual_extract"]
FeedItemInsightArtifactOperationMode = Literal["rebuild", "build_missing"]
SavedItemInsightOperationKind = Literal["rebuild"]
ChannelLifecycleOperationKind = Literal["retire_channel"]
OperationKind = Literal[
    "all",
    "single",
    "auto_create",
    "selected",
    "global",
    "current_source",
    "failed_attempts",
    "retryable_coverage",
    "source_sync_created_feed_items",
    "manual_url",
    "uploaded_material",
    "manual_import",
    "retry_capture",
    "feed_saved_capture",
    "manual_reexport",
    "manual_extract",
    "rebuild",
    "build_missing",
    "retire_channel",
]
OperationStatus = Literal["queued", "running", "stopping", "completed", "stopped", "failed", "interrupted"]


class OperationFailedSourceRead(BaseModel):
    source_id: int
    source_name: str
    status: SourceFetchStatus
    http_status: int | None = None
    error_message: str | None = None


class OperationProgressRead(BaseModel):
    stage: str
    total: int
    completed: int
    success_count: int
    error_count: int
    stopped: bool
    stop_requested: bool
    current_source_name: str | None = None
    current_source_index: int | None = None
    current_fetch_status: SourceFetchStatus | None = None
    current_items_seen_count: int = 0
    current_items_created_count: int = 0
    current_items_matched_count: int = 0
    current_items_invalid_count: int = 0
    current_insight_total: int = 0
    current_insight_completed: int = 0
    current_insight_failed: int = 0
    current_insight_artifact_rows_created: int = 0
    latest_source_name: str | None = None
    latest_status: SourceFetchStatus | None = None
    items_seen_count: int = 0
    items_created_count: int = 0
    items_matched_count: int = 0
    items_invalid_count: int = 0
    insight_total_count: int = 0
    insight_completed_count: int = 0
    insight_failed_count: int = 0
    insight_artifact_rows_created: int = 0
    latest_error_message: str | None = None
    failed_sources: list[OperationFailedSourceRead] = Field(default_factory=list)
    job_total_count: int = 0
    job_pending_count: int = 0
    job_running_count: int = 0
    job_succeeded_count: int = 0
    job_failed_count: int = 0
    job_cancelled_count: int = 0


class OperationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    operation_type: OperationType
    operation_kind: OperationKind
    status: OperationStatus
    source_id: int | None = None
    source_name: str | None = None
    scope: dict[str, Any] | None = None
    progress: OperationProgressRead | None = None
    result: dict[str, Any] | None = None
    message: str | None = None
    error_message: str | None = None
    stop_requested: bool
    runner_id: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    updated_at: datetime
    completed_at: datetime | None = None


class SourceSyncOperationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: SourceSyncOperationKind
    source_id: int | None = Field(default=None, ge=1)
    source_ids: list[int] | None = None
    feed_item_insight_artifact_build_limit: int | None = Field(default=None, ge=0)

    @field_validator("source_ids")
    @classmethod
    def validate_source_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        seen: set[int] = set()
        source_ids: list[int] = []
        for source_id in value:
            if source_id < 1:
                raise ValueError("source_ids must contain positive ids")
            if source_id not in seen:
                seen.add(source_id)
                source_ids.append(source_id)
        return source_ids

    @model_validator(mode="after")
    def validate_source_scope(self) -> "SourceSyncOperationCreate":
        if self.kind in {"single", "auto_create"} and self.source_id is None:
            raise ValueError("source_id is required for single and auto_create source refresh operations")
        if self.kind == "selected" and not self.source_ids:
            raise ValueError("source_ids is required for selected source refresh operations")
        if self.kind in {"all", "selected"} and self.source_id is not None:
            raise ValueError("source_id must be omitted for all and selected source refresh operations")
        if self.kind != "selected" and self.source_ids is not None:
            raise ValueError("source_ids must be omitted unless kind is selected")
        return self

    @property
    def artifact_build_limit(self) -> int | None:
        return self.feed_item_insight_artifact_build_limit


class FeedItemInsightArtifactBatchOperationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: FeedItemInsightArtifactBatchOperationKind
    source_id: int | None = Field(default=None, ge=1)
    limit: int | None = Field(default=None, ge=0)
    profile: str | None = None
    created_after: datetime | None = None
    reason_codes: list[FeedItemInsightArtifactCoverageReasonCode] | None = None

    @model_validator(mode="after")
    def validate_source_scope(self) -> "FeedItemInsightArtifactBatchOperationCreate":
        if self.scope == "current_source" and self.source_id is None:
            raise ValueError("source_id is required for current_source Insight Engine artifact operations")
        if self.scope == "global" and self.source_id is not None:
            raise ValueError("source_id must be omitted for global Insight Engine artifact operations")
        if self.scope != "failed_attempts" and self.profile is not None:
            raise ValueError("profile is only supported for failed_attempts Insight Engine artifact operations")
        if self.scope != "failed_attempts" and self.created_after is not None:
            raise ValueError("created_after is only supported for failed_attempts Insight Engine artifact operations")
        if self.scope != "retryable_coverage" and self.reason_codes is not None:
            raise ValueError("reason_codes is only supported for retryable_coverage Insight Engine artifact operations")
        if self.reason_codes is not None:
            deduped_reason_codes = list(dict.fromkeys(self.reason_codes))
            if not deduped_reason_codes:
                raise ValueError("reason_codes must not be empty when provided")
            self.reason_codes = deduped_reason_codes
        return self


class ManualUrlCaptureOperationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    title: str | None = None
    user_note: str | None = None
    ai_route: str | None = None

    @field_validator("url")
    @classmethod
    def normalize_url_input(cls, value: str) -> str:
        return value.strip()

    @field_validator("title", "user_note", "ai_route")
    @classmethod
    def empty_string_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class SavedItemInsightOperationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_route: str | None = None


class FeedItemInsightArtifactOperationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: FeedItemInsightArtifactOperationMode
