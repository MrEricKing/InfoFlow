import type {ReadingDepth} from '@/types/feed';

export type {ReadingDepth};

export interface StoragePathDto {
  key: string;
  value: string;
  resolved_path: string;
  source: string;
  exists: boolean;
  is_dir: boolean;
  is_writable: boolean;
  is_inside_project: boolean;
  error: string | null;
}

export interface StoragePathsDto {
  knowledge_base_root: StoragePathDto;
}

export interface StoragePath {
  key: string;
  value: string;
  resolvedPath: string;
  source: string;
  exists: boolean;
  isDir: boolean;
  isWritable: boolean;
  isInsideProject: boolean;
  error: string | null;
}

export interface StoragePaths {
  knowledgeBaseRoot: StoragePath;
}

export interface StoragePathsInput {
  knowledgeBaseRoot?: string | null;
}

export interface RsshubSettingsDto {
  key: string;
  value: string;
  source: string;
}

export interface RsshubSettings {
  key: string;
  value: string;
  source: string;
}

export interface RsshubSettingsInput {
  rsshubBaseUrl?: string | null;
}

export type AIProfile = 'general' | 'sensitive';
export type AIProvider = 'deepseek' | 'openai-compatible' | 'lm-studio';
export type DeepSeekThinking = 'disabled' | 'enabled';
export type InsightChannelDefaultFlowPolicy = 'normal' | 'mixed_high_frequency' | 'high_frequency_brief';

export interface InsightChannelDto {
  id: string;
  label: string;
  description: string;
  focus_prompt: string;
  boundary_prompt: string;
  reading_depth: ReadingDepth;
  default_flow_policy: InsightChannelDefaultFlowPolicy;
  sort_order: number;
  enabled: boolean;
  is_system: boolean;
  retired_at: string | null;
  channel_catalog_version: number;
}

export interface InsightChannel {
  id: string;
  label: string;
  description: string;
  focusPrompt: string;
  boundaryPrompt: string;
  readingDepth: ReadingDepth;
  defaultFlowPolicy: InsightChannelDefaultFlowPolicy;
  sortOrder: number;
  enabled: boolean;
  isSystem: boolean;
  retiredAt: string | null;
  channelCatalogVersion: number;
}

export interface InsightChannelCreateInput {
  id: string;
  label: string;
  description: string;
  focusPrompt: string;
  boundaryPrompt: string;
  readingDepth: ReadingDepth;
  defaultFlowPolicy: InsightChannelDefaultFlowPolicy;
  sortOrder: number;
  enabled: boolean;
  expectedChannelCatalogVersion?: number | null;
}

export interface InsightChannelUpdateInput {
  label?: string;
  description?: string;
  focusPrompt?: string;
  boundaryPrompt?: string;
  readingDepth?: ReadingDepth;
  defaultFlowPolicy?: InsightChannelDefaultFlowPolicy;
  sortOrder?: number;
  enabled?: boolean;
  expectedChannelCatalogVersion?: number | null;
}

export interface InsightChannelImpactDto {
  channel_id: string;
  channel_label: string;
  affected_feed_item_count: number;
  source_tendency_count: number;
  human_correction_count: number;
  channel_catalog_version: number;
}

export interface InsightChannelImpact {
  channelId: string;
  channelLabel: string;
  affectedFeedItemCount: number;
  sourceTendencyCount: number;
  humanCorrectionCount: number;
  channelCatalogVersion: number;
}

export interface AIProfileSettingsDto {
  profile: AIProfile;
  provider: AIProvider;
  enabled: boolean;
  base_url: string;
  model: string;
  deepseek_thinking: DeepSeekThinking;
  artifact_concurrency: number;
  api_key_configured: boolean;
  api_key_masked: string | null;
}

export interface AIProfileSettings {
  profile: AIProfile;
  provider: AIProvider;
  enabled: boolean;
  baseUrl: string;
  model: string;
  deepSeekThinking: DeepSeekThinking;
  artifactConcurrency: number;
  apiKeyConfigured: boolean;
  apiKeyMasked: string | null;
}

export interface AISettingsDto {
  profiles: Record<AIProfile, AIProfileSettingsDto>;
}

export interface AISettings {
  profiles: Record<AIProfile, AIProfileSettings>;
}

export interface AIProfileSettingsInput {
  provider?: AIProvider;
  enabled?: boolean | null;
  baseUrl?: string | null;
  model?: string | null;
  deepSeekThinking?: DeepSeekThinking | null;
  artifactConcurrency?: number | null;
  apiKey?: string | null;
}

export interface AISettingsInput {
  profiles?: Partial<Record<AIProfile, AIProfileSettingsInput>>;
}

export interface InsightEngineSettingsDto {
  auto_low_priority_archiving_enabled: boolean;
  low_priority_score_threshold: number;
}

export interface InsightEngineSettings {
  autoLowPriorityArchivingEnabled: boolean;
  lowPriorityScoreThreshold: number;
}

export interface InsightEngineSettingsInput {
  autoLowPriorityArchivingEnabled?: boolean | null;
  lowPriorityScoreThreshold?: number | null;
}

export type InsightEngineAttemptStatus = 'failed' | 'skipped';

export interface InsightEngineAttemptDto {
  id: number;
  feed_item_id: number | null;
  feed_item_title: string | null;
  source_id: number | null;
  source_name: string | null;
  ai_route: string | null;
  profile: string;
  provider: string;
  model: string | null;
  status: InsightEngineAttemptStatus;
  stage: string;
  error_type: string | null;
  error_message: string | null;
  duration_ms: number | null;
  artifact_count: number;
  created_at: string;
  completed_at: string | null;
  request_snapshot: Record<string, unknown> | null;
}

export interface InsightEngineAttempt {
  id: number;
  feedItemId: number | null;
  feedItemTitle: string | null;
  sourceId: number | null;
  sourceName: string | null;
  aiRoute: string | null;
  profile: string;
  provider: string;
  model: string | null;
  status: InsightEngineAttemptStatus;
  stage: string;
  errorType: string | null;
  errorMessage: string | null;
  durationMs: number | null;
  artifactCount: number;
  createdAt: string;
  completedAt: string | null;
  requestSnapshot: Record<string, unknown> | null;
}

export interface InsightEngineAttemptListDto {
  items: InsightEngineAttemptDto[];
  total: number;
  limit: number;
  offset: number;
}

export interface InsightEngineAttemptList {
  items: InsightEngineAttempt[];
  total: number;
  limit: number;
  offset: number;
}

export interface InsightEngineCoverageReasonDto {
  code: string;
  label: string;
  count: number;
}

export interface InsightEngineCoverageLatestAttemptDto {
  id: number;
  status: InsightEngineAttemptStatus;
  stage: string;
  error_type: string | null;
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface InsightEngineCoverageItemDto {
  feed_item_id: number;
  title: string;
  source_id: number;
  source_name: string;
  ai_route: string;
  profile: string;
  provider: string;
  model: string | null;
  existing_artifact_kinds: string[];
  missing_artifact_kinds: string[];
  content_text_source: string;
  content_extraction_status: string;
  content_extraction_error: string | null;
  possible_reason_code: string;
  possible_reason_label: string;
  coverage_category: string;
  retryable: boolean;
  latest_attempt: InsightEngineCoverageLatestAttemptDto | null;
}

export interface InsightEngineCoverageDto {
  total_item_count: number;
  complete_item_count: number;
  incomplete_item_count: number;
  no_artifact_item_count: number;
  partial_artifact_item_count: number;
  retryable_item_count: number;
  problem_item_count: number;
  problem_retryable_item_count: number;
  initial_generation_item_count: number;
  initial_generation_retryable_item_count: number;
  blocked_item_count: number;
  reason_counts: InsightEngineCoverageReasonDto[];
  items: InsightEngineCoverageItemDto[];
  total: number;
  limit: number;
  offset: number;
}

export interface InsightEngineCoverageReason {
  code: string;
  label: string;
  count: number;
}

export interface InsightEngineCoverageLatestAttempt {
  id: number;
  status: InsightEngineAttemptStatus;
  stage: string;
  errorType: string | null;
  errorMessage: string | null;
  createdAt: string;
  completedAt: string | null;
}

export interface InsightEngineCoverageItem {
  feedItemId: number;
  title: string;
  sourceId: number;
  sourceName: string;
  aiRoute: string;
  profile: string;
  provider: string;
  model: string | null;
  existingArtifactKinds: string[];
  missingArtifactKinds: string[];
  contentTextSource: string;
  contentExtractionStatus: string;
  contentExtractionError: string | null;
  possibleReasonCode: string;
  possibleReasonLabel: string;
  coverageCategory: string;
  retryable: boolean;
  latestAttempt: InsightEngineCoverageLatestAttempt | null;
}

export interface InsightEngineCoverage {
  totalItemCount: number;
  completeItemCount: number;
  incompleteItemCount: number;
  noArtifactItemCount: number;
  partialArtifactItemCount: number;
  retryableItemCount: number;
  problemItemCount: number;
  problemRetryableItemCount: number;
  initialGenerationItemCount: number;
  initialGenerationRetryableItemCount: number;
  blockedItemCount: number;
  reasonCounts: InsightEngineCoverageReason[];
  items: InsightEngineCoverageItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface InsightEngineCoverageFilters {
  sourceId?: number | null;
  limit?: number;
  offset?: number;
}

export interface InsightEngineAttemptFilters {
  status?: InsightEngineAttemptStatus | 'all';
  sourceId?: number | null;
  profile?: string | null;
  createdAfter?: string | null;
  createdBefore?: string | null;
  limit?: number;
  offset?: number;
}

export interface AIConnectionTestResponseDto {
  ok: boolean;
  message: string;
  profile: AIProfile;
  provider: AIProvider;
  model: string;
  latency_ms: number | null;
}

export interface AIConnectionTestResponse {
  ok: boolean;
  message: string;
  profile: AIProfile;
  provider: AIProvider;
  model: string;
  latencyMs: number | null;
}

export interface AIConnectionTestInput {
  profile: AIProfile;
  settings?: AIProfileSettingsInput;
}

export type InsightContentTextPayloadSource = 'content_text' | 'summary_text_fallback' | 'empty';

export interface InsightMessageDto {
  role: string;
  content: string;
}

export interface InsightRequestStatsDto {
  source_content_char_count: number;
  content_text_char_count: number;
  content_text_was_truncated: boolean;
  content_text_estimated_token_count: number;
  system_message_char_count: number;
  system_message_estimated_token_count: number;
  user_message_char_count: number;
  user_message_estimated_token_count: number;
}

export interface InsightExcludedFieldDto {
  field: string;
  reason: string;
}

export interface InsightRequestPayloadDto {
  prompt_version: string;
  profile: AIProfile;
  provider: AIProvider;
  model: string;
  endpoint: string;
  temperature: number;
  max_tokens: number;
  messages: InsightMessageDto[];
  system_message: string;
  user_message: string;
  request_body: Record<string, unknown>;
  fallback_request_body: Record<string, unknown>;
  taxonomy: Record<string, unknown>;
  content_type_options: Record<string, unknown>;
  reason_codes: Record<string, unknown>;
  active_insight_profile_context: Record<string, unknown>;
  source_preference_profile: Record<string, unknown>;
  source_profile_version: string | null;
  source_profile_is_empty: boolean;
  content_text: string;
  content_text_source: InsightContentTextPayloadSource;
  stats: InsightRequestStatsDto;
  excluded_fields: InsightExcludedFieldDto[];
}

export interface InsightRequestPreviewSampleDto {
  item_id: number;
  title: string;
  source_id: number;
  source_name: string;
  source_folder: string | null;
  ai_route: string;
  profile: AIProfile;
  content_text_source: string;
  content_extraction_status: string;
  missing_artifact_kinds: string[];
}

export interface InsightRequestPreviewDto {
  available: boolean;
  can_send: boolean;
  message: string;
  candidates: InsightRequestPreviewSampleDto[];
  sample: InsightRequestPreviewSampleDto | null;
  payload: InsightRequestPayloadDto | null;
}

export interface InsightRequestStats {
  sourceContentCharCount: number;
  contentTextCharCount: number;
  contentTextWasTruncated: boolean;
  contentTextEstimatedTokenCount: number;
  systemMessageCharCount: number;
  systemMessageEstimatedTokenCount: number;
  userMessageCharCount: number;
  userMessageEstimatedTokenCount: number;
}

export interface InsightExcludedField {
  field: string;
  reason: string;
}

export interface InsightRequestPayload {
  promptVersion: string;
  profile: AIProfile;
  provider: AIProvider;
  model: string;
  endpoint: string;
  temperature: number;
  maxTokens: number;
  messages: InsightMessageDto[];
  systemMessage: string;
  userMessage: string;
  requestBody: Record<string, unknown>;
  fallbackRequestBody: Record<string, unknown>;
  taxonomy: Record<string, unknown>;
  contentTypeOptions: Record<string, unknown>;
  reasonCodes: Record<string, unknown>;
  activeInsightProfileContext: Record<string, unknown>;
  sourcePreferenceProfile: Record<string, unknown>;
  sourceProfileVersion: string | null;
  sourceProfileIsEmpty: boolean;
  contentText: string;
  contentTextSource: InsightContentTextPayloadSource;
  stats: InsightRequestStats;
  excludedFields: InsightExcludedField[];
}

export interface InsightRequestPreviewSample {
  itemId: number;
  title: string;
  sourceId: number;
  sourceName: string;
  sourceFolder: string | null;
  aiRoute: string;
  profile: AIProfile;
  contentTextSource: string;
  contentExtractionStatus: string;
  missingArtifactKinds: string[];
}

export interface InsightRequestPreview {
  available: boolean;
  canSend: boolean;
  message: string;
  candidates: InsightRequestPreviewSample[];
  sample: InsightRequestPreviewSample | null;
  payload: InsightRequestPayload | null;
}

export type InsightProfileSourceType = 'local_user' | 'codex_import' | 'manual_edit';

export interface InsightProfileVersionDto {
  id: number | null;
  schema_version: string;
  profile_name: string;
  profile_version: string;
  profile_json: Record<string, unknown>;
  source_type: string;
  edit_note: string | null;
  diff_summary: string | null;
  previous_profile_id: number | null;
  created_at: string | null;
}

export interface InsightProfileEventDto {
  id: number | null;
  profile_id: number | null;
  event_type: string;
  note: string | null;
  event_json: Record<string, unknown> | null;
  created_at: string | null;
}

export interface InsightProfileSettingsDto {
  active_profile: InsightProfileVersionDto;
  versions: InsightProfileVersionDto[];
  events: InsightProfileEventDto[];
  template: Record<string, unknown>;
  active_prompt_context: Record<string, unknown>;
}

export interface InsightProfileValidateResponseDto {
  normalized_profile: Record<string, unknown>;
  prompt_context: Record<string, unknown>;
}

export interface InsightProfileVersion {
  id: number | null;
  schemaVersion: string;
  profileName: string;
  profileVersion: string;
  profileJson: Record<string, unknown>;
  sourceType: string;
  editNote: string | null;
  diffSummary: string | null;
  previousProfileId: number | null;
  createdAt: string | null;
}

export interface InsightProfileEvent {
  id: number | null;
  profileId: number | null;
  eventType: string;
  note: string | null;
  eventJson: Record<string, unknown> | null;
  createdAt: string | null;
}

export interface InsightProfileSettings {
  activeProfile: InsightProfileVersion;
  versions: InsightProfileVersion[];
  events: InsightProfileEvent[];
  template: Record<string, unknown>;
  activePromptContext: Record<string, unknown>;
}

export interface InsightProfileValidateResponse {
  normalizedProfile: Record<string, unknown>;
  promptContext: Record<string, unknown>;
}

export type InsightProfileDraftChangeType = 'added' | 'removed' | 'modified' | 'unchanged';

export interface InsightProfileFieldDiffDto {
  field: string;
  change_type: InsightProfileDraftChangeType;
  before: unknown;
  after: unknown;
}

export interface InsightProfileDraftInspectResponseDto {
  normalized_profile: Record<string, unknown>;
  prompt_context: Record<string, unknown>;
  base_profile: InsightProfileVersionDto;
  diff_summary: string;
  field_diffs: InsightProfileFieldDiffDto[];
  evidence_references: Array<Record<string, unknown>>;
}

export interface InsightProfileFieldDiff {
  field: string;
  changeType: InsightProfileDraftChangeType;
  before: unknown;
  after: unknown;
}

export interface InsightProfileDraftInspectResponse {
  normalizedProfile: Record<string, unknown>;
  promptContext: Record<string, unknown>;
  baseProfile: InsightProfileVersion;
  diffSummary: string;
  fieldDiffs: InsightProfileFieldDiff[];
  evidenceReferences: Array<Record<string, unknown>>;
}

export interface InsightProfileSaveInput {
  profileJson: Record<string, unknown>;
  sourceType: InsightProfileSourceType;
  editNote?: string | null;
  activate?: boolean;
}

export type InsightEvidencePackageRange = '7d' | '30d' | '90d' | '180d' | '365d' | 'all' | 'custom';
export type InsightEvidencePackageArtifactMode = 'latest' | 'all_history';

export interface InsightEvidencePackagePreviewDto {
  package_version: string;
  range: InsightEvidencePackageRange;
  artifact_mode: InsightEvidencePackageArtifactMode;
  start_at: string | null;
  end_at: string | null;
  generated_at: string;
  item_count: number;
  excluded_unread_count: number;
  content_text_file_count: number;
  feedback_item_count: number;
  structured_feedback_item_count: number;
  channel_correction_item_count: number;
  channel_corrected_item_count: number;
  strong_signal_count: number;
  medium_signal_count: number;
  weak_cleanup_signal_count: number;
  negative_signal_count: number;
  behavior_event_count: number;
  feedback_clear_event_count: number;
  not_interested_clear_event_count: number;
  auto_low_priority_clear_event_count: number;
  artifact_coverage: Record<string, number>;
  saved_item_count: number;
  saved_item_content_text_file_count: number;
  saved_item_display_markdown_file_count: number;
  saved_item_display_asset_count: number;
  saved_item_ai_insight_count: number;
  saved_item_curated_insight_count: number;
  saved_item_curated_event_count: number;
  external_saved_item_count: number;
  failed_attempt_count: number;
  skipped_attempt_count: number;
}

export interface InsightEvidencePackagePreview {
  packageVersion: string;
  range: InsightEvidencePackageRange;
  artifactMode: InsightEvidencePackageArtifactMode;
  startAt: string | null;
  endAt: string | null;
  generatedAt: string;
  itemCount: number;
  excludedUnreadCount: number;
  contentTextFileCount: number;
  feedbackItemCount: number;
  structuredFeedbackItemCount: number;
  channelCorrectionItemCount: number;
  channelCorrectedItemCount: number;
  strongSignalCount: number;
  mediumSignalCount: number;
  weakCleanupSignalCount: number;
  negativeSignalCount: number;
  behaviorEventCount: number;
  feedbackClearEventCount: number;
  notInterestedClearEventCount: number;
  autoLowPriorityClearEventCount: number;
  artifactCoverage: Record<string, number>;
  savedItemCount: number;
  savedItemContentTextFileCount: number;
  savedItemDisplayMarkdownFileCount: number;
  savedItemDisplayAssetCount: number;
  savedItemAiInsightCount: number;
  savedItemCuratedInsightCount: number;
  savedItemCuratedEventCount: number;
  externalSavedItemCount: number;
  failedAttemptCount: number;
  skippedAttemptCount: number;
}

export interface InsightEvidencePackageInput {
  range: InsightEvidencePackageRange;
  artifactMode?: InsightEvidencePackageArtifactMode;
  startDate?: string | null;
  endDate?: string | null;
}

export interface InsightEvidencePackageExport {
  blob: Blob;
  filename: string;
}

export interface InsightProfileCandidateSourceChangeDto {
  source_id: number;
  source_name: string;
  normalized_profile: Record<string, unknown>;
  prompt_context: Record<string, unknown>;
  diff_summary: string;
  field_diffs: InsightProfileFieldDiffDto[];
  evidence_references: Array<Record<string, unknown>>;
}

export interface InsightProfileCandidateChannelSuggestionDto {
  channel_id: string;
  channel_label: string;
  current_focus_prompt: string;
  suggested_focus_prompt: string | null;
  current_boundary_prompt: string;
  suggested_boundary_prompt: string | null;
  rationale: string | null;
  evidence_references: Array<Record<string, unknown>>;
}

export interface InsightProfileCandidateInspectResponseDto {
  candidate_version: string;
  based_on_package: Record<string, unknown>;
  summary: string | null;
  normalized_candidate: Record<string, unknown>;
  global_profile_inspection: InsightProfileDraftInspectResponseDto | null;
  source_profile_changes: InsightProfileCandidateSourceChangeDto[];
  channel_prompt_boundary_suggestions: InsightProfileCandidateChannelSuggestionDto[];
  scoring_policy_suggestions: Array<Record<string, unknown>>;
  evidence_references: Array<Record<string, unknown>>;
  warnings: string[];
  dry_run: Record<string, unknown>;
}

export interface InsightProfileCandidateSourceChange {
  sourceId: number;
  sourceName: string;
  normalizedProfile: Record<string, unknown>;
  promptContext: Record<string, unknown>;
  diffSummary: string;
  fieldDiffs: InsightProfileFieldDiff[];
  evidenceReferences: Array<Record<string, unknown>>;
}

export interface InsightProfileCandidateChannelSuggestion {
  channelId: string;
  channelLabel: string;
  currentFocusPrompt: string;
  suggestedFocusPrompt: string | null;
  currentBoundaryPrompt: string;
  suggestedBoundaryPrompt: string | null;
  rationale: string | null;
  evidenceReferences: Array<Record<string, unknown>>;
}

export interface InsightProfileCandidateInspectResponse {
  candidateVersion: string;
  basedOnPackage: Record<string, unknown>;
  summary: string | null;
  normalizedCandidate: Record<string, unknown>;
  globalProfileInspection: InsightProfileDraftInspectResponse | null;
  sourceProfileChanges: InsightProfileCandidateSourceChange[];
  channelPromptBoundarySuggestions: InsightProfileCandidateChannelSuggestion[];
  scoringPolicySuggestions: Array<Record<string, unknown>>;
  evidenceReferences: Array<Record<string, unknown>>;
  warnings: string[];
  dryRun: Record<string, unknown>;
}

export interface InsightProfileCandidateSourceApplyResponseDto {
  applied_source_ids: number[];
  applied_count: number;
  skipped_source_ids: number[];
  message: string;
}

export interface InsightProfileCandidateSourceApplyResponse {
  appliedSourceIds: number[];
  appliedCount: number;
  skippedSourceIds: number[];
  message: string;
}
