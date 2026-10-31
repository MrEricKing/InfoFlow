import type {
  OpmlImportCommitPayloadDto,
  OpmlImportPreviewResponseDto,
  SourceCategoryDeletePayload,
  SourceCategoryDto,
  SourceCategoryMutationResponseDto,
  SourceCategoryRenamePayload,
  SourceCreatePayload,
  SourceDeletePreviewDto,
  SourceDeleteResultDto,
  SourceDto,
  SourceFetchDto,
  SourceFetchWithSourceDto,
  SourceImportResponseDto,
  SourceInsightRouting,
  SourceInsightRoutingDto,
  SourceInsightRoutingInput,
  SourcePreferenceProfile,
  SourcePreferenceProfileDto,
  SourcePreferenceProfileFieldError,
  SourcePreferenceProfileFieldErrorDto,
  SourcePreferenceProfileInput,
  SourcePreferenceProfileState,
  SourcePreferenceProfileStateDto,
  SourcePreviewRequestDto,
  SourcePreviewResponseDto,
  SourceUpdatePayload,
} from '@/types/source';
import {requestJson} from '@/api/client';

export function listSources() {
  return requestJson<SourceDto[]>('/sources');
}

export function getSource(sourceId: number) {
  return requestJson<SourceDto>(`/sources/${sourceId}`);
}

export async function getSourceInsightRouting(sourceId: number) {
  const payload = await requestJson<SourceInsightRoutingDto>(`/sources/${sourceId}/insight-routing`);
  return adaptSourceInsightRouting(payload);
}

export async function updateSourceInsightRouting(sourceId: number, input: SourceInsightRoutingInput) {
  const payload = await requestJson<SourceInsightRoutingDto>(`/sources/${sourceId}/insight-routing`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      flow_policy: input.flowPolicy,
      channel_tendencies: input.channelTendencies.map((tendency) => ({
        channel_id: tendency.channelId,
        tendency: tendency.tendency,
        is_primary: tendency.isPrimary,
        notes: tendency.notes,
      })),
    }),
  });
  return adaptSourceInsightRouting(payload);
}

export function adaptSourceInsightRouting(payload: SourceInsightRoutingDto): SourceInsightRouting {
  return {
    sourceId: payload.source_id,
    flowPolicy: payload.flow_policy,
    channelTendencies: payload.channel_tendencies.map((tendency) => ({
      channelId: tendency.channel_id,
      tendency: tendency.tendency,
      isPrimary: tendency.is_primary,
      notes: tendency.notes,
    })),
  };
}

export async function getSourcePreferenceProfile(sourceId: number) {
  const payload = await requestJson<SourcePreferenceProfileStateDto>(`/sources/${sourceId}/preference-profile`);
  return adaptSourcePreferenceProfileState(payload);
}

export async function saveSourcePreferenceProfileDraft(sourceId: number, input: SourcePreferenceProfileInput) {
  const payload = await requestJson<SourcePreferenceProfileStateDto>(`/sources/${sourceId}/preference-profile/draft`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(buildSourcePreferenceProfilePayload(input)),
  });
  return adaptSourcePreferenceProfileState(payload);
}

export async function activateSourcePreferenceProfileDraft(sourceId: number, editNote?: string | null) {
  const payload = await requestJson<SourcePreferenceProfileStateDto>(`/sources/${sourceId}/preference-profile/draft/activate`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({edit_note: editNote ?? null}),
  });
  return adaptSourcePreferenceProfileState(payload);
}

export async function deleteSourcePreferenceProfileDraft(sourceId: number) {
  const payload = await requestJson<SourcePreferenceProfileStateDto>(`/sources/${sourceId}/preference-profile/draft`, {
    method: 'DELETE',
  });
  return adaptSourcePreferenceProfileState(payload);
}

export function adaptSourcePreferenceProfileState(payload: SourcePreferenceProfileStateDto): SourcePreferenceProfileState {
  return {
    sourceId: payload.source_id,
    active: payload.active ? adaptSourcePreferenceProfile(payload.active) : null,
    draft: payload.draft ? adaptSourcePreferenceProfile(payload.draft) : null,
    history: payload.history.map(adaptSourcePreferenceProfile),
    renderedPromptContext: payload.rendered_prompt_context,
    renderStats: payload.render_stats,
    validationErrors: payload.validation_errors.map(adaptSourcePreferenceProfileFieldError),
  };
}

function adaptSourcePreferenceProfile(payload: SourcePreferenceProfileDto): SourcePreferenceProfile {
  return {
    id: payload.id,
    sourceId: payload.source_id,
    status: payload.status,
    schemaVersion: payload.schema_version,
    profileVersion: payload.profile_version,
    subscriptionReason: payload.subscription_reason,
    goodArticleCriteria: payload.good_article_criteria,
    badArticlePatterns: payload.bad_article_patterns,
    preferredTopics: payload.preferred_topics,
    ignoredTopics: payload.ignored_topics,
    preferredContentTypes: payload.preferred_content_types,
    positiveExampleItemIds: payload.positive_example_item_ids,
    negativeExampleItemIds: payload.negative_example_item_ids,
    positiveExampleSummaries: payload.positive_example_summaries,
    negativeExampleSummaries: payload.negative_example_summaries,
    qualityNotes: payload.quality_notes,
    promptContext: payload.prompt_context,
    promptContextStats: payload.prompt_context_stats,
    sourceType: payload.source_type,
    editNote: payload.edit_note,
    activatedAt: payload.activated_at,
    archivedAt: payload.archived_at,
    createdAt: payload.created_at,
    updatedAt: payload.updated_at,
  };
}

function adaptSourcePreferenceProfileFieldError(payload: SourcePreferenceProfileFieldErrorDto): SourcePreferenceProfileFieldError {
  return {
    field: payload.field,
    code: payload.code,
    message: payload.message,
    actual: payload.actual,
    limit: payload.limit,
    unit: payload.unit,
    patternClass: payload.pattern_class,
  };
}

function buildSourcePreferenceProfilePayload(input: SourcePreferenceProfileInput) {
  return {
    subscription_reason: input.subscriptionReason,
    good_article_criteria: input.goodArticleCriteria,
    bad_article_patterns: input.badArticlePatterns,
    preferred_topics: input.preferredTopics,
    ignored_topics: input.ignoredTopics,
    preferred_content_types: input.preferredContentTypes,
    positive_example_item_ids: input.positiveExampleItemIds,
    negative_example_item_ids: input.negativeExampleItemIds,
    positive_example_summaries: input.positiveExampleSummaries,
    negative_example_summaries: input.negativeExampleSummaries,
    quality_notes: input.qualityNotes,
    source_type: input.sourceType,
    edit_note: input.editNote,
  };
}

export function listSourceCategories() {
  return requestJson<SourceCategoryDto[]>('/sources/categories');
}

export function renameSourceCategory(payload: SourceCategoryRenamePayload) {
  return requestJson<SourceCategoryMutationResponseDto>('/sources/categories/rename', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
}

export function deleteSourceCategory(payload: SourceCategoryDeletePayload) {
  return requestJson<SourceCategoryMutationResponseDto>('/sources/categories/delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
}

export function createSource(payload: SourceCreatePayload) {
  return requestJson<SourceDto>('/sources', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
}

export function updateSource(sourceId: number, payload: SourceUpdatePayload) {
  return requestJson<SourceDto>(`/sources/${sourceId}`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
}

export function getSourceDeletePreview(sourceId: number) {
  return requestJson<SourceDeletePreviewDto>(`/sources/${sourceId}/delete-preview`);
}

export function deleteSource(sourceId: number, previewToken: string) {
  return requestJson<SourceDeleteResultDto>(`/sources/${sourceId}`, {
    method: 'DELETE',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({confirm: true, preview_token: previewToken}),
  });
}

export function listSourceFetches(sourceId: number, limit = 10) {
  const params = new URLSearchParams({limit: String(limit)});
  return requestJson<SourceFetchDto[]>(`/sources/${sourceId}/fetches?${params.toString()}`);
}

export function listAllSourceFetches(limit = 50) {
  const params = new URLSearchParams({limit: String(limit)});
  return requestJson<SourceFetchWithSourceDto[]>(`/sources/fetches?${params.toString()}`);
}

export function previewSourcesOpml(file: File) {
  const formData = new FormData();
  formData.append('file', file);

  return requestJson<OpmlImportPreviewResponseDto>('/sources/imports/opml/preview', {
    method: 'POST',
    body: formData,
  });
}

export function commitSourcesOpmlImport(payload: OpmlImportCommitPayloadDto) {
  return requestJson<SourceImportResponseDto>('/sources/imports/opml/commit', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
}

export function previewSource(payload: SourcePreviewRequestDto) {
  return requestJson<SourcePreviewResponseDto>('/sources/preview', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
}
