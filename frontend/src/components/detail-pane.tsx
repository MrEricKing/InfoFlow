import {useEffect, useMemo, useRef, useState, type ReactNode} from 'react';
import {
  Bookmark,
  BookOpenCheck,
  ExternalLink,
  BookOpen,
  MessageSquare,
  RefreshCw,
  Sparkles,
  Smartphone,
  ThumbsDown,
  Undo2,
} from 'lucide-react';
import {StatusBadge} from '@/components/status-badge';
import {formatAbsoluteDate} from '@/lib/date';
import {sanitizeRichHtml} from '@/lib/html';
import {buildArchiveArtifactKey, selectPrimaryKnowledgeMarkdownArtifact} from '@/lib/knowledge-markdown';
import {
  EMPTY_CHANNEL_CORRECTION,
  EMPTY_STRUCTURED_FEEDBACK,
  InsightFeedbackPopover,
  channelFeedbackEquals,
  cloneChannelFeedback,
  cloneStructuredFeedback,
  hasChannelFeedback,
  hasStructuredFeedback,
  normalizeChannelFeedbackDraft,
  normalizeStructuredFeedbackDraft,
  structuredFeedbackEquals,
} from '@/components/detail-pane/insight-feedback-popover';
import {
  KNOWLEDGE_PANEL_COLLAPSE_STORAGE_KEY,
  KnowledgeDetailPanels,
  UploadedMaterialMetadata,
  buildArchiveStatusMessage,
  getArchiveArtifactLabel,
  isUploadSavedItem,
  readCollapsedKnowledgePanels,
  type KnowledgeDetailPanelKey,
  type MarkdownPreviewState,
} from '@/components/detail-pane/knowledge-detail-panels';
import {
  ContentExtractionPill,
  FeedCaptureSummaryCard,
  buildReaderBodyHtml,
  getContentExtractionActionLabel,
  shouldShowContentExtractionAction,
  shouldShowFeedCaptureSummaryCard,
} from '@/components/detail-pane/reader-content';
import {
  EMPTY_SAVED_INSIGHT_DRAFT,
  SavedItemInsightPanel,
  buildSavedInsightDraft,
  buildSavedInsightPayload,
  type SavedInsightDraft,
  type SavedItemRebuildRouteOption,
} from '@/components/detail-pane/saved-item-insight-panel';
import type {
  ArchiveArtifact,
  CaptureOperation,
  FeedItem,
  FeedItemChannelCorrectionInput,
  FeedItemFeedbackInput,
  FeedItemStructuredFeedback,
  ItemStatus,
  SavedItemAIRoute,
  SavedItemCuratedInsightInput,
  SavedItemManualImportInput,
  ViewMode,
} from '@/types/feed';
import type {InsightChannel} from '@/types/settings';
import type {AppOperationDto} from '@/types/source';

interface DetailPaneProps {
  captureOperation: CaptureOperation | null;
  detailOperations: DetailOperationState;
  detailErrorMessage: string | null;
  insightChannels: InsightChannel[];
  isReadOnly: boolean;
  isUnavailable: boolean;
  item: FeedItem | null;
  view: ViewMode;
  onOpenOriginal: (item: FeedItem) => void;
  onOpenSourceApp: (item: FeedItem) => void;
  onInsightEngineArtifactsRebuild: (item: FeedItem) => Promise<void>;
  onClearAutoLowPriority: (item: FeedItem) => Promise<void>;
  onExtractContent: (item: FeedItem) => Promise<void>;
  onChannelCorrectionClear: (item: FeedItem) => Promise<void>;
  onInsightFeedbackSave: (item: FeedItem, input: FeedItemFeedbackInput) => Promise<void>;
  onSavedItemManualImport: (item: FeedItem, input: SavedItemManualImportInput) => Promise<void>;
  onSavedItemCaptureRetry: (item: FeedItem) => Promise<void>;
  onSavedItemInsightRebuild: (item: FeedItem, aiRoute?: SavedItemAIRoute | null) => Promise<void>;
  onSavedItemCuratedInsightClear: (item: FeedItem) => Promise<void>;
  onSavedItemCuratedInsightSave: (item: FeedItem, input: SavedItemCuratedInsightInput) => Promise<void>;
  onSavedItemLocalMirrorReexport: (item: FeedItem) => Promise<void>;
  onSavedItemNoteSave: (item: FeedItem, userNote: string | null) => Promise<void>;
  onStatusChange: (item: FeedItem, nextStatus: ItemStatus) => void;
}

interface DetailOperationState {
  isExtractingContent: boolean;
  isRebuildingInsightEngineArtifacts: boolean;
  isRebuildingSavedItemInsight: boolean;
  savedItemInsightOperation: AppOperationDto | null;
  isReexportingMirror: boolean;
}

export function DetailPane({
  captureOperation,
  detailOperations,
  detailErrorMessage,
  insightChannels,
  isReadOnly,
  isUnavailable,
  item,
  view,
  onOpenOriginal,
  onOpenSourceApp,
  onInsightEngineArtifactsRebuild,
  onClearAutoLowPriority,
  onExtractContent,
  onChannelCorrectionClear,
  onInsightFeedbackSave,
  onSavedItemManualImport,
  onSavedItemCaptureRetry,
  onSavedItemInsightRebuild,
  onSavedItemCuratedInsightClear,
  onSavedItemCuratedInsightSave,
  onSavedItemLocalMirrorReexport,
  onSavedItemNoteSave,
  onStatusChange,
}: DetailPaneProps) {
  const [noteDraft, setNoteDraft] = useState('');
  const [isSavingNote, setIsSavingNote] = useState(false);
  const [noteError, setNoteError] = useState<string | null>(null);
  const [isReexportingMirror, setIsReexportingMirror] = useState(false);
  const [mirrorError, setMirrorError] = useState<string | null>(null);
  const [isExtractingContent, setIsExtractingContent] = useState(false);
  const [contentExtractionActionError, setContentExtractionActionError] = useState<string | null>(null);
  const [isInsightFeedbackOpen, setIsInsightFeedbackOpen] = useState(false);
  const [insightFeedbackDraft, setInsightFeedbackDraft] = useState('');
  const [structuredFeedbackDraft, setStructuredFeedbackDraft] = useState<FeedItemStructuredFeedback>(
    EMPTY_STRUCTURED_FEEDBACK,
  );
  const [channelFeedbackDraft, setChannelFeedbackDraft] =
    useState<FeedItemChannelCorrectionInput>(EMPTY_CHANNEL_CORRECTION);
  const [isInsightFeedbackAdvancedOpen, setIsInsightFeedbackAdvancedOpen] = useState(false);
  const [isSavingInsightFeedback, setIsSavingInsightFeedback] = useState(false);
  const [insightFeedbackError, setInsightFeedbackError] = useState<string | null>(null);
  const [isRebuildingInsightEngineArtifacts, setIsRebuildingInsightEngineArtifacts] = useState(false);
  const [isClearingAutoLowPriority, setIsClearingAutoLowPriority] = useState(false);
  const [insightEngineArtifactActionMessage, setInsightEngineArtifactActionMessage] = useState<string | null>(null);
  const [insightEngineArtifactActionError, setInsightEngineArtifactActionError] = useState<string | null>(null);
  const [isRebuildingSavedItemInsight, setIsRebuildingSavedItemInsight] = useState(false);
  const [savedInsightActionMessage, setSavedInsightActionMessage] = useState<string | null>(null);
  const [savedInsightActionError, setSavedInsightActionError] = useState<string | null>(null);
  const [isEditingSavedInsight, setIsEditingSavedInsight] = useState(false);
  const [savedInsightDraft, setSavedInsightDraft] = useState<SavedInsightDraft>(EMPTY_SAVED_INSIGHT_DRAFT);
  const [savedInsightEditNote, setSavedInsightEditNote] = useState('');
  const [savedInsightRebuildRoute, setSavedInsightRebuildRoute] = useState<SavedItemRebuildRouteOption>('saved_item');
  const [isSavingSavedInsight, setIsSavingSavedInsight] = useState(false);
  const [savedInsightEditError, setSavedInsightEditError] = useState<string | null>(null);
  const [markdownPreview, setMarkdownPreview] = useState<MarkdownPreviewState | null>(null);
  const [collapsedKnowledgePanels, setCollapsedKnowledgePanels] = useState<Record<KnowledgeDetailPanelKey, boolean>>(
    readCollapsedKnowledgePanels,
  );
  const operationIdRef = useRef(0);
  const activeOperationIdsRef = useRef<Record<string, number>>({});
  const itemContextRef = useRef<{itemId: number | null; view: ViewMode}>({itemId: item?.id ?? null, view});
  const renderedSummaryHtml = useMemo(() => (item?.summary ? sanitizeRichHtml(item.summary) : ''), [item?.summary]);
  const renderedContentHtml = useMemo(
    () => (view !== 'kb' && item ? buildReaderBodyHtml(item) : ''),
    [item, view],
  );
  const noteValue = item?.note ?? '';
  const hasNoteChanges = noteDraft !== noteValue;
  const insightFeedbackValue = item?.insightFeedbackText ?? '';
  const structuredFeedbackValue = item?.structuredFeedback ?? null;
  const channelFeedbackValue = item?.channelFeedback ?? null;
  const normalizedStructuredFeedbackDraft = normalizeStructuredFeedbackDraft(structuredFeedbackDraft);
  const normalizedChannelFeedbackDraft = normalizeChannelFeedbackDraft(channelFeedbackDraft);
  const hasInsightFeedbackChanges =
    insightFeedbackDraft !== insightFeedbackValue ||
    !structuredFeedbackEquals(normalizedStructuredFeedbackDraft, structuredFeedbackValue) ||
    !channelFeedbackEquals(normalizedChannelFeedbackDraft, channelFeedbackValue);
  const hasAnyInsightFeedback = Boolean(insightFeedbackValue) || hasStructuredFeedback(structuredFeedbackValue);
  const hasAnyChannelFeedback = hasChannelFeedback(channelFeedbackValue);
  const hasAnyFeedback = hasAnyInsightFeedback || hasAnyChannelFeedback;
  const canChangeReaderState = item?.feedItemId != null && !isReadOnly;
  const canToggleSaved = Boolean(item && !isReadOnly && (item.feedItemId !== null || item.savedItemId !== null));
  const canExtractContent = Boolean(item && !isReadOnly && view !== 'kb' && shouldShowContentExtractionAction(item));
  const canEditInsightFeedback = Boolean(item && !isReadOnly && view !== 'kb' && item.feedItemId !== null);
  const canRebuildInsightEngineArtifacts = Boolean(item && !isReadOnly && view === 'ai' && item.feedItemId !== null);
  const contentExtractionActionLabel = item ? getContentExtractionActionLabel(item) : '报告并提取正文';
  const isSaving = Boolean(item?.isSaving);
  const archiveArtifacts = item?.archiveArtifacts ?? [];
  const coreArchiveArtifacts = archiveArtifacts.filter((artifact) => artifact.artifactKind !== 'page_image_v1');
  const imageArchiveArtifacts = archiveArtifacts.filter((artifact) => artifact.artifactKind === 'page_image_v1');
  const primaryMarkdownArtifact = useMemo(
    () => selectPrimaryKnowledgeMarkdownArtifact(item?.archiveArtifacts),
    [item?.archiveArtifacts],
  );
  const archiveStatusMessage = item ? buildArchiveStatusMessage(item, coreArchiveArtifacts) : null;
  const isCaptureRunning = captureOperation?.status === 'running';
  const canRunCapture = Boolean(
    item && view === 'kb' && item.originalUrl && !isUploadSavedItem(item) && item.captureStatus !== 'pending' && !isCaptureRunning,
  );
  const canManualImportCapture = Boolean(item && view === 'kb' && !isReadOnly && !isUploadSavedItem(item) && !isCaptureRunning);
  const hasMirrorExportableArtifacts = Boolean(
    item?.hasSinglefileHtml ||
      item?.hasCrawl4aiRawMarkdown ||
      item?.hasCrawl4aiFitMarkdown ||
      item?.hasCrawl4aiFitImageReinsertedMarkdown ||
      item?.sourceMaterialArtifact?.status === 'available' ||
      archiveArtifacts.some((artifact) => artifact.artifactKind === 'uploaded_markdown_v1' && artifact.status === 'available') ||
      (item?.imageAssetCount ?? 0) > 0,
  );
  const isExtractingContentBusy = isExtractingContent || detailOperations.isExtractingContent;
  const isRebuildingInsightEngineArtifactsBusy = isRebuildingInsightEngineArtifacts || detailOperations.isRebuildingInsightEngineArtifacts;
  const isRebuildingSavedItemInsightBusy =
    isRebuildingSavedItemInsight || detailOperations.isRebuildingSavedItemInsight;
  const isReexportingMirrorBusy = isReexportingMirror || detailOperations.isReexportingMirror;
  const isMirrorReexportBlocked = isReexportingMirrorBusy || isCaptureRunning;

  function startItemOperation(key: string) {
    const operationId = operationIdRef.current + 1;
    operationIdRef.current = operationId;
    activeOperationIdsRef.current = {...activeOperationIdsRef.current, [key]: operationId};
    return {key, operationId, itemId: item?.id ?? null, view};
  }

  function isCurrentItemOperation(operation: ReturnType<typeof startItemOperation>) {
    return (
      activeOperationIdsRef.current[operation.key] === operation.operationId &&
      itemContextRef.current.itemId === operation.itemId &&
      itemContextRef.current.view === operation.view
    );
  }

  useEffect(() => {
    itemContextRef.current = {itemId: item?.id ?? null, view};
    activeOperationIdsRef.current = {};
    setNoteDraft(noteValue);
    setNoteError(null);
    setIsSavingNote(false);
    setMirrorError(null);
    setIsReexportingMirror(false);
    setIsExtractingContent(false);
    setContentExtractionActionError(null);
    setIsInsightFeedbackOpen(false);
    setInsightFeedbackDraft(insightFeedbackValue);
    setStructuredFeedbackDraft(cloneStructuredFeedback(structuredFeedbackValue));
    setChannelFeedbackDraft(cloneChannelFeedback(channelFeedbackValue));
    setIsInsightFeedbackAdvancedOpen(false);
    setIsSavingInsightFeedback(false);
    setInsightFeedbackError(null);
    setIsRebuildingInsightEngineArtifacts(false);
    setInsightEngineArtifactActionMessage(null);
    setInsightEngineArtifactActionError(null);
    setIsRebuildingSavedItemInsight(false);
    setSavedInsightActionMessage(null);
    setSavedInsightActionError(null);
    setIsEditingSavedInsight(false);
    setSavedInsightDraft(buildSavedInsightDraft(item?.resolvedInsight?.payload ?? null));
    setSavedInsightEditNote('');
    setSavedInsightRebuildRoute('saved_item');
    setIsSavingSavedInsight(false);
    setSavedInsightEditError(null);
    setMarkdownPreview(null);
  }, [
    item?.id,
    insightFeedbackValue,
    item?.resolvedInsight?.payload,
    noteValue,
    structuredFeedbackValue,
    channelFeedbackValue,
    view,
  ]);

  useEffect(() => {
    try {
      window.localStorage.setItem(KNOWLEDGE_PANEL_COLLAPSE_STORAGE_KEY, JSON.stringify(collapsedKnowledgePanels));
    } catch {
      // Local storage is a convenience only; panel state can safely reset.
    }
  }, [collapsedKnowledgePanels]);

  async function saveNote() {
    if (!item || view !== 'kb' || isSavingNote) {
      return;
    }

    const operation = startItemOperation('saveNote');
    setIsSavingNote(true);
    setNoteError(null);
    try {
      await onSavedItemNoteSave(item, noteDraft);
    } catch (error) {
      console.error('Failed to update saved item note', error);
      if (isCurrentItemOperation(operation)) {
        setNoteError('备注保存失败，请稍后重试。');
      }
    } finally {
      if (isCurrentItemOperation(operation)) {
        setIsSavingNote(false);
      }
    }
  }

  async function retryCapture() {
    if (!item || isCaptureRunning || !canRunCapture) {
      return;
    }

    try {
      await onSavedItemCaptureRetry(item);
    } catch (error) {
      console.error('Failed to retry saved item capture', error);
    }
  }

  async function reexportLocalMirror() {
    if (!item || view !== 'kb' || isMirrorReexportBlocked || !hasMirrorExportableArtifacts) {
      return;
    }

    const operation = startItemOperation('reexportLocalMirror');
    setIsReexportingMirror(true);
    setMirrorError(null);
    try {
      await onSavedItemLocalMirrorReexport(item);
    } catch (error) {
      console.error('Failed to re-export saved item local mirror', error);
      if (isCurrentItemOperation(operation)) {
        setMirrorError(error instanceof Error ? error.message : '重新导出本地镜像失败，请稍后重试。');
      }
    } finally {
      if (isCurrentItemOperation(operation)) {
        setIsReexportingMirror(false);
      }
    }
  }

  async function reportAndExtractContent() {
    if (!item || isExtractingContentBusy || !canExtractContent) {
      return;
    }

    const operation = startItemOperation('extractContent');
    setIsExtractingContent(true);
    setContentExtractionActionError(null);
    try {
      await onExtractContent(item);
    } catch (error) {
      console.error('Failed to extract feed item content', error);
      if (isCurrentItemOperation(operation)) {
        setContentExtractionActionError(error instanceof Error ? error.message : '正文提取请求失败，请稍后重试。');
      }
    } finally {
      if (isCurrentItemOperation(operation)) {
        setIsExtractingContent(false);
      }
    }
  }

  async function saveInsightFeedback(
    nextFeedbackText: string | null,
    nextStructuredFeedback: FeedItemStructuredFeedback | null,
    nextChannelFeedback: FeedItemChannelCorrectionInput | null,
  ) {
    if (!item || !canEditInsightFeedback || isSavingInsightFeedback) {
      return;
    }

    const operation = startItemOperation('saveInsightFeedback');
    setIsSavingInsightFeedback(true);
    setInsightFeedbackError(null);
    try {
      await onInsightFeedbackSave(item, {
        insightFeedbackText: nextFeedbackText,
        structuredFeedback: nextStructuredFeedback,
        channelFeedback: nextChannelFeedback,
      });
      if (isCurrentItemOperation(operation)) {
        setIsInsightFeedbackOpen(false);
      }
    } catch (error) {
      console.error('Failed to update feed item insight feedback', error);
      if (isCurrentItemOperation(operation)) {
        setInsightFeedbackError(error instanceof Error ? error.message : '洞察反馈保存失败，请稍后重试。');
      }
    } finally {
      if (isCurrentItemOperation(operation)) {
        setIsSavingInsightFeedback(false);
      }
    }
  }

  async function clearAllInsightFeedback() {
    if (!item || !canEditInsightFeedback || isSavingInsightFeedback) {
      return;
    }

    const operation = startItemOperation('clearInsightFeedback');
    setIsSavingInsightFeedback(true);
    setInsightFeedbackError(null);
    try {
      await onInsightFeedbackSave(item, {
        insightFeedbackText: null,
        structuredFeedback: null,
        channelFeedback: null,
      });
      if (isCurrentItemOperation(operation)) {
        setIsInsightFeedbackOpen(false);
      }
    } catch (error) {
      console.error('Failed to clear feed item insight feedback', error);
      if (isCurrentItemOperation(operation)) {
        setInsightFeedbackError(error instanceof Error ? error.message : '反馈清空失败，请稍后重试。');
      }
    } finally {
      if (isCurrentItemOperation(operation)) {
        setIsSavingInsightFeedback(false);
      }
    }
  }

  async function clearChannelCorrectionOnly() {
    if (!item || !canEditInsightFeedback || isSavingInsightFeedback || !hasAnyChannelFeedback) {
      return;
    }

    const operation = startItemOperation('clearChannelFeedback');
    setIsSavingInsightFeedback(true);
    setInsightFeedbackError(null);
    try {
      await onChannelCorrectionClear(item);
      if (isCurrentItemOperation(operation)) {
        setChannelFeedbackDraft(EMPTY_CHANNEL_CORRECTION);
      }
    } catch (error) {
      console.error('Failed to clear feed item channel feedback', error);
      if (isCurrentItemOperation(operation)) {
        setInsightFeedbackError(error instanceof Error ? error.message : '频道纠偏清除失败，请稍后重试。');
      }
    } finally {
      if (isCurrentItemOperation(operation)) {
        setIsSavingInsightFeedback(false);
      }
    }
  }

  async function rebuildInsightEngineArtifacts() {
    if (!item || isRebuildingInsightEngineArtifactsBusy || !canRebuildInsightEngineArtifacts) {
      return;
    }

    const operation = startItemOperation('rebuildInsightEngineArtifacts');
    setIsRebuildingInsightEngineArtifacts(true);
    setInsightEngineArtifactActionMessage(null);
    setInsightEngineArtifactActionError(null);
    try {
      await onInsightEngineArtifactsRebuild(item);
      if (isCurrentItemOperation(operation)) {
        setInsightEngineArtifactActionMessage('洞察重建任务已在后端运行。');
      }
    } catch (error) {
      console.error('Failed to rebuild feed item Insight Engine artifacts', error);
      if (isCurrentItemOperation(operation)) {
        setInsightEngineArtifactActionError(error instanceof Error ? error.message : '洞察结果重建失败，请稍后重试。');
      }
    } finally {
      if (isCurrentItemOperation(operation)) {
        setIsRebuildingInsightEngineArtifacts(false);
      }
    }
  }

  async function clearAutoLowPriority() {
    if (!item || isClearingAutoLowPriority || item.lowPriorityBucketReason !== 'auto_score_threshold') {
      return;
    }

    const operation = startItemOperation('clearAutoLowPriority');
    setIsClearingAutoLowPriority(true);
    setInsightEngineArtifactActionMessage(null);
    setInsightEngineArtifactActionError(null);
    try {
      await onClearAutoLowPriority(item);
      if (isCurrentItemOperation(operation)) {
        setInsightEngineArtifactActionMessage('已取消低优先级。');
      }
    } catch (error) {
      console.error('Failed to clear auto low priority', error);
      if (isCurrentItemOperation(operation)) {
        setInsightEngineArtifactActionError(error instanceof Error ? error.message : '取消低优先级失败，请稍后重试。');
      }
    } finally {
      if (isCurrentItemOperation(operation)) {
        setIsClearingAutoLowPriority(false);
      }
    }
  }

  async function rebuildSavedItemInsight() {
    if (!item || view !== 'kb' || isRebuildingSavedItemInsightBusy) {
      return;
    }

    const operation = startItemOperation('rebuildSavedItemInsight');
    setIsRebuildingSavedItemInsight(true);
    setSavedInsightActionMessage(null);
    setSavedInsightActionError(null);
    try {
      await onSavedItemInsightRebuild(
        item,
        savedInsightRebuildRoute === 'saved_item' ? null : savedInsightRebuildRoute,
      );
      if (isCurrentItemOperation(operation)) {
        setSavedInsightActionMessage('保存洞察重建任务已在后端运行。');
      }
    } catch (error) {
      console.error('Failed to rebuild saved item insight', error);
      if (isCurrentItemOperation(operation)) {
        setSavedInsightActionError(error instanceof Error ? error.message : '保存洞察重建失败，请稍后重试。');
      }
    } finally {
      if (isCurrentItemOperation(operation)) {
        setIsRebuildingSavedItemInsight(false);
      }
    }
  }

  function startSavedInsightEdit() {
    if (!item) {
      return;
    }
    setSavedInsightDraft(buildSavedInsightDraft(item.resolvedInsight?.payload ?? null));
    setSavedInsightEditNote('');
    setSavedInsightEditError(null);
    setIsEditingSavedInsight(true);
  }

  async function saveSavedItemCuratedInsight() {
    if (!item || view !== 'kb' || isSavingSavedInsight) {
      return;
    }

    const basePayload = item.resolvedInsight?.payload ?? item.aiGeneratedInsight?.payload ?? null;
    const operation = startItemOperation('saveCuratedInsight');
    setIsSavingSavedInsight(true);
    setSavedInsightEditError(null);
    try {
      await onSavedItemCuratedInsightSave(item, {
        payload: buildSavedInsightPayload(savedInsightDraft, basePayload, item),
        editNote: savedInsightEditNote,
      });
      if (isCurrentItemOperation(operation)) {
        setIsEditingSavedInsight(false);
        setSavedInsightActionMessage('人工修订洞察已保存。');
        setSavedInsightActionError(null);
      }
    } catch (error) {
      console.error('Failed to save saved item curated insight', error);
      if (isCurrentItemOperation(operation)) {
        setSavedInsightEditError(error instanceof Error ? error.message : '人工修订洞察保存失败。');
      }
    } finally {
      if (isCurrentItemOperation(operation)) {
        setIsSavingSavedInsight(false);
      }
    }
  }

  async function clearSavedItemCuratedInsight() {
    if (!item || view !== 'kb' || isSavingSavedInsight) {
      return;
    }

    const operation = startItemOperation('clearCuratedInsight');
    setIsSavingSavedInsight(true);
    setSavedInsightEditError(null);
    try {
      await onSavedItemCuratedInsightClear(item);
      if (isCurrentItemOperation(operation)) {
        setIsEditingSavedInsight(false);
        setSavedInsightActionMessage('人工修订洞察已清除。');
        setSavedInsightActionError(null);
      }
    } catch (error) {
      console.error('Failed to clear saved item curated insight', error);
      if (isCurrentItemOperation(operation)) {
        setSavedInsightEditError(error instanceof Error ? error.message : '人工修订洞察清除失败。');
      }
    } finally {
      if (isCurrentItemOperation(operation)) {
        setIsSavingSavedInsight(false);
      }
    }
  }

  async function previewMarkdownArtifact(artifact: ArchiveArtifact) {
    const artifactKey = buildArchiveArtifactKey(artifact);
    const title = getArchiveArtifactLabel(artifact);
    const operation = startItemOperation('markdownPreview');
    if (!artifact.previewUrl) {
      setMarkdownPreview({
        artifactKey,
        title,
        content: '',
        isLoading: false,
        error: '该归档产物当前没有可预览内容。',
      });
      return;
    }

    setMarkdownPreview({
      artifactKey,
      title,
      content: '',
      isLoading: true,
      error: null,
    });

    try {
      const response = await fetch(artifact.previewUrl, {headers: {Accept: 'text/markdown,text/plain,*/*'}});
      if (!response.ok) {
        throw new Error(`Request failed: ${response.status} ${response.statusText}`);
      }
      const content = await response.text();
      if (isCurrentItemOperation(operation)) {
        setMarkdownPreview({
          artifactKey,
          title,
          content,
          isLoading: false,
          error: null,
        });
      }
    } catch (error) {
      console.error('Failed to preview archive markdown', error);
      if (isCurrentItemOperation(operation)) {
        setMarkdownPreview({
          artifactKey,
          title,
          content: '',
          isLoading: false,
          error: error instanceof Error ? error.message : 'Markdown 预览失败。',
        });
      }
    }
  }

  function openArtifactUrl(url: string | null) {
    if (!url) {
      return;
    }
    window.open(url, '_blank', 'noopener,noreferrer');
  }

  function toggleKnowledgePanel(panelKey: KnowledgeDetailPanelKey) {
    setCollapsedKnowledgePanels((current) => ({
      ...current,
      [panelKey]: !current[panelKey],
    }));
  }

  return (
    <section className="panel hidden flex-1 flex-col overflow-hidden lg:flex">
      {item ? (
        <>
          <div className="panel relative z-20 flex h-12 shrink-0 items-center justify-between border-b border-[var(--border-subtle)] px-6">
            <div className="flex items-center gap-3">
              <StatusBadge status={item.status} />
              <span className="text-xs text-[var(--text-soft)]">{formatAbsoluteDate(item.publishedAt)}</span>
            </div>

            <div className="flex items-center gap-2">
              {canChangeReaderState ? (
                <>
                  <ActionButton
                    active={Boolean(item.readFullAt)}
                    icon={<BookOpenCheck size={16} />}
                    label={item.readFullAt ? '取消已读' : '已读全文'}
                    onClick={() => onStatusChange(item, 'read_full')}
                  />
                </>
              ) : null}
              {canToggleSaved ? (
                <ActionButton
                  active={Boolean(item.savedAt) || isSaving}
                  disabled={isSaving}
                  icon={<Bookmark size={16} />}
                  label={isSaving ? '已保存' : item.savedAt ? '取消保存' : '保存'}
                  onClick={() => onStatusChange(item, 'saved')}
                />
              ) : null}
              {view !== 'kb' && !isReadOnly ? (
                <ActionButton
                  active={item.status === 'not_interested'}
                  icon={<ThumbsDown size={16} />}
                  label={item.status === 'not_interested' ? '恢复优先级' : '不感兴趣'}
                  onClick={() => onStatusChange(item, 'not_interested')}
                />
              ) : null}
              {view !== 'kb' && !isReadOnly && item.lowPriorityBucketReason === 'auto_score_threshold' ? (
                <ActionButton
                  disabled={isClearingAutoLowPriority}
                  icon={<Undo2 size={16} className={isClearingAutoLowPriority ? 'animate-pulse' : ''} />}
                  label={isClearingAutoLowPriority ? '处理中' : '取消低优先级'}
                  onClick={() => {
                    void clearAutoLowPriority();
                  }}
                />
              ) : null}
              {canEditInsightFeedback ? (
                <div className="relative flex items-center">
                  <ActionButton
                    active={isInsightFeedbackOpen || hasAnyFeedback}
                    icon={<MessageSquare size={16} />}
                    label="反馈"
                    onClick={() => {
                      setInsightFeedbackDraft(insightFeedbackValue);
                      setStructuredFeedbackDraft(cloneStructuredFeedback(structuredFeedbackValue));
                      setChannelFeedbackDraft(cloneChannelFeedback(channelFeedbackValue));
                      setInsightFeedbackError(null);
                      setIsInsightFeedbackOpen((current) => !current);
                    }}
                  />
                  {isInsightFeedbackOpen ? (
                    <InsightFeedbackPopover
                      channelFeedbackDraft={channelFeedbackDraft}
                      hasAnyChannelFeedback={hasAnyChannelFeedback}
                      hasAnyFeedback={hasAnyFeedback}
                      hasInsightFeedbackChanges={hasInsightFeedbackChanges}
                      insightChannels={insightChannels}
                      insightFeedbackDraft={insightFeedbackDraft}
                      insightFeedbackError={insightFeedbackError}
                      isAdvancedOpen={isInsightFeedbackAdvancedOpen}
                      isSaving={isSavingInsightFeedback}
                      normalizedChannelFeedbackDraft={normalizedChannelFeedbackDraft}
                      normalizedStructuredFeedbackDraft={normalizedStructuredFeedbackDraft}
                      structuredFeedbackDraft={structuredFeedbackDraft}
                      setChannelFeedbackDraft={setChannelFeedbackDraft}
                      setInsightFeedbackDraft={setInsightFeedbackDraft}
                      setIsAdvancedOpen={setIsInsightFeedbackAdvancedOpen}
                      setStructuredFeedbackDraft={setStructuredFeedbackDraft}
                      onCancel={() => {
                        setInsightFeedbackDraft(insightFeedbackValue);
                        setStructuredFeedbackDraft(cloneStructuredFeedback(structuredFeedbackValue));
                        setChannelFeedbackDraft(cloneChannelFeedback(channelFeedbackValue));
                        setInsightFeedbackError(null);
                        setIsInsightFeedbackOpen(false);
                      }}
                      onClearAll={() => {
                        void clearAllInsightFeedback();
                      }}
                      onClearChannelCorrection={() => {
                        void clearChannelCorrectionOnly();
                      }}
                      onSave={(nextFeedbackText, nextStructuredFeedback, nextChannelFeedback) => {
                        void saveInsightFeedback(nextFeedbackText, nextStructuredFeedback, nextChannelFeedback);
                      }}
                    />
                  ) : null}
                </div>
              ) : null}
              {canRebuildInsightEngineArtifacts ? (
                <ActionButton
                  disabled={isRebuildingInsightEngineArtifactsBusy}
                  icon={<Sparkles size={16} className={isRebuildingInsightEngineArtifactsBusy ? 'animate-pulse' : ''} />}
                  label={isRebuildingInsightEngineArtifactsBusy ? '重建中' : '重建洞察'}
                  onClick={() => {
                    void rebuildInsightEngineArtifacts();
                  }}
                />
              ) : null}
              {canExtractContent ? (
                <ActionButton
                  disabled={isExtractingContentBusy}
                  icon={<RefreshCw size={16} className={isExtractingContentBusy ? 'animate-spin' : ''} />}
                  label={isExtractingContentBusy ? '提取中' : contentExtractionActionLabel}
                  onClick={() => {
                    void reportAndExtractContent();
                  }}
                />
              ) : null}
              <div className="mx-1 h-4 w-px bg-[var(--border-subtle)]" />
              <ActionButton
                disabled={!item.originalUrl}
                icon={<ExternalLink size={16} />}
                label="原始链接"
                onClick={() => onOpenOriginal(item)}
              />
              <ActionButton
                disabled={!item.sourceAppUrl}
                icon={<Smartphone size={16} />}
                label="来源站点"
                onClick={() => onOpenSourceApp(item)}
              />
            </div>
          </div>

          <div className="flex-1 overflow-y-auto px-10 py-8">
            <div className="mx-auto max-w-3xl">
              {isReadOnly ? (
                <div className="mb-4 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-xs text-[var(--text-muted)]">
                  当前详情来自上一组筛选结果，已切换为只读。
                </div>
              ) : null}
              {isUnavailable ? (
                <div className="mb-4 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-xs text-[var(--text-muted)]">
                  当前条目的完整详情不可读取，正在显示列表中保留的只读信息。
                </div>
              ) : null}
              {detailErrorMessage ? (
                <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                  {detailErrorMessage}
                </div>
              ) : null}

              <div className="mb-6">
                <div className="mb-3 flex items-center gap-2">
                  <span className="text-sm font-medium text-[var(--accent)]">{item.sourceName}</span>
                  <span className="text-[var(--text-soft)]">•</span>
                  <span className="text-sm text-[var(--text-muted)]">{item.sourceAppName}</span>
                  {view !== 'kb' ? <ContentExtractionPill item={item} /> : null}
                  {item.topics.length ? (
                    <div className="ml-2 flex flex-wrap gap-2">
                      {item.topics.map((topic) => (
                        <span
                          key={topic}
                          className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-2 py-0.5 text-[11px] uppercase tracking-[0.14em] text-[var(--text-soft)]"
                        >
                          {topic}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>

                {view === 'ai' && item.priorityReasons.length ? (
                  <div className="mb-4 flex flex-wrap gap-2">
                    {buildHomepageMeta(item).map((meta) => (
                      <span
                        key={meta}
                        className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-1 text-xs text-[var(--text-soft)]"
                      >
                        {meta}
                      </span>
                    ))}
                    {item.priorityReasons.map((reason) => (
                      <span
                        key={reason.code}
                        className="rounded-full border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-1 text-xs text-[var(--text-muted)]"
                      >
                        {reason.label} +{reason.weight}
                      </span>
                    ))}
                  </div>
                ) : null}

                <h1 className="mb-4 text-3xl font-bold leading-tight text-[var(--text-main)]">{item.title}</h1>

                {view === 'kb' && isUploadSavedItem(item) ? (
                  <UploadedMaterialMetadata item={item} onOpenArtifactUrl={openArtifactUrl} />
                ) : null}

                {view !== 'kb' && shouldShowFeedCaptureSummaryCard(item) ? <FeedCaptureSummaryCard item={item} /> : null}

                {renderedSummaryHtml ? (
                  <div className="relative mt-4 overflow-hidden rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-5">
                    <div className="absolute bottom-0 left-0 top-0 w-1 bg-[var(--accent)]" />
                    <div className="mb-2 text-xs font-semibold text-[var(--text-soft)]">摘要</div>
                    <div
                      className="reader-prose reader-prose-summary text-sm text-[var(--text-main)]"
                      dangerouslySetInnerHTML={{__html: renderedSummaryHtml}}
                    />
                  </div>
                ) : null}

                {view !== 'kb' && (item.contentExtractionError || contentExtractionActionError) ? (
                  <div className="mt-3 rounded-lg border border-red-500/20 bg-red-500/5 px-3 py-2 text-xs text-red-500">
                    {contentExtractionActionError ?? item.contentExtractionError}
                  </div>
                ) : null}

                {view === 'ai' && (insightEngineArtifactActionMessage || insightEngineArtifactActionError) ? (
                  <div
                    className={`mt-3 rounded-lg border px-3 py-2 text-xs ${
                      insightEngineArtifactActionError
                        ? 'border-red-500/20 bg-red-500/5 text-red-500'
                        : 'border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] text-[var(--text-muted)]'
                    }`}
                  >
                    {insightEngineArtifactActionError ?? insightEngineArtifactActionMessage}
                  </div>
                ) : null}

                {view === 'kb' ? (
                  <div className="mt-4 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-4">
                    <div className="mb-2 flex items-center justify-between gap-3">
                      <div className="text-xs font-semibold uppercase tracking-[0.18em] text-[var(--text-soft)]">备注</div>
                      <button
                        className="rounded-md bg-[var(--accent)] px-3 py-1.5 text-xs font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-45"
                        disabled={!hasNoteChanges || isSavingNote}
                        type="button"
                        onClick={() => {
                          void saveNote();
                        }}
                      >
                        {isSavingNote ? '保存中...' : '保存'}
                      </button>
                    </div>
                    <textarea
                      className="min-h-24 w-full resize-y rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm leading-relaxed text-[var(--text-main)] outline-none transition-colors focus:border-[var(--accent)]"
                      placeholder="为这条知识记录添加私人备注..."
                      value={noteDraft}
                      onChange={(event) => setNoteDraft(event.target.value)}
                    />
                    {noteError ? <div className="mt-2 text-xs text-red-500">{noteError}</div> : null}
                  </div>
                ) : null}

                {view === 'kb' ? (
                  <SavedItemInsightPanel
                    actionError={savedInsightActionError}
                    actionMessage={savedInsightActionMessage}
                    draft={savedInsightDraft}
                    editError={savedInsightEditError}
                    editNote={savedInsightEditNote}
                    isEditing={isEditingSavedInsight}
                    isRebuilding={isRebuildingSavedItemInsightBusy}
                    isSaving={isSavingSavedInsight}
                    item={item}
                    operation={detailOperations.savedItemInsightOperation}
                    rebuildRoute={savedInsightRebuildRoute}
                    onCancelEdit={() => {
                      setIsEditingSavedInsight(false);
                      setSavedInsightEditError(null);
                    }}
                    onClear={() => {
                      void clearSavedItemCuratedInsight();
                    }}
                    onDraftChange={setSavedInsightDraft}
                    onEdit={startSavedInsightEdit}
                    onEditNoteChange={setSavedInsightEditNote}
                    onRebuild={() => {
                      void rebuildSavedItemInsight();
                    }}
                    onRebuildRouteChange={setSavedInsightRebuildRoute}
                    onSave={() => {
                      void saveSavedItemCuratedInsight();
                    }}
                  />
                ) : null}

                {view !== 'kb' && item.note ? (
                  <div className="mt-4 rounded-2xl border border-dashed border-[var(--border-subtle)] px-4 py-3 text-sm text-[var(--text-muted)]">
                    <span className="font-medium text-[var(--text-main)]">备注：</span>
                    {item.note}
                  </div>
                ) : null}

                {view === 'kb' ? (
                    <KnowledgeDetailPanels
                      archiveArtifacts={archiveArtifacts}
                      archiveStatusMessage={archiveStatusMessage}
                      canRunCapture={canRunCapture}
                      canManualImportCapture={canManualImportCapture}
                      captureOperation={captureOperation}
                      collapsedKnowledgePanels={collapsedKnowledgePanels}
                      coreArchiveArtifacts={coreArchiveArtifacts}
                      hasMirrorExportableArtifacts={hasMirrorExportableArtifacts}
                      imageArchiveArtifacts={imageArchiveArtifacts}
                      isCaptureRunning={isCaptureRunning}
                      isMirrorReexportBlocked={isMirrorReexportBlocked}
                      isReexportingMirrorBusy={isReexportingMirrorBusy}
                      item={item}
                      markdownPreview={markdownPreview}
                      mirrorError={mirrorError}
                      primaryMarkdownArtifact={primaryMarkdownArtifact}
                      onCloseMarkdownPreview={() => setMarkdownPreview(null)}
                      onOpenArtifactUrl={openArtifactUrl}
                      onPreviewMarkdownArtifact={(artifact) => {
                        void previewMarkdownArtifact(artifact);
                      }}
                      onReexportLocalMirror={() => {
                        void reexportLocalMirror();
                      }}
                      onRetryCapture={() => {
                        void retryCapture();
                      }}
                      onManualImport={(input) => onSavedItemManualImport(item, input)}
                      onTogglePanel={toggleKnowledgePanel}
                    />
                ) : null}
              </div>

              {view !== 'kb' && renderedContentHtml ? (
                <article className="reader-prose reader-prose-body" dangerouslySetInnerHTML={{__html: renderedContentHtml}} />
              ) : view !== 'kb' ? (
                <div className="rounded-2xl border border-dashed border-[var(--border-subtle)] px-6 py-8 text-sm text-[var(--text-muted)]">
                  当前条目还没有可展示的正文，后续会在刷新和快照阶段继续补全。
                </div>
              ) : null}

              {view !== 'kb' && canChangeReaderState ? (
                <div className="mt-8 flex justify-center border-t border-[var(--border-subtle)] pt-5">
                  <button
                    className={`inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm font-medium transition-colors ${
                      item.readFullAt
                        ? 'border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]'
                        : 'border-[var(--border-subtle)] text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]'
                    }`}
                    type="button"
                    onClick={() => onStatusChange(item, 'read_full')}
                  >
                    <BookOpenCheck size={15} />
                    {item.readFullAt ? '取消已读' : '已读全文'}
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        </>
      ) : (
        <div className="flex flex-1 flex-col items-center justify-center gap-4 text-center text-[var(--text-muted)]">
          <BookOpen size={48} className="opacity-20" />
          <p className="text-sm">
            {isUnavailable ? '当前条目不可读取，可能已被删除或不在当前数据集中。' : '请在左侧选择一条内容进行阅读'}
          </p>
        </div>
      )}
    </section>
  );
}

function ActionButton({
  active = false,
  disabled = false,
  icon,
  label,
  onClick,
}: {
  active?: boolean;
  disabled?: boolean;
  icon: ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      className={`flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm transition-colors ${
        active
          ? 'bg-[var(--accent-soft)] text-[var(--accent)]'
          : 'text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]'
      } disabled:cursor-not-allowed disabled:opacity-60`}
      disabled={disabled}
      type="button"
      onClick={onClick}
    >
      {icon}
      <span className="hidden xl:inline">{label}</span>
    </button>
  );
}

function buildHomepageMeta(item: FeedItem) {
  const metadata: string[] = [];

  if (item.summarySource === 'insight_engine_artifact') {
    metadata.push('洞察摘要');
  } else if (item.summarySource === 'canonical_summary') {
    metadata.push('原始摘要');
  } else if (item.summarySource === 'canonical_content') {
    metadata.push('正文截取');
  }

  if (item.topicSource === 'insight_engine_artifact') {
    metadata.push('洞察主题');
  } else if (item.topicSource === 'topic_hints') {
    metadata.push('Topic Hint');
  } else if (item.topicSource === 'source_folder') {
    metadata.push('来源目录');
  }

  if (item.prioritySource === 'deterministic_fallback') {
    metadata.push('2A Fallback');
  } else if (item.prioritySource === 'insight_engine_artifact') {
    metadata.push('洞察排序');
  } else if (item.prioritySource === 'backend_scoring_v1') {
    metadata.push('Scoring v1');
  }

  if (item.artifactGeneratedAt) {
    metadata.push(`生成于 ${formatAbsoluteDate(item.artifactGeneratedAt)}`);
  }

  return metadata;
}
