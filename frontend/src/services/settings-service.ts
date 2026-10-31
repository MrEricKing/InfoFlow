import type {
  AIConnectionTestInput,
  AIConnectionTestResponse,
  AIConnectionTestResponseDto,
  AIProfile,
  AIProfileSettings,
  AIProfileSettingsDto,
  AIProfileSettingsInput,
  AISettings,
  AISettingsDto,
  AISettingsInput,
  InsightChannel,
  InsightChannelCreateInput,
  InsightChannelDto,
  InsightChannelImpact,
  InsightChannelImpactDto,
  InsightChannelUpdateInput,
  InsightEngineSettings,
  InsightEngineSettingsDto,
  InsightEngineSettingsInput,
  InsightEngineAttempt,
  InsightEngineAttemptDto,
  InsightEngineAttemptFilters,
  InsightEngineAttemptList,
  InsightEngineAttemptListDto,
  InsightEngineCoverage,
  InsightEngineCoverageDto,
  InsightEngineCoverageFilters,
  InsightEngineCoverageItem,
  InsightEngineCoverageItemDto,
  InsightEngineCoverageLatestAttempt,
  InsightEngineCoverageLatestAttemptDto,
  InsightRequestPayload,
  InsightRequestPayloadDto,
  InsightRequestPreview,
  InsightRequestPreviewDto,
  InsightRequestPreviewSample,
  InsightRequestPreviewSampleDto,
  InsightProfileDraftInspectResponse,
  InsightProfileDraftInspectResponseDto,
  InsightProfileFieldDiff,
  InsightProfileFieldDiffDto,
  InsightProfileEvent,
  InsightProfileEventDto,
  InsightProfileSaveInput,
  InsightProfileSettings,
  InsightProfileSettingsDto,
  InsightProfileVersion,
  InsightProfileVersionDto,
  InsightEvidencePackageExport,
  InsightEvidencePackageInput,
  InsightEvidencePackagePreview,
  InsightEvidencePackagePreviewDto,
  InsightProfileCandidateInspectResponse,
  InsightProfileCandidateInspectResponseDto,
  InsightProfileCandidateSourceApplyResponse,
  InsightProfileCandidateSourceApplyResponseDto,
  InsightProfileCandidateSourceChange,
  InsightProfileCandidateSourceChangeDto,
  InsightProfileCandidateChannelSuggestion,
  InsightProfileCandidateChannelSuggestionDto,
  RsshubSettings,
  RsshubSettingsDto,
  RsshubSettingsInput,
  StoragePath,
  StoragePathDto,
  StoragePaths,
  StoragePathsDto,
  StoragePathsInput,
} from '@/types/settings';
import type {AppOperationDto} from '@/types/source';
import {requestApi, requestJson} from '@/api/client';

export async function getStoragePaths() {
  const payload = await requestJson<StoragePathsDto>('/settings/storage-paths');
  return adaptStoragePaths(payload);
}

export async function updateStoragePaths(input: StoragePathsInput) {
  const payload = await requestJson<StoragePathsDto>('/settings/storage-paths', {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(buildStoragePathsPayload(input)),
  });
  return adaptStoragePaths(payload);
}

export async function validateStoragePaths(input: StoragePathsInput) {
  const payload = await requestJson<StoragePathsDto>('/settings/storage-paths/validate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(buildStoragePathsPayload(input)),
  });
  return adaptStoragePaths(payload);
}

export async function getRsshubSettings() {
  const payload = await requestJson<RsshubSettingsDto>('/settings/rsshub');
  return adaptRsshubSettings(payload);
}

export async function updateRsshubSettings(input: RsshubSettingsInput) {
  const payload = await requestJson<RsshubSettingsDto>('/settings/rsshub', {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({rsshub_base_url: input.rsshubBaseUrl}),
  });
  return adaptRsshubSettings(payload);
}

export async function getAISettings() {
  const payload = await requestJson<AISettingsDto>('/settings/ai');
  return adaptAISettings(payload);
}

export async function updateAISettings(input: AISettingsInput) {
  const payload = await requestJson<AISettingsDto>('/settings/ai', {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(buildAISettingsPayload(input)),
  });
  return adaptAISettings(payload);
}

export async function getInsightEngineSettings() {
  const payload = await requestJson<InsightEngineSettingsDto>('/settings/insight-engine');
  return adaptInsightEngineSettings(payload);
}

export async function listInsightChannels() {
  const payload = await requestJson<InsightChannelDto[]>('/settings/insight-channels');
  return payload.map(adaptInsightChannel);
}

export async function createInsightChannel(input: InsightChannelCreateInput) {
  const payload = await requestJson<InsightChannelDto>('/settings/insight-channels', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(buildInsightChannelCreatePayload(input)),
  });
  return adaptInsightChannel(payload);
}

export async function updateInsightChannel(channelId: string, input: InsightChannelUpdateInput) {
  const payload = await requestJson<InsightChannelDto>(`/settings/insight-channels/${channelId}`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(buildInsightChannelPayload(input)),
  });
  return adaptInsightChannel(payload);
}

export async function getInsightChannelImpact(channelId: string) {
  const payload = await requestJson<InsightChannelImpactDto>(`/settings/insight-channels/${channelId}/impact`);
  return adaptInsightChannelImpact(payload);
}

export async function retireInsightChannel(channelId: string, expectedChannelCatalogVersion: number) {
  return requestJson<AppOperationDto>(`/settings/insight-channels/${channelId}/retire`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      confirm_retire: true,
      expected_channel_catalog_version: expectedChannelCatalogVersion,
    }),
  });
}

export async function updateInsightEngineSettings(input: InsightEngineSettingsInput) {
  const payload = await requestJson<InsightEngineSettingsDto>('/settings/insight-engine', {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(buildInsightEngineSettingsPayload(input)),
  });
  return adaptInsightEngineSettings(payload);
}

export async function listInsightEngineAttempts(filters: InsightEngineAttemptFilters = {}) {
  const payload = await requestJson<InsightEngineAttemptListDto>(
    `/settings/insight-engine/attempts${buildInsightEngineAttemptQuery(filters)}`,
  );
  return adaptInsightEngineAttemptList(payload);
}

export async function getInsightEngineCoverage(filters: InsightEngineCoverageFilters = {}) {
  const payload = await requestJson<InsightEngineCoverageDto>(
    `/settings/insight-engine/coverage${buildInsightEngineCoverageQuery(filters)}`,
  );
  return adaptInsightEngineCoverage(payload);
}

export async function getInsightEngineAttempt(attemptId: number) {
  const payload = await requestJson<InsightEngineAttemptDto>(`/settings/insight-engine/attempts/${attemptId}`);
  return adaptInsightEngineAttempt(payload);
}

export async function testAIConnectionForProfile(profile: AIProfile, settings?: AIProfileSettingsInput) {
  const body = buildAIConnectionTestPayload({profile, settings});
  const payload = await requestJson<AIConnectionTestResponseDto>('/settings/ai/test', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  return adaptAIConnectionTestResponse(payload);
}

export async function getAIInsightRequestPreview(itemId?: number | null) {
  const searchParams = new URLSearchParams();
  if (itemId !== undefined && itemId !== null) {
    searchParams.set('item_id', String(itemId));
  }
  const query = searchParams.toString();
  const payload = await requestJson<InsightRequestPreviewDto>(
    `/settings/ai/insight-request-preview${query ? `?${query}` : ''}`,
  );
  return adaptInsightRequestPreview(payload);
}

export async function getInsightProfileSettings() {
  const payload = await requestJson<InsightProfileSettingsDto>('/settings/insight-profile');
  return adaptInsightProfileSettings(payload);
}

export async function getInsightEvidencePackagePreview(input: InsightEvidencePackageInput) {
  const payload = await requestJson<InsightEvidencePackagePreviewDto>(
    `/settings/insight-evidence-package/preview${buildEvidencePackageQuery(input)}`,
  );
  return adaptInsightEvidencePackagePreview(payload);
}

export async function exportInsightEvidencePackage(input: InsightEvidencePackageInput): Promise<InsightEvidencePackageExport> {
  const response = await requestApi(`/settings/insight-evidence-package/export${buildEvidencePackageQuery(input)}`, {
    headers: {
      Accept: 'application/zip',
    },
  });
  const blob = await response.blob();
  return {
    blob,
    filename: parseDownloadFilename(response.headers.get('Content-Disposition')) ?? 'insight-evidence.zip',
  };
}

export async function inspectInsightProfileDraft(profileJson: Record<string, unknown>) {
  const payload = await requestJson<InsightProfileDraftInspectResponseDto>('/settings/insight-profile/draft/inspect', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({profile_json: profileJson}),
  });
  return adaptInsightProfileDraftInspectResponse(payload);
}

export async function inspectInsightProfileCandidate(candidateJson: Record<string, unknown>) {
  const payload = await requestJson<InsightProfileCandidateInspectResponseDto>('/settings/insight-profile/candidate/inspect', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({candidate_json: candidateJson}),
  });
  return adaptInsightProfileCandidateInspectResponse(payload);
}

export async function applyInsightProfileCandidateSourceDrafts(
  candidateJson: Record<string, unknown>,
  sourceIds: number[],
) {
  const payload = await requestJson<InsightProfileCandidateSourceApplyResponseDto>(
    '/settings/insight-profile/candidate/source-drafts',
    {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({candidate_json: candidateJson, source_ids: sourceIds}),
    },
  );
  return adaptInsightProfileCandidateSourceApplyResponse(payload);
}

export async function saveInsightProfileVersion(input: InsightProfileSaveInput) {
  const payload = await requestJson<InsightProfileSettingsDto>('/settings/insight-profile/versions', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      profile_json: input.profileJson,
      source_type: input.sourceType,
      edit_note: input.editNote,
      activate: input.activate ?? false,
    }),
  });
  return adaptInsightProfileSettings(payload);
}

export async function activateInsightProfileVersion(profileId: number, note?: string | null) {
  const payload = await requestJson<InsightProfileSettingsDto>(`/settings/insight-profile/versions/${profileId}/activate`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({note}),
  });
  return adaptInsightProfileSettings(payload);
}

export async function rollbackInsightProfileVersion(profileId: number, note?: string | null) {
  const payload = await requestJson<InsightProfileSettingsDto>(`/settings/insight-profile/versions/${profileId}/rollback`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({note}),
  });
  return adaptInsightProfileSettings(payload);
}

function buildStoragePathsPayload(input: StoragePathsInput) {
  return {
    knowledge_base_root: input.knowledgeBaseRoot,
  };
}

function adaptStoragePaths(payload: StoragePathsDto): StoragePaths {
  return {
    knowledgeBaseRoot: adaptStoragePath(payload.knowledge_base_root),
  };
}

function adaptStoragePath(payload: StoragePathDto): StoragePath {
  return {
    key: payload.key,
    value: payload.value,
    resolvedPath: payload.resolved_path,
    source: payload.source,
    exists: payload.exists,
    isDir: payload.is_dir,
    isWritable: payload.is_writable,
    isInsideProject: payload.is_inside_project,
    error: payload.error,
  };
}

function adaptRsshubSettings(payload: RsshubSettingsDto): RsshubSettings {
  return {
    key: payload.key,
    value: payload.value,
    source: payload.source,
  };
}

function buildAISettingsPayload(input: AISettingsInput) {
  const payload: Record<string, unknown> = {};
  if (input.profiles !== undefined) {
    const profilesPayload: Record<string, unknown> = {};
    for (const [profile, profileInput] of Object.entries(input.profiles)) {
      if (profileInput !== undefined) {
        profilesPayload[profile] = buildAIProfileSettingsPayload(profileInput);
      }
    }
    payload.profiles = profilesPayload;
  }
  return payload;
}

function buildAIConnectionTestPayload(input: AIConnectionTestInput) {
  const payload: Record<string, unknown> = {profile: input.profile};
  if (input.settings !== undefined) {
    payload.settings = buildAIProfileSettingsPayload(input.settings);
  }
  return payload;
}

function adaptAISettings(payload: AISettingsDto): AISettings {
  return {
    profiles: {
      general: adaptAIProfileSettings(payload.profiles.general),
      sensitive: adaptAIProfileSettings(payload.profiles.sensitive),
    },
  };
}

function buildAIProfileSettingsPayload(input: AIProfileSettingsInput) {
  const payload: Record<string, unknown> = {};
  if (input.provider !== undefined) {
    payload.provider = input.provider;
  }
  if (input.enabled !== undefined) {
    payload.enabled = input.enabled;
  }
  if (input.baseUrl !== undefined) {
    payload.base_url = input.baseUrl;
  }
  if (input.model !== undefined) {
    payload.model = input.model;
  }
  if (input.deepSeekThinking !== undefined) {
    payload.deepseek_thinking = input.deepSeekThinking;
  }
  if (input.artifactConcurrency !== undefined) {
    payload.artifact_concurrency = input.artifactConcurrency;
  }
  if (input.apiKey !== undefined) {
    payload.api_key = input.apiKey;
  }
  return payload;
}

function adaptAIProfileSettings(payload: AIProfileSettingsDto): AIProfileSettings {
  return {
    profile: payload.profile,
    provider: payload.provider,
    enabled: payload.enabled,
    baseUrl: payload.base_url,
    model: payload.model,
    deepSeekThinking: payload.deepseek_thinking,
    artifactConcurrency: payload.artifact_concurrency,
    apiKeyConfigured: payload.api_key_configured,
    apiKeyMasked: payload.api_key_masked,
  };
}

function adaptAIConnectionTestResponse(payload: AIConnectionTestResponseDto): AIConnectionTestResponse {
  return {
    ok: payload.ok,
    message: payload.message,
    profile: payload.profile,
    provider: payload.provider,
    model: payload.model,
    latencyMs: payload.latency_ms,
  };
}

function buildInsightEngineSettingsPayload(input: InsightEngineSettingsInput) {
  const payload: Record<string, unknown> = {};
  if (input.autoLowPriorityArchivingEnabled !== undefined) {
    payload.auto_low_priority_archiving_enabled = input.autoLowPriorityArchivingEnabled;
  }
  if (input.lowPriorityScoreThreshold !== undefined) {
    payload.low_priority_score_threshold = input.lowPriorityScoreThreshold;
  }
  return payload;
}

function adaptInsightEngineSettings(payload: InsightEngineSettingsDto): InsightEngineSettings {
  return {
    autoLowPriorityArchivingEnabled: payload.auto_low_priority_archiving_enabled,
    lowPriorityScoreThreshold: payload.low_priority_score_threshold,
  };
}

export function buildInsightChannelPayload(input: InsightChannelUpdateInput) {
  const payload: Record<string, unknown> = {};
  if (input.label !== undefined) {
    payload.label = input.label;
  }
  if (input.description !== undefined) {
    payload.description = input.description;
  }
  if (input.focusPrompt !== undefined) {
    payload.focus_prompt = input.focusPrompt;
  }
  if (input.boundaryPrompt !== undefined) {
    payload.boundary_prompt = input.boundaryPrompt;
  }
  if (input.readingDepth !== undefined) {
    payload.reading_depth = input.readingDepth;
  }
  if (input.defaultFlowPolicy !== undefined) {
    payload.default_flow_policy = input.defaultFlowPolicy;
  }
  if (input.sortOrder !== undefined) {
    payload.sort_order = input.sortOrder;
  }
  if (input.enabled !== undefined) {
    payload.enabled = input.enabled;
  }
  if (input.expectedChannelCatalogVersion !== undefined) {
    payload.expected_channel_catalog_version = input.expectedChannelCatalogVersion;
  }
  return payload;
}

function buildInsightChannelCreatePayload(input: InsightChannelCreateInput) {
  return {
    id: input.id,
    label: input.label,
    description: input.description,
    focus_prompt: input.focusPrompt,
    boundary_prompt: input.boundaryPrompt,
    reading_depth: input.readingDepth,
    default_flow_policy: input.defaultFlowPolicy,
    sort_order: input.sortOrder,
    enabled: input.enabled,
    expected_channel_catalog_version: input.expectedChannelCatalogVersion,
  };
}

export function adaptInsightChannel(payload: InsightChannelDto): InsightChannel {
  return {
    id: payload.id,
    label: payload.label,
    description: payload.description,
    focusPrompt: payload.focus_prompt,
    boundaryPrompt: payload.boundary_prompt,
    readingDepth: payload.reading_depth,
    defaultFlowPolicy: payload.default_flow_policy,
    sortOrder: payload.sort_order,
    enabled: payload.enabled,
    isSystem: payload.is_system,
    retiredAt: payload.retired_at,
    channelCatalogVersion: payload.channel_catalog_version,
  };
}

function adaptInsightChannelImpact(payload: InsightChannelImpactDto): InsightChannelImpact {
  return {
    channelId: payload.channel_id,
    channelLabel: payload.channel_label,
    affectedFeedItemCount: payload.affected_feed_item_count,
    sourceTendencyCount: payload.source_tendency_count,
    humanCorrectionCount: payload.human_correction_count,
    channelCatalogVersion: payload.channel_catalog_version,
  };
}

function buildInsightEngineAttemptQuery(filters: InsightEngineAttemptFilters) {
  const searchParams = new URLSearchParams();
  if (filters.status && filters.status !== 'all') {
    searchParams.set('status', filters.status);
  }
  if (filters.sourceId !== undefined && filters.sourceId !== null) {
    searchParams.set('source_id', String(filters.sourceId));
  }
  if (filters.profile) {
    searchParams.set('profile', filters.profile);
  }
  if (filters.createdAfter) {
    searchParams.set('created_after', filters.createdAfter);
  }
  if (filters.createdBefore) {
    searchParams.set('created_before', filters.createdBefore);
  }
  if (filters.limit !== undefined) {
    searchParams.set('limit', String(filters.limit));
  }
  if (filters.offset !== undefined) {
    searchParams.set('offset', String(filters.offset));
  }
  const query = searchParams.toString();
  return query ? `?${query}` : '';
}

function buildInsightEngineCoverageQuery(filters: InsightEngineCoverageFilters) {
  const searchParams = new URLSearchParams();
  if (filters.sourceId !== undefined && filters.sourceId !== null) {
    searchParams.set('source_id', String(filters.sourceId));
  }
  if (filters.limit !== undefined) {
    searchParams.set('limit', String(filters.limit));
  }
  if (filters.offset !== undefined) {
    searchParams.set('offset', String(filters.offset));
  }
  const query = searchParams.toString();
  return query ? `?${query}` : '';
}

function adaptInsightEngineAttemptList(payload: InsightEngineAttemptListDto): InsightEngineAttemptList {
  return {
    items: payload.items.map(adaptInsightEngineAttempt),
    total: payload.total,
    limit: payload.limit,
    offset: payload.offset,
  };
}

function adaptInsightEngineAttempt(payload: InsightEngineAttemptDto): InsightEngineAttempt {
  return {
    id: payload.id,
    feedItemId: payload.feed_item_id,
    feedItemTitle: payload.feed_item_title,
    sourceId: payload.source_id,
    sourceName: payload.source_name,
    aiRoute: payload.ai_route,
    profile: payload.profile,
    provider: payload.provider,
    model: payload.model,
    status: payload.status,
    stage: payload.stage,
    errorType: payload.error_type,
    errorMessage: payload.error_message,
    durationMs: payload.duration_ms,
    artifactCount: payload.artifact_count,
    createdAt: payload.created_at,
    completedAt: payload.completed_at,
    requestSnapshot: payload.request_snapshot,
  };
}

function adaptInsightEngineCoverage(payload: InsightEngineCoverageDto): InsightEngineCoverage {
  return {
    totalItemCount: payload.total_item_count,
    completeItemCount: payload.complete_item_count,
    incompleteItemCount: payload.incomplete_item_count,
    noArtifactItemCount: payload.no_artifact_item_count,
    partialArtifactItemCount: payload.partial_artifact_item_count,
    retryableItemCount: payload.retryable_item_count,
    problemItemCount: payload.problem_item_count,
    problemRetryableItemCount: payload.problem_retryable_item_count,
    initialGenerationItemCount: payload.initial_generation_item_count,
    initialGenerationRetryableItemCount: payload.initial_generation_retryable_item_count,
    blockedItemCount: payload.blocked_item_count,
    reasonCounts: payload.reason_counts,
    items: payload.items.map(adaptInsightEngineCoverageItem),
    total: payload.total,
    limit: payload.limit,
    offset: payload.offset,
  };
}

function adaptInsightEngineCoverageItem(payload: InsightEngineCoverageItemDto): InsightEngineCoverageItem {
  return {
    feedItemId: payload.feed_item_id,
    title: payload.title,
    sourceId: payload.source_id,
    sourceName: payload.source_name,
    aiRoute: payload.ai_route,
    profile: payload.profile,
    provider: payload.provider,
    model: payload.model,
    existingArtifactKinds: payload.existing_artifact_kinds,
    missingArtifactKinds: payload.missing_artifact_kinds,
    contentTextSource: payload.content_text_source,
    contentExtractionStatus: payload.content_extraction_status,
    contentExtractionError: payload.content_extraction_error,
    possibleReasonCode: payload.possible_reason_code,
    possibleReasonLabel: payload.possible_reason_label,
    coverageCategory: payload.coverage_category,
    retryable: payload.retryable,
    latestAttempt: payload.latest_attempt ? adaptInsightEngineCoverageLatestAttempt(payload.latest_attempt) : null,
  };
}

function adaptInsightEngineCoverageLatestAttempt(
  payload: InsightEngineCoverageLatestAttemptDto,
): InsightEngineCoverageLatestAttempt {
  return {
    id: payload.id,
    status: payload.status,
    stage: payload.stage,
    errorType: payload.error_type,
    errorMessage: payload.error_message,
    createdAt: payload.created_at,
    completedAt: payload.completed_at,
  };
}

function adaptInsightRequestPreview(payload: InsightRequestPreviewDto): InsightRequestPreview {
  return {
    available: payload.available,
    canSend: payload.can_send,
    message: payload.message,
    candidates: payload.candidates.map(adaptInsightRequestPreviewSample),
    sample: payload.sample ? adaptInsightRequestPreviewSample(payload.sample) : null,
    payload: payload.payload ? adaptInsightRequestPayload(payload.payload) : null,
  };
}

function adaptInsightRequestPreviewSample(payload: InsightRequestPreviewSampleDto): InsightRequestPreviewSample {
  return {
    itemId: payload.item_id,
    title: payload.title,
    sourceId: payload.source_id,
    sourceName: payload.source_name,
    sourceFolder: payload.source_folder,
    aiRoute: payload.ai_route,
    profile: payload.profile,
    contentTextSource: payload.content_text_source,
    contentExtractionStatus: payload.content_extraction_status,
    missingArtifactKinds: payload.missing_artifact_kinds,
  };
}

function adaptInsightRequestPayload(payload: InsightRequestPayloadDto): InsightRequestPayload {
  return {
    promptVersion: payload.prompt_version,
    profile: payload.profile,
    provider: payload.provider,
    model: payload.model,
    endpoint: payload.endpoint,
    temperature: payload.temperature,
    maxTokens: payload.max_tokens,
    messages: payload.messages,
    systemMessage: payload.system_message,
    userMessage: payload.user_message,
    requestBody: payload.request_body,
    fallbackRequestBody: payload.fallback_request_body,
    taxonomy: payload.taxonomy,
    contentTypeOptions: payload.content_type_options,
    reasonCodes: payload.reason_codes,
    activeInsightProfileContext: payload.active_insight_profile_context,
    sourcePreferenceProfile: payload.source_preference_profile,
    sourceProfileVersion: payload.source_profile_version,
    sourceProfileIsEmpty: payload.source_profile_is_empty,
    contentText: payload.content_text,
    contentTextSource: payload.content_text_source,
    stats: {
      sourceContentCharCount: payload.stats.source_content_char_count,
      contentTextCharCount: payload.stats.content_text_char_count,
      contentTextWasTruncated: payload.stats.content_text_was_truncated,
      contentTextEstimatedTokenCount: payload.stats.content_text_estimated_token_count,
      systemMessageCharCount: payload.stats.system_message_char_count,
      systemMessageEstimatedTokenCount: payload.stats.system_message_estimated_token_count,
      userMessageCharCount: payload.stats.user_message_char_count,
      userMessageEstimatedTokenCount: payload.stats.user_message_estimated_token_count,
    },
    excludedFields: payload.excluded_fields,
  };
}

function adaptInsightProfileSettings(payload: InsightProfileSettingsDto): InsightProfileSettings {
  return {
    activeProfile: adaptInsightProfileVersion(payload.active_profile),
    versions: payload.versions.map(adaptInsightProfileVersion),
    events: payload.events.map(adaptInsightProfileEvent),
    template: payload.template,
    activePromptContext: payload.active_prompt_context,
  };
}

function adaptInsightProfileVersion(payload: InsightProfileVersionDto): InsightProfileVersion {
  return {
    id: payload.id,
    schemaVersion: payload.schema_version,
    profileName: payload.profile_name,
    profileVersion: payload.profile_version,
    profileJson: payload.profile_json,
    sourceType: payload.source_type,
    editNote: payload.edit_note,
    diffSummary: payload.diff_summary,
    previousProfileId: payload.previous_profile_id,
    createdAt: payload.created_at,
  };
}

function adaptInsightProfileEvent(payload: InsightProfileEventDto): InsightProfileEvent {
  return {
    id: payload.id,
    profileId: payload.profile_id,
    eventType: payload.event_type,
    note: payload.note,
    eventJson: payload.event_json,
    createdAt: payload.created_at,
  };
}

function adaptInsightProfileDraftInspectResponse(
  payload: InsightProfileDraftInspectResponseDto,
): InsightProfileDraftInspectResponse {
  return {
    normalizedProfile: payload.normalized_profile,
    promptContext: payload.prompt_context,
    baseProfile: adaptInsightProfileVersion(payload.base_profile),
    diffSummary: payload.diff_summary,
    fieldDiffs: payload.field_diffs.map(adaptInsightProfileFieldDiff),
    evidenceReferences: payload.evidence_references,
  };
}

function adaptInsightProfileFieldDiff(payload: InsightProfileFieldDiffDto): InsightProfileFieldDiff {
  return {
    field: payload.field,
    changeType: payload.change_type,
    before: payload.before,
    after: payload.after,
  };
}

function adaptInsightProfileCandidateInspectResponse(
  payload: InsightProfileCandidateInspectResponseDto,
): InsightProfileCandidateInspectResponse {
  return {
    candidateVersion: payload.candidate_version,
    basedOnPackage: payload.based_on_package,
    summary: payload.summary,
    normalizedCandidate: payload.normalized_candidate,
    globalProfileInspection: payload.global_profile_inspection
      ? adaptInsightProfileDraftInspectResponse(payload.global_profile_inspection)
      : null,
    sourceProfileChanges: payload.source_profile_changes.map(adaptInsightProfileCandidateSourceChange),
    channelPromptBoundarySuggestions: payload.channel_prompt_boundary_suggestions.map(
      adaptInsightProfileCandidateChannelSuggestion,
    ),
    scoringPolicySuggestions: payload.scoring_policy_suggestions,
    evidenceReferences: payload.evidence_references,
    warnings: payload.warnings,
    dryRun: payload.dry_run,
  };
}

function adaptInsightProfileCandidateSourceChange(
  payload: InsightProfileCandidateSourceChangeDto,
): InsightProfileCandidateSourceChange {
  return {
    sourceId: payload.source_id,
    sourceName: payload.source_name,
    normalizedProfile: payload.normalized_profile,
    promptContext: payload.prompt_context,
    diffSummary: payload.diff_summary,
    fieldDiffs: payload.field_diffs.map(adaptInsightProfileFieldDiff),
    evidenceReferences: payload.evidence_references,
  };
}

function adaptInsightProfileCandidateChannelSuggestion(
  payload: InsightProfileCandidateChannelSuggestionDto,
): InsightProfileCandidateChannelSuggestion {
  return {
    channelId: payload.channel_id,
    channelLabel: payload.channel_label,
    currentFocusPrompt: payload.current_focus_prompt,
    suggestedFocusPrompt: payload.suggested_focus_prompt,
    currentBoundaryPrompt: payload.current_boundary_prompt,
    suggestedBoundaryPrompt: payload.suggested_boundary_prompt,
    rationale: payload.rationale,
    evidenceReferences: payload.evidence_references,
  };
}

function adaptInsightProfileCandidateSourceApplyResponse(
  payload: InsightProfileCandidateSourceApplyResponseDto,
): InsightProfileCandidateSourceApplyResponse {
  return {
    appliedSourceIds: payload.applied_source_ids,
    appliedCount: payload.applied_count,
    skippedSourceIds: payload.skipped_source_ids,
    message: payload.message,
  };
}

function buildEvidencePackageQuery(input: InsightEvidencePackageInput) {
  const searchParams = new URLSearchParams();
  searchParams.set('range', input.range);
  searchParams.set('artifact_mode', input.artifactMode ?? 'latest');
  if (input.range === 'custom') {
    if (input.startDate) {
      searchParams.set('start_date', input.startDate);
    }
    if (input.endDate) {
      searchParams.set('end_date', input.endDate);
    }
  }
  const query = searchParams.toString();
  return query ? `?${query}` : '';
}

function adaptInsightEvidencePackagePreview(payload: InsightEvidencePackagePreviewDto): InsightEvidencePackagePreview {
  return {
    packageVersion: payload.package_version,
    range: payload.range,
    artifactMode: payload.artifact_mode,
    startAt: payload.start_at,
    endAt: payload.end_at,
    generatedAt: payload.generated_at,
    itemCount: payload.item_count,
    excludedUnreadCount: payload.excluded_unread_count,
    contentTextFileCount: payload.content_text_file_count,
    feedbackItemCount: payload.feedback_item_count,
    structuredFeedbackItemCount: payload.structured_feedback_item_count,
    channelCorrectionItemCount: payload.channel_correction_item_count,
    channelCorrectedItemCount: payload.channel_corrected_item_count,
    strongSignalCount: payload.strong_signal_count,
    mediumSignalCount: payload.medium_signal_count,
    weakCleanupSignalCount: payload.weak_cleanup_signal_count,
    negativeSignalCount: payload.negative_signal_count,
    behaviorEventCount: payload.behavior_event_count,
    feedbackClearEventCount: payload.feedback_clear_event_count,
    notInterestedClearEventCount: payload.not_interested_clear_event_count,
    autoLowPriorityClearEventCount: payload.auto_low_priority_clear_event_count,
    artifactCoverage: payload.artifact_coverage,
    savedItemCount: payload.saved_item_count,
    savedItemContentTextFileCount: payload.saved_item_content_text_file_count,
    savedItemDisplayMarkdownFileCount: payload.saved_item_display_markdown_file_count,
    savedItemDisplayAssetCount: payload.saved_item_display_asset_count,
    savedItemAiInsightCount: payload.saved_item_ai_insight_count,
    savedItemCuratedInsightCount: payload.saved_item_curated_insight_count,
    savedItemCuratedEventCount: payload.saved_item_curated_event_count,
    externalSavedItemCount: payload.external_saved_item_count,
    failedAttemptCount: payload.failed_attempt_count,
    skippedAttemptCount: payload.skipped_attempt_count,
  };
}

function parseDownloadFilename(contentDisposition: string | null) {
  if (!contentDisposition) {
    return null;
  }
  const utf8Match = /filename\*=UTF-8''([^;]+)/i.exec(contentDisposition);
  if (utf8Match) {
    return decodeURIComponent(utf8Match[1]);
  }
  const asciiMatch = /filename="?([^";]+)"?/i.exec(contentDisposition);
  return asciiMatch?.[1] ?? null;
}
