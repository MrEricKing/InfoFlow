from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class InsightProfile(Base):
    __tablename__ = "insight_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    profile_name: Mapped[str] = mapped_column(String(255), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="manual_edit")
    edit_note: Mapped[Optional[str]] = mapped_column(Text)
    diff_summary: Mapped[Optional[str]] = mapped_column(Text)
    previous_profile_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("insight_profiles.id", ondelete="SET NULL"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class InsightProfileEvent(Base):
    __tablename__ = "insight_profile_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("insight_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    note: Mapped[Optional[str]] = mapped_column(Text)
    event_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class InsightEngineAttempt(Base):
    __tablename__ = "insight_engine_attempts"
    __table_args__ = (
        Index("ix_insight_engine_attempts_status_time", "status", "created_at"),
        Index("ix_insight_engine_attempts_source_time", "source_id", "created_at"),
        Index("ix_insight_engine_attempts_item_time", "feed_item_id", "created_at"),
        Index("ix_insight_engine_attempts_profile_time", "profile", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("feed_items.id", ondelete="SET NULL"), index=True)
    source_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"), index=True)
    ai_route: Mapped[Optional[str]] = mapped_column(String(32))
    profile: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model: Mapped[Optional[str]] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    error_type: Mapped[Optional[str]] = mapped_column(String(128))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    artifact_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_snapshot_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class AppOperation(Base):
    __tablename__ = "app_operations"
    __table_args__ = (
        Index("ix_app_operations_status_updated", "status", "updated_at"),
        Index("ix_app_operations_type_status", "operation_type", "status"),
        Index("ix_app_operations_runner_status", "runner_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    operation_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    source_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"), index=True)
    scope_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    progress_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    result_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    message: Mapped[Optional[str]] = mapped_column(Text)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    stop_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false", index=True)
    runner_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_jobs_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_jobs_attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="ck_jobs_max_attempts_positive"),
        Index(
            "uq_jobs_active_idempotency",
            "queue_name",
            "idempotency_key",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
        Index("ix_jobs_claim_order", "queue_name", "status", "run_after", "priority", "id"),
        Index("ix_jobs_lease_reclaim", "status", "lease_expires_at"),
        Index("ix_jobs_operation_status", "operation_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("app_operations.id", ondelete="SET NULL"), index=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queue_name: Mapped[str] = mapped_column(String(64), nullable=False, default="default", index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    redacted_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(256))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    error_type: Mapped[Optional[str]] = mapped_column(String(128))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    error_detail_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (
        Index("ix_job_events_job_created", "job_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)


class SourceImportRun(Base):
    __tablename__ = "source_import_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_type: Mapped[str] = mapped_column(String(32), nullable=False, default="opml")
    filename: Mapped[Optional[str]] = mapped_column(String(255))
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("kind", "url", name="uq_sources_kind_url"),
        CheckConstraint("ai_route IN ('general', 'sensitive', 'disabled')", name="ck_sources_ai_route"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="rss")
    url: Mapped[str] = mapped_column(Text, nullable=False)
    site_url: Mapped[Optional[str]] = mapped_column(Text)
    folder_path: Mapped[Optional[str]] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ai_route: Mapped[str] = mapped_column(String(32), nullable=False, default="general")
    flow_policy: Mapped[str] = mapped_column(String(64), nullable=False, default="normal", server_default="normal")
    icon_url: Mapped[Optional[str]] = mapped_column(Text)
    icon_source: Mapped[Optional[str]] = mapped_column(String(32))
    icon_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    provenance: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    created_from_import_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("source_import_runs.id", ondelete="SET NULL"),
        index=True,
    )
    import_metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SourcePreferenceProfile(Base):
    __tablename__ = "source_preference_profiles"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'archived')",
            name="ck_source_preference_profiles_status",
        ),
        Index("ix_source_preference_profiles_source_status", "source_id", "status"),
        Index(
            "uq_source_preference_profiles_active_source",
            "source_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index(
            "uq_source_preference_profiles_draft_source",
            "source_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
            sqlite_where=text("status = 'draft'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False, default="source_preference_profile_v1")
    profile_version: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    subscription_reason: Mapped[Optional[str]] = mapped_column(Text)
    good_article_criteria_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    bad_article_patterns_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    preferred_topics_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    ignored_topics_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    preferred_content_types_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    positive_example_item_ids_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    negative_example_item_ids_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    positive_example_summaries_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    negative_example_summaries_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    quality_notes: Mapped[Optional[str]] = mapped_column(Text)
    prompt_context_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    prompt_context_stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="user_edit")
    edit_note: Mapped[Optional[str]] = mapped_column(Text)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    source = relationship("Source")


class InsightChannel(Base):
    __tablename__ = "insight_channels"
    __table_args__ = (
        Index("ix_insight_channels_sort_order", "sort_order"),
        Index("ix_insight_channels_enabled_sort", "enabled", "sort_order"),
        Index("ix_insight_channels_lifecycle_sort", "retired_at", "enabled", "sort_order"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    focus_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    boundary_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    reading_depth: Mapped[str] = mapped_column(String(32), nullable=False, default="medium")
    default_flow_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="normal", server_default="normal")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    retired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class InsightChannelCatalogState(Base):
    __tablename__ = "insight_channel_catalog_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_catalog_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SourceChannelTendency(Base):
    __tablename__ = "source_channel_tendencies"
    __table_args__ = (
        UniqueConstraint("source_id", "channel_id", name="uq_source_channel_tendencies_source_channel"),
        Index("ix_source_channel_tendencies_source_lookup", "source_id", "is_primary"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True)
    channel_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("insight_channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tendency: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    source = relationship("Source")
    channel = relationship("InsightChannel")


class SourceFetch(Base):
    __tablename__ = "source_fetches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    http_status: Mapped[Optional[int]] = mapped_column(Integer)
    etag: Mapped[Optional[str]] = mapped_column(String(255))
    last_modified: Mapped[Optional[str]] = mapped_column(String(255))
    response_body: Mapped[Optional[str]] = mapped_column(Text)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    items_seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_matched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_invalid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetch_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    fetch_finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class FeedItem(Base):
    __tablename__ = "feed_items"
    __table_args__ = (
        CheckConstraint(
            "content_text_source IN ('feed_content', 'feed_summary', 'extracted_page')",
            name="ck_feed_items_content_text_source",
        ),
        Index(
            "uq_feed_items_canonical_url_not_null",
            "canonical_url",
            unique=True,
            postgresql_where=text("canonical_url IS NOT NULL"),
            sqlite_where=text("canonical_url IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[Optional[str]] = mapped_column(Text, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    author_name: Mapped[Optional[str]] = mapped_column(String(255))
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    summary_text: Mapped[Optional[str]] = mapped_column(Text)
    feed_content_text: Mapped[Optional[str]] = mapped_column(Text)
    content_text: Mapped[Optional[str]] = mapped_column(Text)
    content_text_source: Mapped[str] = mapped_column(String(32), nullable=False)
    content_extraction_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_checked")
    content_extraction_attempted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    content_extracted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    content_extraction_error: Mapped[Optional[str]] = mapped_column(Text)
    content_extraction_debug_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    insight_feedback_text: Mapped[Optional[str]] = mapped_column(Text)
    topic_hints: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class FeedItemStructuredFeedback(Base):
    __tablename__ = "feed_item_structured_feedback"
    __table_args__ = (
        UniqueConstraint("feed_item_id", name="uq_feed_item_structured_feedback_feed_item_id"),
        Index("ix_feed_item_structured_feedback_item_updated", "feed_item_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_item_id: Mapped[int] = mapped_column(
        ForeignKey("feed_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    overall_value: Mapped[Optional[str]] = mapped_column(String(32))
    priority_correction: Mapped[Optional[str]] = mapped_column(String(32))
    topic_add_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    topic_remove_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    content_type_correction: Mapped[Optional[str]] = mapped_column(String(64))
    reason_code_add_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    reason_code_remove_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    issue_markers_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    primary_channel_correction: Mapped[Optional[str]] = mapped_column(
        String(128),
        ForeignKey("insight_channels.id", ondelete="SET NULL"),
    )
    channel_add_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    channel_remove_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    reading_depth_correction: Mapped[Optional[str]] = mapped_column(String(32))
    flow_lane_correction: Mapped[Optional[str]] = mapped_column(String(32))
    channel_correction_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    feed_item = relationship("FeedItem")
    primary_channel = relationship(
        "InsightChannel",
        primaryjoin="FeedItemStructuredFeedback.primary_channel_correction == InsightChannel.id",
        foreign_keys=[primary_channel_correction],
    )


class SourceItemOccurrence(Base):
    __tablename__ = "source_item_occurrences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True)
    source_fetch_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("source_fetches.id", ondelete="SET NULL"),
        index=True,
    )
    feed_item_id: Mapped[int] = mapped_column(ForeignKey("feed_items.id", ondelete="CASCADE"), nullable=False, index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(512), index=True)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[Optional[str]] = mapped_column(Text, index=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    raw_entry_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    match_rule: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    match_value: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ItemState(Base):
    __tablename__ = "item_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_item_id: Mapped[int] = mapped_column(
        ForeignKey("feed_items.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unread", index=True)
    browsed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    read_full_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    saved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    not_interested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class FeedItemHomepageReadModel(Base):
    __tablename__ = "feed_item_homepage_read_models"
    __table_args__ = (
        Index("ix_homepage_rm_source_state", "source_id", "state"),
        Index("ix_homepage_rm_source_category_state", "source_category_id", "state"),
        Index("ix_homepage_rm_newest", text("published_at DESC"), text("feed_item_id DESC")),
        Index(
            "ix_homepage_rm_priority",
            "low_priority_bucket",
            text("priority_score DESC"),
            text("published_at DESC"),
            text("feed_item_id DESC"),
        ),
        Index("ix_homepage_rm_source_sort", "source_name", text("published_at DESC"), text("feed_item_id DESC")),
        Index("ix_homepage_rm_flow_lane", "flow_lane_facet"),
        Index("ix_homepage_rm_reading_depth", "reading_depth"),
        Index("ix_homepage_rm_stale", "is_stale", "read_model_version"),
    )

    feed_item_id: Mapped[int] = mapped_column(
        ForeignKey("feed_items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    card_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    read_model_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_category_id: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    source_category_label: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    saved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false", index=True)
    not_interested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false", index=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    time_window_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    priority_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    low_priority_bucket: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    flow_lane: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    flow_lane_facet: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reading_depth: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false", index=True)
    stale_reason: Mapped[Optional[str]] = mapped_column(String(128))
    rebuilt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class FeedItemHomepageFacet(Base):
    __tablename__ = "feed_item_homepage_facets"
    __table_args__ = (
        UniqueConstraint("feed_item_id", "facet_kind", "facet_id", name="uq_homepage_facet_item_kind_id"),
        Index("ix_homepage_facets_kind_id", "facet_kind", "facet_id"),
        Index("ix_homepage_facets_item_kind", "feed_item_id", "facet_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_item_id: Mapped[int] = mapped_column(
        ForeignKey("feed_item_homepage_read_models.feed_item_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    facet_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    facet_id: Mapped[str] = mapped_column(String(512), nullable=False)
    facet_label: Mapped[str] = mapped_column(String(512), nullable=False)


class FeedItemBehaviorEvent(Base):
    __tablename__ = "feed_item_behavior_events"
    __table_args__ = (
        Index("ix_feed_item_behavior_events_item_time", "feed_item_id", "created_at"),
        Index("ix_feed_item_behavior_events_type_time", "event_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_item_id: Mapped[int] = mapped_column(
        ForeignKey("feed_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SavedItem(Base):
    __tablename__ = "saved_items"
    __table_args__ = (
        CheckConstraint("ai_route IN ('general', 'sensitive', 'disabled')", name="ck_saved_items_ai_route"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("feed_items.id", ondelete="SET NULL"), unique=True)
    origin_type: Mapped[str] = mapped_column(String(32), nullable=False, default="manual_url")
    ai_route: Mapped[str] = mapped_column(String(32), nullable=False, default="sensitive")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[Optional[str]] = mapped_column(Text)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_app: Mapped[Optional[str]] = mapped_column(String(128))
    original_filename: Mapped[Optional[str]] = mapped_column(String(255))
    mime_type: Mapped[Optional[str]] = mapped_column(String(128))
    source_asset_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey(
            "saved_item_assets.id",
            ondelete="SET NULL",
            name="fk_saved_items_source_asset_id_saved_item_assets",
            use_alter=True,
        ),
        index=True,
    )
    processing_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_applicable")
    processing_error: Mapped[Optional[str]] = mapped_column(Text)
    user_note: Mapped[Optional[str]] = mapped_column(Text)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SavedItemCuratedInsight(Base):
    __tablename__ = "saved_item_curated_insights"
    __table_args__ = (
        UniqueConstraint("saved_item_id", name="uq_saved_item_curated_insights_saved_item_id"),
        Index("ix_saved_item_curated_insights_item_updated", "saved_item_id", "updated_at"),
        Index("ix_curated_insights_base_ie_artifact", "base_insight_engine_artifact_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    saved_item_id: Mapped[int] = mapped_column(
        ForeignKey("saved_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    base_insight_engine_artifact_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("insight_engine_artifacts.id", ondelete="SET NULL"),
    )
    curated_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    diff_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    edit_note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SavedItemCuratedInsightEvent(Base):
    __tablename__ = "saved_item_curated_insight_events"
    __table_args__ = (
        Index("ix_saved_item_curated_insight_events_item_time", "saved_item_id", "created_at"),
        Index("ix_saved_item_curated_insight_events_type_time", "event_type", "created_at"),
        Index("ix_curated_insight_events_base_ie_artifact", "base_insight_engine_artifact_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    saved_item_id: Mapped[int] = mapped_column(
        ForeignKey("saved_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    curated_insight_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("saved_item_curated_insights.id", ondelete="SET NULL"),
        index=True,
    )
    base_insight_engine_artifact_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("insight_engine_artifacts.id", ondelete="SET NULL"),
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class SavedItemCaptureRun(Base):
    __tablename__ = "saved_item_capture_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    saved_item_id: Mapped[int] = mapped_column(
        ForeignKey("saved_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    requested_targets_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    retry_of_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("saved_item_capture_runs.id", ondelete="SET NULL"),
        index=True,
    )
    error_summary: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SavedItemCaptureToolCall(Base):
    __tablename__ = "saved_item_capture_tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    capture_run_id: Mapped[int] = mapped_column(
        ForeignKey("saved_item_capture_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_version: Mapped[Optional[str]] = mapped_column(String(64))
    tool_args_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer)
    stdout_tail: Mapped[Optional[str]] = mapped_column(Text)
    stderr_tail: Mapped[Optional[str]] = mapped_column(Text)
    error_summary: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SavedSnapshot(Base):
    __tablename__ = "saved_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    saved_item_id: Mapped[int] = mapped_column(ForeignKey("saved_items.id", ondelete="CASCADE"), nullable=False, index=True)
    capture_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("saved_item_capture_runs.id", ondelete="SET NULL"),
        index=True,
    )
    snapshot_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="html")
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False, default="text/html")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SavedItemAsset(Base):
    __tablename__ = "saved_item_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_key: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True, default=lambda: str(uuid4()))
    saved_item_id: Mapped[int] = mapped_column(
        ForeignKey("saved_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    capture_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("saved_item_capture_runs.id", ondelete="SET NULL"),
        index=True,
    )
    asset_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset_role: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_name: Mapped[Optional[str]] = mapped_column(String(255))
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(32), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SavedSnapshotAssetRef(Base):
    __tablename__ = "saved_snapshot_asset_refs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    saved_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("saved_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("saved_item_assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ref_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    ref_token: Mapped[str] = mapped_column(Text, nullable=False)
    alt_text: Mapped[Optional[str]] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MirrorExportBatch(Base):
    __tablename__ = "mirror_export_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mirror_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    requested_scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_summary: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SavedItemMirrorExport(Base):
    __tablename__ = "saved_item_mirror_exports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    saved_item_id: Mapped[int] = mapped_column(
        ForeignKey("saved_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_capture_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("saved_item_capture_runs.id", ondelete="SET NULL"),
        index=True,
    )
    batch_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("mirror_export_batches.id", ondelete="SET NULL"),
        index=True,
    )
    mirror_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    requested_targets_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    error_summary: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SavedItemMirrorExportFile(Base):
    __tablename__ = "saved_item_mirror_export_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mirror_export_id: Mapped[int] = mapped_column(
        ForeignKey("saved_item_mirror_exports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    file_role: Mapped[str] = mapped_column(String(64), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    expected_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    actual_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    expected_byte_size: Mapped[Optional[int]] = mapped_column(Integer)
    actual_byte_size: Mapped[Optional[int]] = mapped_column(Integer)
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    error_summary: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class InsightEngineArtifact(Base):
    __tablename__ = "insight_engine_artifacts"
    __table_args__ = (
        CheckConstraint(
            "(feed_item_id IS NULL) <> (saved_item_id IS NULL)",
            name="ck_ie_artifacts_exactly_one_owner",
        ),
        CheckConstraint(
            "("
            "artifact_kind IN ("
            "'feed_item_summary_v1', "
            "'feed_item_topics_v1', "
            "'feed_item_ranking_v1', "
            "'feed_item_routing_v1'"
            ") AND feed_item_id IS NOT NULL AND saved_item_id IS NULL"
            ") OR ("
            "artifact_kind = 'saved_item_insight_v1' AND saved_item_id IS NOT NULL AND feed_item_id IS NULL"
            ")",
            name="ck_ie_artifacts_kind_owner",
        ),
        Index("ix_insight_engine_artifacts_kind_feed_latest", "artifact_kind", "feed_item_id", text("id DESC")),
        Index("ix_insight_engine_artifacts_kind_saved_latest", "artifact_kind", "saved_item_id", text("id DESC")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("feed_items.id", ondelete="CASCADE"), index=True)
    saved_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("saved_items.id", ondelete="CASCADE"), index=True)
    artifact_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(128))
    prompt_version: Mapped[Optional[str]] = mapped_column(String(64))
    artifact_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
