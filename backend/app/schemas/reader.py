from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ReaderItemStatus = Literal["unread", "browsed", "seen", "read_full", "saved", "not_interested"]
ContentTextSource = Literal["feed_content", "feed_summary", "extracted_page"]
ContentExtractionStatus = Literal["not_checked", "not_needed", "needs_extraction", "succeeded", "failed", "skipped"]
ReadingDepth = Literal["light", "medium", "deep"]
HomepageFlowLane = Literal["main", "brief_pool", "low_priority", "needs_review"]
StructuredFeedbackOverallValue = Literal["no_value", "neutral", "useful", "very_useful", "must_read", "high"]
StructuredFeedbackPriorityCorrection = Literal["too_low", "appropriate", "too_high"]
StructuredFeedbackIssueMarker = Literal[
    "misleading_summary",
    "wrong_topic",
    "wrong_priority",
    "thin_content",
    "duplicate",
    "source_quality",
    "extraction_problem",
]


class ReaderItemStateRead(BaseModel):
    status: ReaderItemStatus
    browsed_at: datetime | None
    seen_at: datetime | None
    read_full_at: datetime | None
    saved_at: datetime | None
    not_interested_at: datetime | None
    updated_at: datetime


class ReaderStructuredFeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    overall_value: StructuredFeedbackOverallValue | None = None
    priority_correction: StructuredFeedbackPriorityCorrection | None = None
    topic_add_ids: list[str] = Field(default_factory=list)
    topic_remove_ids: list[str] = Field(default_factory=list)
    content_type_correction: str | None = None
    reason_code_add_ids: list[str] = Field(default_factory=list)
    reason_code_remove_ids: list[str] = Field(default_factory=list)
    issue_markers: list[StructuredFeedbackIssueMarker] = Field(default_factory=list)
    primary_channel_correction: str | None = None
    channel_add_ids: list[str] = Field(default_factory=list, max_length=12)
    channel_remove_ids: list[str] = Field(default_factory=list, max_length=12)
    reading_depth_correction: ReadingDepth | None = None
    flow_lane_correction: HomepageFlowLane | None = None
    channel_correction_notes: str = Field(default="", max_length=1000)


class ReaderStructuredFeedbackRead(BaseModel):
    overall_value: StructuredFeedbackOverallValue | None = None
    priority_correction: StructuredFeedbackPriorityCorrection | None = None
    topic_add_ids: list[str] = Field(default_factory=list)
    topic_remove_ids: list[str] = Field(default_factory=list)
    content_type_correction: str | None = None
    reason_code_add_ids: list[str] = Field(default_factory=list)
    reason_code_remove_ids: list[str] = Field(default_factory=list)
    issue_markers: list[StructuredFeedbackIssueMarker] = Field(default_factory=list)
    primary_channel_correction: str | None = None
    channel_add_ids: list[str] = Field(default_factory=list)
    channel_remove_ids: list[str] = Field(default_factory=list)
    reading_depth_correction: ReadingDepth | None = None
    flow_lane_correction: HomepageFlowLane | None = None
    channel_correction_notes: str = ""
    created_at: datetime
    updated_at: datetime


class BehaviorViewContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: Literal["ai_homepage", "classic_reader", "knowledge_base", "unknown"] = "unknown"
    scope_type: Literal[
        "global",
        "source",
        "source_category",
        "channel",
        "flow_lane",
        "reading_depth",
        "search",
        "reader",
        "unknown",
    ] = "unknown"
    source_id: int | None = None
    source_category: str | None = None
    channel_id: str | None = None
    flow_lane: str | None = None
    reading_depth: str | None = None
    search_query: str | None = None


class ReaderListItemRead(BaseModel):
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


class ReaderDetailRead(ReaderListItemRead):
    feed_content_text: str | None
    content_text: str | None


class ReaderStateMutationResponse(BaseModel):
    item_id: int
    state: ReaderItemStateRead
    saved_item_id: int | None = None
    has_canonical_snapshot: bool | None = None
    has_original_page_snapshot: bool | None = None
    has_singlefile_html: bool | None = None
    has_crawl4ai_raw_markdown: bool | None = None
    has_crawl4ai_fit_markdown: bool | None = None
    has_crawl4ai_fit_image_reinserted_markdown: bool | None = None
    image_asset_count: int | None = None


class ReaderBulkMarkSeenRequest(BaseModel):
    source_id: int
    view_context: BehaviorViewContext | None = None


class ReaderBulkMarkSeenResponse(BaseModel):
    source_id: int
    matched_item_count: int
    updated_item_count: int


class ReaderInsightFeedbackUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    insight_feedback_text: str | None = None
    structured_feedback: ReaderStructuredFeedbackInput | None = None
    view_context: BehaviorViewContext | None = None


class ReaderBehaviorEventContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view_context: BehaviorViewContext | None = None


class ReaderCombinedFeedbackUpdateRequest(ReaderInsightFeedbackUpdateRequest):
    pass


class ReaderInsightFeedbackResponse(BaseModel):
    item_id: int
    insight_feedback_text: str | None
    structured_feedback: ReaderStructuredFeedbackRead | None = None
