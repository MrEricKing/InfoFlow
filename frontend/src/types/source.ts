export type SourceKind = 'rss' | 'rsshub';
export type SourceProvenance = 'manual' | 'opml_import';
export type SourceFetchStatus = 'pending' | 'success' | 'not_modified' | 'error';
export type ImportResultStatus = 'created' | 'matched_existing' | 'invalid';
export type OpmlImportCandidateStatus = 'ready' | 'matched_existing' | 'duplicate_in_file' | 'invalid';
export type SourceAIRoute = 'general' | 'sensitive' | 'disabled';
export type SourceFlowPolicy = 'normal' | 'mixed_high_frequency' | 'high_frequency_brief';
export type SourcePreferenceProfileStatus = 'draft' | 'active' | 'archived';
export type SourcePreferenceProfileSourceType = 'user_edit' | 'codex_import' | 'future_suggestion';
export type SourceIconSource =
  | 'rss_channel_image'
  | 'atom_icon'
  | 'atom_logo'
  | 'opml_site_favicon'
  | 'source_site_favicon'
  | 'fallback_avatar';

export interface SourceDto {
  id: number;
  name: string;
  kind: SourceKind;
  url: string;
  site_url: string | null;
  folder_path: string | null;
  is_active: boolean;
  ai_route: SourceAIRoute;
  icon_url: string | null;
  icon_source: SourceIconSource | null;
  icon_updated_at: string | null;
  provenance: SourceProvenance;
  created_from_import_run_id: number | null;
  item_count: number;
  latest_fetch_is_available: boolean | null;
  latest_fetch_status: SourceFetchStatus | null;
  latest_fetch_http_status: number | null;
  latest_fetch_error_message: string | null;
  latest_fetch_items_seen_count: number | null;
  latest_fetch_items_created_count: number | null;
  latest_fetch_items_matched_count: number | null;
  latest_fetch_items_invalid_count: number | null;
  latest_fetch_started_at: string | null;
  latest_fetch_finished_at: string | null;
  source_profile_draft_source_type: SourcePreferenceProfileSourceType | null;
  source_profile_pending_external_suggestion: boolean;
  created_at: string;
  updated_at: string;
}

export interface SourceCategoryDto {
  path: string;
  source_count: number;
}

export interface SourceCategoryRenamePayload {
  old_path: string;
  new_path: string;
}

export interface SourceCategoryDeletePayload {
  path: string;
}

export interface SourceCategoryMutationResponseDto {
  updated_source_count: number;
  categories: SourceCategoryDto[];
}

export interface SourceCreatePayload {
  name: string;
  kind: SourceKind;
  url: string;
  site_url: string | null;
  folder_path: string | null;
  is_active: boolean;
  ai_route: SourceAIRoute;
  icon_url?: string | null;
  icon_source?: SourceIconSource | null;
}

export interface SourceUpdatePayload {
  name: string;
  url: string;
  site_url: string | null;
  folder_path: string | null;
  is_active: boolean;
  ai_route: SourceAIRoute;
}

export interface SourceDeleteSourceDto {
  id: number;
  name: string;
  kind: SourceKind;
  url: string;
  site_url: string | null;
  folder_path: string | null;
  is_active: boolean;
  ai_route: SourceAIRoute;
  item_count: number;
}

export interface SourceDeletePreviewCountsDto {
  source_fetches: number;
  source_item_occurrences: number;
  exclusive_unsaved_feed_items: number;
  retained_feed_items: number;
  shared_feed_items_retained: number;
  saved_items_retained: number;
  active_saved_items_retained: number;
  removed_saved_items_retained: number;
  source_preference_profiles: number;
  source_channel_tendencies: number;
  homepage_read_models: number;
  feed_item_insight_artifacts: number;
  diagnostic_operations_to_detach: number;
  diagnostic_attempts_to_detach: number;
}

export interface SourceDeleteBlockerDto {
  operation_id: number;
  operation_type: string;
  operation_kind: string;
  status: string;
  source_id: number | null;
  reason: string;
  message: string | null;
}

export interface SourceDeletePlanDto {
  deletes: string[];
  retains: string[];
  detaches: string[];
}

export interface SourceDeletePreviewDto {
  source: SourceDeleteSourceDto;
  can_delete: boolean;
  blockers: SourceDeleteBlockerDto[];
  counts: SourceDeletePreviewCountsDto;
  preview_token: string;
  delete_plan: SourceDeletePlanDto;
}

export interface SourceDeleteResultDto {
  source: SourceDeleteSourceDto;
  deleted: boolean;
  counts: SourceDeletePreviewCountsDto;
  retained_saved_items: number;
}

export interface SourceChannelTendencyDto {
  channel_id: string;
  tendency: number;
  is_primary: boolean;
  notes: string;
}

export interface SourceInsightRoutingDto {
  source_id: number;
  flow_policy: SourceFlowPolicy;
  channel_tendencies: SourceChannelTendencyDto[];
}

export interface SourceChannelTendency {
  channelId: string;
  tendency: number;
  isPrimary: boolean;
  notes: string;
}

export interface SourceInsightRouting {
  sourceId: number;
  flowPolicy: SourceFlowPolicy;
  channelTendencies: SourceChannelTendency[];
}

export interface SourceInsightRoutingInput {
  flowPolicy: SourceFlowPolicy;
  channelTendencies: SourceChannelTendency[];
}

export interface SourcePreferenceProfileFieldError {
  field: string;
  code: string;
  message: string;
  actual: number | null;
  limit: number | null;
  unit: string | null;
  patternClass: string | null;
}

export interface SourcePreferenceProfileFieldErrorDto {
  field: string;
  code: string;
  message: string;
  actual: number | null;
  limit: number | null;
  unit: string | null;
  pattern_class: string | null;
}

export interface SourcePreferenceProfileDto {
  id: number;
  source_id: number;
  status: SourcePreferenceProfileStatus;
  schema_version: string;
  profile_version: string | null;
  subscription_reason: string | null;
  good_article_criteria: string[];
  bad_article_patterns: string[];
  preferred_topics: string[];
  ignored_topics: string[];
  preferred_content_types: string[];
  positive_example_item_ids: number[];
  negative_example_item_ids: number[];
  positive_example_summaries: string[];
  negative_example_summaries: string[];
  quality_notes: string | null;
  prompt_context: Record<string, unknown>;
  prompt_context_stats: Record<string, unknown>;
  source_type: string;
  edit_note: string | null;
  activated_at: string | null;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface SourcePreferenceProfile {
  id: number;
  sourceId: number;
  status: SourcePreferenceProfileStatus;
  schemaVersion: string;
  profileVersion: string | null;
  subscriptionReason: string | null;
  goodArticleCriteria: string[];
  badArticlePatterns: string[];
  preferredTopics: string[];
  ignoredTopics: string[];
  preferredContentTypes: string[];
  positiveExampleItemIds: number[];
  negativeExampleItemIds: number[];
  positiveExampleSummaries: string[];
  negativeExampleSummaries: string[];
  qualityNotes: string | null;
  promptContext: Record<string, unknown>;
  promptContextStats: Record<string, unknown>;
  sourceType: string;
  editNote: string | null;
  activatedAt: string | null;
  archivedAt: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface SourcePreferenceProfileStateDto {
  source_id: number;
  active: SourcePreferenceProfileDto | null;
  draft: SourcePreferenceProfileDto | null;
  history: SourcePreferenceProfileDto[];
  rendered_prompt_context: Record<string, unknown>;
  render_stats: Record<string, unknown>;
  validation_errors: SourcePreferenceProfileFieldErrorDto[];
}

export interface SourcePreferenceProfileState {
  sourceId: number;
  active: SourcePreferenceProfile | null;
  draft: SourcePreferenceProfile | null;
  history: SourcePreferenceProfile[];
  renderedPromptContext: Record<string, unknown>;
  renderStats: Record<string, unknown>;
  validationErrors: SourcePreferenceProfileFieldError[];
}

export interface SourcePreferenceProfileInput {
  subscriptionReason?: string | null;
  goodArticleCriteria?: string[];
  badArticlePatterns?: string[];
  preferredTopics?: string[];
  ignoredTopics?: string[];
  preferredContentTypes?: string[];
  positiveExampleItemIds?: number[];
  negativeExampleItemIds?: number[];
  positiveExampleSummaries?: string[];
  negativeExampleSummaries?: string[];
  qualityNotes?: string | null;
  sourceType?: SourcePreferenceProfileSourceType;
  editNote?: string | null;
}

export interface SourceFetchDto {
  id: number;
  source_id: number;
  requested_url: string;
  status: SourceFetchStatus;
  http_status: number | null;
  etag: string | null;
  last_modified: string | null;
  error_message: string | null;
  items_seen_count: number;
  items_created_count: number;
  items_matched_count: number;
  items_invalid_count: number;
  fetch_started_at: string;
  fetch_finished_at: string | null;
}

export interface SourceFetchWithSourceDto extends SourceFetchDto {
  source_name: string;
}

export interface FeedItemInsightArtifactBuildStatsDto {
  processed_feed_item_count: number;
  artifact_rows_created: number;
  failed_feed_item_count: number;
}

export interface SingleSourceSyncResponseDto {
  source_id: number;
  source_name: string;
  fetch: SourceFetchDto;
  created_feed_item_ids: number[];
  feed_item_insight_artifacts: FeedItemInsightArtifactBuildStatsDto;
}

export interface BulkSourceSyncResponseDto {
  source_count: number;
  success_count: number;
  error_count: number;
  results: SingleSourceSyncResponseDto[];
  stopped?: boolean;
}

export type SourceSyncStage = string;
export type SourceSyncOperationKind = 'all' | 'single' | 'auto_create' | 'selected';
export type KnowledgeCaptureOperationKind =
  | 'manual_url'
  | 'uploaded_material'
  | 'manual_import'
  | 'retry_capture'
  | 'feed_saved_capture';
export type AppOperationType =
  | 'source_sync'
  | 'feed_item_insight_artifacts'
  | 'feed_item_insight_artifact'
  | 'knowledge_capture'
  | 'local_mirror_export'
  | 'feed_item_extract'
  | 'saved_item_insight'
  | 'channel_lifecycle';
export type AppOperationKind =
  | SourceSyncOperationKind
  | FeedItemInsightArtifactBatchOperationScope
  | KnowledgeCaptureOperationKind
  | 'manual_reexport'
  | 'manual_extract'
  | 'rebuild'
  | 'build_missing'
  | 'retire_channel';
export type AppOperationStatus = 'queued' | 'running' | 'stopping' | 'completed' | 'stopped' | 'failed' | 'interrupted';
export type SourceSyncOperationStatus = AppOperationStatus;
export type FeedItemInsightArtifactBatchOperationCreateScope = 'current_source' | 'global' | 'failed_attempts' | 'retryable_coverage';
export type FeedItemInsightArtifactBatchOperationScope = FeedItemInsightArtifactBatchOperationCreateScope | 'source_sync_created_feed_items';
export type FeedItemInsightArtifactBatchOperationStatus = AppOperationStatus;

export interface OperationProgressDto {
  stage: SourceSyncStage;
  total: number;
  completed: number;
  success_count: number;
  error_count: number;
  stopped: boolean;
  stop_requested: boolean;
  current_source_name: string | null;
  current_source_index: number | null;
  current_fetch_status: SourceFetchStatus | null;
  current_items_seen_count: number;
  current_items_created_count: number;
  current_items_matched_count: number;
  current_items_invalid_count: number;
  current_insight_total: number;
  current_insight_completed: number;
  current_insight_failed: number;
  current_insight_artifact_rows_created: number;
  latest_source_name: string | null;
  latest_status: SourceFetchStatus | null;
  items_seen_count: number;
  items_created_count: number;
  items_matched_count: number;
  items_invalid_count: number;
  insight_total_count: number;
  insight_completed_count: number;
  insight_failed_count: number;
  insight_artifact_rows_created: number;
  latest_error_message: string | null;
  failed_sources: SourceSyncFailedSource[];
  job_total_count: number;
  job_pending_count: number;
  job_running_count: number;
  job_succeeded_count: number;
  job_failed_count: number;
  job_cancelled_count: number;
}

export interface AppOperationDto {
  id: number;
  operation_type: AppOperationType;
  operation_kind: AppOperationKind;
  status: AppOperationStatus;
  source_id: number | null;
  source_name: string | null;
  scope: Record<string, unknown> | null;
  progress: OperationProgressDto | null;
  result: Record<string, unknown> | null;
  message: string | null;
  error_message: string | null;
  stop_requested: boolean;
  runner_id: string | null;
  created_at: string;
  started_at: string | null;
  updated_at: string;
  completed_at: string | null;
}

export interface SourceSyncOperationCreatePayloadDto {
  kind: SourceSyncOperationKind;
  source_id?: number | null;
  source_ids?: number[] | null;
  feed_item_insight_artifact_build_limit?: number | null;
}

export interface FeedItemInsightArtifactBatchOperationCreatePayloadDto {
  scope: FeedItemInsightArtifactBatchOperationCreateScope;
  source_id?: number | null;
  limit?: number | null;
  profile?: string | null;
  created_after?: string | null;
  reason_codes?: string[] | null;
}

export interface SourceSyncOperationState {
  id: number;
  kind: SourceSyncOperationKind;
  status: SourceSyncOperationStatus;
  sourceId: number | null;
  sourceName: string | null;
  stopRequested: boolean;
  message: string | null;
  error: string | null;
  result: BulkSourceSyncResponseDto | SingleSourceSyncResponseDto | null;
  startedAt: string | null;
  updatedAt: string;
  completedAt: string | null;
}

export interface FeedItemInsightArtifactBatchOperationState {
  id: number;
  scope: FeedItemInsightArtifactBatchOperationScope;
  status: FeedItemInsightArtifactBatchOperationStatus;
  sourceId: number | null;
  sourceName: string | null;
  label: string;
  stopRequested: boolean;
  message: string | null;
  error: string | null;
  result: Record<string, unknown> | null;
  startedAt: string | null;
  updatedAt: string;
  completedAt: string | null;
}

export interface SourceSyncFailedSource {
  source_id: number;
  source_name: string;
  status: SourceFetchStatus;
  http_status: number | null;
  error_message: string | null;
}

export interface SourceSyncProgressState {
  stage: SourceSyncStage;
  total: number;
  completed: number;
  successCount: number;
  errorCount: number;
  stopped: boolean;
  stopRequested: boolean;
  currentSourceName: string | null;
  currentSourceIndex: number | null;
  currentFetchStatus: SourceFetchStatus | null;
  currentItemsSeenCount: number;
  currentItemsCreatedCount: number;
  currentItemsMatchedCount: number;
  currentItemsInvalidCount: number;
  currentInsightTotal: number;
  currentInsightCompleted: number;
  currentInsightFailed: number;
  currentInsightArtifactRowsCreated: number;
  latestSourceName: string | null;
  latestStatus: SourceFetchStatus | null;
  itemsSeenCount: number;
  itemsCreatedCount: number;
  itemsMatchedCount: number;
  itemsInvalidCount: number;
  insightTotalCount: number;
  insightCompletedCount: number;
  insightFailedCount: number;
  insightArtifactRowsCreated: number;
  latestErrorMessage: string | null;
  failedSources: SourceSyncFailedSource[];
  jobTotalCount: number;
  jobPendingCount: number;
  jobRunningCount: number;
  jobSucceededCount: number;
  jobFailedCount: number;
  jobCancelledCount: number;
}

export interface SourceImportResultDto {
  status: ImportResultStatus;
  candidate_id: string | null;
  source_id: number | null;
  name: string;
  kind: SourceKind | null;
  input_url: string | null;
  url: string | null;
  folder_path: string | null;
  ai_route: SourceAIRoute | null;
  icon_url: string | null;
  icon_source: SourceIconSource | null;
  icon_updated_at: string | null;
  message: string;
}

export interface SourceImportResponseDto {
  import_run_id: number;
  filename: string | null;
  total_entries: number;
  selected_count: number | null;
  skipped_count: number | null;
  created_count: number;
  matched_existing_count: number;
  invalid_count: number;
  results: SourceImportResultDto[];
}

export interface OpmlImportCandidateDto {
  candidate_id: string;
  entry_index: number;
  status: OpmlImportCandidateStatus;
  source_id: number | null;
  name: string;
  kind: SourceKind | null;
  input_url: string | null;
  url: string | null;
  site_url: string | null;
  folder_path: string | null;
  duplicate_of_candidate_id: string | null;
  icon_url: string | null;
  icon_source: SourceIconSource | null;
  message: string;
  suggested_ai_route: SourceAIRoute;
  selected_by_default: boolean;
}

export interface OpmlImportPreviewResponseDto {
  import_run_id: number;
  filename: string | null;
  total_entries: number;
  ready_count: number;
  matched_existing_count: number;
  duplicate_in_file_count: number;
  invalid_count: number;
  candidates: OpmlImportCandidateDto[];
}

export interface OpmlImportCommitSelectionDto {
  candidate_id: string;
  ai_route?: SourceAIRoute | null;
  folder_path?: string | null;
}

export interface OpmlImportCommitPayloadDto {
  import_run_id: number;
  default_ai_route: SourceAIRoute;
  selected_candidates: OpmlImportCommitSelectionDto[];
}

export interface SourcePreviewRequestDto {
  kind?: SourceKind | null;
  url: string;
}

export interface SourcePreviewSummaryDto {
  detected_kind: SourceKind;
  canonical_url: string;
  resolved_url: string;
  title: string | null;
  site_url: string | null;
  description: string | null;
  language: string | null;
  icon_url: string | null;
  icon_source: SourceIconSource | null;
}

export interface SourcePreviewEntryDto {
  title: string;
  published_at: string | null;
  author_name: string | null;
  summary_text: string | null;
  original_url: string;
}

export interface SourcePreviewDiagnosticsDto {
  requested_url: string;
  final_url: string;
  http_status: number;
  content_type: string | null;
}

export interface SourcePreviewResponseDto {
  source: SourcePreviewSummaryDto;
  entries: SourcePreviewEntryDto[];
  diagnostics: SourcePreviewDiagnosticsDto;
}
