export type ViewMode = 'ai' | 'reader' | 'kb' | 'settings';
export type ItemStatus =
  | 'unread'
  | 'browsed'
  | 'seen'
  | 'read_full'
  | 'saved'
  | 'not_interested';
export type HomepageState = ItemStatus;
export type CaptureStatus = 'pending' | 'succeeded' | 'partial_failed' | 'failed';
export type MirrorStatus = 'pending' | 'succeeded' | 'partial_failed' | 'failed';
export type CaptureOperationStatus = 'running' | 'failed';
export type ArchiveArtifactKind =
  | 'singlefile_html_v1'
  | 'crawl4ai_raw_markdown_v1'
  | 'crawl4ai_fit_markdown_v1'
  | 'crawl4ai_fit_image_reinserted_markdown_v1'
  | 'page_image_v1'
  | 'uploaded_source_material_v1'
  | 'uploaded_markdown_v1';
export type ArchiveArtifactStatus = 'available' | 'unavailable';
export type SavedItemResolvedInsightSource = 'ai_generated' | 'curated';
export type SavedItemAIRoute = 'general' | 'sensitive' | 'disabled';
export type GroupMode = 'time' | 'channel' | 'flow_lane' | 'topic' | 'source' | 'low_priority';
export type KnowledgeScope = 'all' | 'feed_saved' | 'external_source';
export type SourceScopeKind = 'all_sources' | 'source_category' | 'source';
export type SortMode = 'latest' | 'priority';
export type HomepageSort = 'priority' | 'newest' | 'source';
export type TimeWindow = '1h' | '4h' | '6h' | '24h' | 'all';
export type ThemeMode = 'light' | 'dark';
export type HomepagePriorityBand = 'high' | 'medium' | 'low';
export type LowPriorityBucketReason = 'not_interested' | 'auto_score_threshold';
export type HomepageSummarySource = 'insight_engine_artifact' | 'canonical_summary' | 'canonical_content' | 'none';
export type HomepageTopicSource = 'insight_engine_artifact' | 'topic_hints' | 'source_folder' | 'none';
export type HomepagePrioritySource = 'insight_engine_artifact' | 'deterministic_fallback' | 'backend_scoring_v1';
export type ReadingDepth = 'light' | 'medium' | 'deep';
export type ReadingDepthFilter = 'all' | ReadingDepth;
export type HomepageFlowLane = 'main' | 'brief_pool' | 'low_priority' | 'needs_review';
export type HomepageRoutingSource =
  | 'insight_engine_artifact'
  | 'channel_lifecycle'
  | 'deterministic_fallback'
  | 'user_corrected';
export type HomepageReadModelState = 'fresh' | 'rebuilding' | 'fallback_live';
export type HomepageSecondaryChannelDisplayMode = 'list' | 'tag_only';
export type HomepageSourcePolicyApplied = 'normal' | 'mixed_high_frequency' | 'high_frequency_brief';
export type FeedItemInsightArtifactMissingCandidateProvider = 'deepseek' | 'openai-compatible' | 'lm-studio' | 'disabled';
export type FeedItemInsightArtifactMissingCandidateProfile = 'general' | 'sensitive' | 'disabled';
export type ContentTextSource = 'feed_content' | 'feed_summary' | 'extracted_page';
export type BehaviorViewSurface = 'ai_homepage' | 'classic_reader' | 'knowledge_base' | "unknown";
export type BehaviorViewScopeType =
  | 'global'
  | 'source'
  | 'source_category'
  | 'channel'
  | 'flow_lane'
  | 'reading_depth'
  | 'search'
  | 'reader'
  | "unknown";
export type ContentExtractionStatus =
  | 'not_checked'
  | 'not_needed'
  | 'needs_extraction'
  | 'succeeded'
  | 'failed'
  | 'skipped';
export type StructuredFeedbackOverallValue = 'no_value' | 'neutral' | 'useful' | 'very_useful' | 'must_read' | 'high';
export type StructuredFeedbackPriorityCorrection = 'too_low' | 'appropriate' | 'too_high';
export type StructuredFeedbackIssueMarker =
  | 'misleading_summary'
  | 'wrong_topic'
  | 'wrong_priority'
  | 'thin_content'
  | 'duplicate'
  | 'source_quality'
  | 'extraction_problem';

export interface HomepagePriorityReason {
  code: string;
  label: string;
  weight: number;
}

export interface HomepageSecondaryChannel {
  channelId: string;
  confidence: number;
  displayMode: HomepageSecondaryChannelDisplayMode;
}

export interface FeedItemStructuredFeedback {
  overallValue: StructuredFeedbackOverallValue | null;
  priorityCorrection: StructuredFeedbackPriorityCorrection | null;
  topicAddIds: string[];
  topicRemoveIds: string[];
  contentTypeCorrection: string | null;
  reasonCodeAddIds: string[];
  reasonCodeRemoveIds: string[];
  issueMarkers: StructuredFeedbackIssueMarker[];
  primaryChannelCorrection: string | null;
  channelAddIds: string[];
  channelRemoveIds: string[];
  readingDepthCorrection: ReadingDepth | null;
  flowLaneCorrection: HomepageFlowLane | null;
  channelCorrectionNotes: string;
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface FeedItemChannelCorrection {
  primaryChannelId: string | null;
  addChannelIds: string[];
  removeChannelIds: string[];
  readingDepth: ReadingDepth | null;
  flowLane: HomepageFlowLane | null;
  notes: string;
  updatedAt?: string | null;
}

export interface ReaderStructuredFeedbackDto {
  overall_value: StructuredFeedbackOverallValue | null;
  priority_correction: StructuredFeedbackPriorityCorrection | null;
  topic_add_ids: string[];
  topic_remove_ids: string[];
  content_type_correction: string | null;
  reason_code_add_ids: string[];
  reason_code_remove_ids: string[];
  issue_markers: StructuredFeedbackIssueMarker[];
  primary_channel_correction: string | null;
  channel_add_ids: string[];
  channel_remove_ids: string[];
  reading_depth_correction: ReadingDepth | null;
  flow_lane_correction: HomepageFlowLane | null;
  channel_correction_notes: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface FeedItemChannelCorrectionDto {
  primary_channel_id: string | null;
  add_channel_ids: string[];
  remove_channel_ids: string[];
  reading_depth: ReadingDepth | null;
  flow_lane: HomepageFlowLane | null;
  notes: string;
  updated_at?: string | null;
}

export interface ReaderStructuredFeedbackInputDto {
  overall_value?: StructuredFeedbackOverallValue | null;
  priority_correction?: StructuredFeedbackPriorityCorrection | null;
  topic_add_ids?: string[];
  topic_remove_ids?: string[];
  content_type_correction?: string | null;
  reason_code_add_ids?: string[];
  reason_code_remove_ids?: string[];
  issue_markers?: StructuredFeedbackIssueMarker[];
  primary_channel_correction?: string | null;
  channel_add_ids?: string[];
  channel_remove_ids?: string[];
  reading_depth_correction?: ReadingDepth | null;
  flow_lane_correction?: HomepageFlowLane | null;
  channel_correction_notes?: string;
}

export interface BehaviorViewContextDto {
  surface: BehaviorViewSurface;
  scope_type: BehaviorViewScopeType;
  source_id?: number | null;
  source_category?: string | null;
  channel_id?: string | null;
  flow_lane?: string | null;
  reading_depth?: string | null;
  search_query?: string | null;
}

export interface FeedItemInsightFeedbackInput {
  insightFeedbackText: string | null;
  structuredFeedback: FeedItemStructuredFeedback | null;
}

export interface FeedItemChannelCorrectionInput {
  primaryChannelId: string | null;
  addChannelIds: string[];
  removeChannelIds: string[];
  readingDepth: ReadingDepth | null;
  flowLane: HomepageFlowLane | null;
  notes: string;
}

export type FeedItemRoutingCorrectionInput = {
  primaryChannelCorrection?: string | null;
  addChannelIds?: string[];
  removeChannelIds?: string[];
  readingDepthCorrection?: ReadingDepth | null;
  flowLaneCorrection?: HomepageFlowLane | null;
  channelCorrectionNotes?: string;
};

export interface FeedItemFeedbackInput extends FeedItemInsightFeedbackInput {
  channelFeedback: FeedItemChannelCorrectionInput | null;
}

export interface CaptureTitleMetadata {
  capturedAt: string | null;
  capturedPageTitle: string | null;
  singlefileTitle: string | null;
  crawl4aiTitle: string | null;
  feedTitle: string | null;
  titleSource: string | null;
  userTitlePresent: boolean | null;
  titleUpdated: boolean | null;
  savedItemTitleBefore: string | null;
  savedItemTitleAfter: string | null;
}

export interface CaptureOperation {
  savedItemId: number;
  operationKind?: string | null;
  status: CaptureOperationStatus;
  startedAt: string;
  updatedAt: string;
  message: string;
  detail: string | null;
  error: string | null;
}

export interface FeedItem {
  id: number;
  feedItemId: number | null;
  savedItemId: number | null;
  sourceId: number | null;
  title: string;
  summary: string;
  feedContentText?: string | null;
  content: string;
  sourceName: string;
  sourceAppName: string;
  sourceFolder: string;
  sourceCategoryId: string;
  publishedAt: string;
  topics: string[];
  status: ItemStatus;
  relevanceScore: number | null;
  priorityBand: HomepagePriorityBand | null;
  priorityReasons: HomepagePriorityReason[];
  isLowPriorityBucket: boolean;
  lowPriorityBucketReason: LowPriorityBucketReason | null;
  autoLowPriorityCleared: boolean;
  originalUrl: string;
  sourceAppUrl: string;
  contentTextSource: ContentTextSource | null;
  contentExtractionStatus: ContentExtractionStatus;
  contentExtractionAttemptedAt?: string | null;
  contentExtractedAt?: string | null;
  contentExtractionError?: string | null;
  contentExtractionMethod?: string | null;
  contentExtractionFinalUrl?: string | null;
  contentExtractionVisibleCharCount?: number | null;
  insightFeedbackText?: string | null;
  structuredFeedback?: FeedItemStructuredFeedback | null;
  channelFeedback?: FeedItemChannelCorrection | null;
  browsedAt?: string | null;
  seenAt?: string | null;
  readFullAt?: string | null;
  savedAt?: string | null;
  notInterestedAt?: string | null;
  note?: string | null;
  originType?: string | null;
  aiRoute?: SavedItemAIRoute | null;
  originalFilename?: string | null;
  mimeType?: string | null;
  processingStatus?: string | null;
  processingError?: string | null;
  sourceMaterialArtifact?: ArchiveArtifact | null;
  resolvedInsight?: SavedItemResolvedInsight | null;
  aiGeneratedInsight?: SavedItemAIInsight | null;
  curatedInsight?: SavedItemCuratedInsight | null;
  hasCanonicalSnapshot?: boolean;
  hasOriginalPageSnapshot?: boolean;
  hasSinglefileHtml?: boolean;
  hasCrawl4aiRawMarkdown?: boolean;
  hasCrawl4aiFitMarkdown?: boolean;
  hasCrawl4aiFitImageReinsertedMarkdown?: boolean;
  imageAssetCount?: number;
  captureStatus?: CaptureStatus | null;
  latestCaptureRunId?: number | null;
  captureErrorSummary?: string | null;
  captureStartedAt?: string | null;
  captureFinishedAt?: string | null;
  captureRetryable?: boolean;
  captureTitleMetadata?: CaptureTitleMetadata | null;
  captureToolCalls?: CaptureToolCall[];
  localMirrorStatus?: MirrorStatus | null;
  latestLocalMirrorExportId?: number | null;
  localMirrorErrorSummary?: string | null;
  localMirrorStartedAt?: string | null;
  localMirrorFinishedAt?: string | null;
  localMirrorSourceCaptureRunId?: number | null;
  localMirrorWrittenFileCount?: number;
  localMirrorPaths?: SavedItemLocalMirrorPaths | null;
  archiveArtifacts?: ArchiveArtifact[];
  summarySource?: HomepageSummarySource;
  topicSource?: HomepageTopicSource;
  prioritySource?: HomepagePrioritySource;
  artifactGeneratedAt?: string | null;
  primaryChannelId?: string | null;
  secondaryChannels?: HomepageSecondaryChannel[];
  readingDepth?: ReadingDepth | null;
  flowLane?: HomepageFlowLane;
  routingConfidence?: number | null;
  routingSource?: HomepageRoutingSource;
  routingReasonCodes?: string[];
  promotionReasonCodes?: string[];
  isolationReasonCodes?: string[];
  sourcePolicyApplied?: HomepageSourcePolicyApplied | null;
  routingNotes?: string | null;
  /** UI field derived from routingReasonCodes. */
  routingReasons?: string[];
  isSaving?: boolean;
}

export interface FacetCount {
  label: string;
  count: number;
}

export interface ChannelFacetCount extends FacetCount {
  id: string;
  channelId: string;
  enabled: boolean;
}

export interface FlowLaneFacetCount extends FacetCount {
  id: HomepageFlowLane;
  lane: HomepageFlowLane;
  enabled: boolean;
}

export interface SourceScope {
  kind: SourceScopeKind;
  value: string | number | null;
  label: string;
}

export interface ReaderItemStateDto {
  status: ItemStatus;
  browsed_at: string | null;
  seen_at: string | null;
  read_full_at: string | null;
  saved_at: string | null;
  not_interested_at: string | null;
  updated_at: string;
}

export interface ReaderListItemDto {
  id: number;
  title: string;
  author_name: string | null;
  published_at: string | null;
  summary_text: string | null;
  original_url: string;
  canonical_url: string | null;
  source_id: number;
  source_name: string;
  source_kind: string;
  source_url: string;
  source_site_url: string | null;
  source_folder_path: string | null;
  content_text_source: ContentTextSource;
  content_extraction_status: ContentExtractionStatus;
  content_extraction_attempted_at: string | null;
  content_extracted_at: string | null;
  content_extraction_error: string | null;
  content_extraction_method: string | null;
  content_extraction_final_url: string | null;
  content_extraction_visible_char_count: number | null;
  insight_feedback_text: string | null;
  structured_feedback: ReaderStructuredFeedbackDto | null;
  state: ReaderItemStateDto;
}

export interface ReaderDetailDto extends ReaderListItemDto {
  feed_content_text: string | null;
  content_text: string | null;
}

export interface ReaderStateMutationResponseDto {
  item_id: number;
  state: ReaderItemStateDto;
  saved_item_id?: number | null;
  has_canonical_snapshot?: boolean | null;
  has_original_page_snapshot?: boolean | null;
  has_singlefile_html?: boolean | null;
  has_crawl4ai_raw_markdown?: boolean | null;
  has_crawl4ai_fit_markdown?: boolean | null;
  has_crawl4ai_fit_image_reinserted_markdown?: boolean | null;
  image_asset_count?: number | null;
}

export interface ReaderBulkMarkSeenRequestDto {
  source_id: number;
  view_context?: BehaviorViewContextDto | null;
}

export interface ReaderBulkMarkSeenResponseDto {
  source_id: number;
  matched_item_count: number;
  updated_item_count: number;
}

export interface ReaderInsightFeedbackResponseDto {
  item_id: number;
  insight_feedback_text: string | null;
  structured_feedback: ReaderStructuredFeedbackDto | null;
}

export interface SavedItemRemoveResponseDto {
  saved_item_id: number;
  feed_item_id: number | null;
  removed_at: string;
  state: ReaderItemStateDto | null;
}

export interface HomepageCardDto extends ReaderListItemDto {
  topic_labels: string[];
  priority_score: number;
  priority_band: HomepagePriorityBand;
  priority_reasons: HomepagePriorityReason[];
  low_priority_bucket: boolean;
  low_priority_bucket_reason: LowPriorityBucketReason | null;
  auto_low_priority_cleared: boolean;
  summary_source: HomepageSummarySource;
  topic_source: HomepageTopicSource;
  priority_source: HomepagePrioritySource;
  artifact_generated_at: string | null;
  primary_channel_id: string | null;
  secondary_channels: {
    channel_id: string;
    confidence: number;
    display_mode: HomepageSecondaryChannelDisplayMode;
  }[];
  reading_depth: ReadingDepth | null;
  flow_lane: HomepageFlowLane;
  routing_confidence: number | null;
  routing_source: HomepageRoutingSource;
  routing_reason_codes: string[];
  promotion_reason_codes: string[];
  isolation_reason_codes: string[];
  source_policy_applied: HomepageSourcePolicyApplied | null;
  routing_notes: string | null;
}

export interface HomepageFacetCountDto {
  id: string;
  label: string;
  count: number;
}

export interface HomepageReadModelStatusDto {
  state: HomepageReadModelState;
  missing_count: number;
  stale_count: number;
  message: string | null;
}

export interface HomepageSummaryDto {
  query_signature: string;
  generated_at: string;
  total_count: number;
  unread_count: number;
  saved_count: number;
  not_interested_count: number;
  channel_counts: HomepageFacetCountDto[];
  source_counts: HomepageFacetCountDto[];
  source_category_counts: HomepageFacetCountDto[];
  topic_counts: HomepageFacetCountDto[];
  flow_lane_counts: HomepageFacetCountDto[];
  state_counts: HomepageFacetCountDto[];
  read_model_status?: HomepageReadModelStatusDto | null;
}

export interface HomepageItemsDto {
  query_signature: string;
  generated_at: string;
  items: HomepageCardDto[];
  next_cursor: string | null;
  has_more: boolean;
  page_size: number;
  snapshot_token: string | null;
  snapshot_expires_at: string | null;
  read_model_status?: HomepageReadModelStatusDto | null;
}

export interface HomepageBootstrapDto extends HomepageItemsDto {
  summary: HomepageSummaryDto;
}

export interface HomepageFacetCount {
  id: string;
  label: string;
  count: number;
}

export interface HomepageReadModelStatus {
  state: HomepageReadModelState;
  missingCount: number;
  staleCount: number;
  message: string | null;
}

export interface HomepageSummary {
  querySignature: string;
  generatedAt: string;
  totalCount: number;
  unreadCount: number;
  savedCount: number;
  notInterestedCount: number;
  channelCounts: HomepageFacetCount[];
  sourceCounts: HomepageFacetCount[];
  sourceCategoryCounts: HomepageFacetCount[];
  topicCounts: HomepageFacetCount[];
  flowLaneCounts: HomepageFacetCount[];
  stateCounts: HomepageFacetCount[];
  readModelStatus: HomepageReadModelStatus | null;
}

export interface HomepageItemsResponse {
  querySignature: string;
  generatedAt: string;
  items: FeedItem[];
  nextCursor: string | null;
  hasMore: boolean;
  pageSize: number;
  snapshotToken: string | null;
  snapshotExpiresAt: string | null;
  readModelStatus: HomepageReadModelStatus | null;
}

export interface HomepageBootstrapResponse extends HomepageItemsResponse {
  summary: HomepageSummary;
}

export interface FeedItemInsightArtifactMissingCandidateDto {
  feed_item_id: number;
  source_id: number;
  ai_route: SavedItemAIRoute;
  profile: FeedItemInsightArtifactMissingCandidateProfile;
  provider: FeedItemInsightArtifactMissingCandidateProvider;
}

export interface FeedItemInsightArtifactMissingCandidatesResponseDto {
  scope: string;
  total_count: number;
  has_more: boolean;
  candidates: FeedItemInsightArtifactMissingCandidateDto[];
}

export interface SavedItemSnapshotStatusDto {
  has_canonical_snapshot: boolean;
  has_original_page_snapshot: boolean;
  has_singlefile_html: boolean;
  has_crawl4ai_raw_markdown: boolean;
  has_crawl4ai_fit_markdown: boolean;
  has_crawl4ai_fit_image_reinserted_markdown: boolean;
  image_asset_count: number;
}

export interface SavedItemCaptureDto {
  status: CaptureStatus;
  latest_run_id: number | null;
  latest_error_summary: string | null;
  latest_started_at: string | null;
  latest_finished_at: string | null;
  retryable: boolean;
  title_metadata: SavedItemCaptureTitleMetadataDto | null;
}

export interface SavedItemCaptureTitleMetadataDto {
  captured_at: string | null;
  captured_page_title: string | null;
  singlefile_title: string | null;
  crawl4ai_title: string | null;
  feed_title: string | null;
  title_source: string | null;
  user_title_present: boolean | null;
  title_updated: boolean | null;
  saved_item_title_before: string | null;
  saved_item_title_after: string | null;
}

export interface SavedItemCaptureToolCallDto {
  id: number;
  tool_name: string;
  tool_version: string | null;
  status: string;
  exit_code: number | null;
  error_summary: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface SavedItemLocalMirrorDto {
  status: MirrorStatus;
  latest_export_id: number | null;
  latest_error_summary: string | null;
  latest_started_at: string | null;
  latest_finished_at: string | null;
  source_capture_run_id: number | null;
  written_file_count: number;
  paths: SavedItemLocalMirrorPathsDto;
}

export interface SavedItemLocalMirrorPathsDto {
  knowledge_base_root: string;
  article_dir: string;
  raw_markdown: string;
  fit_markdown: string;
  fit_image_reinserted_markdown: string;
  uploaded_markdown: string;
  manifest: string;
  markdown_assets_dir: string;
  singlefile_html: string;
  source_material: string | null;
}

export interface SavedItemLocalMirrorPaths {
  knowledgeBaseRoot: string;
  articleDir: string;
  rawMarkdown: string;
  fitMarkdown: string;
  fitImageReinsertedMarkdown: string;
  uploadedMarkdown: string;
  manifest: string;
  markdownAssetsDir: string;
  singlefileHtml: string;
  sourceMaterial: string | null;
}

export interface SavedItemArchiveArtifactDto {
  artifact_kind: ArchiveArtifactKind;
  status: ArchiveArtifactStatus;
  snapshot_id: number | null;
  asset_id: number | null;
  asset_key: string | null;
  logical_name: string | null;
  mime_type: string | null;
  byte_size: number | null;
  created_at: string | null;
  capture_run_id: number | null;
  source_url: string | null;
  preview_url: string | null;
  download_url: string | null;
}

export interface SavedItemInsightPayloadDto {
  summary: string | null;
  long_term_value: string | null;
  actionable_takeaways: string[];
  key_concepts: string[];
  suggested_tags: string[];
  topic_ids: string[];
  topic_labels: string[];
  content_type: string | null;
  content_type_label: string | null;
  source_kind: string | null;
  ai_route: SavedItemAIRoute | null;
  input_snapshot_kind: string | null;
  input_text_source: string | null;
  input_char_count: number;
  input_text_was_truncated: boolean;
}

export interface SavedItemAIInsightDto {
  artifact_id: number;
  artifact_kind: string;
  model_name: string | null;
  prompt_version: string | null;
  payload: SavedItemInsightPayloadDto;
  created_at: string;
}

export interface SavedItemCuratedInsightDto {
  id: number;
  saved_item_id: number;
  base_insight_engine_artifact_id: number | null;
  payload: SavedItemInsightPayloadDto;
  diff_json: Record<string, unknown> | null;
  edit_note: string | null;
  created_at: string;
  updated_at: string;
}

export interface SavedItemResolvedInsightDto {
  source: SavedItemResolvedInsightSource;
  payload: SavedItemInsightPayloadDto;
  insight_engine_artifact_id: number | null;
  curated_insight_id: number | null;
  model_name: string | null;
  prompt_version: string | null;
  generated_at: string | null;
  curated_updated_at: string | null;
}

export interface SavedItemCuratedInsightEventDto {
  id: number;
  saved_item_id: number;
  curated_insight_id: number | null;
  base_insight_engine_artifact_id: number | null;
  event_type: string;
  event_payload_json: Record<string, unknown>;
  created_at: string;
}

export interface SavedItemInsightPayload {
  summary: string | null;
  longTermValue: string | null;
  actionableTakeaways: string[];
  keyConcepts: string[];
  suggestedTags: string[];
  topicIds: string[];
  topicLabels: string[];
  contentType: string | null;
  contentTypeLabel: string | null;
  sourceKind: string | null;
  aiRoute: SavedItemAIRoute | null;
  inputSnapshotKind: string | null;
  inputTextSource: string | null;
  inputCharCount: number;
  inputTextWasTruncated: boolean;
}

export interface SavedItemAIInsight {
  artifactId: number;
  artifactKind: string;
  modelName: string | null;
  promptVersion: string | null;
  payload: SavedItemInsightPayload;
  createdAt: string;
}

export interface SavedItemCuratedInsight {
  id: number;
  savedItemId: number;
  baseInsightEngineArtifactId: number | null;
  payload: SavedItemInsightPayload;
  diffJson: Record<string, unknown> | null;
  editNote: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface SavedItemResolvedInsight {
  source: SavedItemResolvedInsightSource;
  payload: SavedItemInsightPayload;
  insightEngineArtifactId: number | null;
  curatedInsightId: number | null;
  modelName: string | null;
  promptVersion: string | null;
  generatedAt: string | null;
  curatedUpdatedAt: string | null;
}

export interface SavedItemCuratedInsightEvent {
  id: number;
  savedItemId: number;
  curatedInsightId: number | null;
  baseInsightEngineArtifactId: number | null;
  eventType: string;
  eventPayloadJson: Record<string, unknown>;
  createdAt: string;
}

export interface SavedItemCuratedInsightInput {
  payload: SavedItemInsightPayload;
  editNote: string | null;
}

export interface ArchiveArtifact {
  artifactKind: ArchiveArtifactKind;
  status: ArchiveArtifactStatus;
  snapshotId: number | null;
  assetId: number | null;
  assetKey: string | null;
  logicalName: string | null;
  mimeType: string | null;
  byteSize: number | null;
  createdAt: string | null;
  captureRunId: number | null;
  sourceUrl: string | null;
  previewUrl: string | null;
  downloadUrl: string | null;
}

export interface KnowledgeSavedItemListDto {
  id: number;
  feed_item_id: number | null;
  origin_type: string;
  ai_route: SavedItemAIRoute;
  title: string;
  author_name: string | null;
  published_at: string | null;
  summary_text: string | null;
  canonical_url: string | null;
  original_url: string | null;
  source_id: number | null;
  source_name: string;
  source_kind: string | null;
  source_url: string | null;
  source_site_url: string | null;
  source_folder_path: string | null;
  original_filename: string | null;
  mime_type: string | null;
  processing_status: string;
  processing_error: string | null;
  source_material_artifact: SavedItemArchiveArtifactDto | null;
  saved_at: string;
  user_note: string | null;
  state: ReaderItemStateDto;
  snapshots: SavedItemSnapshotStatusDto;
  capture: SavedItemCaptureDto | null;
  local_mirror: SavedItemLocalMirrorDto | null;
  resolved_insight: SavedItemResolvedInsightDto | null;
}

export interface KnowledgeSavedItemDetailDto extends KnowledgeSavedItemListDto {
  content_html: string | null;
  capture_tool_calls: SavedItemCaptureToolCallDto[];
  archive_artifacts: SavedItemArchiveArtifactDto[];
  ai_generated_insight: SavedItemAIInsightDto | null;
  curated_insight: SavedItemCuratedInsightDto | null;
}

export interface ManualUrlCaptureInput {
  url: string;
  title: string | null;
  userNote: string | null;
  aiRoute: SavedItemAIRoute;
}

export type UploadMaterialType = 'markdown' | 'html' | 'singlefile_html';

export interface UploadedMaterialCaptureInput {
  file: File;
  materialType: UploadMaterialType;
  title: string | null;
  userNote: string | null;
  aiRoute: SavedItemAIRoute;
}

export interface SavedItemManualImportInput {
  file: File;
  materialType: UploadMaterialType;
  title: string | null;
  userNote: string | null;
}

export interface CaptureToolCall {
  id: number;
  toolName: string;
  toolVersion: string | null;
  status: string;
  exitCode: number | null;
  errorSummary: string | null;
  startedAt: string;
  finishedAt: string | null;
}
