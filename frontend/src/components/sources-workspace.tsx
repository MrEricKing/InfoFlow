import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react';
import {AlertTriangle, RefreshCw, StopCircle, Trash2} from 'lucide-react';
import {ApiError} from '@/api/errors';
import {
  commitSourcesOpmlImport,
  createSource,
  deleteSource,
  deleteSourceCategory,
  getSourceDeletePreview,
  getSource,
  listAllSourceFetches,
  listSourceFetches,
  listSources,
  previewSourcesOpml,
  previewSource,
  renameSourceCategory,
  updateSource,
} from '@/services/source-service';
import {SourceIcon, formatSourceIconSource} from '@/components/source-icon';
import {SourceSyncProgressPanel} from '@/components/source-sync-progress-panel';
import type {
  BulkSourceSyncResponseDto,
  OpmlImportCandidateDto,
  OpmlImportPreviewResponseDto,
  SingleSourceSyncResponseDto,
  SourceCategoryDto,
  SourceCreatePayload,
  SourceDeletePreviewDto,
  FeedItemInsightArtifactBatchOperationState,
  SourceAIRoute,
  SourceDto,
  SourceFetchDto,
  SourceFetchWithSourceDto,
  SourceImportResponseDto,
  SourceKind,
  SourcePreviewResponseDto,
  SourceSyncProgressState,
  SourceSyncOperationState,
  SourceUpdatePayload,
} from '@/types/source';

interface SourcesWorkspaceProps {
  mode: 'manage' | 'import';
  externalSources?: SourceDto[];
  searchQuery: string;
  insightEngineArtifactOperation: FeedItemInsightArtifactBatchOperationState | null;
  insightEngineArtifactProgress: SourceSyncProgressState | null;
  isInsightEngineArtifactStopRequesting: boolean;
  isSourceSyncStopRequesting: boolean;
  sourceSyncBusyReason: string | null;
  sourceSyncMessage: string | null;
  sourceSyncOperation: SourceSyncOperationState | null;
  sourceSyncProgress: SourceSyncProgressState | null;
  onCreateRequest?: () => void;
  onSourceIdsSync: (sourceIds: number[]) => Promise<void>;
  onSourceSync: (source: SourceDto) => Promise<void>;
  onSourcesSync: () => Promise<void>;
  onSourcesSyncStop: () => Promise<void>;
  onInsightEngineArtifactsStop: () => Promise<void>;
  onSourcesChanged?: () => Promise<SourceDto[]>;
  onSourceEdit?: (source: SourceDto) => void;
}

interface SourceFormState {
  name: string;
  kind: SourceKind;
  url: string;
  siteUrl: string;
  folderPath: string;
  isActive: boolean;
  aiRoute: SourceAIRoute;
  iconUrl: string | null;
  iconSource: SourceDto['icon_source'];
}

interface OpmlCandidateSelectionState {
  selected: boolean;
  aiRoute: SourceAIRoute;
  folderPath: string;
}

const IMPORT_PREVIEW_PANE_WIDTH_KEY = 'ifa:settings-source-import-preview-width:v1';
const IMPORT_PREVIEW_PANE_DEFAULT_WIDTH = 560;
const IMPORT_PREVIEW_PANE_MIN_WIDTH = 420;
const IMPORT_PREVIEW_PANE_MAX_WIDTH = 820;
const SOURCE_LIST_WIDTH_KEY = 'ifa:sources-workspace:source-list-width:v1';
const SOURCE_LIST_DEFAULT_WIDTH = 420;
const SOURCE_LIST_MIN_WIDTH = 340;
const SOURCE_LIST_MAX_WIDTH = 620;
const SOURCE_WORKSPACE_LEFT_WIDTH_KEY = 'ifa:sources-workspace:left-width:v1';
const SOURCE_WORKSPACE_LEFT_DEFAULT_WIDTH = 980;
const SOURCE_WORKSPACE_LEFT_MIN_WIDTH = 820;
const SOURCE_WORKSPACE_LEFT_MAX_WIDTH = 1320;
const SOURCE_GLOBAL_STATUS_MIN_WIDTH = 360;
const SOURCE_DETAIL_MIN_WIDTH = 460;
const SOURCE_RESIZE_GUTTER_WIDTH = 8;

const EMPTY_FORM: SourceFormState = {
  name: '',
  kind: 'rss',
  url: '',
  siteUrl: '',
  folderPath: '',
  isActive: true,
  aiRoute: 'general',
  iconUrl: null,
  iconSource: null,
};

export function SourcesWorkspace({
  mode,
  externalSources,
  searchQuery,
  insightEngineArtifactOperation,
  insightEngineArtifactProgress,
  isInsightEngineArtifactStopRequesting,
  isSourceSyncStopRequesting,
  sourceSyncBusyReason,
  sourceSyncMessage,
  sourceSyncOperation,
  sourceSyncProgress,
  onCreateRequest,
  onSourceIdsSync,
  onSourceSync,
  onSourcesSync,
  onSourcesSyncStop,
  onInsightEngineArtifactsStop,
  onSourcesChanged,
  onSourceEdit,
}: SourcesWorkspaceProps) {
  const [sources, setSources] = useState<SourceDto[]>([]);
  const [selectedSourceId, setSelectedSourceId] = useState<number | null>(null);
  const [formState, setFormState] = useState<SourceFormState>(EMPTY_FORM);
  const [fetches, setFetches] = useState<SourceFetchDto[]>([]);
  const [allFetches, setAllFetches] = useState<SourceFetchWithSourceDto[]>([]);
  const [isLoadingSources, setIsLoadingSources] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [updatingCategoryPath, setUpdatingCategoryPath] = useState<string | null>(null);
  const [isImporting, setIsImporting] = useState(false);
  const [isCommittingImport, setIsCommittingImport] = useState(false);
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [previewUrlInput, setPreviewUrlInput] = useState('');
  const [previewResult, setPreviewResult] = useState<SourcePreviewResponseDto | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [opmlPreviewResult, setOpmlPreviewResult] = useState<OpmlImportPreviewResponseDto | null>(null);
  const [opmlCandidateSelections, setOpmlCandidateSelections] = useState<Record<string, OpmlCandidateSelectionState>>(
    {},
  );
  const [opmlBulkAiRoute, setOpmlBulkAiRoute] = useState<SourceAIRoute>('general');
  const [selectedOpmlPreviewCandidateId, setSelectedOpmlPreviewCandidateId] = useState<string | null>(null);
  const [opmlCandidatePreviewResult, setOpmlCandidatePreviewResult] = useState<SourcePreviewResponseDto | null>(null);
  const [isPreviewingOpmlCandidate, setIsPreviewingOpmlCandidate] = useState(false);
  const [importPreviewPaneWidth, setImportPreviewPaneWidth] = useState(readStoredImportPreviewPaneWidth);
  const [sourceListWidth, setSourceListWidth] = useState(readStoredSourceListWidth);
  const [sourceWorkspaceLeftWidth, setSourceWorkspaceLeftWidth] = useState(readStoredSourceWorkspaceLeftWidth);
  const [lastImportResult, setLastImportResult] = useState<SourceImportResponseDto | null>(null);
  const [sourceDeletePreview, setSourceDeletePreview] = useState<SourceDeletePreviewDto | null>(null);
  const [isLoadingSourceDeletePreview, setIsLoadingSourceDeletePreview] = useState(false);
  const [isDeletingSource, setIsDeletingSource] = useState(false);
  const [sourceDeleteConfirmed, setSourceDeleteConfirmed] = useState(false);
  const opmlCandidatePreviewRequestIdRef = useRef(0);
  const terminalSyncRefreshOperationIdRef = useRef<number | null>(null);
  const sourceListScrollRef = useRef<HTMLDivElement | null>(null);
  const sourceListScrollTopRef = useRef(0);
  const shouldRestoreSourceListScrollRef = useRef(false);

  useEffect(() => {
    void refreshSources();
  }, []);

  useEffect(() => {
    if (mode !== 'manage') {
      return;
    }

    void refreshAllFetches();
  }, [mode]);

  useEffect(() => {
    if (!externalSources) {
      return;
    }

    setSources(externalSources);
    setIsLoadingSources(false);
    setSelectedSourceId((current) => {
      if (mode === 'import') {
        return null;
      }
      if (current !== null && externalSources.some((source) => source.id === current)) {
        return current;
      }
      return externalSources[0]?.id ?? null;
    });
  }, [externalSources, mode]);

  useEffect(() => {
    const selectedSource = sources.find((source) => source.id === selectedSourceId) ?? null;
    if (!selectedSource) {
      setFormState(EMPTY_FORM);
      setFetches([]);
      return;
    }

    setFormState({
      name: selectedSource.name,
      kind: selectedSource.kind,
      url: selectedSource.url,
      siteUrl: selectedSource.site_url ?? '',
      folderPath: selectedSource.folder_path ?? '',
      isActive: selectedSource.is_active,
      aiRoute: selectedSource.ai_route,
      iconUrl: selectedSource.icon_url,
      iconSource: selectedSource.icon_source,
    });
  }, [selectedSourceId, sources]);

  useEffect(() => {
    setSourceDeletePreview(null);
    setSourceDeleteConfirmed(false);
  }, [selectedSourceId]);

  useEffect(() => {
    if (selectedSourceId === null) {
      setFetches([]);
      return;
    }

    const sourceId = selectedSourceId;
    let isCancelled = false;

    async function loadFetches() {
      try {
        const nextFetches = await listSourceFetches(sourceId, 10);
        if (!isCancelled) {
          setFetches(nextFetches);
        }
      } catch (error) {
        if (!isCancelled) {
          setErrorMessage(buildUiErrorMessage(error, '加载刷新记录失败'));
        }
      }
    }

    void loadFetches();

    return () => {
      isCancelled = true;
    };
  }, [selectedSourceId]);

  const selectedSource = sources.find((source) => source.id === selectedSourceId) ?? null;
  const isSourceSyncRunning =
    sourceSyncOperation?.status === 'queued' ||
    sourceSyncOperation?.status === 'running' ||
    sourceSyncOperation?.status === 'stopping';
  const isSyncingGlobal = Boolean(
    sourceSyncOperation &&
      (sourceSyncOperation.kind === 'all' || sourceSyncOperation.kind === 'selected') &&
      isSourceSyncRunning,
  );
  const isSyncingSingle = Boolean(
    sourceSyncOperation &&
      sourceSyncOperation.kind !== 'all' &&
      sourceSyncOperation.kind !== 'selected' &&
      isSourceSyncRunning,
  );
  const isSyncStopRequested = Boolean(
    sourceSyncOperation?.stopRequested || sourceSyncProgress?.stopRequested || isSourceSyncStopRequesting,
  );
  const isInsightEngineArtifactRunning =
    insightEngineArtifactOperation?.status === 'queued' ||
    insightEngineArtifactOperation?.status === 'running' ||
    insightEngineArtifactOperation?.status === 'stopping';
  const isInsightEngineArtifactStopRequested = Boolean(
    insightEngineArtifactOperation?.stopRequested || insightEngineArtifactProgress?.stopRequested,
  );
  const syncProgress = sourceSyncProgress;
  const operationMessage = message ?? (isSourceSyncRunning ? null : sourceSyncMessage);

  useEffect(() => {
    if (!sourceSyncOperation || !isTerminalSourceSyncStatus(sourceSyncOperation.status)) {
      return;
    }
    if (terminalSyncRefreshOperationIdRef.current === sourceSyncOperation.id) {
      return;
    }

    terminalSyncRefreshOperationIdRef.current = sourceSyncOperation.id;
    let isCancelled = false;

    async function refreshSyncArtifacts() {
      try {
        await refreshSources(selectedSourceId);
        await notifySourcesChanged();
        if (selectedSourceId !== null) {
          const nextFetches = await listSourceFetches(selectedSourceId, 10);
          if (!isCancelled) {
            setFetches(nextFetches);
          }
        }
        const nextAllFetches = await listAllSourceFetches(50);
        if (!isCancelled) {
          setAllFetches(nextAllFetches);
        }
      } catch (error) {
        if (!isCancelled) {
          setErrorMessage(buildUiErrorMessage(error, '刷新结果失败'));
        }
      }
    }

    void refreshSyncArtifacts();

    return () => {
      isCancelled = true;
    };
  }, [selectedSourceId, sourceSyncOperation?.id, sourceSyncOperation?.status]);

  const filteredSources = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    if (!query) {
      return sources;
    }

    return sources.filter((source) =>
      [
        source.name,
        source.kind,
        source.url,
        source.site_url ?? '',
        source.folder_path ?? '',
        source.provenance,
        source.ai_route,
      ]
        .join(' ')
        .toLowerCase()
        .includes(query),
    );
  }, [searchQuery, sources]);

  const categorySuggestions = useMemo(
    () =>
      [...new Set(sources.map((source) => source.folder_path).filter((value): value is string => Boolean(value)))]
        .sort((left, right) => left.localeCompare(right, 'zh-CN')),
    [sources],
  );

  const sourceCategorySummaries = useMemo<SourceCategoryDto[]>(() => {
    const counts = new Map<string, number>();
    for (const source of sources) {
      const categoryPath = source.folder_path?.trim();
      if (!categoryPath) {
        continue;
      }
      counts.set(categoryPath, (counts.get(categoryPath) ?? 0) + 1);
    }

    return [...counts.entries()]
      .map(([path, source_count]) => ({path, source_count}))
      .sort((left, right) => left.path.localeCompare(right.path, 'zh-CN'));
  }, [sources]);

  const importCategorySuggestions = useMemo(
    () =>
      [
        ...new Set([
          ...categorySuggestions,
          ...(opmlPreviewResult?.candidates
            .map((candidate) => candidate.folder_path)
            .filter((value): value is string => Boolean(value)) ?? []),
        ]),
      ].sort((left, right) => left.localeCompare(right, 'zh-CN')),
    [categorySuggestions, opmlPreviewResult],
  );

  const selectedOpmlPreviewCandidate = useMemo(
    () =>
      opmlPreviewResult?.candidates.find((candidate) => candidate.candidate_id === selectedOpmlPreviewCandidateId) ??
      null,
    [opmlPreviewResult, selectedOpmlPreviewCandidateId],
  );

  const groupedSources = useMemo(() => {
    const groups = new Map<string, SourceDto[]>();
    for (const source of filteredSources) {
      const category = source.folder_path?.trim() || '未分类';
      groups.set(category, [...(groups.get(category) ?? []), source]);
    }

    return [...groups.entries()]
      .map(([category, items]) => ({
        category,
        sources: items.sort((left, right) => left.name.localeCompare(right.name, 'zh-CN')),
      }))
      .sort((left, right) => {
        if (left.category === '未分类') {
          return 1;
        }
        if (right.category === '未分类') {
          return -1;
        }
        return left.category.localeCompare(right.category, 'zh-CN');
      });
  }, [filteredSources]);

  useLayoutEffect(() => {
    if (mode !== 'manage' || isLoadingSources || !shouldRestoreSourceListScrollRef.current) {
      return;
    }

    const scrollContainer = sourceListScrollRef.current;
    if (!scrollContainer) {
      return;
    }

    const maxScrollTop = Math.max(0, scrollContainer.scrollHeight - scrollContainer.clientHeight);
    scrollContainer.scrollTop = Math.min(sourceListScrollTopRef.current, maxScrollTop);
    shouldRestoreSourceListScrollRef.current = false;
  }, [groupedSources, isLoadingSources, mode]);

  async function refreshSources(nextSelectedSourceId?: number | null) {
    rememberSourceListScroll();
    if (sources.length === 0) {
      setIsLoadingSources(true);
    }
    try {
      const nextSources = await listSources();
      setSources(nextSources);
      setSelectedSourceId((current) => {
        if (nextSelectedSourceId !== undefined) {
          return nextSelectedSourceId;
        }

        if (mode === 'import') {
          return null;
        }

        if (current !== null && nextSources.some((source) => source.id === current)) {
          return current;
        }

        return nextSources[0]?.id ?? null;
      });
    } catch (error) {
      setErrorMessage(buildUiErrorMessage(error, '加载 source 列表失败'));
    } finally {
      setIsLoadingSources(false);
    }
  }

  async function refreshAllFetches() {
    try {
      const nextFetches = await listAllSourceFetches(50);
      setAllFetches(nextFetches);
    } catch (error) {
      setErrorMessage(buildUiErrorMessage(error, '加载全局刷新记录失败'));
    }
  }

  function rememberSourceListScroll() {
    const scrollContainer = sourceListScrollRef.current;
    if (!scrollContainer) {
      return;
    }

    sourceListScrollTopRef.current = scrollContainer.scrollTop;
    shouldRestoreSourceListScrollRef.current = true;
  }

  async function notifySourcesChanged() {
    rememberSourceListScroll();
    await onSourcesChanged?.();
  }

  function resetCreateFlow() {
    setSelectedSourceId(null);
    setFormState(EMPTY_FORM);
    setPreviewUrlInput('');
    setPreviewResult(null);
  }

  function selectSource(sourceId: number) {
    setErrorMessage(null);
    setMessage(null);
    setPreviewResult(null);
    setPreviewUrlInput('');
    setSourceDeletePreview(null);
    setSourceDeleteConfirmed(false);
    setSelectedSourceId(sourceId);
  }

  function startCreateMode() {
    setErrorMessage(null);
    setMessage(null);
    resetCreateFlow();
  }

  function updateForm<K extends keyof SourceFormState>(key: K, value: SourceFormState[K]) {
    setFormState((current) => ({...current, [key]: value}));
  }

  function fillPreviewTitle() {
    if (!previewResult?.source.title) {
      return;
    }
    updateForm('name', previewResult.source.title);
  }

  async function handlePreview() {
    const previewUrl = previewUrlInput.trim();
    if (!previewUrl) {
      setErrorMessage('请先输入 Feed URL 或 RSSHub route。');
      return;
    }

    setErrorMessage(null);
    setMessage(null);
    setIsPreviewing(true);

    try {
      const response = await previewSource({url: previewUrl});
      setPreviewResult(response);
      setSelectedOpmlPreviewCandidateId(null);
      setOpmlCandidatePreviewResult(null);
      setFormState({
        name: response.source.title ?? '',
        kind: response.source.detected_kind,
        url: response.source.canonical_url,
        siteUrl: response.source.site_url ?? '',
        folderPath: '',
        isActive: true,
        aiRoute: 'general',
        iconUrl: response.source.icon_url,
        iconSource: response.source.icon_source,
      });
      setMessage('预览成功，确认后即可创建 source。');
    } catch (error) {
      setPreviewResult(null);
      setErrorMessage(buildUiErrorMessage(error, '预览 source 失败'));
    } finally {
      setIsPreviewing(false);
    }
  }

  async function handleSave() {
    setErrorMessage(null);
    setMessage(null);
    setIsSaving(true);

    try {
      if (selectedSource) {
        const updated = await updateSource(selectedSource.id, buildUpdatePayload(formState));
        await refreshSources(updated.id);
        await notifySourcesChanged();
        const refreshed = await getSource(updated.id);
        setSources((current) => current.map((source) => (source.id === refreshed.id ? refreshed : source)));
        setMessage('Source 已更新。');
      } else {
        if (!previewResult) {
          setErrorMessage('请先完成预览，再创建 source。');
          return;
        }

        const created = await createSource(buildCreatePayload(formState));
        await refreshSources(mode === 'import' ? null : created.id);
        await notifySourcesChanged();
        setPreviewResult(null);
        setPreviewUrlInput('');
        if (mode === 'import') {
          setFormState(EMPTY_FORM);
        }
        setMessage('Source 已创建。');
      }
    } catch (error) {
      const conflictSourceId = getConflictSourceId(error);
      if (conflictSourceId !== null) {
        await refreshSources(conflictSourceId);
      }
      setErrorMessage(buildUiErrorMessage(error, '保存 source 失败'));
    } finally {
      setIsSaving(false);
    }
  }

  async function handleSyncAll() {
    if (sourceSyncBusyReason) {
      return;
    }

    setErrorMessage(null);
    setMessage(null);

    try {
      await onSourcesSync();
      await refreshSources(selectedSourceId);
      await notifySourcesChanged();
      if (selectedSourceId !== null) {
        const nextFetches = await listSourceFetches(selectedSourceId, 10);
        setFetches(nextFetches);
      }
      await refreshAllFetches();
    } catch (error) {
      setErrorMessage(buildUiErrorMessage(error, '全量刷新失败'));
    }
  }

  async function handleSyncFailedSources(result: BulkSourceSyncResponseDto) {
    if (sourceSyncBusyReason) {
      return;
    }

    const failedSourceIds = getFailedSourceIds(result);
    if (!failedSourceIds.length) {
      return;
    }

    setErrorMessage(null);
    setMessage(null);
    try {
      await onSourceIdsSync(failedSourceIds);
      await refreshSources(selectedSourceId);
      await notifySourcesChanged();
      if (selectedSourceId !== null) {
        const nextFetches = await listSourceFetches(selectedSourceId, 10);
        setFetches(nextFetches);
      }
      await refreshAllFetches();
    } catch (error) {
      setErrorMessage(buildUiErrorMessage(error, '失败 source 重试失败'));
    }
  }

  async function handleStopSyncAll() {
    if (!isSyncingGlobal) {
      return;
    }

    setErrorMessage(null);
    setMessage('正在请求停止刷新。');
    try {
      await onSourcesSyncStop();
      setMessage('停止请求已提交，等待当前步骤结束。');
    } catch (error) {
      setMessage(null);
      setErrorMessage(buildUiErrorMessage(error, '停止刷新请求失败'));
    }
  }

  async function handleStopInsightEngineArtifacts() {
    if (!isInsightEngineArtifactRunning) {
      return;
    }

    setErrorMessage(null);
    setMessage('正在请求停止洞察补齐。');
    try {
      await onInsightEngineArtifactsStop();
      setMessage('停止请求已提交，等待当前步骤结束。');
    } catch (error) {
      setMessage(null);
      setErrorMessage(buildUiErrorMessage(error, '停止洞察补齐失败'));
    }
  }

  async function handleSyncSelected() {
    if (!selectedSource || sourceSyncBusyReason) {
      return;
    }

    setErrorMessage(null);
    setMessage(null);
    try {
      await onSourceSync(selectedSource);
      const nextFetches = await listSourceFetches(selectedSource.id, 10);
      setFetches(nextFetches);
      await refreshAllFetches();
      await refreshSources(selectedSource.id);
      await notifySourcesChanged();
    } catch (error) {
      setErrorMessage(buildUiErrorMessage(error, '单源刷新失败'));
    }
  }

  async function handleLoadSourceDeletePreview() {
    if (!selectedSource) {
      return;
    }

    setErrorMessage(null);
    setMessage(null);
    setSourceDeleteConfirmed(false);
    setIsLoadingSourceDeletePreview(true);
    try {
      const preview = await getSourceDeletePreview(selectedSource.id);
      setSourceDeletePreview(preview);
    } catch (error) {
      setSourceDeletePreview(null);
      setErrorMessage(buildUiErrorMessage(error, '加载删除预览失败'));
    } finally {
      setIsLoadingSourceDeletePreview(false);
    }
  }

  function handleCancelSourceDeletePreview() {
    setSourceDeletePreview(null);
    setSourceDeleteConfirmed(false);
  }

  async function handleDeleteSelectedSource() {
    if (!sourceDeletePreview || !sourceDeleteConfirmed || isDeletingSource) {
      return;
    }

    setErrorMessage(null);
    setMessage(null);
    setIsDeletingSource(true);
    try {
      const result = await deleteSource(sourceDeletePreview.source.id, sourceDeletePreview.preview_token);
      setSourceDeletePreview(null);
      setSourceDeleteConfirmed(false);
      setFetches([]);
      await refreshSources(null);
      await notifySourcesChanged();
      await refreshAllFetches();
      setMessage(`Source“${result.source.name}”已彻底删除；保留 ${result.retained_saved_items} 个知识库条目。`);
    } catch (error) {
      const nextPreview = getSourceDeletePreviewFromError(error);
      if (nextPreview) {
        setSourceDeletePreview(nextPreview);
        setSourceDeleteConfirmed(false);
      }
      setErrorMessage(buildUiErrorMessage(error, '删除 source 失败'));
    } finally {
      setIsDeletingSource(false);
    }
  }

  async function handleRenameCategory(oldPath: string, newPath: string) {
    const nextPath = newPath.trim();
    if (!nextPath) {
      setErrorMessage('分类名称不能为空。');
      return;
    }
    if (nextPath === oldPath) {
      setErrorMessage('新的分类名称需要和当前分类不同。');
      return;
    }

    setErrorMessage(null);
    setMessage(null);
    setUpdatingCategoryPath(oldPath);

    try {
      const response = await renameSourceCategory({old_path: oldPath, new_path: nextPath});
      await refreshSources(selectedSourceId);
      await notifySourcesChanged();
      setMessage(`分类“${oldPath}”已重命名为“${nextPath}”，更新 ${response.updated_source_count} 个 source。`);
    } catch (error) {
      setErrorMessage(buildUiErrorMessage(error, '重命名订阅源分类失败'));
    } finally {
      setUpdatingCategoryPath(null);
    }
  }

  async function handleDeleteCategory(path: string) {
    setErrorMessage(null);
    setMessage(null);
    setUpdatingCategoryPath(path);

    try {
      const response = await deleteSourceCategory({path});
      await refreshSources(selectedSourceId);
      await notifySourcesChanged();
      setMessage(`分类“${path}”已删除，${response.updated_source_count} 个 source 已设为未分类。`);
    } catch (error) {
      setErrorMessage(buildUiErrorMessage(error, '删除订阅源分类失败'));
    } finally {
      setUpdatingCategoryPath(null);
    }
  }

  async function handleOpmlPreview(file: File | null) {
    if (!file) {
      return;
    }

    setErrorMessage(null);
    setMessage(null);
    setIsImporting(true);

    try {
      const response = await previewSourcesOpml(file);
      setOpmlPreviewResult(response);
      setOpmlCandidateSelections(buildInitialOpmlCandidateSelections(response.candidates));
      setOpmlBulkAiRoute('general');
      setSelectedOpmlPreviewCandidateId(null);
      setOpmlCandidatePreviewResult(null);
      setLastImportResult(null);
      setMessage('OPML preview 已完成，请选择要创建的 sources。');
    } catch (error) {
      setOpmlPreviewResult(null);
      setOpmlCandidateSelections({});
      setSelectedOpmlPreviewCandidateId(null);
      setOpmlCandidatePreviewResult(null);
      setLastImportResult(null);
      setErrorMessage(buildUiErrorMessage(error, 'OPML preview 失败'));
    } finally {
      setIsImporting(false);
    }
  }

  function updateOpmlCandidateSelected(candidateId: string, selected: boolean) {
    setOpmlCandidateSelections((current) => ({
      ...current,
      [candidateId]: {
        selected,
        aiRoute: current[candidateId]?.aiRoute ?? 'general',
        folderPath: current[candidateId]?.folderPath ?? '',
      },
    }));
  }

  function updateOpmlCandidateAIRoute(candidateId: string, aiRoute: SourceAIRoute) {
    setOpmlCandidateSelections((current) => ({
      ...current,
      [candidateId]: {
        selected: current[candidateId]?.selected ?? false,
        aiRoute,
        folderPath: current[candidateId]?.folderPath ?? '',
      },
    }));
  }

  function updateOpmlCandidateFolderPath(candidateId: string, folderPath: string) {
    setOpmlCandidateSelections((current) => ({
      ...current,
      [candidateId]: {
        selected: current[candidateId]?.selected ?? false,
        aiRoute: current[candidateId]?.aiRoute ?? 'general',
        folderPath,
      },
    }));
  }

  function applyBulkOpmlAIRoute() {
    setOpmlCandidateSelections((current) =>
      Object.fromEntries(
        Object.entries(current).map(([candidateId, selection]) => [
          candidateId,
          selection.selected ? {...selection, aiRoute: opmlBulkAiRoute} : selection,
        ]),
      ),
    );
  }

  async function handleOpmlCommit() {
    if (!opmlPreviewResult) {
      return;
    }

    const selectedCandidates = opmlPreviewResult.candidates
      .filter((candidate) => opmlCandidateSelections[candidate.candidate_id]?.selected)
      .map((candidate) => ({
        candidate_id: candidate.candidate_id,
        ai_route: opmlCandidateSelections[candidate.candidate_id]?.aiRoute ?? opmlBulkAiRoute,
        folder_path: normalizeOptionalField(opmlCandidateSelections[candidate.candidate_id]?.folderPath ?? ''),
      }));

    if (selectedCandidates.length === 0) {
      setErrorMessage('请至少选择一个 OPML candidate。');
      return;
    }

    setErrorMessage(null);
    setMessage(null);
    setIsCommittingImport(true);

    try {
      const response = await commitSourcesOpmlImport({
        import_run_id: opmlPreviewResult.import_run_id,
        default_ai_route: opmlBulkAiRoute,
        selected_candidates: selectedCandidates,
      });
      setLastImportResult(response);
      await refreshSources(selectedSourceId);
      await notifySourcesChanged();
      setMessage('OPML commit 已完成。');
    } catch (error) {
      setErrorMessage(buildUiErrorMessage(error, 'OPML commit 失败'));
    } finally {
      setIsCommittingImport(false);
    }
  }

  async function handleOpmlCandidatePreview(candidate: OpmlImportCandidateDto) {
    if (!candidate.kind || !candidate.url) {
      setErrorMessage('这个 OPML candidate 没有可读取的 canonical URL。');
      return;
    }

    setErrorMessage(null);
    setMessage(null);
    setSelectedOpmlPreviewCandidateId(candidate.candidate_id);
    setOpmlCandidatePreviewResult(null);
    setIsPreviewingOpmlCandidate(true);
    const requestId = opmlCandidatePreviewRequestIdRef.current + 1;
    opmlCandidatePreviewRequestIdRef.current = requestId;

    try {
      const response = await previewSource({kind: candidate.kind, url: candidate.url});
      if (opmlCandidatePreviewRequestIdRef.current === requestId) {
        setOpmlCandidatePreviewResult(response);
      }
    } catch (error) {
      if (opmlCandidatePreviewRequestIdRef.current === requestId) {
        setErrorMessage(buildUiErrorMessage(error, '读取 OPML candidate 失败'));
      }
    } finally {
      if (opmlCandidatePreviewRequestIdRef.current === requestId) {
        setIsPreviewingOpmlCandidate(false);
      }
    }
  }

  function startImportPreviewPaneResize(event: ReactPointerEvent<HTMLButtonElement>) {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = importPreviewPaneWidth;
    let latestWidth = startWidth;

    function handlePointerMove(pointerEvent: PointerEvent) {
      const viewportMax = Math.min(IMPORT_PREVIEW_PANE_MAX_WIDTH, Math.floor(window.innerWidth * 0.48));
      const nextWidth = clamp(
        startWidth - (pointerEvent.clientX - startX),
        IMPORT_PREVIEW_PANE_MIN_WIDTH,
        Math.max(IMPORT_PREVIEW_PANE_MIN_WIDTH, viewportMax),
      );
      latestWidth = nextWidth;
      setImportPreviewPaneWidth(nextWidth);
    }

    function handlePointerUp() {
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', handlePointerUp);
      window.localStorage.setItem(IMPORT_PREVIEW_PANE_WIDTH_KEY, String(latestWidth));
    }

    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', handlePointerUp);
  }

  function startSourceListResize(event: ReactPointerEvent<HTMLButtonElement>) {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = sourceListWidth;
    let latestWidth = startWidth;

    function handlePointerMove(pointerEvent: PointerEvent) {
      const nextWidth = clamp(
        startWidth + pointerEvent.clientX - startX,
        SOURCE_LIST_MIN_WIDTH,
        SOURCE_LIST_MAX_WIDTH,
      );
      latestWidth = nextWidth;
      setSourceListWidth(nextWidth);
    }

    function handlePointerUp() {
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', handlePointerUp);
      window.localStorage.setItem(SOURCE_LIST_WIDTH_KEY, String(latestWidth));
    }

    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', handlePointerUp);
  }

  function startSourceWorkspaceResize(event: ReactPointerEvent<HTMLButtonElement>) {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = effectiveSourceWorkspaceLeftWidth;
    const containerWidth = event.currentTarget.parentElement?.getBoundingClientRect().width ?? window.innerWidth;
    const minWidth = Math.max(
      SOURCE_WORKSPACE_LEFT_MIN_WIDTH,
      sourceListWidth + SOURCE_RESIZE_GUTTER_WIDTH + SOURCE_GLOBAL_STATUS_MIN_WIDTH,
    );
    const maxWidth = Math.max(
      minWidth,
      Math.min(
        SOURCE_WORKSPACE_LEFT_MAX_WIDTH,
        containerWidth - SOURCE_DETAIL_MIN_WIDTH - SOURCE_RESIZE_GUTTER_WIDTH,
      ),
    );
    let latestWidth = clamp(startWidth, minWidth, maxWidth);

    function handlePointerMove(pointerEvent: PointerEvent) {
      const nextWidth = clamp(startWidth + pointerEvent.clientX - startX, minWidth, maxWidth);
      latestWidth = nextWidth;
      setSourceWorkspaceLeftWidth(nextWidth);
    }

    function handlePointerUp() {
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', handlePointerUp);
      window.localStorage.setItem(SOURCE_WORKSPACE_LEFT_WIDTH_KEY, String(latestWidth));
    }

    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', handlePointerUp);
  }

  const sourceListPane = (
    <aside className="flex min-h-[360px] flex-col border-b border-[var(--border-subtle)] bg-[var(--bg-panel)] xl:min-h-0 xl:border-b-0">
      <div
        ref={sourceListScrollRef}
        className="min-h-0 flex-1 space-y-4 overflow-y-auto p-3"
        onScroll={(event) => {
          sourceListScrollTopRef.current = event.currentTarget.scrollTop;
        }}
      >
        {isLoadingSources ? (
          <PanelHint title="加载中" body="正在读取 source 列表..." />
        ) : filteredSources.length === 0 ? (
          <PanelHint title="暂无 source" body="当前没有匹配的订阅源，或者还没有导入任何内容。" />
        ) : (
          groupedSources.map((group) => (
            <div key={group.category}>
              <div className="flex items-center justify-between px-1 text-xs font-semibold text-[var(--text-soft)]">
                <span className="truncate">{group.category}</span>
                <span>{group.sources.length}</span>
              </div>
              <div className="mt-2 space-y-2">
                {group.sources.map((source) => (
                  <SourceListButton
                    key={source.id}
                    isSelected={selectedSourceId === source.id}
                    source={source}
                    onSelect={() => selectSource(source.id)}
                  />
                ))}
              </div>
            </div>
          ))
        )}
      </div>
    </aside>
  );
  const lastSyncResult = sourceSyncOperation?.result ?? null;
  const selectedSyncResult = getSyncResultForSource(lastSyncResult, selectedSourceId);
  const globalSyncProgress = isSyncingGlobal ? syncProgress : null;
  const globalInsightProgress = insightEngineArtifactProgress;
  const selectedSyncProgress =
    isSyncingSingle && sourceSyncOperation?.sourceId === selectedSourceId ? syncProgress : null;
  const effectiveSourceWorkspaceLeftWidth = Math.max(
    sourceWorkspaceLeftWidth,
    sourceListWidth + SOURCE_RESIZE_GUTTER_WIDTH + SOURCE_GLOBAL_STATUS_MIN_WIDTH,
  );

  if (mode === 'import') {
    return (
      <div
        className="grid h-full min-h-0 overflow-hidden xl:grid-cols-[minmax(0,1fr)_10px_var(--import-preview-pane-width)]"
        style={{'--import-preview-pane-width': `${importPreviewPaneWidth}px`} as CSSProperties}
      >
        <main className="min-w-0 overflow-y-auto bg-[var(--bg-app)] px-6 py-5">
          <CreateSourceFlow
            categorySuggestions={categorySuggestions}
            errorMessage={errorMessage}
            formState={formState}
            isPreviewing={isPreviewing}
            isSaving={isSaving}
            message={message}
            previewResult={previewResult}
            previewUrlInput={previewUrlInput}
            onFillPreviewTitle={fillPreviewTitle}
            onPreview={() => void handlePreview()}
            onPreviewUrlInputChange={setPreviewUrlInput}
            onReset={startCreateMode}
            onSave={() => void handleSave()}
            onUpdateForm={updateForm}
          />
          <div className="mt-5">
            <OpmlImportCard
              bulkAiRoute={opmlBulkAiRoute}
              candidateSelections={opmlCandidateSelections}
              isCommittingImport={isCommittingImport}
              isImporting={isImporting}
              lastImportResult={lastImportResult}
              previewResult={opmlPreviewResult}
              onApplyBulkAiRoute={applyBulkOpmlAIRoute}
              onBulkAiRouteChange={setOpmlBulkAiRoute}
              onCandidateAiRouteChange={updateOpmlCandidateAIRoute}
              onCandidateFolderPathChange={updateOpmlCandidateFolderPath}
              onCandidatePreview={(candidate) => void handleOpmlCandidatePreview(candidate)}
              onCandidateSelectedChange={updateOpmlCandidateSelected}
              onCommit={() => void handleOpmlCommit()}
              onPreview={(file) => void handleOpmlPreview(file)}
              previewingCandidateId={isPreviewingOpmlCandidate ? selectedOpmlPreviewCandidateId : null}
              selectedPreviewCandidateId={selectedOpmlPreviewCandidateId}
              categorySuggestions={importCategorySuggestions}
            />
          </div>
        </main>

        <button
          aria-label="调整预览栏宽度"
          className="hidden cursor-col-resize border-x border-[var(--border-subtle)] bg-[var(--bg-hover)] transition hover:bg-[var(--border-subtle)] xl:block"
          onPointerDown={startImportPreviewPaneResize}
          type="button"
        />

        <section className="panel flex min-h-0 flex-col border-t border-[var(--border-subtle)] xl:border-l xl:border-t-0">
          <ImportPreviewPane
            isPreviewingOpmlCandidate={isPreviewingOpmlCandidate}
            opmlCandidate={selectedOpmlPreviewCandidate}
            opmlPreviewResult={opmlCandidatePreviewResult}
            singlePreviewResult={previewResult}
          />
        </section>
      </div>
    );
  }

  return (
    <div
      className="grid h-full min-h-0 overflow-hidden bg-[var(--bg-app)] xl:grid-cols-[var(--source-workspace-left-width)_8px_minmax(460px,1fr)]"
      style={{'--source-workspace-left-width': `${effectiveSourceWorkspaceLeftWidth}px`} as CSSProperties}
    >
      <section
        className="flex min-h-0 flex-col overflow-hidden border-b border-[var(--border-subtle)] bg-[var(--bg-panel)] xl:border-b-0"
        style={{'--source-list-width': `${sourceListWidth}px`} as CSSProperties}
      >
        <div className="border-b border-[var(--border-subtle)] px-5 py-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <div className="text-sm font-semibold text-[var(--text-main)]">所有订阅源与全局状态</div>
                <span className="text-xs text-[var(--text-soft)]">{filteredSources.length} 个 source</span>
              </div>
              <p className="mt-1 text-sm text-[var(--text-muted)]">
                按分类查看 source、触发全量刷新，并在同一侧查看刷新摘要、刷新记录和分类。
              </p>
            </div>
            <div className="flex shrink-0 flex-wrap justify-end gap-2">
              <button
                className="rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white shadow-sm"
                onClick={onCreateRequest ?? startCreateMode}
                type="button"
              >
                新建 Source
              </button>
              {isSyncingGlobal ? (
                <button
                  className="inline-flex items-center gap-2 rounded-lg border border-[var(--warning)] px-3 py-2 text-sm text-[var(--warning)] disabled:cursor-not-allowed disabled:opacity-50"
                  onClick={() => void handleStopSyncAll()}
                  title="停止刷新：当前来源完成后停止"
                  type="button"
                  disabled={isSyncStopRequested}
                >
                  <StopCircle size={16} />
                  {isSyncStopRequested ? '停止已请求' : '停止刷新'}
                </button>
              ) : (
                <button
                  className="rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                  onClick={() => void handleSyncAll()}
                  type="button"
                  disabled={Boolean(sourceSyncBusyReason)}
                  title={sourceSyncBusyReason ?? undefined}
                >
                  全量刷新
                </button>
              )}
              {isInsightEngineArtifactRunning ? (
                <button
                  className="inline-flex items-center gap-2 rounded-lg border border-[var(--warning)] px-3 py-2 text-sm text-[var(--warning)] disabled:cursor-not-allowed disabled:opacity-50"
                  onClick={() => void handleStopInsightEngineArtifacts()}
                  title="停止洞察补齐：当前步骤完成后取消剩余任务"
                  type="button"
                  disabled={isInsightEngineArtifactStopRequested || isInsightEngineArtifactStopRequesting}
                >
                  <StopCircle size={16} />
                  {isInsightEngineArtifactStopRequested || isInsightEngineArtifactStopRequesting ? '停止已请求' : '停止洞察'}
                </button>
              ) : null}
            </div>
          </div>
        </div>

        <div className="grid min-h-0 flex-1 overflow-hidden xl:grid-cols-[var(--source-list-width)_8px_minmax(0,1fr)]">
          {sourceListPane}

          <button
            aria-label="调整所有订阅源列表宽度"
            className="hidden cursor-col-resize border-x border-[var(--border-subtle)] bg-[var(--bg-panel)] transition hover:bg-[var(--bg-hover)] xl:block"
            onPointerDown={startSourceListResize}
            type="button"
          />

          <aside className="min-h-[420px] overflow-y-auto border-t border-[var(--border-subtle)] bg-[var(--bg-panel)] xl:min-h-0 xl:border-l xl:border-t-0">
            {(operationMessage || errorMessage || globalSyncProgress || globalInsightProgress) && (
              <div className="space-y-3 border-b border-[var(--border-subtle)] px-5 py-4">
                {operationMessage ? <MessageCard tone="success" text={operationMessage} /> : null}
                {errorMessage ? <MessageCard tone="error" text={errorMessage} /> : null}
                {globalSyncProgress ? <SourceSyncProgressPanel progress={globalSyncProgress} /> : null}
                {globalInsightProgress ? <SourceSyncProgressPanel progress={globalInsightProgress} variant="insight" /> : null}
              </div>
            )}

            <div className="divide-y divide-[var(--border-subtle)]">
              <section className="px-5 py-5">
                <div className="text-sm font-semibold text-[var(--text-main)]">所有订阅源刷新结果</div>
                {lastSyncResult ? (
                  <SyncSummary
                    result={lastSyncResult}
                    disableSyncFailedSources={Boolean(sourceSyncBusyReason)}
                    onSourceSelect={selectSource}
                    onSyncFailedSources={(result) => void handleSyncFailedSources(result)}
                  />
                ) : (
                  <p className="mt-2 text-sm text-[var(--text-muted)]">本轮还没有触发新的刷新。</p>
                )}
              </section>

              <FetchHistoryCard
                emptyText="所有订阅源还没有刷新记录。"
                fetches={allFetches}
                layout="section"
                showSourceName
                title="所有订阅源刷新记录"
              />

              <SourceCategoryManager
                categories={sourceCategorySummaries}
                layout="section"
                updatingCategoryPath={updatingCategoryPath}
                onDelete={(path) => void handleDeleteCategory(path)}
                onRename={(oldPath, newPath) => void handleRenameCategory(oldPath, newPath)}
              />
            </div>
          </aside>
        </div>
      </section>

      <button
        aria-label="调整全局状态和当前订阅源详情宽度"
        className="hidden cursor-col-resize border-x border-[var(--border-subtle)] bg-[var(--bg-app)] transition hover:bg-[var(--bg-hover)] xl:block"
        onPointerDown={startSourceWorkspaceResize}
        type="button"
      />

      <main className="min-h-[520px] min-w-0 overflow-y-auto px-6 py-5 xl:min-h-0">
        <div className="mx-auto grid max-w-5xl content-start gap-4">
          {selectedSource ? (
            <>
              <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex min-w-0 items-start gap-3">
                    <SourceIcon source={selectedSource} size={36} />
                    <div className="min-w-0">
                      <div className="truncate text-base font-semibold text-[var(--text-main)]">{selectedSource.name}</div>
                      <div className="mt-2 break-all text-sm text-[var(--text-muted)]">{selectedSource.url}</div>
                      {selectedSource.site_url ? (
                        <div className="mt-1 break-all text-sm text-[var(--text-muted)]">site: {selectedSource.site_url}</div>
                      ) : null}
                    </div>
                  </div>
                  <SourceStatusPill isActive={selectedSource.is_active} />
                </div>
                <div className="mt-4 flex flex-wrap gap-2 text-[11px] text-[var(--text-soft)]">
                  <Pill label={selectedSource.kind.toUpperCase()} />
                  <Pill label={selectedSource.provenance} />
                  <Pill label={selectedSource.folder_path ?? '未分类'} />
                  <Pill label={`AI ${formatAIRouteLabel(selectedSource.ai_route)}`} />
                </div>
                <SourceDerivedInfo source={selectedSource} layout="detail" />
                <div className="mt-4 flex flex-wrap gap-2">
                  <button
                    className="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-white shadow-sm"
                    onClick={() => onSourceEdit?.(selectedSource)}
                    type="button"
                  >
                    编辑订阅
                  </button>
                  <button
                    className="rounded-lg border border-[var(--border-subtle)] px-4 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                    onClick={() => void handleSyncSelected()}
                    type="button"
                    disabled={Boolean(sourceSyncBusyReason)}
                    title={sourceSyncBusyReason ?? undefined}
                  >
                    {isSyncingSingle ? '刷新中...' : '刷新当前来源'}
                  </button>
                  <button
                    className="inline-flex items-center gap-2 rounded-lg border border-red-200 px-4 py-2 text-sm text-red-700 transition hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
                    onClick={() => void handleLoadSourceDeletePreview()}
                    type="button"
                    disabled={isLoadingSourceDeletePreview || isDeletingSource}
                  >
                    <Trash2 size={15} />
                    {isLoadingSourceDeletePreview ? '加载预览...' : '删除源'}
                  </button>
                </div>
              </div>

              {selectedSyncProgress ? <SourceSyncProgressPanel progress={selectedSyncProgress} /> : null}

              {sourceDeletePreview?.source.id === selectedSource.id ? (
                <SourceDeletePreviewPanel
                  confirmed={sourceDeleteConfirmed}
                  isDeleting={isDeletingSource}
                  isLoading={isLoadingSourceDeletePreview}
                  onCancel={handleCancelSourceDeletePreview}
                  onConfirmChange={setSourceDeleteConfirmed}
                  onDelete={() => void handleDeleteSelectedSource()}
                  onRefresh={() => void handleLoadSourceDeletePreview()}
                  preview={sourceDeletePreview}
                />
              ) : null}

              <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-4 text-sm text-[var(--text-muted)]">
                <div className="font-medium text-[var(--text-main)]">当前元数据</div>
                <div className="mt-2">provenance: {selectedSource.provenance}</div>
                <div className="mt-1">created_from_import_run_id: {selectedSource.created_from_import_run_id ?? '—'}</div>
                <div className="mt-1">item_count: {formatNullableCount(selectedSource.item_count)}</div>
                <div className="mt-1">latest_fetch_available: {formatSourceFetchAvailability(selectedSource)}</div>
                <div className="mt-1">latest_fetch_counts: {formatLatestFetchCounters(selectedSource) ?? '—'}</div>
                <div className="mt-1">latest_fetch_finished_at: {formatDateTime(selectedSource.latest_fetch_finished_at)}</div>
                {selectedSource.latest_fetch_error_message ? (
                  <div className="mt-1 break-words text-[var(--warning)]">
                    latest_fetch_error: {selectedSource.latest_fetch_error_message}
                  </div>
                ) : null}
                <div className="mt-1">icon_source: {formatSourceIconSource(selectedSource.icon_source)}</div>
                <div className="mt-1 break-all">icon_url: {selectedSource.icon_url ?? '—'}</div>
                <div className="mt-1">icon_updated_at: {formatDateTime(selectedSource.icon_updated_at)}</div>
                <div className="mt-1">updated_at: {formatDateTime(selectedSource.updated_at)}</div>
              </div>

              <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-4">
                <div className="text-sm font-semibold text-[var(--text-main)]">当前订阅源刷新结果</div>
                {selectedSyncResult ? (
                  <SyncSummary result={selectedSyncResult} onSourceSelect={selectSource} />
                ) : (
                  <p className="mt-2 text-sm text-[var(--text-muted)]">当前来源本轮还没有新的刷新结果。</p>
                )}
              </div>

              <FetchHistoryCard
                emptyText="当前来源还没有刷新记录。"
                fetches={fetches}
                title="当前订阅源刷新记录"
              />
            </>
          ) : (
            <PanelHint title="请选择 source" body="从左侧分类列表选择一个订阅源，或进入“导入 / OPML”新增 source。" />
          )}
        </div>
      </main>
    </div>
  );
}

function buildCreatePayload(formState: SourceFormState): SourceCreatePayload {
  return {
    name: formState.name.trim(),
    kind: formState.kind,
    url: formState.url.trim(),
    site_url: normalizeOptionalField(formState.siteUrl),
    folder_path: normalizeOptionalField(formState.folderPath),
    is_active: formState.isActive,
    ai_route: formState.aiRoute,
    icon_url: formState.iconUrl,
    icon_source: formState.iconSource,
  };
}

function buildUpdatePayload(formState: SourceFormState): SourceUpdatePayload {
  return {
    name: formState.name.trim(),
    url: formState.url.trim(),
    site_url: normalizeOptionalField(formState.siteUrl),
    folder_path: normalizeOptionalField(formState.folderPath),
    is_active: formState.isActive,
    ai_route: formState.aiRoute,
  };
}

function normalizeOptionalField(value: string) {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function readStoredImportPreviewPaneWidth() {
  const rawValue = window.localStorage.getItem(IMPORT_PREVIEW_PANE_WIDTH_KEY);
  const parsedValue = rawValue ? Number.parseInt(rawValue, 10) : Number.NaN;
  if (!Number.isFinite(parsedValue)) {
    return IMPORT_PREVIEW_PANE_DEFAULT_WIDTH;
  }

  return clamp(parsedValue, IMPORT_PREVIEW_PANE_MIN_WIDTH, IMPORT_PREVIEW_PANE_MAX_WIDTH);
}

function readStoredSourceListWidth() {
  const rawValue = window.localStorage.getItem(SOURCE_LIST_WIDTH_KEY);
  const parsedValue = rawValue ? Number.parseInt(rawValue, 10) : Number.NaN;
  if (!Number.isFinite(parsedValue)) {
    return SOURCE_LIST_DEFAULT_WIDTH;
  }

  return clamp(parsedValue, SOURCE_LIST_MIN_WIDTH, SOURCE_LIST_MAX_WIDTH);
}

function readStoredSourceWorkspaceLeftWidth() {
  const rawValue = window.localStorage.getItem(SOURCE_WORKSPACE_LEFT_WIDTH_KEY);
  const parsedValue = rawValue ? Number.parseInt(rawValue, 10) : Number.NaN;
  if (!Number.isFinite(parsedValue)) {
    return SOURCE_WORKSPACE_LEFT_DEFAULT_WIDTH;
  }

  return clamp(parsedValue, SOURCE_WORKSPACE_LEFT_MIN_WIDTH, SOURCE_WORKSPACE_LEFT_MAX_WIDTH);
}

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

function buildInitialOpmlCandidateSelections(candidates: OpmlImportCandidateDto[]) {
  return Object.fromEntries(
    candidates.map((candidate) => [
      candidate.candidate_id,
      {
        selected: candidate.selected_by_default && isOpmlCandidateSelectable(candidate),
        aiRoute: candidate.suggested_ai_route,
        folderPath: candidate.folder_path ?? '',
      },
    ]),
  ) as Record<string, OpmlCandidateSelectionState>;
}

function isOpmlCandidateSelectable(candidate: OpmlImportCandidateDto) {
  return candidate.status === 'ready' || candidate.status === 'duplicate_in_file';
}

function buildUiErrorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError) {
    const conflictSourceId = getConflictSourceId(error);
    if (conflictSourceId !== null) {
      return `${error.message}（已存在 source #${conflictSourceId}，已尝试定位到该条目）`;
    }
    return error.message;
  }

  if (error instanceof Error) {
    return error.message;
  }

  return fallback;
}

function getConflictSourceId(error: unknown) {
  if (!(error instanceof ApiError)) {
    return null;
  }

  if (!error.detail || typeof error.detail !== 'object' || !('detail' in error.detail)) {
    return null;
  }

  const nestedDetail = (error.detail as {detail: unknown}).detail;
  if (!nestedDetail || typeof nestedDetail !== 'object' || !('source_id' in nestedDetail)) {
    return null;
  }

  const sourceId = (nestedDetail as {source_id: unknown}).source_id;
  return typeof sourceId === 'number' ? sourceId : null;
}

function getSourceDeletePreviewFromError(error: unknown): SourceDeletePreviewDto | null {
  if (!(error instanceof ApiError)) {
    return null;
  }
  if (!error.detail || typeof error.detail !== 'object' || !('detail' in error.detail)) {
    return null;
  }
  const nestedDetail = (error.detail as {detail: unknown}).detail;
  if (!nestedDetail || typeof nestedDetail !== 'object' || !('preview' in nestedDetail)) {
    return null;
  }
  const preview = (nestedDetail as {preview: unknown}).preview;
  if (!preview || typeof preview !== 'object' || !('preview_token' in preview) || !('counts' in preview)) {
    return null;
  }
  return preview as SourceDeletePreviewDto;
}

function formatDateTime(value: string | null) {
  if (!value) {
    return '—';
  }

  return new Date(value).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function CreateSourceFlow({
  categorySuggestions,
  errorMessage,
  formState,
  isPreviewing,
  isSaving,
  message,
  previewResult,
  previewUrlInput,
  onFillPreviewTitle,
  onPreview,
  onPreviewUrlInputChange,
  onReset,
  onSave,
  onUpdateForm,
}: {
  categorySuggestions: string[];
  errorMessage: string | null;
  formState: SourceFormState;
  isPreviewing: boolean;
  isSaving: boolean;
  message: string | null;
  previewResult: SourcePreviewResponseDto | null;
  previewUrlInput: string;
  onFillPreviewTitle: () => void;
  onPreview: () => void;
  onPreviewUrlInputChange: (nextValue: string) => void;
  onReset: () => void;
  onSave: () => void;
  onUpdateForm: <K extends keyof SourceFormState>(key: K, value: SourceFormState[K]) => void;
}) {
  return (
    <div className="grid gap-5">
      {message ? <MessageCard tone="success" text={message} /> : null}
      {errorMessage ? <MessageCard tone="error" text={errorMessage} /> : null}

      <div className="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-4">
        <div className="text-sm font-semibold text-[var(--text-main)]">Step 1 · Preview</div>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          粘贴 Feed URL 或 `rsshub://` route，先读取真实 metadata 与最近条目。
        </p>
        <div className="mt-4 flex flex-col gap-3 sm:flex-row">
          <input
            className="flex-1 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-app)] px-3 py-2"
            value={previewUrlInput}
            placeholder="https://example.com/feed.xml 或 rsshub://..."
            onChange={(event) => onPreviewUrlInputChange(event.target.value)}
          />
          <button
            className="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-white shadow-sm"
            onClick={onPreview}
            type="button"
            disabled={isPreviewing}
          >
            {isPreviewing ? '预览中...' : '预览'}
          </button>
        </div>
      </div>

      {previewResult ? (
        <>
          <PreviewSummaryCard
            preview={previewResult}
            onOpenOriginalSite={() => {
              const siteUrl = previewResult.source.site_url;
              if (siteUrl) {
                window.open(siteUrl, '_blank', 'noopener,noreferrer');
              }
            }}
          />

          <div className="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-4">
            <div className="text-sm font-semibold text-[var(--text-main)]">Step 2 · Confirm</div>
            <p className="mt-1 text-sm text-[var(--text-muted)]">
              Preview 结果只读；这里确认最终要写入 source registry 的字段。
            </p>

            <div className="mt-4 grid gap-4">
              <LabeledField label="名称">
                <div className="flex gap-2">
                  <input
                    className="w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-app)] px-3 py-2"
                    value={formState.name}
                    onChange={(event) => onUpdateForm('name', event.target.value)}
                  />
                  <button
                    className="shrink-0 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)]"
                    onClick={onFillPreviewTitle}
                    type="button"
                    disabled={!previewResult.source.title || formState.name === (previewResult.source.title ?? '')}
                  >
                    填充默认
                  </button>
                </div>
              </LabeledField>

              <LabeledField label="类型">
                <select
                  className="w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-app)] px-3 py-2"
                  value={formState.kind}
                  onChange={(event) => onUpdateForm('kind', event.target.value as SourceKind)}
                >
                  <option value="rss">RSS</option>
                  <option value="rsshub">RSSHub</option>
                </select>
              </LabeledField>

              <LabeledField label="Canonical Source URL">
                <input
                  className="w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-hover)] px-3 py-2 text-[var(--text-soft)]"
                  value={formState.url}
                  readOnly
                />
              </LabeledField>

              <LabeledField label="Site URL">
                <input
                  className="w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-app)] px-3 py-2"
                  value={formState.siteUrl}
                  onChange={(event) => onUpdateForm('siteUrl', event.target.value)}
                />
              </LabeledField>

              <CategoryField
                suggestions={categorySuggestions}
                value={formState.folderPath}
                onChange={(value) => onUpdateForm('folderPath', value)}
              />

              <AIRouteField value={formState.aiRoute} onChange={(value) => onUpdateForm('aiRoute', value)} />

              <label className="flex items-center gap-3 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-app)] px-3 py-3 text-sm text-[var(--text-main)]">
                <input
                  checked={formState.isActive}
                  onChange={(event) => onUpdateForm('isActive', event.target.checked)}
                  type="checkbox"
                />
                启用 source
              </label>

              <div className="flex flex-wrap gap-2 pt-2">
                <button
                  className="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-white shadow-sm"
                  onClick={onSave}
                  type="button"
                  disabled={isSaving}
                >
                  {isSaving ? '创建中...' : '创建 Source'}
                </button>
                <button
                  className="rounded-lg border border-[var(--border-subtle)] px-4 py-2 text-sm text-[var(--text-main)]"
                  onClick={onReset}
                  type="button"
                >
                  重新开始
                </button>
              </div>
            </div>
          </div>
        </>
      ) : (
        <PanelHint title="等待预览" body="完成 preview 后，会在这里展示 metadata 回填与确认创建表单。" />
      )}
    </div>
  );
}

function PanelHint({title, body}: {title: string; body: string}) {
  return (
    <div className="rounded-2xl border border-dashed border-[var(--border-subtle)] bg-[var(--bg-panel)] px-4 py-6 text-center">
      <div className="text-sm font-semibold text-[var(--text-main)]">{title}</div>
      <p className="mt-2 text-sm text-[var(--text-muted)]">{body}</p>
    </div>
  );
}

function SourceListButton({
  source,
  isSelected,
  onSelect,
}: {
  source: SourceDto;
  isSelected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      className={`w-full rounded-lg border px-4 py-3 text-left transition-colors ${
        isSelected
          ? 'border-[var(--accent)] bg-[var(--bg-panel-strong)]'
          : 'border-[var(--border-subtle)] bg-[var(--bg-panel)] hover:bg-[var(--bg-hover)]'
      }`}
      onClick={onSelect}
      type="button"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <SourceIcon source={source} size={24} />
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-[var(--text-main)]">{source.name}</div>
            <div className="mt-1 truncate text-xs text-[var(--text-soft)]">{source.url}</div>
          </div>
        </div>
        <SourceStatusPill isActive={source.is_active} />
      </div>
      <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-[var(--text-soft)]">
        <Pill label={source.kind.toUpperCase()} />
        <Pill label={source.provenance} />
        <Pill label={`AI ${formatAIRouteLabel(source.ai_route)}`} />
        {source.source_profile_pending_external_suggestion ? <PendingSourceProfilePill /> : null}
      </div>
      <SourceDerivedInfo source={source} layout="list" />
    </button>
  );
}

function PendingSourceProfilePill() {
  return (
    <span className="inline-flex max-w-full items-center rounded-full border border-amber-300/70 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-800 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200">
      外部 Source Profile 建议待审阅
    </span>
  );
}

function SourceDerivedInfo({source, layout}: {source: SourceDto; layout: 'list' | 'detail'}) {
  const latestFetchCounters = formatLatestFetchCounters(source);
  const errorSummary = formatLatestFetchErrorSummary(source, layout === 'detail' ? 140 : 76);
  const textClassName = source.latest_fetch_is_available === false ? 'text-[var(--warning)]' : 'text-[var(--text-muted)]';

  return (
    <div className={layout === 'detail' ? 'mt-4' : 'mt-3'}>
      <div className={`text-xs ${textClassName}`} title={formatLatestFetchTooltip(source)}>
        已入库 {formatNullableCount(source.item_count)} 条 · {formatSourceFetchAvailability(source)}
      </div>
      {latestFetchCounters ? <div className="mt-1 text-xs text-[var(--text-soft)]">{latestFetchCounters}</div> : null}
      {errorSummary ? <div className="mt-1 break-words text-xs text-[var(--warning)]">{errorSummary}</div> : null}
    </div>
  );
}

function SourceCategoryManager({
  categories,
  layout = 'card',
  updatingCategoryPath,
  onDelete,
  onRename,
}: {
  categories: SourceCategoryDto[];
  layout?: 'card' | 'section';
  updatingCategoryPath: string | null;
  onDelete: (path: string) => void;
  onRename: (oldPath: string, newPath: string) => void;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const categoryPaths = useMemo(() => new Set(categories.map((category) => category.path)), [categories]);

  useEffect(() => {
    setDrafts((current) => {
      const nextDrafts: Record<string, string> = {};
      for (const category of categories) {
        nextDrafts[category.path] = current[category.path] ?? category.path;
      }
      return nextDrafts;
    });
  }, [categories]);

  function updateDraft(path: string, nextValue: string) {
    setDrafts((current) => ({...current, [path]: nextValue}));
  }

  function submitRename(category: SourceCategoryDto) {
    const nextPath = (drafts[category.path] ?? category.path).trim();
    if (!nextPath || nextPath === category.path) {
      return;
    }

    if (
      categoryPaths.has(nextPath) &&
      !window.confirm(`分类“${nextPath}”已存在。继续会把“${category.path}”合并过去。`)
    ) {
      return;
    }

    onRename(category.path, nextPath);
  }

  function submitDelete(category: SourceCategoryDto) {
    if (!window.confirm(`删除分类“${category.path}”？不会删除 source，只会把 ${category.source_count} 个 source 设为未分类。`)) {
      return;
    }

    onDelete(category.path);
  }

  const containerClassName =
    layout === 'section'
      ? 'px-5 py-5'
      : 'rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-4';

  return (
    <div className={containerClassName}>
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm font-semibold text-[var(--text-main)]">订阅源分类管理</div>
        <span className="text-xs text-[var(--text-soft)]">{categories.length} 个分类</span>
      </div>

      {categories.length ? (
        <div className="mt-3 max-h-80 space-y-3 overflow-y-auto pr-1">
          {categories.map((category) => {
            const draftValue = drafts[category.path] ?? category.path;
            const normalizedDraft = draftValue.trim();
            const isBusy = updatingCategoryPath === category.path;
            const hasAnyBusyCategory = updatingCategoryPath !== null;
            return (
              <div
                key={category.path}
                className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3"
              >
                <div className="flex items-center justify-between gap-3 text-xs text-[var(--text-soft)]">
                  <span>{category.source_count} 个 source</span>
                  <button
                    className="rounded-lg border border-red-200 px-2 py-1 text-red-600 transition hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
                    disabled={hasAnyBusyCategory}
                    onClick={() => submitDelete(category)}
                    type="button"
                  >
                    删除
                  </button>
                </div>
                <div className="mt-3 flex gap-2">
                  <input
                    className="min-w-0 flex-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-app)] px-3 py-2 text-sm"
                    value={draftValue}
                    onChange={(event) => updateDraft(category.path, event.target.value)}
                  />
                  <button
                    className="rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                    disabled={hasAnyBusyCategory || !normalizedDraft || normalizedDraft === category.path}
                    onClick={() => submitRename(category)}
                    type="button"
                  >
                    {isBusy ? '更新中...' : '重命名'}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <p className="mt-3 text-sm text-[var(--text-muted)]">当前没有已分类的订阅源。</p>
      )}
    </div>
  );
}

function LabeledField({label, children}: {label: string; children: ReactNode}) {
  return (
    <label className="grid gap-2">
      <span className="text-sm font-medium text-[var(--text-main)]">{label}</span>
      {children}
    </label>
  );
}

function CategoryField({
  suggestions,
  value,
  onChange,
}: {
  suggestions: string[];
  value: string;
  onChange: (nextValue: string) => void;
}) {
  const [isOpen, setIsOpen] = useState(false);

  const normalizedValue = value.trim().toLowerCase();
  const filteredSuggestions = useMemo(() => {
    if (normalizedValue.length === 0) {
      return suggestions.slice(0, 8);
    }

    return suggestions
      .filter((suggestion) => suggestion.toLowerCase().includes(normalizedValue))
      .slice(0, 8);
  }, [normalizedValue, suggestions]);

  const hasExactMatch = suggestions.some((suggestion) => suggestion.toLowerCase() === normalizedValue);
  const showCreateOption = value.trim().length > 0 && !hasExactMatch;
  const showDropdown = isOpen && (filteredSuggestions.length > 0 || showCreateOption);

  const applyValue = (nextValue: string) => {
    onChange(nextValue);
    setIsOpen(false);
  };

  return (
    <LabeledField label="分类">
      <div
        className="space-y-2"
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
            setIsOpen(false);
          }
        }}
      >
        <div className="relative">
          <input
            className="w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 pr-12"
            value={value}
            onChange={(event) => {
              onChange(event.target.value);
              setIsOpen(true);
            }}
            onFocus={() => setIsOpen(true)}
            placeholder={suggestions.length ? '可选择现有分类，也可直接输入新分类' : '输入分类'}
          />
          <button
            aria-label={isOpen ? '收起分类建议' : '展开分类建议'}
            className="absolute inset-y-1 right-1 rounded-lg px-3 text-sm text-[var(--text-muted)] transition hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]"
            onClick={() => setIsOpen((current) => !current)}
            type="button"
          >
            {isOpen ? '收起' : '展开'}
          </button>
          {showDropdown ? (
            <div className="absolute z-10 mt-2 w-full overflow-hidden rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-panel)] shadow-[0_20px_60px_rgba(15,23,42,0.18)]">
              {showCreateOption ? (
                <button
                  className="flex w-full items-center justify-between px-3 py-2 text-left text-sm text-[var(--text-main)] transition hover:bg-[var(--bg-hover)]"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => applyValue(value.trim())}
                  type="button"
                >
                  <span>使用“{value.trim()}”作为新分类</span>
                  <span className="text-xs text-[var(--text-soft)]">新建</span>
                </button>
              ) : null}
              {filteredSuggestions.map((suggestion) => (
                <button
                  key={suggestion}
                  className={`flex w-full items-center justify-between px-3 py-2 text-left text-sm transition hover:bg-[var(--bg-hover)] ${
                    suggestion.toLowerCase() === normalizedValue
                      ? 'bg-[var(--accent-soft)] text-[var(--accent)]'
                      : 'text-[var(--text-main)]'
                  }`}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => applyValue(suggestion)}
                  type="button"
                >
                  <span>{suggestion}</span>
                  <span className="text-xs text-[var(--text-soft)]">已有分类</span>
                </button>
              ))}
            </div>
          ) : null}
        </div>
        <div className="text-xs text-[var(--text-soft)]">
          {suggestions.length
            ? `已收录 ${suggestions.length} 个分类，可直接输入新值或从建议中选择。`
            : '当前还没有历史分类，可直接输入新的分类名称。'}
        </div>
      </div>
    </LabeledField>
  );
}

function AIRouteField({value, onChange}: {value: SourceAIRoute; onChange: (nextValue: SourceAIRoute) => void}) {
  return (
    <LabeledField label="AI 路由">
      <select
        className="w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-app)] px-3 py-2"
        value={value}
        onChange={(event) => onChange(event.target.value as SourceAIRoute)}
      >
        <option value="general">通用内容 · general</option>
        <option value="sensitive">敏感内容 · sensitive</option>
        <option value="disabled">不生成 AI · disabled</option>
      </select>
      <div className="text-xs text-[var(--text-soft)]">
        敏感来源会使用 sensitive profile；disabled 会保留来源刷新，但不生成洞察结果。
      </div>
    </LabeledField>
  );
}

function SourceStatusPill({isActive}: {isActive: boolean}) {
  return (
    <span
      className={`rounded-full px-2 py-1 text-[11px] font-medium ${
        isActive ? 'bg-[var(--accent-soft)] text-[var(--accent)]' : 'bg-[var(--bg-hover)] text-[var(--text-soft)]'
      }`}
    >
      {isActive ? 'active' : 'paused'}
    </span>
  );
}

function Pill({label}: {label: string}) {
  return <span className="rounded-full border border-[var(--border-subtle)] px-2 py-0.5">{label}</span>;
}

function SourceDeletePreviewPanel({
  confirmed,
  isDeleting,
  isLoading,
  onCancel,
  onConfirmChange,
  onDelete,
  onRefresh,
  preview,
}: {
  confirmed: boolean;
  isDeleting: boolean;
  isLoading: boolean;
  onCancel: () => void;
  onConfirmChange: (confirmed: boolean) => void;
  onDelete: () => void;
  onRefresh: () => void;
  preview: SourceDeletePreviewDto;
}) {
  const counts = preview.counts;
  const countRows = [
    ['刷新原始响应', counts.source_fetches],
    ['Source occurrence', counts.source_item_occurrences],
    ['未保存独占 item', counts.exclusive_unsaved_feed_items],
    ['保留 feed item', counts.retained_feed_items],
    ['保留 saved item', counts.saved_items_retained],
    ['Source Profile', counts.source_preference_profiles],
    ['Routing', counts.source_channel_tendencies],
    ['派生首页行', counts.homepage_read_models],
    ['Feed item 洞察产物', counts.feed_item_insight_artifacts],
    ['诊断引用脱钩', counts.diagnostic_operations_to_detach + counts.diagnostic_attempts_to_detach],
  ] as const;

  return (
    <div className="rounded-lg border border-red-200 bg-red-50/70 p-4 text-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <AlertTriangle className="mt-0.5 shrink-0 text-red-700" size={18} />
          <div className="min-w-0">
            <div className="font-semibold text-red-800">彻底删除源预览</div>
            <div className="mt-1 break-words text-red-700">
              {preview.source.name} · {preview.source.url}
            </div>
          </div>
        </div>
        <div className="flex shrink-0 gap-2">
          <button
            className="inline-flex items-center gap-1 rounded-lg border border-red-200 bg-white px-3 py-1.5 text-xs text-red-700 disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isLoading || isDeleting}
            onClick={onRefresh}
            type="button"
          >
            <RefreshCw size={13} />
            刷新预览
          </button>
          <button
            className="rounded-lg border border-red-200 bg-white px-3 py-1.5 text-xs text-red-700 disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isDeleting}
            onClick={onCancel}
            type="button"
          >
            取消
          </button>
        </div>
      </div>

      <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {countRows.map(([label, value]) => (
          <div key={label} className="rounded-md border border-red-100 bg-white/80 px-3 py-2">
            <div className="text-xs text-red-600">{label}</div>
            <div className="mt-1 font-semibold text-[var(--text-main)]">{formatNullableCount(value)}</div>
          </div>
        ))}
      </div>

      <div className="mt-4 grid gap-3 lg:grid-cols-3">
        <SourceDeletePlanList title="会删除" items={preview.delete_plan.deletes} />
        <SourceDeletePlanList title="会保留" items={preview.delete_plan.retains} />
        <SourceDeletePlanList title="会脱钩" items={preview.delete_plan.detaches} />
      </div>

      {preview.blockers.length ? (
        <div className="mt-4 rounded-md border border-red-200 bg-white p-3 text-red-700">
          <div className="font-medium">当前拒绝删除</div>
          <div className="mt-2 grid gap-2">
            {preview.blockers.map((blocker) => (
              <div key={blocker.operation_id} className="text-xs">
                操作 #{blocker.operation_id} · {formatSourceDeleteBlockerType(blocker.operation_type)}/
                {formatSourceDeleteBlockerKind(blocker.operation_kind)} · {formatSourceDeleteBlockerStatus(blocker.status)}
                <div className="mt-1 text-red-600">{blocker.reason}</div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <label className="flex min-w-0 items-center gap-2 text-sm text-red-800">
          <input
            checked={confirmed}
            className="h-4 w-4"
            disabled={!preview.can_delete || isDeleting}
            onChange={(event) => onConfirmChange(event.currentTarget.checked)}
            type="checkbox"
          />
          <span>确认只删除预览中列出的来源归属数据，保留知识库已保存记录。</span>
        </label>
        <button
          className="inline-flex items-center gap-2 rounded-lg bg-red-700 px-4 py-2 text-sm font-semibold text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
          disabled={!preview.can_delete || !confirmed || isDeleting}
          onClick={onDelete}
          type="button"
        >
          <Trash2 size={15} />
          {isDeleting ? '删除中...' : '确认删除源'}
        </button>
      </div>
    </div>
  );
}

function SourceDeletePlanList({items, title}: {items: string[]; title: string}) {
  return (
    <div className="rounded-md border border-red-100 bg-white/80 p-3">
      <div className="text-xs font-semibold text-red-800">{title}</div>
      <ul className="mt-2 grid gap-1 text-xs text-red-700">
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function formatSourceDeleteBlockerType(operationType: string) {
  switch (operationType) {
    case 'source_sync':
      return '来源刷新';
    case 'feed_item_insight_artifacts':
      return '条目洞察批量构建';
    case 'channel_lifecycle':
      return '频道生命周期';
    case 'feed_item_extract':
      return '条目提取';
    case 'feed_item_insight_artifact':
      return '条目洞察构建';
    default:
      return '其他任务';
  }
}

function formatSourceDeleteBlockerKind(operationKind: string | null | undefined) {
  if (!operationKind) {
    return '默认';
  }
  switch (operationKind) {
    case 'all':
      return '全部来源';
    case 'single':
      return '单个来源';
    case 'selected':
      return '选中来源';
    case 'global':
      return '全局';
    case 'current_source':
      return '当前来源';
    case 'source_sync':
      return '来源刷新';
    case 'source_sync_created_feed_items':
      return '来源刷新新增条目';
    case 'feed_item_insight_artifacts':
      return '条目洞察批量构建';
    case 'channel_lifecycle':
      return '频道生命周期';
    case 'feed_item_extract':
      return '条目提取';
    case 'feed_item_insight_artifact':
      return '条目洞察构建';
    default:
      return '其他范围';
  }
}

function formatSourceDeleteBlockerStatus(status: string) {
  switch (status) {
    case 'queued':
      return '排队中';
    case 'running':
      return '运行中';
    case 'stopping':
      return '停止中';
    default:
      return '未知状态';
  }
}

function formatAIRouteLabel(route: SourceAIRoute) {
  if (route === 'sensitive') {
    return 'sensitive';
  }
  if (route === 'disabled') {
    return 'off';
  }
  return 'general';
}

function formatOpmlCandidateStatus(status: OpmlImportCandidateDto['status']) {
  if (status === 'ready') {
    return 'ready';
  }
  if (status === 'matched_existing') {
    return 'matched';
  }
  if (status === 'duplicate_in_file') {
    return 'duplicate';
  }
  return 'invalid';
}

function MessageCard({tone, text}: {tone: 'success' | 'error'; text: string}) {
  return (
    <div
      className={`rounded-lg border px-3 py-2 text-sm ${
        tone === 'success'
          ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
          : 'border-red-200 bg-red-50 text-red-700'
      }`}
    >
      {text}
    </div>
  );
}

function ImportPreviewPane({
  isPreviewingOpmlCandidate,
  opmlCandidate,
  opmlPreviewResult,
  singlePreviewResult,
}: {
  isPreviewingOpmlCandidate: boolean;
  opmlCandidate: OpmlImportCandidateDto | null;
  opmlPreviewResult: SourcePreviewResponseDto | null;
  singlePreviewResult: SourcePreviewResponseDto | null;
}) {
  const activePreview = opmlPreviewResult ?? singlePreviewResult;

  return (
    <>
      <div className="border-b border-[var(--border-subtle)] px-6 py-4">
        <div className="text-sm font-semibold text-[var(--text-main)]">Feed Preview</div>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          {opmlCandidate
            ? '当前显示选中的 OPML source 内容。'
            : '单源添加会自动显示；OPML candidate 可点击读取内容。'}
        </p>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto px-6 py-5">
        {opmlCandidate ? (
          <div className="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-4">
            <div className="flex items-center gap-2">
              <SourceIcon source={opmlCandidate} size={24} />
              <div className="min-w-0 truncate text-sm font-semibold text-[var(--text-main)]">{opmlCandidate.name}</div>
            </div>
            <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-[var(--text-soft)]">
              <Pill label={opmlCandidate.status} />
              {opmlCandidate.kind ? <Pill label={opmlCandidate.kind.toUpperCase()} /> : null}
              <Pill label={opmlCandidate.folder_path ?? '未分类'} />
              <Pill label={formatSourceIconSource(opmlCandidate.icon_source)} />
            </div>
            {opmlCandidate.url ? (
              <div className="mt-3 break-all text-xs text-[var(--text-muted)]">canonical: {opmlCandidate.url}</div>
            ) : null}
          </div>
        ) : null}

        {isPreviewingOpmlCandidate ? (
          <PanelHint title="读取中" body="正在拉取这个 source 的 metadata 和最近条目。" />
        ) : activePreview ? (
          <>
            {opmlPreviewResult ? (
              <PreviewSummaryCard
                preview={opmlPreviewResult}
                onOpenOriginalSite={() => {
                  const siteUrl = opmlPreviewResult.source.site_url;
                  if (siteUrl) {
                    window.open(siteUrl, '_blank', 'noopener,noreferrer');
                  }
                }}
              />
            ) : null}
            <PreviewEntriesCard preview={activePreview} />
          </>
        ) : (
          <PanelHint title="等待预览" body="中间完成单源 preview，或在 OPML candidate 上点击“读取内容”。" />
        )}
      </div>
    </>
  );
}

function PreviewSummaryCard({
  preview,
  onOpenOriginalSite,
}: {
  preview: SourcePreviewResponseDto;
  onOpenOriginalSite: () => void;
}) {
  return (
    <div className="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <SourceIcon
            source={{
              name: preview.source.title ?? preview.source.canonical_url,
              icon_url: preview.source.icon_url,
              icon_source: preview.source.icon_source,
            }}
            size={32}
          />
          <div className="min-w-0">
            <div className="text-sm font-semibold text-[var(--text-main)]">Preview Summary</div>
            <div className="mt-2 text-lg font-semibold text-[var(--text-main)]">{preview.source.title ?? '未提供标题'}</div>
            <div className="mt-1 break-all text-sm text-[var(--text-muted)]">canonical: {preview.source.canonical_url}</div>
            <div className="mt-1 break-all text-sm text-[var(--text-muted)]">读取地址：{preview.source.resolved_url}</div>
          </div>
        </div>
        {preview.source.site_url ? (
          <button
            className="rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)]"
            onClick={onOpenOriginalSite}
            type="button"
          >
            打开来源站点
          </button>
        ) : null}
      </div>
      {preview.source.description ? <p className="mt-3 text-sm text-[var(--text-muted)]">{preview.source.description}</p> : null}
      <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-[var(--text-soft)]">
        <Pill label={preview.source.detected_kind.toUpperCase()} />
        <Pill label={formatSourceIconSource(preview.source.icon_source)} />
        {preview.source.language ? <Pill label={preview.source.language} /> : null}
        <Pill label={`HTTP ${preview.diagnostics.http_status}`} />
      </div>
    </div>
  );
}

function PreviewEntriesCard({preview}: {preview: SourcePreviewResponseDto}) {
  return (
    <div className="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-4">
      <div className="text-sm font-semibold text-[var(--text-main)]">Preview 条目</div>
      <p className="mt-1 text-sm text-[var(--text-muted)]">创建前先确认 feed 里最近的内容是否正确。</p>
      <div className="mt-3 space-y-3">
        {preview.entries.length ? (
          preview.entries.map((entry, index) => (
            <div key={`${entry.original_url}-${index}`} className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
              <div className="text-sm font-medium text-[var(--text-main)]">{entry.title}</div>
              <div className="mt-1 text-xs text-[var(--text-soft)]">
                {entry.published_at ? formatDateTime(entry.published_at) : '无发布时间'}
                {entry.author_name ? ` · ${entry.author_name}` : ''}
              </div>
              {entry.summary_text ? <div className="mt-2 text-sm text-[var(--text-muted)]">{entry.summary_text}</div> : null}
              <div className="mt-3">
                <button
                  className="rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)]"
                  onClick={() => window.open(entry.original_url, '_blank', 'noopener,noreferrer')}
                  type="button"
                >
                  打开原文
                </button>
              </div>
            </div>
          ))
        ) : (
          <PanelHint title="没有可预览条目" body="这个 feed 能被读取，但目前没有提取到可展示的有效条目。" />
        )}
      </div>
      <div className="mt-4 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3 text-xs text-[var(--text-muted)]">
        <div>requested_url: {preview.diagnostics.requested_url}</div>
        <div className="mt-1 break-all">canonical_url: {preview.source.canonical_url}</div>
        <div className="mt-1 break-all">resolved_url: {preview.source.resolved_url}</div>
        <div className="mt-1">final_url: {preview.diagnostics.final_url}</div>
        <div className="mt-1">content_type: {preview.diagnostics.content_type ?? '未知'}</div>
      </div>
    </div>
  );
}

function OpmlImportCard({
  bulkAiRoute,
  categorySuggestions,
  candidateSelections,
  isCommittingImport,
  isImporting,
  lastImportResult,
  previewResult,
  onApplyBulkAiRoute,
  onBulkAiRouteChange,
  onCandidateAiRouteChange,
  onCandidateFolderPathChange,
  onCandidatePreview,
  onCandidateSelectedChange,
  onCommit,
  onPreview,
  previewingCandidateId,
  selectedPreviewCandidateId,
}: {
  bulkAiRoute: SourceAIRoute;
  categorySuggestions: string[];
  candidateSelections: Record<string, OpmlCandidateSelectionState>;
  isCommittingImport: boolean;
  isImporting: boolean;
  lastImportResult: SourceImportResponseDto | null;
  previewResult: OpmlImportPreviewResponseDto | null;
  onApplyBulkAiRoute: () => void;
  onBulkAiRouteChange: (nextValue: SourceAIRoute) => void;
  onCandidateAiRouteChange: (candidateId: string, nextValue: SourceAIRoute) => void;
  onCandidateFolderPathChange: (candidateId: string, nextValue: string) => void;
  onCandidatePreview: (candidate: OpmlImportCandidateDto) => void;
  onCandidateSelectedChange: (candidateId: string, selected: boolean) => void;
  onCommit: () => void;
  onPreview: (file: File | null) => void;
  previewingCandidateId: string | null;
  selectedPreviewCandidateId: string | null;
}) {
  const selectedCount = previewResult
    ? previewResult.candidates.filter((candidate) => candidateSelections[candidate.candidate_id]?.selected).length
    : 0;

  return (
    <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-4">
      <div className="text-sm font-semibold text-[var(--text-main)]">OPML 导入</div>
      <p className="mt-1 text-sm text-[var(--text-muted)]">先上传 OPML 生成候选项，再选择要写入的 sources。</p>
      <label className="mt-4 block cursor-pointer rounded-lg border border-dashed border-[var(--border-subtle)] px-4 py-6 text-center text-sm text-[var(--text-muted)]">
        <input
          className="hidden"
          disabled={isImporting}
          onChange={(event) => {
            onPreview(event.target.files?.[0] ?? null);
            event.currentTarget.value = '';
          }}
          type="file"
          accept=".opml,.xml,text/xml,application/xml"
        />
        {isImporting ? '解析中...' : '点击选择 OPML 文件'}
      </label>
      {previewResult ? (
        <div className="mt-4 space-y-3">
          <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3 text-sm text-[var(--text-main)]">
            <div>filename: {previewResult.filename ?? '—'}</div>
            <div className="mt-1">
              total {previewResult.total_entries} · ready {previewResult.ready_count} · matched{' '}
              {previewResult.matched_existing_count} · duplicate {previewResult.duplicate_in_file_count} · invalid{' '}
              {previewResult.invalid_count}
            </div>
          </div>
          <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
            <div className="flex flex-wrap items-end gap-2">
              <LabeledField label="批量 AI 路由">
                <select
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-app)] px-3 py-2 text-sm"
                  value={bulkAiRoute}
                  onChange={(event) => onBulkAiRouteChange(event.target.value as SourceAIRoute)}
                >
                  <option value="general">general</option>
                  <option value="sensitive">sensitive</option>
                  <option value="disabled">disabled</option>
                </select>
              </LabeledField>
              <button
                className="rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                onClick={onApplyBulkAiRoute}
                type="button"
                disabled={selectedCount === 0}
              >
                应用到已选
              </button>
            </div>
            <div className="mt-2 text-xs text-[var(--text-soft)]">当前已选 {selectedCount} 个候选项。</div>
          </div>
          <div className="max-h-[42rem] space-y-2 overflow-y-auto pr-1">
            {previewResult.candidates.map((candidate) => (
              <OpmlCandidateRow
                key={candidate.candidate_id}
                candidate={candidate}
                categorySuggestions={categorySuggestions}
                isPreviewSelected={selectedPreviewCandidateId === candidate.candidate_id}
                isPreviewing={previewingCandidateId === candidate.candidate_id}
                selection={candidateSelections[candidate.candidate_id]}
                onAiRouteChange={(nextValue) => onCandidateAiRouteChange(candidate.candidate_id, nextValue)}
                onFolderPathChange={(nextValue) => onCandidateFolderPathChange(candidate.candidate_id, nextValue)}
                onPreview={() => onCandidatePreview(candidate)}
                onSelectedChange={(selected) => onCandidateSelectedChange(candidate.candidate_id, selected)}
              />
            ))}
          </div>
          <button
            className="w-full rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
            onClick={onCommit}
            type="button"
            disabled={isCommittingImport || selectedCount === 0}
          >
            {isCommittingImport ? '提交中...' : `提交已选 ${selectedCount} 个 source`}
          </button>
        </div>
      ) : null}
      {lastImportResult ? <ImportSummary result={lastImportResult} /> : null}
    </div>
  );
}

function OpmlCandidateRow({
  candidate,
  categorySuggestions,
  isPreviewSelected,
  isPreviewing,
  selection,
  onAiRouteChange,
  onFolderPathChange,
  onPreview,
  onSelectedChange,
}: {
  candidate: OpmlImportCandidateDto;
  categorySuggestions: string[];
  isPreviewSelected: boolean;
  isPreviewing: boolean;
  selection: OpmlCandidateSelectionState | undefined;
  onAiRouteChange: (nextValue: SourceAIRoute) => void;
  onFolderPathChange: (nextValue: string) => void;
  onPreview: () => void;
  onSelectedChange: (selected: boolean) => void;
}) {
  const isSelectable = isOpmlCandidateSelectable(candidate);
  const isSelected = selection?.selected ?? false;
  const aiRoute = selection?.aiRoute ?? candidate.suggested_ai_route;
  const folderPath = selection?.folderPath ?? candidate.folder_path ?? '';
  const canPreview = Boolean(candidate.kind && candidate.url);

  return (
    <div
      className={`rounded-lg border p-3 ${
        isPreviewSelected
          ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
          : 'border-[var(--border-subtle)] bg-[var(--bg-panel-strong)]'
      }`}
    >
      <div className="grid gap-3 lg:grid-cols-[auto_minmax(0,1fr)_320px] lg:items-start">
        <input
          className="mt-1"
          checked={isSelected}
          disabled={!isSelectable}
          onChange={(event) => onSelectedChange(event.target.checked)}
          type="checkbox"
        />
        <div className="flex min-w-0 flex-1 items-start gap-3">
          <SourceIcon source={candidate} size={24} />
          <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="break-words text-sm font-medium text-[var(--text-main)]">{candidate.name}</span>
            <Pill label={formatOpmlCandidateStatus(candidate.status)} />
            {candidate.kind ? <Pill label={candidate.kind.toUpperCase()} /> : null}
            <Pill label={formatSourceIconSource(candidate.icon_source)} />
          </div>
          <div className="mt-1 text-xs text-[var(--text-soft)]">
            #{candidate.entry_index + 1}
            {candidate.folder_path ? ` · ${candidate.folder_path}` : ''}
            {candidate.source_id ? ` · source #${candidate.source_id}` : ''}
          </div>
          {candidate.input_url ? (
            <div className="mt-2 break-all text-xs text-[var(--text-muted)]">input: {candidate.input_url}</div>
          ) : null}
          {candidate.url ? <div className="mt-1 break-all text-xs text-[var(--text-muted)]">canonical: {candidate.url}</div> : null}
          {candidate.duplicate_of_candidate_id ? (
            <div className="mt-1 break-all text-xs text-[var(--text-muted)]">
              duplicate_of: {candidate.duplicate_of_candidate_id}
            </div>
          ) : null}
          <div className="mt-2 text-xs text-[var(--text-muted)]">{candidate.message}</div>
          <button
            className="mt-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
            disabled={!canPreview || isPreviewing}
            onClick={onPreview}
            type="button"
          >
            {isPreviewing ? '读取中...' : '读取内容'}
          </button>
          </div>
        </div>
        <div className="space-y-3">
          {isSelectable ? (
            <>
              <CategoryField
                suggestions={categorySuggestions}
                value={folderPath}
                onChange={onFolderPathChange}
              />
              <LabeledField label="AI 路由">
                <select
                  className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-app)] px-3 py-2 text-sm disabled:opacity-60"
                  value={aiRoute}
                  onChange={(event) => onAiRouteChange(event.target.value as SourceAIRoute)}
                  disabled={!isSelected}
                >
                  <option value="general">通用内容 · general</option>
                  <option value="sensitive">敏感内容 · sensitive</option>
                  <option value="disabled">不生成 AI · disabled</option>
                </select>
              </LabeledField>
            </>
          ) : (
            <div className="text-xs text-[var(--text-soft)]">不参与 commit</div>
          )}
        </div>
      </div>
    </div>
  );
}

function ImportSummary({result}: {result: SourceImportResponseDto}) {
  return (
    <div className="mt-4 space-y-3">
      <div className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3 text-sm text-[var(--text-main)]">
        <div>filename: {result.filename ?? '—'}</div>
        <div className="mt-1">
          total {result.total_entries} · created {result.created_count} · matched {result.matched_existing_count} · invalid {result.invalid_count}
        </div>
        {result.selected_count !== null || result.skipped_count !== null ? (
          <div className="mt-1">
            selected {result.selected_count ?? '—'} · skipped {result.skipped_count ?? '—'}
          </div>
        ) : null}
      </div>
      <div className="max-h-56 space-y-2 overflow-y-auto">
        {result.results.map((entry, index) => (
          <div key={entry.candidate_id ?? `${entry.name}-${index}`} className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
            <div className="text-sm font-medium text-[var(--text-main)]">{entry.name}</div>
            <div className="mt-1 text-xs text-[var(--text-soft)]">
              {entry.status} · {entry.kind?.toUpperCase() ?? 'UNKNOWN'}
              {entry.folder_path ? ` · ${entry.folder_path}` : ''}
              {entry.ai_route ? ` · AI ${entry.ai_route}` : ''}
            </div>
            {entry.candidate_id ? <div className="mt-2 break-all text-xs text-[var(--text-muted)]">candidate: {entry.candidate_id}</div> : null}
            {entry.input_url ? <div className="mt-2 break-all text-xs text-[var(--text-muted)]">input: {entry.input_url}</div> : null}
            {entry.url ? <div className="mt-1 break-all text-xs text-[var(--text-muted)]">canonical: {entry.url}</div> : null}
            <div className="mt-2 text-xs text-[var(--text-muted)]">{entry.message}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function SyncSummary({
  result,
  disableSyncFailedSources = false,
  onSourceSelect,
  onSyncFailedSources,
}: {
  result: BulkSourceSyncResponseDto | SingleSourceSyncResponseDto;
  disableSyncFailedSources?: boolean;
  onSourceSelect?: (sourceId: number) => void;
  onSyncFailedSources?: (result: BulkSourceSyncResponseDto) => void;
}) {
  if ('results' in result) {
    const failedSourceIds = getFailedSourceIds(result);
    const canSyncFailedSources = failedSourceIds.length > 0 && !disableSyncFailedSources && Boolean(onSyncFailedSources);

    return (
      <div className="mt-3 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3 text-sm text-[var(--text-main)]">
          <span>
            source 总数 {result.source_count} · 成功 {result.success_count} · 失败 {result.error_count}
          </span>
          {failedSourceIds.length ? (
            <button
              className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-1.5 text-xs font-medium text-[var(--text-main)] transition hover:bg-[var(--bg-hover)] disabled:cursor-not-allowed disabled:opacity-50"
              disabled={!canSyncFailedSources}
              onClick={() => onSyncFailedSources?.(result)}
              type="button"
            >
              <RefreshCw size={13} />
              刷新失败 source
            </button>
          ) : null}
        </div>
        {result.results.length ? (
          <div className="space-y-2">
            {result.results.map((item) => (
              <SourceSyncResultCard key={item.fetch.id} item={item} onSourceSelect={onSourceSelect} />
            ))}
          </div>
        ) : (
          <p className="text-sm text-[var(--text-muted)]">正在等待第一个 source 完成。</p>
        )}
      </div>
    );
  }

  return (
    <SourceSyncResultCard className="mt-3" item={result} onSourceSelect={onSourceSelect} />
  );
}

function SourceSyncResultCard({
  item,
  className = '',
  onSourceSelect,
}: {
  item: SingleSourceSyncResponseDto;
  className?: string;
  onSourceSelect?: (sourceId: number) => void;
}) {
  const isClickable = Boolean(onSourceSelect);
  const classNames = [
    'w-full rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3 text-left',
    isClickable ? 'transition hover:border-[var(--accent)] hover:bg-[var(--bg-hover)]' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <button
      className={classNames}
      disabled={!isClickable}
      onClick={() => onSourceSelect?.(item.source_id)}
      title={isClickable ? `打开 source：${item.source_name}` : undefined}
      type="button"
    >
      <div className="text-sm font-medium text-[var(--text-main)]">{item.source_name}</div>
      <div className="mt-1 text-xs text-[var(--text-muted)]">
        {formatFetchStatus(item.fetch.status)} · HTTP {item.fetch.http_status ?? '—'} · 已见 {item.fetch.items_seen_count} · 新增{' '}
        {item.fetch.items_created_count}
      </div>
      <div className="mt-1 text-xs text-[var(--text-soft)]">
        洞察处理 {getSourceSyncInsightArtifactStats(item).processed_feed_item_count} · 生成{' '}
        {getSourceSyncInsightArtifactStats(item).artifact_rows_created} · 失败{' '}
        {getSourceSyncInsightArtifactStats(item).failed_feed_item_count}
      </div>
      {item.fetch.error_message ? (
        <div className="mt-2 break-words text-xs text-[var(--warning)]">{item.fetch.error_message}</div>
      ) : null}
    </button>
  );
}

function getFailedSourceIds(result: BulkSourceSyncResponseDto): number[] {
  const sourceIds = new Set<number>();
  for (const item of result.results) {
    if (item.fetch.status === 'error') {
      sourceIds.add(item.source_id);
    }
  }
  return Array.from(sourceIds);
}

function FetchHistoryCard({
  title,
  fetches,
  emptyText,
  layout = 'card',
  showSourceName = false,
}: {
  title: string;
  fetches: Array<SourceFetchDto | SourceFetchWithSourceDto>;
  emptyText: string;
  layout?: 'card' | 'section';
  showSourceName?: boolean;
}) {
  const containerClassName =
    layout === 'section'
      ? 'px-5 py-5'
      : 'rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-4';

  return (
    <div className={containerClassName}>
      <div className="text-sm font-semibold text-[var(--text-main)]">{title}</div>
      {fetches.length ? (
        <div className="mt-3 max-h-80 space-y-3 overflow-y-auto pr-1">
          {fetches.map((fetch) => (
            <div key={fetch.id} className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-[var(--text-main)]">
                    {showSourceName && 'source_name' in fetch ? fetch.source_name : formatFetchStatus(fetch.status)}
                  </div>
                  {showSourceName ? (
                    <div className="mt-1 text-xs text-[var(--text-soft)]">{formatFetchStatus(fetch.status)}</div>
                  ) : null}
                </div>
                <span className="shrink-0 text-xs text-[var(--text-soft)]">{formatDateTime(fetch.fetch_started_at)}</span>
              </div>
              <div className="mt-2 text-xs text-[var(--text-muted)]">
                HTTP {fetch.http_status ?? '—'} · 已见 {fetch.items_seen_count} · 新增 {fetch.items_created_count} · 匹配{' '}
                {fetch.items_matched_count} · 无效 {fetch.items_invalid_count}
              </div>
              {fetch.error_message ? <div className="mt-2 break-words text-xs text-red-600">{fetch.error_message}</div> : null}
            </div>
          ))}
        </div>
      ) : (
        <p className="mt-2 text-sm text-[var(--text-muted)]">{emptyText}</p>
      )}
    </div>
  );
}

function getSyncResultForSource(
  result: BulkSourceSyncResponseDto | SingleSourceSyncResponseDto | null,
  sourceId: number | null,
) {
  if (!result || sourceId === null) {
    return null;
  }
  if ('results' in result) {
    return result.results.find((item) => item.source_id === sourceId) ?? null;
  }
  return result.source_id === sourceId ? result : null;
}

function isTerminalSourceSyncStatus(status: SourceSyncOperationState['status']) {
  return status === 'completed' || status === 'stopped' || status === 'failed' || status === 'interrupted';
}

function formatFetchStatus(status: SourceFetchDto['status']) {
  switch (status) {
    case 'success':
      return '成功';
    case 'not_modified':
      return '无更新';
    case 'error':
      return '失败';
    default:
      return '等待中';
  }
}

function formatSourceFetchAvailability(source: SourceDto) {
  if (source.latest_fetch_is_available === true) {
    return source.latest_fetch_status ? `上次刷新：可用（${formatFetchStatus(source.latest_fetch_status)}）` : '上次刷新：可用';
  }
  if (source.latest_fetch_is_available === false) {
    return '上次刷新：失败';
  }
  if (source.latest_fetch_status) {
    return `上次刷新：${formatFetchStatus(source.latest_fetch_status)}`;
  }
  return '上次刷新：暂无记录';
}

function formatLatestFetchCounters(source: SourceDto) {
  if (!source.latest_fetch_status) {
    return null;
  }
  return [
    `已见 ${formatNullableCount(source.latest_fetch_items_seen_count)}`,
    `新增 ${formatNullableCount(source.latest_fetch_items_created_count)}`,
    `匹配 ${formatNullableCount(source.latest_fetch_items_matched_count)}`,
    `无效 ${formatNullableCount(source.latest_fetch_items_invalid_count)}`,
  ].join(' · ');
}

function formatLatestFetchErrorSummary(source: SourceDto, maxLength: number) {
  if (source.latest_fetch_is_available !== false) {
    return null;
  }
  const parts = [`HTTP ${source.latest_fetch_http_status ?? '—'}`];
  if (source.latest_fetch_error_message) {
    parts.push(truncateText(source.latest_fetch_error_message, maxLength));
  }
  return parts.join(' · ');
}

function formatLatestFetchTooltip(source: SourceDto) {
  const lines = [
    `已入库 ${formatNullableCount(source.item_count)} 条`,
    formatSourceFetchAvailability(source),
  ];
  const counters = formatLatestFetchCounters(source);
  if (counters) {
    lines.push(counters);
  }
  if (source.latest_fetch_http_status !== null) {
    lines.push(`HTTP ${source.latest_fetch_http_status}`);
  }
  if (source.latest_fetch_started_at) {
    lines.push(`开始：${formatDateTime(source.latest_fetch_started_at)}`);
  }
  if (source.latest_fetch_finished_at) {
    lines.push(`完成：${formatDateTime(source.latest_fetch_finished_at)}`);
  }
  if (source.latest_fetch_error_message) {
    lines.push(source.latest_fetch_error_message);
  }
  return lines.join('\n');
}

function formatNullableCount(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : value.toLocaleString('zh-CN');
}

function getSourceSyncInsightArtifactStats(result: SingleSourceSyncResponseDto) {
  return result.feed_item_insight_artifacts;
}

function truncateText(value: string, maxLength: number) {
  return value.length <= maxLength ? value : `${value.slice(0, maxLength - 1)}…`;
}
