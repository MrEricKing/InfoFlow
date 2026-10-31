from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.reader import (
    ContentExtractionStatus,
    ContentTextSource,
    ReaderItemStateRead,
    ReaderStructuredFeedbackRead,
)
from app.schemas.settings import LowPriorityBucketReason


HomepagePriorityBand = Literal["high", "medium", "low"]
HomepageSummarySource = Literal["insight_engine_artifact", "canonical_summary", "canonical_content", "none"]
HomepageTopicSource = Literal["insight_engine_artifact", "topic_hints", "source_folder", "none"]
HomepagePrioritySource = Literal["insight_engine_artifact", "deterministic_fallback", "backend_scoring_v1"]
HomepageReadingDepth = Literal["light", "medium", "deep"]
HomepageFlowLane = Literal["main", "brief_pool", "low_priority", "needs_review"]
HomepageSort = Literal["priority", "newest", "source"]
HomepageState = Literal["unread", "browsed", "seen", "read_full", "saved", "not_interested"]
HomepageTimeWindow = Literal["1h", "4h", "6h", "24h", "all"]
HomepageReadModelState = Literal["fresh", "rebuilding", "fallback_live"]
HOMEPAGE_EMPTY_SOURCE_CATEGORY_ID = "__empty__"


class HomepageSecondaryChannelRead(BaseModel):
    channel_id: str
    confidence: float
    display_mode: Literal["list", "tag_only"] = "list"


class FeedItemInsightRoutingSource(str, Enum):
    insight_engine_artifact = "insight_engine_artifact"
    channel_lifecycle = "channel_lifecycle"
    deterministic_fallback = "deterministic_fallback"
    user_corrected = "user_corrected"


class HomepagePriorityReasonRead(BaseModel):
    code: str
    label: str
    weight: int


class HomepageCardRead(BaseModel):
    id: int
    title: str
    author_name: str | None
    published_at: datetime | None
    summary_text: str | None
    original_url: str
    canonical_url: str | None
    source_id: int
    source_name: str
    source_kind: str
    source_url: str
    source_site_url: str | None
    source_folder_path: str | None
    content_text_source: ContentTextSource
    content_extraction_status: ContentExtractionStatus
    content_extraction_attempted_at: datetime | None
    content_extracted_at: datetime | None
    content_extraction_error: str | None
    content_extraction_method: str | None = None
    content_extraction_final_url: str | None = None
    content_extraction_visible_char_count: int | None = None
    insight_feedback_text: str | None
    structured_feedback: ReaderStructuredFeedbackRead | None = None
    state: ReaderItemStateRead
    topic_labels: list[str]
    priority_score: int
    priority_band: HomepagePriorityBand
    priority_reasons: list[HomepagePriorityReasonRead]
    low_priority_bucket: bool
    low_priority_bucket_reason: LowPriorityBucketReason | None = None
    auto_low_priority_cleared: bool = False
    summary_source: HomepageSummarySource
    topic_source: HomepageTopicSource
    priority_source: HomepagePrioritySource
    artifact_generated_at: datetime | None
    primary_channel_id: str | None = None
    secondary_channels: list[HomepageSecondaryChannelRead] = Field(default_factory=list)
    reading_depth: HomepageReadingDepth | None = None
    flow_lane: HomepageFlowLane = "main"
    routing_confidence: float | None = None
    routing_source: FeedItemInsightRoutingSource = FeedItemInsightRoutingSource.deterministic_fallback
    routing_reason_codes: list[str] = Field(default_factory=list)
    promotion_reason_codes: list[str] = Field(default_factory=list)
    isolation_reason_codes: list[str] = Field(default_factory=list)
    source_policy_applied: str | None = None
    routing_notes: str | None = None


class HomepageQuery(BaseModel):
    channel_id: str | None = None
    source_id: int | None = Field(default=None, ge=1)
    source_category: str | None = None
    flow_lane: HomepageFlowLane | None = None
    reading_depth: HomepageReadingDepth | None = None
    state: HomepageState | None = None
    saved: bool | None = None
    not_interested: bool | None = None
    topic: str | None = None
    search_text: str | None = None
    time_window: HomepageTimeWindow = "all"
    sort: HomepageSort = "priority"
    page_size: int = Field(default=30, ge=1, le=100)
    cursor: str | None = None
    snapshot_token: str | None = None


class HomepageFacetCountRead(BaseModel):
    id: str
    label: str
    count: int


class HomepageReadModelStatusRead(BaseModel):
    state: HomepageReadModelState
    missing_count: int = 0
    stale_count: int = 0
    message: str | None = None


class HomepageSummaryRead(BaseModel):
    query_signature: str
    generated_at: datetime
    total_count: int
    unread_count: int
    saved_count: int
    not_interested_count: int
    channel_counts: list[HomepageFacetCountRead]
    source_counts: list[HomepageFacetCountRead]
    source_category_counts: list[HomepageFacetCountRead]
    topic_counts: list[HomepageFacetCountRead]
    flow_lane_counts: list[HomepageFacetCountRead]
    state_counts: list[HomepageFacetCountRead]
    read_model_status: HomepageReadModelStatusRead | None = None


class HomepageItemsRead(BaseModel):
    query_signature: str
    generated_at: datetime
    items: list[HomepageCardRead]
    next_cursor: str | None
    has_more: bool
    page_size: int
    snapshot_token: str | None = None
    snapshot_expires_at: datetime | None = None
    read_model_status: HomepageReadModelStatusRead | None = None


class HomepageBootstrapRead(BaseModel):
    query_signature: str
    generated_at: datetime
    summary: HomepageSummaryRead
    items: list[HomepageCardRead]
    next_cursor: str | None
    has_more: bool
    page_size: int
    snapshot_token: str | None = None
    snapshot_expires_at: datetime | None = None
    read_model_status: HomepageReadModelStatusRead | None = None


class FeedItemInsightArtifactBuildResult(BaseModel):
    scope: str
    processed_feed_item_count: int
    artifact_rows_created: int
    failed_feed_item_count: int


class FeedItemInsightArtifactMissingCandidateRead(BaseModel):
    feed_item_id: int
    source_id: int
    ai_route: str
    profile: str
    provider: str


class FeedItemInsightArtifactMissingCandidatesResponse(BaseModel):
    scope: str
    total_count: int = 0
    has_more: bool = False
    candidates: list[FeedItemInsightArtifactMissingCandidateRead]
