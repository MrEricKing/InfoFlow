import type {
  AppOperationDto,
  FeedItemInsightArtifactBatchOperationCreatePayloadDto,
  SourceSyncOperationCreatePayloadDto,
} from '@/types/source';
import type {
  ManualUrlCaptureInput,
  SavedItemAIRoute,
  SavedItemManualImportInput,
  UploadedMaterialCaptureInput,
} from '@/types/feed';
import {requestJson} from '@/api/client';

export function startSourceSyncOperation(payload: SourceSyncOperationCreatePayloadDto) {
  return requestJson<AppOperationDto>('/operations/source-sync', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
}

export function startFeedItemInsightArtifactBatchOperation(payload: FeedItemInsightArtifactBatchOperationCreatePayloadDto) {
  return requestJson<AppOperationDto>('/operations/feed-item-insight-artifacts', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
}

export function getActiveOperation() {
  return requestJson<AppOperationDto | null>('/operations/active');
}

export function getActiveOperations() {
  return requestJson<AppOperationDto[]>('/operations/active-list');
}

export function getOperation(operationId: number) {
  return requestJson<AppOperationDto>(`/operations/${operationId}`);
}

export function stopOperation(operationId: number) {
  return requestJson<AppOperationDto>(`/operations/${operationId}/stop`, {
    method: 'POST',
  });
}

export function startManualUrlCaptureOperation(input: ManualUrlCaptureInput) {
  return requestJson<AppOperationDto>('/operations/captures/manual-url', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      url: input.url,
      title: input.title,
      user_note: input.userNote,
      ai_route: input.aiRoute,
    }),
  });
}

export function startUploadedMaterialCaptureOperation(input: UploadedMaterialCaptureInput) {
  const formData = new FormData();
  formData.set('file', input.file);
  formData.set('material_type', input.materialType);
  if (input.title) {
    formData.set('title', input.title);
  }
  if (input.userNote) {
    formData.set('user_note', input.userNote);
  }
  formData.set('ai_route', input.aiRoute);

  return requestJson<AppOperationDto>('/operations/captures/upload', {
    method: 'POST',
    body: formData,
  });
}

export function startRetryCaptureOperation(savedItemId: number) {
  return requestJson<AppOperationDto>(`/operations/saved-items/${savedItemId}/capture-runs`, {
    method: 'POST',
  });
}

export function startSavedItemManualImportOperation(savedItemId: number, input: SavedItemManualImportInput) {
  const formData = new FormData();
  formData.set('file', input.file);
  formData.set('material_type', input.materialType);
  if (input.title) {
    formData.set('title', input.title);
  }
  if (input.userNote) {
    formData.set('user_note', input.userNote);
  }

  return requestJson<AppOperationDto>(`/operations/saved-items/${savedItemId}/capture-import`, {
    method: 'POST',
    body: formData,
  });
}

export function startLocalMirrorExportOperation(savedItemId: number) {
  return requestJson<AppOperationDto>(`/operations/saved-items/${savedItemId}/local-mirror-exports`, {
    method: 'POST',
  });
}

export function startSavedItemInsightOperation(savedItemId: number, aiRoute?: SavedItemAIRoute | 'saved_item' | null) {
  return requestJson<AppOperationDto>(`/operations/saved-items/${savedItemId}/insight-rebuild`, {
    method: 'POST',
    headers: aiRoute
      ? {
          'Content-Type': 'application/json',
        }
      : undefined,
    body: aiRoute ? JSON.stringify({ai_route: aiRoute}) : undefined,
  });
}

export function startFeedItemExtractOperation(feedItemId: number) {
  return requestJson<AppOperationDto>(`/operations/feed-items/${feedItemId}/extract-content`, {
    method: 'POST',
  });
}

export function startFeedItemInsightArtifactOperation(feedItemId: number, mode: 'rebuild' | 'build_missing') {
  return requestJson<AppOperationDto>(`/operations/feed-items/${feedItemId}/insight-artifacts`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mode}),
  });
}
