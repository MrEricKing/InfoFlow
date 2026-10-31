import {type Dispatch, type SetStateAction, useEffect, useMemo, useRef, useState} from 'react';
import {
  getNextOperationPollDelay,
  getOperationListPollSignature,
  getOperationPollSignature,
  OPERATION_POLL_INITIAL_DELAY_MS,
} from '@/lib/operation-polling';
import {
  getActiveOperation,
  getActiveOperations,
  getOperation,
  startFeedItemInsightArtifactOperation,
  startFeedItemInsightArtifactBatchOperation,
  startSourceSyncOperation,
  stopOperation,
} from '@/services/operation-service';
import {listMissingFeedItemInsightArtifactCandidates} from '@/services/homepage-service';
import {ApiError as OperationApiError} from '@/api/errors';
import {getFeedItemDetail} from '@/services/feed-service';
import {getSavedItemDetail} from '@/services/knowledge-service';
import {buildInsightArtifactFillPlan} from '@/hooks/info-flow/insight-artifact-fill';
import type {
  FeedItem,
  HomepageSummary,
  SourceScope,
  ViewMode,
} from '@/types/feed';
import type {
  AppOperationDto,
  FeedItemInsightArtifactBatchOperationState,
  SourceDto,
  SourceSyncOperationState,
  SourceSyncProgressState,
} from '@/types/source';
import {
  adaptFeedItemInsightArtifactBatchOperation,
  adaptOperationProgress,
  adaptSourceSyncOperation,
  extractConflictOperation,
  getOperationFeedItemId,
  getOperationSavedItemId,
  isFeedItemInsightArtifactBatchOperation,
  isFeedItemInsightArtifactItemOperation,
  isItemOperation,
  isOperationActiveStatus,
  isOperationTerminalStatus,
  mergeFeedItem,
} from '@/hooks/info-flow/helpers';

interface UseInfoFlowOperationsArgs {
  view: ViewMode;
  sourceScope: SourceScope;
  reloadSources: () => Promise<SourceDto[]>;
  reloadInsightChannels: () => Promise<unknown>;
  refreshHomepageSummary: () => Promise<HomepageSummary>;
  invalidateHomepageSnapshotWindow: () => void;
  reloadReaderItems: () => Promise<FeedItem[]>;
  reloadSavedItems: (options?: {query?: string}) => Promise<FeedItem[]>;
  getSavedQuery: () => string;
  getSelectedId: () => number | null;
  getView: () => ViewMode;
  setFeedDetailItems: Dispatch<SetStateAction<Record<number, FeedItem>>>;
  setSavedDetailItems: Dispatch<SetStateAction<Record<number, FeedItem>>>;
}

export function useInfoFlowOperations({
  view,
  sourceScope,
  reloadSources,
  reloadInsightChannels,
  refreshHomepageSummary,
  invalidateHomepageSnapshotWindow,
  reloadReaderItems,
  reloadSavedItems,
  getSavedQuery,
  getSelectedId,
  getView,
  setFeedDetailItems,
  setSavedDetailItems,
}: UseInfoFlowOperationsArgs) {
  const [activeItemOperations, setActiveItemOperations] = useState<Record<number, AppOperationDto>>({});
  const [latestItemOperations, setLatestItemOperations] = useState<Record<number, AppOperationDto>>({});
  const [feedItemArtifactRefreshVersion, setFeedItemArtifactRefreshVersion] = useState(0);
  const [isRebuildingInsightEngineArtifacts, setIsRebuildingInsightEngineArtifacts] = useState(false);
  const [isInsightEngineArtifactStopRequesting, setIsInsightEngineArtifactStopRequesting] = useState(false);
  const [insightEngineArtifactMessage, setInsightEngineArtifactMessage] = useState<string | null>(null);
  const [insightEngineArtifactOperation, setInsightEngineArtifactOperation] =
    useState<FeedItemInsightArtifactBatchOperationState | null>(null);
  const [isSyncingSources, setIsSyncingSources] = useState(false);
  const [isSourceSyncStopRequesting, setIsSourceSyncStopRequesting] = useState(false);
  const [sourceSyncOperation, setSourceSyncOperation] = useState<SourceSyncOperationState | null>(null);
  const [sourceSyncMessage, setSourceSyncMessage] = useState<string | null>(null);
  const [sourceSyncProgress, setSourceSyncProgress] = useState<SourceSyncProgressState | null>(null);
  const [insightEngineArtifactProgress, setInsightEngineArtifactProgress] =
    useState<SourceSyncProgressState | null>(null);

  const activeSourceSyncOperationRef = useRef<SourceSyncOperationState | null>(null);
  const activeInsightEngineArtifactOperationRef = useRef<FeedItemInsightArtifactBatchOperationState | null>(null);
  const terminalOperationRefreshRef = useRef<Set<number>>(new Set());
  const activeItemOperationsRef = useRef<Record<number, AppOperationDto>>({});

  useEffect(() => {
    let isCancelled = false;

    async function restoreActiveOperation() {
      try {
        const operation = await getActiveOperation();
        if (!isCancelled && operation) {
          applyBackendOperation(operation);
        }
      } catch (error) {
        console.error('Failed to restore active operation', error);
      }
    }

    void restoreActiveOperation();

    return () => {
      isCancelled = true;
    };
  }, []);

  useEffect(() => {
    let isCancelled = false;

    async function restoreActiveItemOperations() {
      try {
        const operations = await getActiveOperations();
        if (!isCancelled) {
          applyActiveItemOperations(operations);
        }
      } catch (error) {
        console.error('Failed to restore active item operations', error);
      }
    }

    void restoreActiveItemOperations();

    return () => {
      isCancelled = true;
    };
  }, []);

  useEffect(() => {
    activeItemOperationsRef.current = activeItemOperations;
  }, [activeItemOperations]);

  const activeItemOperationIds = useMemo(() => Object.keys(activeItemOperations).join(','), [activeItemOperations]);
  const activeBackendOperationIds = useMemo(
    () =>
      [
        sourceSyncOperation && isOperationActiveStatus(sourceSyncOperation.status) ? sourceSyncOperation.id : null,
        insightEngineArtifactOperation && isOperationActiveStatus(insightEngineArtifactOperation.status)
          ? insightEngineArtifactOperation.id
          : null,
      ]
        .filter((id): id is number => id !== null)
        .sort((left, right) => left - right),
    [insightEngineArtifactOperation, sourceSyncOperation],
  );
  const activeBackendOperationIdSignature = activeBackendOperationIds.join(',');
  const activeOperationPollSignature = [activeItemOperationIds, activeBackendOperationIdSignature]
    .filter(Boolean)
    .join('|');

  useEffect(() => {
    let isCancelled = false;

    async function refreshActiveItemOperations() {
      try {
        const operations = await getActiveOperations();
        if (!isCancelled) {
          applyActiveItemOperations(operations);
        }
      } catch (error) {
        console.error('Failed to refresh active item operations', error);
      }
    }

    function handleVisibleFocus() {
      if (document.visibilityState !== 'hidden') {
        void refreshActiveItemOperations();
      }
    }

    window.addEventListener('focus', handleVisibleFocus);
    document.addEventListener('visibilitychange', handleVisibleFocus);

    return () => {
      isCancelled = true;
      window.removeEventListener('focus', handleVisibleFocus);
      document.removeEventListener('visibilitychange', handleVisibleFocus);
    };
  }, []);

  useEffect(() => {
    if (!activeOperationPollSignature) {
      return;
    }

    let isCancelled = false;
    let timeoutId: number | null = null;
    let pollDelay = OPERATION_POLL_INITIAL_DELAY_MS;
    let lastSignature = '';
    let isPolling = false;

    function clearPollTimeout() {
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
        timeoutId = null;
      }
    }

    function schedulePoll(delay = pollDelay) {
      clearPollTimeout();
      if (isCancelled || document.visibilityState === 'hidden') {
        return;
      }
      timeoutId = window.setTimeout(() => {
        void pollActiveItemOperations();
      }, delay);
    }

    async function pollActiveItemOperations() {
      if (isPolling || isCancelled || document.visibilityState === 'hidden') {
        return;
      }
      isPolling = true;
      try {
        const operations = await getActiveOperations();
        if (!isCancelled) {
          const signature = getOperationListPollSignature(operations);
          const changed = signature !== lastSignature;
          lastSignature = signature;
          pollDelay = getNextOperationPollDelay(pollDelay, changed);
          applyActiveItemOperations(operations);
        }
      } catch (error) {
        console.error('Failed to poll active item operations', error);
        pollDelay = getNextOperationPollDelay(pollDelay, false);
      } finally {
        isPolling = false;
        schedulePoll();
      }
    }

    function handleVisiblePoll() {
      if (document.visibilityState !== 'hidden') {
        pollDelay = OPERATION_POLL_INITIAL_DELAY_MS;
        clearPollTimeout();
        void pollActiveItemOperations();
      }
    }

    document.addEventListener('visibilitychange', handleVisiblePoll);
    schedulePoll(OPERATION_POLL_INITIAL_DELAY_MS);

    return () => {
      isCancelled = true;
      clearPollTimeout();
      document.removeEventListener('visibilitychange', handleVisiblePoll);
    };
  }, [activeOperationPollSignature]);

  useEffect(() => {
    if (activeBackendOperationIds.length === 0) {
      return;
    }

    let isCancelled = false;
    let timeoutId: number | null = null;
    let pollDelay = OPERATION_POLL_INITIAL_DELAY_MS;
    let lastSignature = '';
    let isPolling = false;
    const reachedTerminalOperationIds = new Set<number>();
    const operationIds = activeBackendOperationIds;

    function clearPollTimeout() {
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
        timeoutId = null;
      }
    }

    function schedulePoll(delay = pollDelay) {
      clearPollTimeout();
      if (isCancelled || reachedTerminalOperationIds.size === operationIds.length || document.visibilityState === 'hidden') {
        return;
      }
      timeoutId = window.setTimeout(() => {
        void pollOperation();
      }, delay);
    }

    async function pollOperation() {
      if (
        isPolling ||
        isCancelled ||
        reachedTerminalOperationIds.size === operationIds.length ||
        document.visibilityState === 'hidden'
      ) {
        return;
      }
      isPolling = true;
      try {
        const operations = await Promise.all(operationIds.map((operationId) => getOperation(operationId)));
        if (isCancelled) {
          return;
        }
        const signature = operations.map((operation) => getOperationPollSignature(operation)).join('|');
        const changed = signature !== lastSignature;
        lastSignature = signature;
        pollDelay = getNextOperationPollDelay(pollDelay, changed);
        for (const operation of operations) {
          applyBackendOperation(operation);
          if (isOperationTerminalStatus(operation.status)) {
            reachedTerminalOperationIds.add(operation.id);
            await refreshAfterTerminalOperation(operation);
          }
        }
      } catch (error) {
        console.error('Failed to poll operation', error);
        pollDelay = getNextOperationPollDelay(pollDelay, false);
      } finally {
        isPolling = false;
        schedulePoll();
      }
    }

    function handleVisiblePoll() {
      if (document.visibilityState !== 'hidden') {
        pollDelay = OPERATION_POLL_INITIAL_DELAY_MS;
        clearPollTimeout();
        void pollOperation();
      }
    }

    void pollOperation();
    document.addEventListener('visibilitychange', handleVisiblePoll);

    return () => {
      isCancelled = true;
      clearPollTimeout();
      document.removeEventListener('visibilitychange', handleVisiblePoll);
    };
  }, [activeBackendOperationIdSignature]);

  function applyBackendOperation(operation: AppOperationDto | null) {
    if (!operation) {
      return;
    }

    if (isItemOperation(operation)) {
      trackItemOperation(operation);
      return;
    }

    const progress = adaptOperationProgress(operation);
    if (operation.operation_type === 'source_sync') {
      const nextOperation = adaptSourceSyncOperation(operation);
      const isActive = isOperationActiveStatus(operation.status);
      activeSourceSyncOperationRef.current = isActive ? nextOperation : null;
      setSourceSyncOperation(nextOperation);
      setIsSyncingSources(isActive);
      if (operation.stop_requested || isOperationTerminalStatus(operation.status)) {
        setIsSourceSyncStopRequesting(false);
      }
      setSourceSyncProgress(isActive ? progress : null);
      if (isOperationTerminalStatus(operation.status)) {
        setSourceSyncMessage(buildTerminalOperationMessage(operation, '来源刷新任务已结束。'));
      }
      return;
    }

    if (!isFeedItemInsightArtifactBatchOperation(operation)) {
      return;
    }

    const nextOperation = adaptFeedItemInsightArtifactBatchOperation(operation);
    const isActive = isOperationActiveStatus(operation.status);
    activeInsightEngineArtifactOperationRef.current = isActive ? nextOperation : null;
    setInsightEngineArtifactOperation(nextOperation);
    setIsRebuildingInsightEngineArtifacts(isActive);
    if (operation.stop_requested || isOperationTerminalStatus(operation.status)) {
      setIsInsightEngineArtifactStopRequesting(false);
    }
    setInsightEngineArtifactProgress(isActive ? progress : null);
    const linkedSourceSyncOperationId = getLinkedSourceSyncOperationId(operation);
    if (linkedSourceSyncOperationId !== null) {
      void refreshLinkedSourceSyncOperation(linkedSourceSyncOperationId);
    }
    if (isOperationTerminalStatus(operation.status)) {
      setInsightEngineArtifactMessage(buildTerminalOperationMessage(operation, 'Feed item insight artifact operation finished.'));
    }
  }

  function buildTerminalOperationMessage(operation: AppOperationDto, fallback: string) {
    return operation.message || operation.error_message || fallback;
  }

  function getLinkedSourceSyncOperationId(operation: AppOperationDto) {
    if (
      operation.operation_type !== 'feed_item_insight_artifacts' ||
      operation.operation_kind !== 'source_sync_created_feed_items'
    ) {
      return null;
    }
    const sourceSyncOperationId = operation.scope?.source_sync_operation_id;
    return typeof sourceSyncOperationId === 'number' && Number.isInteger(sourceSyncOperationId) ? sourceSyncOperationId : null;
  }

  async function refreshLinkedSourceSyncOperation(operationId: number) {
    try {
      const sourceOperation = await getOperation(operationId);
      if (sourceOperation.operation_type === 'source_sync') {
        applyBackendOperation(sourceOperation);
      }
    } catch (error) {
      console.error('Failed to refresh linked source refresh operation', error);
    }
  }

  function trackBackendOperation(operation: AppOperationDto) {
    terminalOperationRefreshRef.current.delete(operation.id);
    applyBackendOperation(operation);
  }

  function trackItemOperation(operation: AppOperationDto) {
    rememberItemOperations([operation]);
    if (!isOperationActiveStatus(operation.status)) {
      setActiveItemOperations((current) => {
        const next = {...current};
        delete next[operation.id];
        return next;
      });
      void refreshAfterItemOperation(operation);
      return;
    }

    setActiveItemOperations((current) => ({...current, [operation.id]: operation}));
  }

  function applyActiveItemOperations(operations: AppOperationDto[]) {
    for (const operation of operations) {
      if (!isItemOperation(operation) && isOperationActiveStatus(operation.status)) {
        applyBackendOperation(operation);
      }
    }

    const nextEntries = operations.filter((operation) => isItemOperation(operation) && isOperationActiveStatus(operation.status));
    rememberItemOperations(nextEntries);
    const next = Object.fromEntries(nextEntries.map((operation) => [operation.id, operation]));
    const previous = activeItemOperationsRef.current;
    const removed = Object.values(previous).filter((operation) => !(operation.id in next));
    setActiveItemOperations(next);
    if (removed.length) {
      void resolveRemovedItemOperations(removed);
    }
  }

  function rememberItemOperations(operations: AppOperationDto[]) {
    if (operations.length === 0) {
      return;
    }
    setLatestItemOperations((current) => {
      const next = {...current};
      for (const operation of operations) {
        if (isItemOperation(operation)) {
          next[operation.id] = operation;
        }
      }
      return next;
    });
  }

  async function resolveRemovedItemOperations(operations: AppOperationDto[]) {
    await Promise.all(
      operations.map(async (operation) => {
        try {
          const latestOperation = await getOperation(operation.id);
          trackItemOperation(latestOperation);
        } catch (error) {
          console.error('Failed to resolve removed item operation', error);
          await refreshAfterItemOperation(operation);
        }
      }),
    );
  }

  async function refreshAfterItemOperation(operation: AppOperationDto) {
    const savedItemId = getOperationSavedItemId(operation);
    const feedItemId = getOperationFeedItemId(operation);
    const affectsHomepageReadModel =
      operation.operation_type === 'feed_item_extract' || isFeedItemInsightArtifactItemOperation(operation);
    if (affectsHomepageReadModel) {
      invalidateHomepageSnapshotWindow();
    }
    await Promise.all([
      affectsHomepageReadModel
        ? refreshHomepageSummary().catch((error) => console.error('Failed to refresh homepage summary after item operation', error))
        : Promise.resolve(),
      affectsHomepageReadModel
        ? reloadReaderItems().catch((error) => console.error('Failed to reload reader after item operation', error))
        : Promise.resolve(),
      savedItemId !== null || getView() === 'kb'
        ? reloadSavedItems({query: getSavedQuery()}).catch((error) =>
            console.error('Failed to reload saved items after item operation', error),
          )
        : Promise.resolve(),
    ]);

    if (feedItemId !== null && getSelectedId() === feedItemId && getView() !== 'kb') {
      getFeedItemDetail(feedItemId)
        .then((detailItem) => {
          setFeedDetailItems((current) => ({
            ...current,
            [feedItemId]: mergeFeedItem(current[feedItemId], detailItem),
          }));
        })
        .catch((error) => console.error('Failed to refresh feed detail after item operation', error));
    }
    if (savedItemId !== null && getSelectedId() === savedItemId && getView() === 'kb') {
      getSavedItemDetail(savedItemId)
        .then((detailItem) => {
          setSavedDetailItems((current) => ({
            ...current,
            [savedItemId]: mergeFeedItem(current[savedItemId], detailItem),
          }));
        })
        .catch((error) => console.error('Failed to refresh saved detail after item operation', error));
    }
    if (isFeedItemInsightArtifactItemOperation(operation)) {
      setFeedItemArtifactRefreshVersion((current) => current + 1);
    }
  }

  function applyOperationConflict(error: unknown, messageTarget: 'source_sync' | 'feed_item_insight_artifacts') {
    const operation = extractConflictOperation(error);
    if (!operation) {
      return false;
    }

    applyBackendOperation(operation);
    const message =
      operation.operation_type === 'source_sync'
        ? '当前已有来源刷新任务正在运行。'
        : '当前已有洞察构建任务正在运行。';
    if (messageTarget === 'source_sync') {
      setSourceSyncMessage(message);
    } else {
      setInsightEngineArtifactMessage(message);
    }
    return true;
  }

  async function refreshAfterTerminalOperation(operation: AppOperationDto) {
    if (!isOperationTerminalStatus(operation.status) || terminalOperationRefreshRef.current.has(operation.id)) {
      return;
    }

    terminalOperationRefreshRef.current.add(operation.id);
    invalidateHomepageSnapshotWindow();
    await Promise.all([
      reloadSources().catch((error) => console.error('Failed to reload sources after operation', error)),
      reloadInsightChannels().catch((error) => console.error('Failed to reload insight channels after operation', error)),
      refreshHomepageSummary().catch((error) => console.error('Failed to refresh homepage summary after operation', error)),
      reloadReaderItems().catch((error) => console.error('Failed to reload reader after operation', error)),
      getView() === 'kb'
        ? reloadSavedItems({query: getSavedQuery()}).catch((error) =>
            console.error('Failed to reload knowledge base after operation', error),
          )
        : Promise.resolve(),
    ]);
    if (operation.operation_type === 'source_sync') {
      try {
        applyActiveItemOperations(await getActiveOperations());
      } catch (error) {
        console.error('Failed to discover follow-up operation after source refresh', error);
      }
    }
  }

  async function startAutoCreateSourceSync(source: SourceDto, sourceName: string) {
    try {
      const operation = await startSourceSyncOperation({
        kind: 'auto_create',
        source_id: source.id,
      });
      terminalOperationRefreshRef.current.delete(operation.id);
      setSourceSyncMessage(null);
      applyBackendOperation(operation);
    } catch (error) {
      if (!applyOperationConflict(error, 'source_sync')) {
        throw error;
      }
      setSourceSyncMessage(`已添加订阅：${sourceName}。当前已有任务运行，未自动刷新新来源。`);
    }
  }

  async function syncSourcesNow(options?: {scope?: 'current_view' | 'all'}) {
    const shouldUseCurrentSource =
      options?.scope !== 'all' && (view === 'ai' || view === 'reader') && sourceScope.kind === 'source';
    const sourceId = shouldUseCurrentSource && sourceScope.kind === 'source' ? Number(sourceScope.value) : null;

    try {
      const operation = await startSourceSyncOperation({
        kind: shouldUseCurrentSource ? 'single' : 'all',
        source_id: sourceId,
      });
      terminalOperationRefreshRef.current.delete(operation.id);
      setSourceSyncMessage(null);
      applyBackendOperation(operation);
    } catch (error) {
      console.error('Failed to sync sources', error);
      if (applyOperationConflict(error, 'source_sync')) {
        return;
      }
      setSourceSyncMessage(error instanceof Error ? `刷新失败：${error.message}` : '刷新失败，请查看后端日志。');
    }
  }

  async function stopSourcesSyncNow() {
    const operation = activeSourceSyncOperationRef.current;
    if (!operation) {
      setSourceSyncMessage('当前没有正在运行的来源刷新。');
      return;
    }
    if (operation.stopRequested || isSourceSyncStopRequesting) {
      return;
    }
    setIsSourceSyncStopRequesting(true);
    setSourceSyncMessage('正在请求停止刷新。');
    const optimisticOperation = {...operation, status: 'stopping' as const, stopRequested: true};
    activeSourceSyncOperationRef.current = optimisticOperation;
    setSourceSyncOperation(optimisticOperation);
    setSourceSyncProgress((current) =>
      current
        ? {
            ...current,
            stage: current.stage === 'completed' || current.stage === 'stopped' ? current.stage : 'stopping',
            stopRequested: true,
          }
        : current,
    );
    try {
      const stoppedOperation = await stopOperation(operation.id);
      applyBackendOperation(stoppedOperation);
      setSourceSyncMessage(stoppedOperation.message || '停止请求已提交，等待当前步骤结束。');
    } catch (error) {
      console.error('Failed to request source refresh stop', error);
      setSourceSyncMessage(error instanceof Error ? `停止刷新请求失败：${error.message}` : '停止刷新请求失败。');
      setIsSourceSyncStopRequesting(false);
      activeSourceSyncOperationRef.current = operation;
      setSourceSyncOperation(operation);
      setSourceSyncProgress((current) => (current ? {...current, stopRequested: false} : current));
      throw error;
    }
  }

  async function stopInsightEngineArtifactsNow() {
    const operation = activeInsightEngineArtifactOperationRef.current;
    if (!operation) {
      setInsightEngineArtifactMessage('当前没有正在运行的洞察补齐任务。');
      return;
    }
    if (operation.stopRequested || isInsightEngineArtifactStopRequesting) {
      return;
    }
    setIsInsightEngineArtifactStopRequesting(true);
    setInsightEngineArtifactMessage('正在请求停止洞察补齐。');
    const optimisticOperation = {...operation, status: 'stopping' as const, stopRequested: true};
    activeInsightEngineArtifactOperationRef.current = optimisticOperation;
    setInsightEngineArtifactOperation(optimisticOperation);
    setInsightEngineArtifactProgress((current) =>
      current
        ? {
            ...current,
            stage: current.stage === 'completed' || current.stage === 'stopped' ? current.stage : 'stopping',
            stopRequested: true,
          }
        : current,
    );
    try {
      const stoppedOperation = await stopOperation(operation.id);
      applyBackendOperation(stoppedOperation);
      setInsightEngineArtifactMessage(stoppedOperation.message || '停止请求已提交，等待当前步骤结束。');
    } catch (error) {
      console.error('Failed to request Insight Engine artifact stop', error);
      setInsightEngineArtifactMessage(error instanceof Error ? `停止洞察补齐失败：${error.message}` : '停止洞察补齐失败。');
      setIsInsightEngineArtifactStopRequesting(false);
      activeInsightEngineArtifactOperationRef.current = operation;
      setInsightEngineArtifactOperation(operation);
      setInsightEngineArtifactProgress((current) => (current ? {...current, stopRequested: false} : current));
      throw error;
    }
  }

  async function syncSingleSourceNow(source: SourceDto) {
    try {
      const operation = await startSourceSyncOperation({
        kind: 'single',
        source_id: source.id,
      });
      terminalOperationRefreshRef.current.delete(operation.id);
      setSourceSyncMessage(null);
      applyBackendOperation(operation);
    } catch (error) {
      console.error('Failed to sync source', error);
      if (applyOperationConflict(error, 'source_sync')) {
        return;
      }
      setSourceSyncMessage(error instanceof Error ? `来源刷新失败：${error.message}` : '来源刷新失败。');
    }
  }

  async function syncSourceIdsNow(sourceIds: number[]) {
    const dedupedSourceIds = Array.from(new Set(sourceIds.filter((sourceId) => Number.isInteger(sourceId) && sourceId > 0)));
    if (!dedupedSourceIds.length) {
      return;
    }

    try {
      const operation = await startSourceSyncOperation({
        kind: 'selected',
        source_ids: dedupedSourceIds,
      });
      terminalOperationRefreshRef.current.delete(operation.id);
      setSourceSyncMessage(null);
      applyBackendOperation(operation);
    } catch (error) {
      console.error('Failed to sync selected sources', error);
      if (applyOperationConflict(error, 'source_sync')) {
        return;
      }
      setSourceSyncMessage(error instanceof Error ? `失败来源重试失败：${error.message}` : '失败来源重试失败。');
    }
  }

  async function rebuildInsightEngineArtifactsNow(scope: 'current_source' | 'global' = 'global') {
    const sourceId = scope === 'current_source' && sourceScope.kind === 'source' ? Number(sourceScope.value) : null;
    if (scope === 'current_source' && sourceId === null) {
      setInsightEngineArtifactMessage('请先选择一个来源再补齐当前来源洞察。');
      return;
    }

    try {
      setInsightEngineArtifactMessage(
        scope === 'global' ? '正在统计全局未成功洞察数量。' : '正在统计当前来源未成功洞察数量。',
      );
      const missingCandidates = await listMissingFeedItemInsightArtifactCandidates({sourceId, limit: 200});
      const fillPlan = buildInsightArtifactFillPlan(scope, missingCandidates.total_count);
      if (!fillPlan.shouldStartOperation) {
        setInsightEngineArtifactMessage(fillPlan.message);
        return;
      }
      const operation = await startFeedItemInsightArtifactBatchOperation({
        scope,
        source_id: sourceId,
        limit: fillPlan.operationLimit,
      });
      terminalOperationRefreshRef.current.delete(operation.id);
      setInsightEngineArtifactMessage(fillPlan.message);
      applyBackendOperation(operation);
    } catch (error) {
      console.error('Failed to rebuild Insight Engine artifacts', error);
      if (applyOperationConflict(error, 'feed_item_insight_artifacts')) {
        return;
      }
      const message =
        error instanceof Error ? `补齐洞察结果失败：${error.message}` : '补齐洞察结果失败，请查看控制台或后端日志。';
      setInsightEngineArtifactMessage(message);
    }
  }

  async function runFeedItemInsightArtifactOperation(feedItemId: number, mode: 'rebuild' | 'build_missing') {
    try {
      const operation = await startFeedItemInsightArtifactOperation(feedItemId, mode);
      trackItemOperation(operation);
      return operation;
    } catch (error) {
      if (error instanceof OperationApiError && error.status === 409) {
        const activeOperation = extractConflictOperation(error);
        if (activeOperation) {
          trackItemOperation(activeOperation);
          return activeOperation;
        }
      }
      throw error;
    }
  }

  const sourceSyncBusyReason = isSyncingSources
    ? '来源刷新正在运行'
    : isRebuildingInsightEngineArtifacts
      ? '洞察构建正在运行'
      : null;

  const insightEngineArtifactBusyReason = isRebuildingInsightEngineArtifacts
    ? '洞察构建正在运行'
    : isSyncingSources
      ? '来源刷新正在运行'
      : null;

  return {
    activeItemOperations,
    feedItemArtifactRefreshVersion,
    isRebuildingInsightEngineArtifacts,
    insightEngineArtifactMessage,
    setInsightEngineArtifactMessage,
    insightEngineArtifactOperation,
    insightEngineArtifactProgress,
    isInsightEngineArtifactStopRequesting,
    insightEngineArtifactBusyReason,
    latestItemOperations,
    isSyncingSources,
    isSourceSyncStopRequesting,
    sourceSyncOperation,
    sourceSyncMessage,
    setSourceSyncMessage,
    sourceSyncProgress,
    sourceSyncBusyReason,
    applyOperationConflict,
    rebuildInsightEngineArtifactsNow,
    runFeedItemInsightArtifactOperation,
    startAutoCreateSourceSync,
    stopInsightEngineArtifactsNow,
    stopSourcesSyncNow,
    syncSourceIdsNow,
    syncSingleSourceNow,
    syncSourcesNow,
    trackBackendOperation,
    trackItemOperation,
  };
}
