import {useEffect, useMemo, useRef, useState, type ReactNode} from 'react';
import {AlertTriangle, ChevronDown, ChevronRight, Download, Eye, RefreshCw, Save, Search, SlidersHorizontal, Sparkles} from 'lucide-react';
import {
  InsightEvidencePackagePanel,
  InsightProfileManager,
  InsightRequestInspector,
} from '@/components/ai-settings-panel';
import {InsightChannelSettingsPanel} from '@/components/insight-channel-settings-panel';
import {
  activateInsightProfileVersion,
  applyInsightProfileCandidateSourceDrafts,
  exportInsightEvidencePackage,
  getAIInsightRequestPreview,
  getInsightEngineCoverage,
  getInsightEngineAttempt,
  getInsightEngineSettings,
  getInsightEvidencePackagePreview,
  getInsightProfileSettings,
  inspectInsightProfileCandidate,
  inspectInsightProfileDraft,
  listInsightEngineAttempts,
  rollbackInsightProfileVersion,
  saveInsightProfileVersion,
  updateInsightEngineSettings,
} from '@/services/settings-service';
import {getOperation, startFeedItemInsightArtifactBatchOperation} from '@/services/operation-service';
import {extractConflictOperation} from '@/hooks/info-flow/helpers';
import type {AppOperationDto, AppOperationStatus, SourceDto} from '@/types/source';
import type {
  InsightEngineAttempt,
  InsightEngineAttemptFilters,
  InsightEngineAttemptList,
  InsightEngineAttemptStatus,
  InsightEngineCoverage,
  InsightEngineCoverageItem,
  InsightEngineSettings,
  InsightEvidencePackageInput,
  InsightEvidencePackageArtifactMode,
  InsightEvidencePackagePreview,
  InsightEvidencePackageRange,
  InsightProfileCandidateInspectResponse,
  InsightProfileDraftInspectResponse,
  InsightProfileSettings,
  InsightProfileSourceType,
  InsightRequestPreview,
} from '@/types/settings';

const PROFILE_SOURCE_OPTIONS_DEFAULT: InsightProfileSourceType = 'manual_edit';
const ATTEMPT_STATUS_OPTIONS: Array<{value: InsightEngineAttemptStatus | 'all'; label: string}> = [
  {value: 'all', label: '全部状态'},
  {value: 'failed', label: '失败'},
  {value: 'skipped', label: '跳过'},
];
const ATTEMPT_PROFILE_OPTIONS = ['all', 'general', 'sensitive', 'disabled'];
const ATTEMPT_TIME_OPTIONS: Array<{value: '24h' | '7d' | '30d' | 'all'; label: string; days: number | null}> = [
  {value: '24h', label: '最近 24 小时', days: 1},
  {value: '7d', label: '最近 7 天', days: 7},
  {value: '30d', label: '最近 30 天', days: 30},
  {value: 'all', label: '全部时间', days: null},
];
const ATTEMPT_LIST_PAGE_SIZE = 50;
const ITEM_ARTIFACT_BATCH_OPERATION_LIMIT = 25;
const COVERAGE_PROBLEM_REASON_CODES = ['previous_failed_attempt', 'previous_skipped_attempt', 'partial_artifacts'];
const COVERAGE_INITIAL_GENERATION_REASON_CODES = ['no_attempt_history'];
const COVERAGE_INITIAL_GENERATION_REASON_CODE = 'no_attempt_history';
const MAX_PROFILE_CANDIDATE_FILE_BYTES = 1024 * 1024;
const OPERATION_TERMINAL_STATUSES = new Set<AppOperationStatus>(['completed', 'stopped', 'failed', 'interrupted']);
const OPERATION_ACTIVE_STATUSES = new Set<AppOperationStatus>(['queued', 'running', 'stopping']);

interface AttemptBatchRetryRun {
  id: number;
  startedAt: string;
  title: string;
  filterSummary: string;
  phase: 'creating' | 'running' | 'completed';
  operation: AppOperationDto | null;
  error: string | null;
}

export function InsightEngineSettingsPanel({
  sources,
  operationRefreshSignal,
  onChannelsChanged,
  onBackendOperation,
  onFeedItemInsightArtifactOperation,
  onOpenFeedItem,
  onSourcesChanged,
}: {
  sources: SourceDto[];
  operationRefreshSignal: number;
  onChannelsChanged?: () => void | Promise<void>;
  onBackendOperation: (operation: AppOperationDto) => void;
  onFeedItemInsightArtifactOperation: (feedItemId: number, mode: 'rebuild' | 'build_missing') => Promise<AppOperationDto>;
  onOpenFeedItem: (itemId: number) => void;
  onSourcesChanged?: () => Promise<SourceDto[]>;
}) {
  const [engineSettings, setEngineSettings] = useState<InsightEngineSettings | null>(null);
  const [autoLowPriorityEnabled, setAutoLowPriorityEnabled] = useState(false);
  const [lowPriorityThreshold, setLowPriorityThreshold] = useState(50);
  const [isEngineLoading, setIsEngineLoading] = useState(true);
  const [isEngineSaving, setIsEngineSaving] = useState(false);
  const [engineMessage, setEngineMessage] = useState<string | null>(null);
  const [engineError, setEngineError] = useState<string | null>(null);

  const [requestPreview, setRequestPreview] = useState<InsightRequestPreview | null>(null);
  const [selectedPreviewItemId, setSelectedPreviewItemId] = useState<number | null>(null);
  const [isPreviewLoading, setIsPreviewLoading] = useState(true);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const [evidenceRange, setEvidenceRange] = useState<InsightEvidencePackageRange>('90d');
  const [evidenceArtifactMode, setEvidenceArtifactMode] = useState<InsightEvidencePackageArtifactMode>('latest');
  const [evidenceStartDate, setEvidenceStartDate] = useState('');
  const [evidenceEndDate, setEvidenceEndDate] = useState('');
  const [evidencePreview, setEvidencePreview] = useState<InsightEvidencePackagePreview | null>(null);
  const [isEvidenceLoading, setIsEvidenceLoading] = useState(true);
  const [isEvidenceExporting, setIsEvidenceExporting] = useState(false);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);
  const [evidenceMessage, setEvidenceMessage] = useState<string | null>(null);

  const [insightProfileSettings, setInsightProfileSettings] = useState<InsightProfileSettings | null>(null);
  const [profileEditorText, setProfileEditorText] = useState('');
  const [profileEditNote, setProfileEditNote] = useState('');
  const [profileSourceType, setProfileSourceType] = useState<InsightProfileSourceType>(PROFILE_SOURCE_OPTIONS_DEFAULT);
  const [profileDraftInspection, setProfileDraftInspection] = useState<InsightProfileDraftInspectResponse | null>(null);
  const [isProfileLoading, setIsProfileLoading] = useState(true);
  const [isProfileSaving, setIsProfileSaving] = useState(false);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [profileMessage, setProfileMessage] = useState<string | null>(null);
  const [candidateInspection, setCandidateInspection] = useState<InsightProfileCandidateInspectResponse | null>(null);
  const [candidateJson, setCandidateJson] = useState<Record<string, unknown> | null>(null);
  const [candidateSelectedSourceIds, setCandidateSelectedSourceIds] = useState<number[]>([]);
  const [isCandidateLoading, setIsCandidateLoading] = useState(false);
  const [candidateError, setCandidateError] = useState<string | null>(null);
  const [candidateMessage, setCandidateMessage] = useState<string | null>(null);

  const [attemptStatus, setAttemptStatus] = useState<InsightEngineAttemptStatus | 'all'>('all');
  const [attemptSourceId, setAttemptSourceId] = useState<number | null>(null);
  const [attemptProfile, setAttemptProfile] = useState('all');
  const [attemptTimeRange, setAttemptTimeRange] = useState<'24h' | '7d' | '30d' | 'all'>('7d');
  const [attemptOffset, setAttemptOffset] = useState(0);
  const [attemptList, setAttemptList] = useState<InsightEngineAttemptList | null>(null);
  const [selectedAttempt, setSelectedAttempt] = useState<InsightEngineAttempt | null>(null);
  const [isAttemptLoading, setIsAttemptLoading] = useState(true);
  const [attemptError, setAttemptError] = useState<string | null>(null);
  const [attemptActionMessage, setAttemptActionMessage] = useState<string | null>(null);
  const [attemptActionItemId, setAttemptActionItemId] = useState<number | null>(null);
  const [attemptBatchRetryRun, setAttemptBatchRetryRun] = useState<AttemptBatchRetryRun | null>(null);
  const [coverageSourceId, setCoverageSourceId] = useState<number | null>(null);
  const [coverage, setCoverage] = useState<InsightEngineCoverage | null>(null);
  const [isCoverageLoading, setIsCoverageLoading] = useState(false);
  const [isCoverageBatchBuilding, setIsCoverageBatchBuilding] = useState(false);
  const [coverageError, setCoverageError] = useState<string | null>(null);
  const [coverageMessage, setCoverageMessage] = useState<string | null>(null);
  const [coverageActionItemId, setCoverageActionItemId] = useState<number | null>(null);
  const attemptRequestSeq = useRef(0);
  const attemptFiltersRef = useRef<InsightEngineAttemptFilters | null>(null);
  const coverageSourceIdRef = useRef<number | null>(null);
  const hasCoverageScanRef = useRef(false);

  const attemptFilters = useMemo<InsightEngineAttemptFilters>(() => {
    const range = ATTEMPT_TIME_OPTIONS.find((option) => option.value === attemptTimeRange);
    return {
      status: attemptStatus,
      sourceId: attemptSourceId,
      profile: attemptProfile === 'all' ? null : attemptProfile,
      createdAfter: range?.days ? new Date(Date.now() - range.days * 24 * 60 * 60 * 1000).toISOString() : null,
      limit: ATTEMPT_LIST_PAGE_SIZE,
      offset: attemptOffset,
    };
  }, [attemptOffset, attemptProfile, attemptSourceId, attemptStatus, attemptTimeRange]);

  useEffect(() => {
    attemptFiltersRef.current = attemptFilters;
  }, [attemptFilters]);

  useEffect(() => {
    coverageSourceIdRef.current = coverageSourceId;
  }, [coverageSourceId]);

  useEffect(() => {
    hasCoverageScanRef.current = coverage !== null;
  }, [coverage]);

  useEffect(() => {
    void loadEngineSettings();
    void loadInsightProfile();
    void loadEvidencePreview();
    void loadRequestPreview();
  }, []);

  useEffect(() => {
    void loadAttempts({append: attemptOffset > 0});
  }, [attemptFilters]);

  useEffect(() => {
    setCoverage(null);
    setCoverageError(null);
    setCoverageMessage(null);
  }, [coverageSourceId]);

  useEffect(() => {
    if (operationRefreshSignal === 0) {
      return;
    }

    refreshAttempts();
    if (coverage) {
      void loadCoverage('后台洞察任务完成，已重新检测当前范围。', coverageSourceIdRef.current);
    }
    if (selectedPreviewItemId !== null) {
      void loadRequestPreview(selectedPreviewItemId);
    }
  }, [operationRefreshSignal]);

  const attemptBatchRetryActiveOperationId =
    attemptBatchRetryRun?.operation && isOperationActiveStatus(attemptBatchRetryRun.operation.status) ? attemptBatchRetryRun.operation.id : null;

  useEffect(() => {
    if (!attemptBatchRetryRun || attemptBatchRetryActiveOperationId === null) {
      return;
    }

    let isCancelled = false;
    let timeoutId: number | null = null;
    const operationId = attemptBatchRetryActiveOperationId;
    const retryRunId = attemptBatchRetryRun.id;

    function schedulePoll() {
      if (isCancelled || document.visibilityState === 'hidden') {
        return;
      }
      timeoutId = window.setTimeout(() => {
        void pollBatchRetryOperations();
      }, 1200);
    }

    async function pollBatchRetryOperations() {
      try {
        const operation = await getOperation(operationId);
        if (isCancelled) {
          return;
        }
        onBackendOperation(operation);
        setAttemptBatchRetryRun((current) => {
          if (!current || current.id !== retryRunId) {
            return current;
          }
          return {
            ...current,
            phase: isOperationActiveStatus(operation.status) ? 'running' : 'completed',
            operation,
            error: operation.error_message,
          };
        });

        if (!isOperationTerminalStatus(operation.status)) {
          schedulePoll();
        } else {
          void refreshAttemptsForLatestFilters();
          if (hasCoverageScanRef.current) {
            void loadCoverage('后台洞察任务完成，已重新检测当前范围。', coverageSourceIdRef.current);
          }
        }
      } catch (error) {
        console.error('Failed to poll batch retry operations', error);
        setAttemptBatchRetryRun((current) => {
          if (!current || current.id !== retryRunId) {
            return current;
          }
          return {
            ...current,
            error: error instanceof Error ? `刷新 operation 状态失败，正在重试：${error.message}` : '刷新 operation 状态失败，正在重试。',
          };
        });
        schedulePoll();
      }
    }

    function handleVisiblePoll() {
      if (document.visibilityState !== 'hidden') {
        void pollBatchRetryOperations();
      }
    }

    void pollBatchRetryOperations();
    document.addEventListener('visibilitychange', handleVisiblePoll);

    return () => {
      isCancelled = true;
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
      document.removeEventListener('visibilitychange', handleVisiblePoll);
    };
  }, [attemptBatchRetryRun?.id, attemptBatchRetryActiveOperationId]);

  async function loadEngineSettings() {
    setIsEngineLoading(true);
    setEngineError(null);
    try {
      const settings = await getInsightEngineSettings();
      setEngineSettings(settings);
      setAutoLowPriorityEnabled(settings.autoLowPriorityArchivingEnabled);
      setLowPriorityThreshold(settings.lowPriorityScoreThreshold);
    } catch (error) {
      setEngineError(error instanceof Error ? error.message : '加载 Insight Engine 设置失败。');
    } finally {
      setIsEngineLoading(false);
    }
  }

  async function saveEngineSettings() {
    setIsEngineSaving(true);
    setEngineError(null);
    setEngineMessage(null);
    try {
      const nextSettings = await updateInsightEngineSettings({
        autoLowPriorityArchivingEnabled: autoLowPriorityEnabled,
        lowPriorityScoreThreshold: lowPriorityThreshold,
      });
      setEngineSettings(nextSettings);
      setEngineMessage('Insight Engine 设置已保存。');
    } catch (error) {
      setEngineError(error instanceof Error ? error.message : '保存 Insight Engine 设置失败。');
    } finally {
      setIsEngineSaving(false);
    }
  }

  async function loadInsightProfile() {
    setIsProfileLoading(true);
    setProfileError(null);
    try {
      const nextSettings = await getInsightProfileSettings();
      applyInsightProfileSettings(nextSettings);
    } catch (error) {
      setProfileError(error instanceof Error ? error.message : '加载 Insight Profile 失败。');
    } finally {
      setIsProfileLoading(false);
    }
  }

  function applyInsightProfileSettings(nextSettings: InsightProfileSettings) {
    setInsightProfileSettings(nextSettings);
    setProfileEditorText(formatJson(nextSettings.activeProfile.profileJson));
    setProfileDraftInspection(null);
  }

  function parseProfileEditorJson() {
    const parsed = JSON.parse(profileEditorText) as unknown;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('Insight Profile JSON 必须是一个对象。');
    }
    return parsed as Record<string, unknown>;
  }

  async function inspectProfileDraft(profileJson?: Record<string, unknown>, successMessage = 'Insight Profile 草稿校验通过。') {
    setProfileError(null);
    setProfileMessage(null);
    try {
      const parsed = profileJson ?? parseProfileEditorJson();
      const result = await inspectInsightProfileDraft(parsed);
      setProfileDraftInspection(result);
      setProfileEditorText(formatJson(result.normalizedProfile));
      setProfileMessage(successMessage);
    } catch (error) {
      setProfileDraftInspection(null);
      setProfileError(error instanceof Error ? error.message : 'Insight Profile 草稿审阅失败。');
    }
  }

  async function importProfileDraft(file: File | null) {
    if (!file) {
      return;
    }
    setProfileError(null);
    setProfileMessage(null);
    try {
      const parsed = JSON.parse(await file.text()) as Record<string, unknown>;
      setProfileEditorText(formatJson(parsed));
      setProfileSourceType('codex_import');
      await inspectProfileDraft(parsed, 'Insight Profile 草稿已导入。');
    } catch (error) {
      setProfileDraftInspection(null);
      setProfileError(error instanceof Error ? error.message : '导入 Insight Profile 草稿失败。');
    }
  }

  async function importProfileCandidate(file: File | null) {
    if (!file) {
      return;
    }
    setIsCandidateLoading(true);
    setCandidateError(null);
    setCandidateMessage(null);
    try {
      if (file.size > MAX_PROFILE_CANDIDATE_FILE_BYTES) {
        throw new Error('Profile candidate JSON 不能超过 1 MB。');
      }
      const parsed = JSON.parse(await file.text()) as unknown;
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('Profile candidate JSON 必须是一个对象。');
      }
      const candidate = parsed as Record<string, unknown>;
      const inspection = await inspectInsightProfileCandidate(candidate);
      setCandidateJson(candidate);
      setCandidateInspection(inspection);
      setCandidateSelectedSourceIds(inspection.sourceProfileChanges.map((change) => change.sourceId));
      if (inspection.globalProfileInspection) {
        setProfileEditorText(formatJson(inspection.globalProfileInspection.normalizedProfile));
        setProfileDraftInspection(inspection.globalProfileInspection);
        setProfileSourceType('codex_import');
      }
      setCandidateMessage('Profile candidate 已导入，请审阅后再保存。');
    } catch (error) {
      setCandidateInspection(null);
      setCandidateJson(null);
      setCandidateSelectedSourceIds([]);
      setCandidateError(error instanceof Error ? error.message : '导入 Profile candidate 失败。');
    } finally {
      setIsCandidateLoading(false);
    }
  }

  function toggleCandidateSource(sourceId: number) {
    setCandidateSelectedSourceIds((current) =>
      current.includes(sourceId) ? current.filter((id) => id !== sourceId) : [...current, sourceId],
    );
  }

  async function applyCandidateSourceDrafts() {
    if (!candidateJson || candidateSelectedSourceIds.length === 0) {
      setCandidateError('请至少选择一个 Source Profile 建议。');
      return;
    }
    setIsCandidateLoading(true);
    setCandidateError(null);
    setCandidateMessage(null);
    try {
      const result = await applyInsightProfileCandidateSourceDrafts(candidateJson, candidateSelectedSourceIds);
      await onSourcesChanged?.();
      setCandidateMessage(result.message);
    } catch (error) {
      setCandidateError(error instanceof Error ? error.message : '保存 Source Profile candidate drafts 失败。');
    } finally {
      setIsCandidateLoading(false);
    }
  }

  async function saveProfileDraft(activate: boolean) {
    setIsProfileSaving(true);
    setProfileError(null);
    setProfileMessage(null);
    try {
      const nextSettings = await saveInsightProfileVersion({
        profileJson: parseProfileEditorJson(),
        sourceType: profileSourceType,
        editNote: profileEditNote,
        activate,
      });
      applyInsightProfileSettings(nextSettings);
      setProfileEditNote('');
      await loadRequestPreview();
      setProfileMessage(activate ? 'Insight Profile 已保存并启用。' : 'Insight Profile 已保存。');
    } catch (error) {
      setProfileError(error instanceof Error ? error.message : '保存 Insight Profile 失败。');
    } finally {
      setIsProfileSaving(false);
    }
  }

  async function activateProfileVersion(profileId: number) {
    setIsProfileSaving(true);
    setProfileError(null);
    setProfileMessage(null);
    try {
      const nextSettings = await activateInsightProfileVersion(profileId, profileEditNote || null);
      applyInsightProfileSettings(nextSettings);
      await loadRequestPreview();
      setProfileMessage('Insight Profile 版本已启用。');
    } catch (error) {
      setProfileError(error instanceof Error ? error.message : '启用 Insight Profile 失败。');
    } finally {
      setIsProfileSaving(false);
    }
  }

  async function rollbackProfileVersion(profileId: number) {
    setIsProfileSaving(true);
    setProfileError(null);
    setProfileMessage(null);
    try {
      const nextSettings = await rollbackInsightProfileVersion(profileId, profileEditNote || null);
      applyInsightProfileSettings(nextSettings);
      await loadRequestPreview();
      setProfileMessage('Insight Profile 已回滚。');
    } catch (error) {
      setProfileError(error instanceof Error ? error.message : '回滚 Insight Profile 失败。');
    } finally {
      setIsProfileSaving(false);
    }
  }

  function resetProfileEditorToActive() {
    if (!insightProfileSettings) {
      return;
    }
    setProfileEditorText(formatJson(insightProfileSettings.activeProfile.profileJson));
    setProfileDraftInspection(null);
    setProfileError(null);
    setProfileMessage(null);
  }

  function loadTemplateIntoEditor() {
    if (!insightProfileSettings) {
      return;
    }
    setProfileEditorText(formatJson(insightProfileSettings.template));
    setProfileDraftInspection(null);
    setProfileError(null);
    setProfileMessage(null);
  }

  function buildEvidenceInput(): InsightEvidencePackageInput {
    return {
      range: evidenceRange,
      artifactMode: evidenceArtifactMode,
      startDate: evidenceRange === 'custom' ? evidenceStartDate || null : undefined,
      endDate: evidenceRange === 'custom' ? evidenceEndDate || null : undefined,
    };
  }

  async function loadEvidencePreview() {
    setIsEvidenceLoading(true);
    setEvidenceError(null);
    setEvidenceMessage(null);
    try {
      setEvidencePreview(await getInsightEvidencePackagePreview(buildEvidenceInput()));
    } catch (error) {
      setEvidenceError(error instanceof Error ? error.message : '加载 Evidence Package 预览失败。');
    } finally {
      setIsEvidenceLoading(false);
    }
  }

  async function downloadEvidencePackage() {
    setIsEvidenceExporting(true);
    setEvidenceError(null);
    setEvidenceMessage(null);
    try {
      const result = await exportInsightEvidencePackage(buildEvidenceInput());
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = result.filename;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      await loadEvidencePreview();
      setEvidenceMessage('Evidence Package 已生成。');
    } catch (error) {
      setEvidenceError(error instanceof Error ? error.message : '导出 Evidence Package 失败。');
    } finally {
      setIsEvidenceExporting(false);
    }
  }

  async function loadRequestPreview(itemId = selectedPreviewItemId) {
    setIsPreviewLoading(true);
    setPreviewError(null);
    try {
      const nextPreview = await getAIInsightRequestPreview(itemId);
      setRequestPreview(nextPreview);
      setSelectedPreviewItemId(nextPreview.sample?.itemId ?? itemId ?? null);
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : '加载请求检查器失败。');
    } finally {
      setIsPreviewLoading(false);
    }
  }

  function selectPreviewSample(itemId: number) {
    setSelectedPreviewItemId(itemId);
    void loadRequestPreview(itemId);
  }

  async function loadAttempts({
    append = false,
    filters = attemptFilters,
  }: {
    append?: boolean;
    filters?: InsightEngineAttemptFilters;
  } = {}) {
    const requestId = attemptRequestSeq.current + 1;
    attemptRequestSeq.current = requestId;
    setIsAttemptLoading(true);
    setAttemptError(null);
    try {
      const nextList = await listInsightEngineAttempts(filters);
      if (requestId !== attemptRequestSeq.current) {
        return;
      }
      const shouldClearSelectedAttempt =
        !append && selectedAttempt !== null && !nextList.items.some((attempt) => attempt.id === selectedAttempt.id);
      setAttemptList((current) => {
        const currentItems = append ? current?.items ?? [] : [];
        const currentIds = new Set(currentItems.map((attempt) => attempt.id));
        return append && currentItems.length > 0
          ? {
              ...nextList,
              items: [...currentItems, ...nextList.items.filter((attempt) => !currentIds.has(attempt.id))],
              offset: 0,
            }
          : nextList;
      });
      if (shouldClearSelectedAttempt) {
        setSelectedAttempt(null);
      }
    } catch (error) {
      if (requestId === attemptRequestSeq.current) {
        setAttemptError(error instanceof Error ? error.message : '加载 attempts 失败。');
      }
    } finally {
      if (requestId === attemptRequestSeq.current) {
        setIsAttemptLoading(false);
      }
    }
  }

  function refreshAttempts(): Promise<void> {
    if (attemptOffset === 0) {
      return loadAttempts();
    }
    attemptRequestSeq.current += 1;
    setAttemptOffset(0);
    return Promise.resolve();
  }

  function refreshAttemptsForLatestFilters(): Promise<void> {
    const latestFilters = attemptFiltersRef.current ?? attemptFilters;
    setAttemptOffset(0);
    return loadAttempts({filters: {...latestFilters, offset: 0}});
  }

  function loadMoreAttempts() {
    if (!attemptList || isAttemptLoading || attemptList.items.length >= attemptList.total) {
      return;
    }
    setAttemptOffset(attemptList.items.length);
  }

  function changeAttemptStatus(value: InsightEngineAttemptStatus | 'all') {
    attemptRequestSeq.current += 1;
    setAttemptStatus(value);
    setAttemptOffset(0);
  }

  function changeAttemptSourceId(value: number | null) {
    attemptRequestSeq.current += 1;
    setAttemptSourceId(value);
    setAttemptOffset(0);
  }

  function changeAttemptProfile(value: string) {
    attemptRequestSeq.current += 1;
    setAttemptProfile(value);
    setAttemptOffset(0);
  }

  function changeAttemptTimeRange(value: '24h' | '7d' | '30d' | 'all') {
    attemptRequestSeq.current += 1;
    setAttemptTimeRange(value);
    setAttemptOffset(0);
  }

  async function openAttempt(attempt: InsightEngineAttempt) {
    setAttemptError(null);
    try {
      setSelectedAttempt(await getInsightEngineAttempt(attempt.id));
    } catch (error) {
      setAttemptError(error instanceof Error ? error.message : '加载 attempt 详情失败。');
    }
  }

  async function runAttemptItemAction(attempt: InsightEngineAttempt, mode: 'missing' | 'rebuild') {
    if (!attempt.feedItemId) {
      return;
    }
    setAttemptActionItemId(attempt.feedItemId);
    setAttemptActionMessage(null);
    setAttemptError(null);
    try {
      await onFeedItemInsightArtifactOperation(attempt.feedItemId, mode === 'missing' ? 'build_missing' : 'rebuild');
      setAttemptActionMessage(`${mode === 'missing' ? '补齐' : '重建'}任务已创建，后端完成后会刷新对应 item。`);
      await refreshAttempts();
      await loadRequestPreview(attempt.feedItemId);
    } catch (error) {
      setAttemptError(error instanceof Error ? error.message : `${mode === 'missing' ? '补齐' : '重建'}失败。`);
    } finally {
      setAttemptActionItemId(null);
    }
  }

  async function loadCoverage(successMessage?: string, sourceId: number | null = coverageSourceId) {
    setIsCoverageLoading(true);
    setCoverageError(null);
    setCoverageMessage(null);
    try {
      const nextCoverage = await getInsightEngineCoverage({
        sourceId,
        limit: 500,
        offset: 0,
      });
      setCoverage(nextCoverage);
      setCoverageMessage(
        successMessage ??
          `检测完成：${nextCoverage.totalItemCount} 条 item，${nextCoverage.problemItemCount} 条当前问题，${nextCoverage.blockedItemCount} 条阻塞诊断，${nextCoverage.initialGenerationItemCount} 条待首次生成。`,
      );
    } catch (error) {
      setCoverageError(error instanceof Error ? error.message : '检测洞察覆盖失败。');
    } finally {
      setIsCoverageLoading(false);
    }
  }

  async function runCoverageItemAction(item: InsightEngineCoverageItem) {
    setCoverageActionItemId(item.feedItemId);
    setCoverageError(null);
    setCoverageMessage(null);
    try {
      await onFeedItemInsightArtifactOperation(item.feedItemId, 'build_missing');
      await loadCoverage('单条洞察补齐任务已创建，后端完成后会刷新对应 item。');
      await refreshAttempts();
      await loadRequestPreview(item.feedItemId);
    } catch (error) {
      setCoverageError(error instanceof Error ? error.message : '补齐洞察失败。');
    } finally {
      setCoverageActionItemId(null);
    }
  }

  async function buildCoverageBatch(kind: 'problem' | 'initial_generation') {
    if (!coverage || isCoverageBatchBuilding) {
      return;
    }

    setIsCoverageBatchBuilding(true);
    setCoverageError(null);
    setCoverageMessage(null);
    const runId = Date.now();
    const sourceLabel =
      coverageSourceId === null
        ? '全部来源'
        : sources.find((source) => source.id === coverageSourceId)?.name ?? `source #${coverageSourceId}`;
    const isInitialGeneration = kind === 'initial_generation';
    const title = isInitialGeneration ? '待首次生成批量补齐' : '问题项批量重试';
    const reasonCodes = isInitialGeneration ? COVERAGE_INITIAL_GENERATION_REASON_CODES : COVERAGE_PROBLEM_REASON_CODES;
    setAttemptBatchRetryRun({
      id: runId,
      startedAt: new Date().toISOString(),
      title,
      filterSummary: `${sourceLabel} · ${isInitialGeneration ? '待首次生成' : '失败/跳过/部分缺失'} · 上限 ${ITEM_ARTIFACT_BATCH_OPERATION_LIMIT}`,
      phase: 'creating',
      operation: null,
      error: null,
    });
    try {
      setCoverageMessage(`正在创建${title} operation，上限 ${ITEM_ARTIFACT_BATCH_OPERATION_LIMIT} 个 item。`);
      const operation = await startFeedItemInsightArtifactBatchOperation({
        scope: 'retryable_coverage',
        source_id: coverageSourceId,
        limit: ITEM_ARTIFACT_BATCH_OPERATION_LIMIT,
        reason_codes: reasonCodes,
      });
      onBackendOperation(operation);
      setAttemptBatchRetryRun((current) =>
        current?.id === runId
          ? {
              ...current,
              phase: isOperationActiveStatus(operation.status) ? 'running' : 'completed',
              operation,
              error: operation.error_message,
            }
          : current,
      );
      await loadCoverage(
        operation.status === 'completed'
          ? `${title} operation #${operation.id} 已完成：${operation.message ?? '没有可处理条目。'}`
          : `${title} operation #${operation.id} 已创建；后端会在同一个 operation 下处理最多 ${ITEM_ARTIFACT_BATCH_OPERATION_LIMIT} 个 item。`,
      );
      await refreshAttempts();
    } catch (error) {
      const conflictOperation = extractConflictOperation(error);
      if (conflictOperation) {
        onBackendOperation(conflictOperation);
        if (!isFeedItemInsightArtifactBatchOperationDto(conflictOperation)) {
          const message = `已有 ${formatOperationType(conflictOperation)} operation #${conflictOperation.id} 正在处理，未重复创建新任务；请等待它结束或先停止它。`;
          setAttemptBatchRetryRun((current) => (current?.id === runId ? {...current, phase: 'completed', operation: null, error: message} : current));
          setCoverageMessage(message);
          return;
        }
        setAttemptBatchRetryRun((current) =>
          current?.id === runId
            ? {
                ...current,
                phase: isOperationActiveStatus(conflictOperation.status) ? 'running' : 'completed',
                operation: conflictOperation,
                error: conflictOperation.error_message,
              }
            : current,
        );
        setCoverageMessage(`已有后台 operation #${conflictOperation.id} 正在处理，未重复创建新任务；请等待它结束或先停止它。`);
        return;
      }
      const message = error instanceof Error ? error.message : `${title}失败。`;
      setAttemptBatchRetryRun((current) => (current?.id === runId ? {...current, phase: 'completed', error: message} : current));
      setCoverageError(message);
    } finally {
      setCoverageActionItemId(null);
      setIsCoverageBatchBuilding(false);
    }
  }

  const isAttemptBatchOperationRunning = Boolean(
    attemptBatchRetryRun?.operation && isOperationActiveStatus(attemptBatchRetryRun.operation.status),
  );

  return (
    <section className="panel min-h-full border-b border-[var(--border-subtle)] px-5 py-4">
      <div className="mx-auto w-full max-w-[1440px] space-y-4">
        <InsightEngineControls
          autoLowPriorityEnabled={autoLowPriorityEnabled}
          engineError={engineError}
          engineMessage={engineMessage}
          engineSettings={engineSettings}
          isLoading={isEngineLoading}
          isSaving={isEngineSaving}
          lowPriorityThreshold={lowPriorityThreshold}
          onAutoLowPriorityChange={setAutoLowPriorityEnabled}
          onRefresh={() => void loadEngineSettings()}
          onSave={() => void saveEngineSettings()}
          onThresholdChange={setLowPriorityThreshold}
        />

        <InsightChannelSettingsPanel onBackendOperation={onBackendOperation} onChannelsChanged={onChannelsChanged} />

        <CollapsibleSettingsSection
          title="洞察画像"
          summary={
            insightProfileSettings?.activeProfile
              ? `${insightProfileSettings.activeProfile.profileName} · ${insightProfileSettings.activeProfile.profileVersion}`
              : '默认收起；展开后导入、审阅、保存和启用 Insight Profile。'
          }
        >
          <InsightProfileManager
            editorText={profileEditorText}
            editNote={profileEditNote}
            isLoading={isProfileLoading}
            isSaving={isProfileSaving}
            message={profileMessage}
            profileError={profileError}
            settings={insightProfileSettings}
            sourceType={profileSourceType}
            draftInspection={profileDraftInspection}
            candidateInspection={candidateInspection}
            candidateError={candidateError}
            candidateMessage={candidateMessage}
            candidateSelectedSourceIds={candidateSelectedSourceIds}
            isCandidateLoading={isCandidateLoading}
            onActivate={activateProfileVersion}
            onApplyCandidateSourceDrafts={() => void applyCandidateSourceDrafts()}
            onCandidateSourceToggle={toggleCandidateSource}
            onEditNoteChange={setProfileEditNote}
            onEditorTextChange={(value) => {
              setProfileEditorText(value);
              setProfileDraftInspection(null);
            }}
            onImportDraft={(file) => void importProfileDraft(file)}
            onImportProfileCandidate={(file) => void importProfileCandidate(file)}
            onLoadTemplate={loadTemplateIntoEditor}
            onRefresh={() => void loadInsightProfile()}
            onReset={resetProfileEditorToActive}
            onRollback={rollbackProfileVersion}
            onSave={(activate) => void saveProfileDraft(activate)}
            onSourceTypeChange={setProfileSourceType}
            onValidate={() => void inspectProfileDraft()}
          />
        </CollapsibleSettingsSection>

        <CollapsibleSettingsSection
          title="洞察证据包"
          summary={
            evidencePreview
              ? `${evidencePreview.itemCount} 篇 · ${evidencePreview.feedbackItemCount} 条反馈 · ${evidencePreview.behaviorEventCount} 条行为事件`
              : '默认收起；展开后预览和导出 Evidence Package。'
          }
        >
          <InsightEvidencePackagePanel
            artifactMode={evidenceArtifactMode}
            endDate={evidenceEndDate}
            error={evidenceError}
            isExporting={isEvidenceExporting}
            isLoading={isEvidenceLoading}
            message={evidenceMessage}
            preview={evidencePreview}
            range={evidenceRange}
            startDate={evidenceStartDate}
            onDownload={() => void downloadEvidencePackage()}
            onArtifactModeChange={setEvidenceArtifactMode}
            onEndDateChange={setEvidenceEndDate}
            onPreview={() => void loadEvidencePreview()}
            onRangeChange={setEvidenceRange}
            onStartDateChange={setEvidenceStartDate}
          />
          <EvidencePackageChannelCounts preview={evidencePreview} />
        </CollapsibleSettingsSection>

        <InsightAttemptInspector
          actionItemId={attemptActionItemId}
          actionMessage={attemptActionMessage}
          attemptList={attemptList}
          batchRetryRun={attemptBatchRetryRun}
          coverage={coverage}
          coverageActionItemId={coverageActionItemId}
          coverageError={coverageError}
          coverageMessage={coverageMessage}
          error={attemptError}
          isArtifactBatchRunning={isAttemptBatchOperationRunning}
          isCoverageBatchBuilding={isCoverageBatchBuilding || isAttemptBatchOperationRunning}
          isCoverageLoading={isCoverageLoading}
          isLoading={isAttemptLoading}
          profile={attemptProfile}
          selectedAttempt={selectedAttempt}
          coverageSourceId={coverageSourceId}
          sourceId={attemptSourceId}
          sources={sources}
          status={attemptStatus}
          timeRange={attemptTimeRange}
          onLoadMore={loadMoreAttempts}
          onOpenAttempt={(attempt) => void openAttempt(attempt)}
          onOpenFeedItem={onOpenFeedItem}
          onProfileChange={changeAttemptProfile}
          onRefreshCoverage={() => void loadCoverage()}
          onRefresh={refreshAttempts}
          onRunCoverageAction={(item) => void runCoverageItemAction(item)}
          onRunCoverageBatch={(kind) => void buildCoverageBatch(kind)}
          onCoverageSourceChange={setCoverageSourceId}
          onRunAction={(attempt, mode) => void runAttemptItemAction(attempt, mode)}
          onSourceChange={changeAttemptSourceId}
          onStatusChange={changeAttemptStatus}
          onTimeRangeChange={changeAttemptTimeRange}
        />

        <CollapsibleSettingsSection title="洞察请求检查器" summary={formatRequestPreviewSummary(requestPreview, previewError, isPreviewLoading)}>
          <InsightRequestInspector
            isLoading={isPreviewLoading}
            preview={requestPreview}
            previewError={previewError}
            selectedItemId={selectedPreviewItemId}
            onRefresh={() => void loadRequestPreview()}
            onSelectSample={selectPreviewSample}
          />
        </CollapsibleSettingsSection>
      </div>
    </section>
  );
}

function CollapsibleSettingsSection({
  children,
  defaultOpen = false,
  summary,
  title,
}: {
  children: ReactNode;
  defaultOpen?: boolean;
  summary: string;
  title: string;
}) {
  const [isOpen, setIsOpen] = useState(defaultOpen);

  return (
    <section className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-4 py-3">
      <button
        className="flex w-full items-start justify-between gap-3 text-left"
        type="button"
        onClick={() => setIsOpen((current) => !current)}
      >
        <div className="min-w-0">
          <div className="text-sm font-semibold text-[var(--text-main)]">{title}</div>
          <p className="mt-1 truncate text-sm text-[var(--text-muted)]">{summary}</p>
        </div>
        <span className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-[var(--border-subtle)] text-[var(--text-muted)]">
          {isOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </span>
      </button>
      {isOpen ? <div className="mt-3">{children}</div> : null}
    </section>
  );
}

function InsightEngineControls({
  autoLowPriorityEnabled,
  engineError,
  engineMessage,
  engineSettings,
  isLoading,
  isSaving,
  lowPriorityThreshold,
  onAutoLowPriorityChange,
  onRefresh,
  onSave,
  onThresholdChange,
}: {
  autoLowPriorityEnabled: boolean;
  engineError: string | null;
  engineMessage: string | null;
  engineSettings: InsightEngineSettings | null;
  isLoading: boolean;
  isSaving: boolean;
  lowPriorityThreshold: number;
  onAutoLowPriorityChange: (value: boolean) => void;
  onRefresh: () => void;
  onSave: () => void;
  onThresholdChange: (value: number) => void;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const summary = `当前：${engineSettings?.autoLowPriorityArchivingEnabled ? '已启用' : '默认关闭'} · 阈值 ${
    engineSettings?.lowPriorityScoreThreshold ?? lowPriorityThreshold
  }`;

  return (
    <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-4 py-3">
      <button
        className="flex w-full items-start justify-between gap-3 text-left"
        type="button"
        onClick={() => setIsOpen((current) => !current)}
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-semibold text-[var(--text-main)]">
            <SlidersHorizontal size={16} />
            自动低优先级归档
          </div>
          <p className="mt-1 truncate text-sm text-[var(--text-muted)]">{summary}</p>
        </div>
        <span className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-[var(--border-subtle)] text-[var(--text-muted)]">
          {isOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </span>
      </button>

      {isOpen ? (
        <div className="mt-4">
          <div className="flex flex-wrap justify-end gap-2">
          <button
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isLoading || isSaving}
            type="button"
            onClick={onRefresh}
          >
            <RefreshCw size={15} className={isLoading ? 'animate-spin' : ''} />
            刷新设置
          </button>
          <button
            className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isLoading || isSaving}
            type="button"
            onClick={onSave}
          >
            <Save size={15} />
            {isSaving ? '保存中...' : '保存'}
          </button>
        </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-[1fr_1fr]">
        <label className="flex items-center justify-between gap-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-3 text-sm">
          <span className="font-medium text-[var(--text-main)]">启用自动低优先级归档</span>
          <input
            checked={autoLowPriorityEnabled}
            className="h-4 w-4 accent-[var(--accent)]"
            disabled={isLoading || isSaving}
            type="checkbox"
            onChange={(event) => onAutoLowPriorityChange(event.target.checked)}
          />
        </label>
        <label className="grid gap-2 text-sm">
          <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">LOW_PRIORITY_SCORE_THRESHOLD</span>
          <div className="grid grid-cols-[1fr_72px] items-center gap-3">
            <input
              className="accent-[var(--accent)]"
              disabled={isLoading || isSaving}
              max={100}
              min={0}
              type="range"
              value={lowPriorityThreshold}
              onChange={(event) => onThresholdChange(Number(event.target.value))}
            />
            <input
              className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)]"
              disabled={isLoading || isSaving}
              max={100}
              min={0}
              type="number"
              value={lowPriorityThreshold}
              onChange={(event) => onThresholdChange(clampThreshold(Number(event.target.value)))}
            />
          </div>
        </label>
      </div>
        </div>
      ) : null}
      {engineMessage ? <div className="mt-3 text-sm text-emerald-600">{engineMessage}</div> : null}
      {engineError ? <div className="mt-3 text-sm text-red-600">{engineError}</div> : null}
    </div>
  );
}

function EvidencePackageChannelCounts({preview}: {preview: InsightEvidencePackagePreview | null}) {
  if (!preview) {
    return null;
  }

  return (
    <div className="-mt-2 flex flex-wrap gap-2 border-b border-[var(--border-subtle)] pb-4">
      <EvidenceCountPill label="频道纠偏" value={preview.channelCorrectionItemCount} />
      <EvidenceCountPill label="纠偏条目" value={preview.channelCorrectedItemCount} />
      <EvidenceCountPill label="强反馈" value={preview.strongSignalCount} />
      <EvidenceCountPill label="中反馈" value={preview.mediumSignalCount} />
      <EvidenceCountPill label="弱清理" value={preview.weakCleanupSignalCount} />
      <EvidenceCountPill label="负反馈" value={preview.negativeSignalCount} />
    </div>
  );
}

function EvidenceCountPill({label, value}: {label: string; value: number}) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-2.5 py-1 text-xs text-[var(--text-muted)]">
      <span>{label}</span>
      <span className="font-semibold text-[var(--text-main)]">{value}</span>
    </span>
  );
}

function formatRequestPreviewSummary(
  preview: InsightRequestPreview | null,
  previewError: string | null,
  isLoading: boolean,
): string {
  if (previewError) {
    return `加载失败：${previewError}`;
  }
  if (isLoading && !preview) {
    return '正在加载请求预览。';
  }
  if (!preview) {
    return '尚未加载请求预览。';
  }
  const payload = preview.payload;
  const sample = preview.sample;
  const sendState = preview.canSend ? '可发送' : '仅预览';
  if (!preview.available || !payload || !sample) {
    return `${sendState} · ${preview.message}`;
  }
  return `${sendState} · ${payload.provider}/${payload.profile}/${payload.model} · #${sample.itemId} ${sample.title}`;
}

function InsightAttemptInspector({
  actionItemId,
  actionMessage,
  attemptList,
  batchRetryRun,
  coverage,
  coverageActionItemId,
  coverageError,
  coverageMessage,
  error,
  isArtifactBatchRunning,
  isCoverageBatchBuilding,
  isCoverageLoading,
  isLoading,
  profile,
  selectedAttempt,
  coverageSourceId,
  sourceId,
  sources,
  status,
  timeRange,
  onOpenAttempt,
  onOpenFeedItem,
  onLoadMore,
  onProfileChange,
  onCoverageSourceChange,
  onRefreshCoverage,
  onRefresh,
  onRunCoverageAction,
  onRunCoverageBatch,
  onRunAction,
  onSourceChange,
  onStatusChange,
  onTimeRangeChange,
}: {
  actionItemId: number | null;
  actionMessage: string | null;
  attemptList: InsightEngineAttemptList | null;
  batchRetryRun: AttemptBatchRetryRun | null;
  coverage: InsightEngineCoverage | null;
  coverageActionItemId: number | null;
  coverageError: string | null;
  coverageMessage: string | null;
  error: string | null;
  isArtifactBatchRunning: boolean;
  isCoverageBatchBuilding: boolean;
  isCoverageLoading: boolean;
  isLoading: boolean;
  profile: string;
  selectedAttempt: InsightEngineAttempt | null;
  coverageSourceId: number | null;
  sourceId: number | null;
  sources: SourceDto[];
  status: InsightEngineAttemptStatus | 'all';
  timeRange: '24h' | '7d' | '30d' | 'all';
  onOpenAttempt: (attempt: InsightEngineAttempt) => void;
  onOpenFeedItem: (itemId: number) => void;
  onLoadMore: () => void;
  onProfileChange: (value: string) => void;
  onCoverageSourceChange: (value: number | null) => void;
  onRefreshCoverage: () => void;
  onRefresh: () => void;
  onRunCoverageAction: (item: InsightEngineCoverageItem) => void;
  onRunCoverageBatch: (kind: 'problem' | 'initial_generation') => void;
  onRunAction: (attempt: InsightEngineAttempt, mode: 'missing' | 'rebuild') => void;
  onSourceChange: (value: number | null) => void;
  onStatusChange: (value: InsightEngineAttemptStatus | 'all') => void;
  onTimeRangeChange: (value: '24h' | '7d' | '30d' | 'all') => void;
}) {
  const items = attemptList?.items ?? [];
  const loadedAttemptCount = items.length;
  const hasMoreAttempts = attemptList ? loadedAttemptCount < attemptList.total : false;
  const coverageProblemItems = coverage?.items.filter((item) => item.coverageCategory === 'problem') ?? [];
  const coverageBlockedItems = coverage?.items.filter((item) => item.coverageCategory === 'blocked') ?? [];
  const coverageInitialGenerationItems =
    coverage?.items.filter((item) => item.coverageCategory === 'initial_generation') ?? [];
  const coverageProblemRetryableItemCount = coverage?.problemRetryableItemCount ?? 0;
  const coverageInitialGenerationRetryableItemCount = coverage?.initialGenerationRetryableItemCount ?? 0;
  const truncationInfo = getAttemptTruncationInfo(selectedAttempt?.requestSnapshot ?? null);
  const [isInspectorOpen, setIsInspectorOpen] = useState(true);
  const [isAttemptHistoryOpen, setIsAttemptHistoryOpen] = useState(false);
  const coverageSummary = coverage
    ? `覆盖 ${coverage.totalItemCount} 个 item：${coverage.completeItemCount} 已完整，${coverage.problemItemCount} 当前问题，${coverage.initialGenerationItemCount} 待首次生成`
    : '尚未扫描覆盖';
  const attemptSummary = attemptList ? `历史日志 ${attemptList.total} 条，已显示 ${loadedAttemptCount} 条` : '历史日志加载中';
  const batchSummary = batchRetryRun ? ` · ${formatAttemptBatchRetryPhase(batchRetryRun, summarizeAttemptBatchRetryRun(batchRetryRun))}` : '';
  return (
    <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-4 py-3">
      <button
        className="flex w-full items-start justify-between gap-3 text-left"
        type="button"
        onClick={() => setIsInspectorOpen((current) => !current)}
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-semibold text-[var(--text-main)]">
            <Search size={16} />
            洞察覆盖与生成诊断
          </div>
          <p className="mt-1 truncate text-sm text-[var(--text-muted)]">
            {coverageSummary} · {attemptSummary}
            {batchSummary}
          </p>
        </div>
        <span className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-[var(--border-subtle)] text-[var(--text-muted)]">
          {isInspectorOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </span>
      </button>

      {isInspectorOpen ? (
        <div className="mt-4">
          <p className="text-sm text-[var(--text-muted)]">
            以 item 覆盖为主口径；failed/skipped attempt 是历史日志，重试成功后不会删除旧记录。
          </p>

      {batchRetryRun ? <AttemptBatchRetryPanel run={batchRetryRun} /> : null}

      <div className="mt-4 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-[var(--text-main)]">洞察覆盖检测</div>
            <p className="mt-1 text-xs text-[var(--text-muted)]">
              按当前来源扫描四类 insight artifact；当前问题、阻塞诊断和待首次生成分开处理。
            </p>
          </div>
          <div className="flex shrink-0 gap-2">
            <button
              className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
              disabled={isCoverageLoading || isCoverageBatchBuilding}
              type="button"
              onClick={onRefreshCoverage}
            >
              <Search size={15} className={isCoverageLoading ? 'animate-spin' : ''} />
              扫描当前覆盖
            </button>
            <button
              className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
              disabled={coverageProblemRetryableItemCount === 0 || isCoverageLoading || isCoverageBatchBuilding}
              type="button"
              onClick={() => onRunCoverageBatch('problem')}
            >
              <RefreshCw size={15} className={isCoverageBatchBuilding ? 'animate-spin' : ''} />
              重试问题项
            </button>
            <button
              className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
              disabled={coverageInitialGenerationRetryableItemCount === 0 || isCoverageLoading || isCoverageBatchBuilding}
              type="button"
              onClick={() => onRunCoverageBatch('initial_generation')}
            >
              <Sparkles size={15} className={isCoverageBatchBuilding ? 'animate-spin' : ''} />
              生成待首次项
            </button>
          </div>
        </div>

        <div className="mt-3 grid gap-3 lg:grid-cols-[minmax(220px,360px)_minmax(0,1fr)]">
          <FilterSelect
            label="覆盖来源"
            value={coverageSourceId === null ? 'all' : String(coverageSourceId)}
            onChange={(value) => onCoverageSourceChange(value === 'all' ? null : Number(value))}
          >
            <option value="all">全部来源</option>
            {sources.map((source) => (
              <option key={source.id} value={source.id}>
                {source.name}
              </option>
            ))}
          </FilterSelect>
          <div className="self-end rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-xs leading-5 text-[var(--text-muted)]">
            这个来源筛选只影响覆盖扫描和上方批量生成；历史 failed/skipped 日志有独立筛选。
          </div>
        </div>

        {coverage ? (
          <div className="mt-3 grid gap-3">
            <div className="grid grid-cols-2 gap-2 text-xs text-[var(--text-muted)] md:grid-cols-6">
              <Metric label="总条目" value={String(coverage.totalItemCount)} />
              <Metric label="已完整" value={String(coverage.completeItemCount)} />
              <Metric label="当前问题" value={String(coverage.problemItemCount)} />
              <Metric label="阻塞诊断" value={String(coverage.blockedItemCount)} />
              <Metric label="待首次生成" value={String(coverage.initialGenerationItemCount)} />
              <Metric label="可重试问题" value={String(coverage.problemRetryableItemCount)} />
            </div>
            {coverage.reasonCounts.length > 0 ? (
              <div className="flex flex-wrap gap-2 text-xs">
                {coverage.reasonCounts.map((reason) => (
                  <span key={reason.code} className="rounded-full border border-[var(--border-subtle)] px-2 py-1 text-[var(--text-muted)]">
                    {formatCoverageReasonLabel(reason.code, reason.label)} · {reason.count}
                  </span>
                ))}
              </div>
            ) : null}
            <CoverageItemList
              actionItemId={coverageActionItemId}
              isBatchBuilding={isCoverageBatchBuilding}
              isLoading={isCoverageLoading}
              items={coverageProblemItems}
              title={`当前问题 · ${coverage.problemItemCount}`}
              emptyText="当前范围没有 failed/skipped/partial artifact 问题。"
              onOpenFeedItem={onOpenFeedItem}
              onRunCoverageAction={onRunCoverageAction}
            />
            {coverage.blockedItemCount > 0 ? (
              <CoverageItemList
                actionItemId={coverageActionItemId}
                isBatchBuilding={isCoverageBatchBuilding}
                isLoading={isCoverageLoading}
                items={coverageBlockedItems}
                title={`阻塞诊断 · ${coverage.blockedItemCount}`}
                emptyText="当前页没有阻塞诊断。"
                onOpenFeedItem={onOpenFeedItem}
                onRunCoverageAction={onRunCoverageAction}
              />
            ) : null}
            {coverage.initialGenerationItemCount > 0 ? (
              <CoverageItemList
                actionItemId={coverageActionItemId}
                isBatchBuilding={isCoverageBatchBuilding}
                isLoading={isCoverageLoading}
                items={coverageInitialGenerationItems}
                title={`待首次生成 · ${coverage.initialGenerationItemCount}`}
                emptyText="当前页没有待首次生成条目。"
                onOpenFeedItem={onOpenFeedItem}
                onRunCoverageAction={onRunCoverageAction}
              />
            ) : null}
            {coverage.total > coverage.items.length ? (
              <div className="text-xs text-[var(--text-soft)]">
                当前显示前 {coverage.items.length} 条 artifact 不完整项；批量操作只处理所选分类内的可重试条目。
              </div>
            ) : null}
          </div>
        ) : (
          <div className="mt-3 text-sm text-[var(--text-muted)]">尚未扫描；点击“扫描当前覆盖”查看 artifact 覆盖、问题项和待首次生成 backlog。</div>
        )}
        {coverageError ? <div className="mt-3 text-sm text-red-600">{coverageError}</div> : null}
        {coverageMessage ? <div className="mt-3 text-sm text-emerald-600">{coverageMessage}</div> : null}
      </div>

      <div className="mt-4 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <button
            className="flex min-w-0 flex-1 items-start gap-2 text-left"
            type="button"
            onClick={() => setIsAttemptHistoryOpen((current) => !current)}
          >
            <span className="mt-0.5 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-[var(--border-subtle)] text-[var(--text-muted)]">
              {isAttemptHistoryOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
            </span>
            <span className="min-w-0">
              <span className="block text-sm font-semibold text-[var(--text-main)]">失败/跳过历史日志</span>
              <span className="mt-1 block text-xs text-[var(--text-muted)]">
                {attemptList
                  ? `当前筛选 ${attemptList.total} 条，已显示 ${loadedAttemptCount} 条；默认收起，展开后查看日志和详情。`
                  : '加载历史 attempt 日志中。'}
              </span>
              <span className="mt-1 block text-xs text-[var(--text-soft)]">
                历史日志不会因 item 补齐成功而删除；批量生成以上方覆盖检测口径为准。
              </span>
            </span>
          </button>
          <button
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isLoading || isArtifactBatchRunning}
            type="button"
            onClick={onRefresh}
          >
            <RefreshCw size={15} className={isLoading ? 'animate-spin' : ''} />
            刷新历史日志
          </button>
        </div>

        {isAttemptHistoryOpen ? (
          <div className="mt-3">
            <div className="grid gap-3 lg:grid-cols-4">
              <FilterSelect label="历史状态" value={status} onChange={(value) => onStatusChange(value as InsightEngineAttemptStatus | 'all')}>
                {ATTEMPT_STATUS_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </FilterSelect>
              <FilterSelect
                label="历史来源"
                value={sourceId === null ? 'all' : String(sourceId)}
                onChange={(value) => onSourceChange(value === 'all' ? null : Number(value))}
              >
                <option value="all">全部来源</option>
                {sources.map((source) => (
                  <option key={source.id} value={source.id}>
                    {source.name}
                  </option>
                ))}
              </FilterSelect>
              <FilterSelect label="历史 Profile" value={profile} onChange={onProfileChange}>
                {ATTEMPT_PROFILE_OPTIONS.map((option) => (
                  <option key={option} value={option}>
                    {option === 'all' ? '全部 profile' : option}
                  </option>
                ))}
              </FilterSelect>
              <FilterSelect label="历史时间" value={timeRange} onChange={(value) => onTimeRangeChange(value as '24h' | '7d' | '30d' | 'all')}>
                {ATTEMPT_TIME_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </FilterSelect>
            </div>
            <p className="mt-1 text-xs text-[var(--text-muted)]">
              状态、来源、Profile、时间只筛选历史 failed/skipped attempt 日志，不影响上方覆盖扫描。
            </p>
            {status === 'all' && attemptList && hasMoreAttempts ? (
              <p className="mt-1 text-xs text-[var(--text-soft)]">全部状态按时间倒序展示；其他状态可能在后续结果中，也可用状态筛选查看。</p>
            ) : null}
            {error ? <div className="mt-3 text-sm text-red-600">{error}</div> : null}
            {actionMessage ? <div className="mt-3 text-sm text-emerald-600">{actionMessage}</div> : null}

            <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(360px,0.85fr)]">
        <div className="overflow-hidden rounded-lg border border-[var(--border-subtle)]">
          <div className="grid grid-cols-[86px_120px_minmax(0,1fr)_150px_170px] gap-3 border-b border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-xs font-semibold uppercase text-[var(--text-soft)]">
            <span>状态</span>
            <span>阶段</span>
            <span>条目</span>
            <span>Provider</span>
            <span>时间</span>
          </div>
          <div className="max-h-[420px] overflow-y-auto">
            {items.map((attempt) => (
              <button
                key={attempt.id}
                className="grid w-full grid-cols-[86px_120px_minmax(0,1fr)_150px_170px] gap-3 border-b border-[var(--border-subtle)] px-3 py-2 text-left text-sm transition-colors hover:bg-[var(--bg-hover)]"
                type="button"
                onClick={() => onOpenAttempt(attempt)}
              >
                <span className={attempt.status === 'failed' ? 'font-medium text-red-600' : 'font-medium text-amber-600'}>
                  {attempt.status}
                </span>
                <span className="truncate text-[var(--text-muted)]">{attempt.stage}</span>
                <span className="truncate text-[var(--text-main)]">{attempt.feedItemTitle ?? `#${attempt.feedItemId ?? attempt.id}`}</span>
                <span className="truncate text-[var(--text-muted)]">
                  {attempt.provider}/{attempt.model ?? '-'}
                </span>
                <span className="truncate text-[var(--text-soft)]">{formatDateTime(attempt.createdAt)}</span>
              </button>
            ))}
            {!isLoading && items.length === 0 ? (
              <div className="px-3 py-10 text-center text-sm text-[var(--text-muted)]">当前筛选暂无 attempt</div>
            ) : null}
          </div>
          {attemptList && hasMoreAttempts ? (
            <div className="flex items-center justify-between gap-3 border-t border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-xs text-[var(--text-muted)]">
              <span>
                已显示 {loadedAttemptCount} / {attemptList.total}
              </span>
              <button
                className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border-subtle)] px-2.5 py-1.5 text-xs text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                disabled={isLoading}
                type="button"
                onClick={onLoadMore}
              >
                <RefreshCw size={13} className={isLoading ? 'animate-spin' : ''} />
                加载更多
              </button>
            </div>
          ) : null}
        </div>

        <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-3">
          {selectedAttempt ? (
            <div className="grid gap-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold text-[var(--text-main)]">
                    {selectedAttempt.feedItemTitle ?? `Item ${selectedAttempt.feedItemId ?? selectedAttempt.id}`}
                  </div>
                  <div className="mt-1 text-xs text-[var(--text-soft)]">
                    {selectedAttempt.sourceName ?? '未知来源'} · {selectedAttempt.profile} · {selectedAttempt.stage}
                  </div>
                </div>
                {selectedAttempt.feedItemId ? (
                  <div className="flex shrink-0 gap-2">
                    <button
                      className="inline-flex items-center gap-1 rounded-md border border-[var(--border-subtle)] px-2 py-1.5 text-xs text-[var(--text-main)]"
                      type="button"
                      onClick={() => onOpenFeedItem(selectedAttempt.feedItemId as number)}
                    >
                      <Eye size={14} />
                      打开
                    </button>
                    <button
                      className="inline-flex items-center gap-1 rounded-md border border-[var(--border-subtle)] px-2 py-1.5 text-xs text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                      disabled={isArtifactBatchRunning || actionItemId === selectedAttempt.feedItemId}
                      type="button"
                      onClick={() => onRunAction(selectedAttempt, 'missing')}
                    >
                      <Download size={14} />
                      补齐
                    </button>
                    <button
                      className="inline-flex items-center gap-1 rounded-md border border-[var(--border-subtle)] px-2 py-1.5 text-xs text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                      disabled={isArtifactBatchRunning || actionItemId === selectedAttempt.feedItemId}
                      type="button"
                      onClick={() => onRunAction(selectedAttempt, 'rebuild')}
                    >
                      <Sparkles size={14} />
                      重建
                    </button>
                  </div>
                ) : null}
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs text-[var(--text-muted)]">
                <Metric label="error" value={selectedAttempt.errorType ?? '-'} />
                <Metric label="duration" value={selectedAttempt.durationMs === null ? '-' : `${selectedAttempt.durationMs}ms`} />
                <Metric label="created" value={formatDateTime(selectedAttempt.createdAt)} />
                <Metric label="artifacts" value={String(selectedAttempt.artifactCount)} />
              </div>
              {truncationInfo ? <AttemptTruncationNotice info={truncationInfo} /> : null}
              {selectedAttempt.errorMessage ? (
                <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-700">
                  {selectedAttempt.errorMessage}
                </div>
              ) : null}
              <JsonBlock value={selectedAttempt.requestSnapshot} />
            </div>
          ) : (
            <div className="flex min-h-[260px] flex-col items-center justify-center gap-2 text-center text-sm text-[var(--text-muted)]">
              <Eye size={18} />
              选择一条 attempt 查看详情
            </div>
          )}
        </div>
            </div>
          </div>
        ) : null}
      </div>
        </div>
      ) : null}
    </div>
  );
}

function AttemptBatchRetryPanel({run}: {run: AttemptBatchRetryRun}) {
  const summary = summarizeAttemptBatchRetryRun(run);
  const percent =
    summary.totalItemCount > 0 ? Math.round((summary.completedItemCount / summary.totalItemCount) * 100) : run.phase === 'completed' ? 100 : 0;
  const statusText = formatAttemptBatchRetryPhase(run, summary);
  const operation = run.operation;

  return (
    <div className="mt-4 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-semibold text-[var(--text-main)]">
            <RefreshCw size={15} className={summary.isActive || run.phase === 'creating' ? 'animate-spin' : ''} />
            {run.title}
          </div>
          <p className="mt-1 text-xs text-[var(--text-muted)]">
            {statusText} · {run.filterSummary} · {formatDateTime(run.startedAt)}
          </p>
        </div>
        <div className="shrink-0 text-xs font-medium text-[var(--text-muted)]">{percent}%</div>
      </div>

      <div className="mt-3 h-2 overflow-hidden rounded-full bg-[var(--bg-panel)]">
        <div className="h-full rounded-full bg-[var(--accent)] transition-all" style={{width: `${percent}%`}} />
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-[var(--text-muted)] md:grid-cols-4 xl:grid-cols-8">
        <Metric label="operation" value={operation ? `#${operation.id}` : '-'} />
        <Metric label="status" value={operation ? formatOperationStatus(operation.status) : run.phase} />
        <Metric label="items" value={`${summary.completedItemCount}/${summary.totalItemCount}`} />
        <Metric label="jobs" value={`${summary.jobSucceededCount}/${summary.jobTotalCount}`} />
        <Metric label="running" value={String(summary.jobRunningCount)} />
        <Metric label="pending" value={String(summary.jobPendingCount)} />
        <Metric label="artifact rows" value={String(summary.artifactRowsCreated)} />
        <Metric label="failed items" value={String(summary.failedFeedItemCount)} />
      </div>

      {operation?.message ? (
        <div className="mt-3 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-xs leading-relaxed text-[var(--text-muted)]">
          {operation.message}
        </div>
      ) : null}

      {summary.totalItemCount >= ITEM_ARTIFACT_BATCH_OPERATION_LIMIT && summary.isActive ? (
        <div className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-800">
          本次批处理上限为 {ITEM_ARTIFACT_BATCH_OPERATION_LIMIT} 个 item；如果还有剩余未成功洞察，当前 operation 结束后可以再次补齐。
        </div>
      ) : null}

      {summary.problemCount > 0 ? (
        <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-700">
          已发现需要查看的问题：operation 状态 {operation ? formatOperationStatus(operation.status) : run.phase}，job 失败{' '}
          {summary.jobFailedCount} 个，job 取消 {summary.jobCancelledCount} 个，洞察构建失败 item {summary.failedFeedItemCount} 个。
        </div>
      ) : null}

      {run.error ? (
        <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-700">
          {run.error}
        </div>
      ) : null}
    </div>
  );
}

function CoverageItemList({
  actionItemId,
  emptyText,
  isBatchBuilding,
  isLoading,
  items,
  title,
  onOpenFeedItem,
  onRunCoverageAction,
}: {
  actionItemId: number | null;
  emptyText: string;
  isBatchBuilding: boolean;
  isLoading: boolean;
  items: InsightEngineCoverageItem[];
  title: string;
  onOpenFeedItem: (itemId: number) => void;
  onRunCoverageAction: (item: InsightEngineCoverageItem) => void;
}) {
  return (
    <div className="overflow-hidden rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)]">
      <div className="border-b border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-xs font-semibold text-[var(--text-muted)]">
        {title}
      </div>
      <div className="max-h-[260px] overflow-y-auto">
        {items.map((item) => (
          <div
            key={item.feedItemId}
            className="grid gap-2 border-b border-[var(--border-subtle)] px-3 py-2 text-sm lg:grid-cols-[minmax(0,1fr)_220px_150px]"
          >
            <div className="min-w-0">
              <div className="truncate font-medium text-[var(--text-main)]">{item.title}</div>
              <div className="mt-0.5 truncate text-xs text-[var(--text-soft)]">
                {item.sourceName} · 缺少 {item.missingArtifactKinds.length} 类 · {item.provider}/{item.model ?? '-'}
              </div>
            </div>
            <div className="min-w-0 text-xs text-[var(--text-muted)]">
              <div className="truncate">{formatCoverageReasonLabel(item.possibleReasonCode, item.possibleReasonLabel)}</div>
              {item.latestAttempt ? <div className="mt-0.5 truncate">latest: {item.latestAttempt.status}/{item.latestAttempt.stage}</div> : null}
            </div>
            <div className="flex items-center justify-end gap-2">
              <button
                className="inline-flex items-center gap-1 rounded-md border border-[var(--border-subtle)] px-2 py-1.5 text-xs text-[var(--text-main)]"
                type="button"
                onClick={() => onOpenFeedItem(item.feedItemId)}
              >
                <Eye size={14} />
                打开
              </button>
              <button
                className="inline-flex items-center gap-1 rounded-md border border-[var(--border-subtle)] px-2 py-1.5 text-xs text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                disabled={!item.retryable || actionItemId === item.feedItemId || isBatchBuilding}
                type="button"
                onClick={() => onRunCoverageAction(item)}
              >
                <Download size={14} />
                {item.possibleReasonCode === COVERAGE_INITIAL_GENERATION_REASON_CODE ? '生成' : item.retryable ? '补齐' : '不可补齐'}
              </button>
            </div>
          </div>
        ))}
        {!isLoading && items.length === 0 ? (
          <div className="px-3 py-8 text-center text-sm text-[var(--text-muted)]">{emptyText}</div>
        ) : null}
      </div>
    </div>
  );
}

function summarizeAttemptBatchRetryRun(run: AttemptBatchRetryRun) {
  const operation = run.operation;
  const progress = operation?.progress;
  const result = operation?.result;
  const totalItemCount = progress?.insight_total_count ?? progress?.current_insight_total ?? readOperationResultCount(result, 'processed_feed_item_count');
  const completedItemCount =
    progress?.insight_completed_count ??
    progress?.current_insight_completed ??
    readOperationResultCount(result, 'processed_feed_item_count');
  const failedFeedItemCount =
    progress?.insight_failed_count ?? progress?.current_insight_failed ?? readOperationResultCount(result, 'failed_feed_item_count');
  const artifactRowsCreated =
    progress?.insight_artifact_rows_created ??
    progress?.current_insight_artifact_rows_created ??
    readOperationResultCount(result, 'artifact_rows_created');
  const isActive = operation ? isOperationActiveStatus(operation.status) : run.phase === 'creating';
  const isFailedOperation = operation?.status === 'failed' || operation?.status === 'interrupted';
  const jobFailedCount = progress?.job_failed_count ?? 0;
  const jobCancelledCount = progress?.job_cancelled_count ?? 0;
  return {
    artifactRowsCreated,
    completedItemCount,
    failedFeedItemCount,
    isActive,
    jobCancelledCount,
    jobFailedCount,
    jobPendingCount: progress?.job_pending_count ?? 0,
    jobRunningCount: progress?.job_running_count ?? 0,
    jobSucceededCount: progress?.job_succeeded_count ?? 0,
    jobTotalCount: progress?.job_total_count ?? 0,
    problemCount: (run.error ? 1 : 0) + (isFailedOperation ? 1 : 0) + jobFailedCount + jobCancelledCount + failedFeedItemCount,
    totalItemCount,
  };
}

function readOperationResultCount(result: Record<string, unknown> | null | undefined, key: string) {
  const value = result?.[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function isOperationActiveStatus(status: AppOperationStatus): status is AppOperationStatus {
  return OPERATION_ACTIVE_STATUSES.has(status);
}

function isOperationTerminalStatus(status: AppOperationStatus): status is AppOperationStatus {
  return OPERATION_TERMINAL_STATUSES.has(status);
}

function formatAttemptBatchRetryPhase(
  run: AttemptBatchRetryRun,
  summary: ReturnType<typeof summarizeAttemptBatchRetryRun>,
) {
  if (run.phase === 'creating') {
    return '正在创建批处理 operation';
  }
  if (summary.isActive) {
    return `后台运行中 ${summary.completedItemCount}/${summary.totalItemCount} item 已处理`;
  }
  if (!run.operation) {
    return '未创建 operation';
  }
  return `批处理已结束 ${summary.completedItemCount}/${summary.totalItemCount} item`;
}

function formatOperationStatus(status: AppOperationStatus) {
  switch (status) {
    case 'queued':
      return '排队中';
    case 'running':
      return '运行中';
    case 'stopping':
      return '停止中';
    case 'completed':
      return '已完成';
    case 'stopped':
      return '已停止';
    case 'failed':
      return 'operation 失败';
    case 'interrupted':
      return '已中断';
    default:
      return status;
  }
}

function isFeedItemInsightArtifactBatchOperationDto(operation: AppOperationDto) {
  return operation.operation_type === 'feed_item_insight_artifacts';
}

function formatOperationType(operation: AppOperationDto) {
  switch (operation.operation_type) {
    case 'source_sync':
      return '来源刷新';
    case 'feed_item_insight_artifacts':
      return 'feed item insight artifacts';
    case 'channel_lifecycle':
      return 'channel lifecycle';
    default:
      return operation.operation_type;
  }
}

function formatCoverageReasonLabel(code: string, fallback: string) {
  switch (code) {
    case 'previous_failed_attempt':
      return '最近一次生成失败';
    case 'previous_skipped_attempt':
      return '最近一次生成被跳过';
    case 'partial_artifacts':
      return 'artifact 部分缺失';
    case 'no_attempt_history':
      return '待首次生成';
    case 'source_ai_route_disabled':
      return '来源已禁用 AI 生成';
    case 'provider_disabled_or_unconfigured':
      return 'AI provider 未启用或未配置';
    case 'low_confidence_content':
      return '正文提取失败/仅跳转链接';
    default:
      return fallback;
  }
}

function FilterSelect({
  children,
  label,
  value,
  onChange,
}: {
  children: ReactNode;
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="grid gap-1.5 text-sm">
      <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">{label}</span>
      <select
        className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)]"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {children}
      </select>
    </label>
  );
}

function Metric({label, value}: {label: string; value: string}) {
  return (
    <div className="rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-2 py-1.5">
      <div className="uppercase text-[var(--text-soft)]">{label}</div>
      <div className="mt-0.5 truncate font-medium text-[var(--text-main)]">{value}</div>
    </div>
  );
}

interface AttemptTruncationInfo {
  sourceChars: number | null;
  contentChars: number | null;
}

function AttemptTruncationNotice({info}: {info: AttemptTruncationInfo}) {
  const sourceChars = formatCharCount(info.sourceChars);
  const contentChars = formatCharCount(info.contentChars);
  const detail =
    info.sourceChars === null || info.contentChars === null
      ? '请求正文已按后端上限截断。'
      : `原文 ${sourceChars} chars -> 请求 ${contentChars} chars，保留开头和结尾正文。`;

  return (
    <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-800">
      <AlertTriangle className="mt-0.5 shrink-0" size={14} />
      <div className="min-w-0">
        <div className="font-semibold">请求正文已截断</div>
        <div>{detail}</div>
      </div>
    </div>
  );
}

function getAttemptTruncationInfo(snapshot: Record<string, unknown> | null): AttemptTruncationInfo | null {
  const stats = snapshot?.stats;
  if (!isRecord(stats) || stats.content_text_was_truncated !== true) {
    return null;
  }
  return {
    sourceChars: readFiniteNumber(stats.source_content_char_count),
    contentChars: readFiniteNumber(stats.content_text_char_count),
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readFiniteNumber(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function formatCharCount(value: number | null) {
  return value === null ? '-' : new Intl.NumberFormat('en-US').format(value);
}

function JsonBlock({value}: {value: Record<string, unknown> | null}) {
  return (
    <pre className="max-h-[360px] overflow-auto rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-3 text-xs leading-relaxed text-[var(--text-main)]">
      {value ? JSON.stringify(value, null, 2) : 'No request snapshot.'}
    </pre>
  );
}

function clampThreshold(value: number) {
  if (!Number.isFinite(value)) {
    return 50;
  }
  return Math.max(0, Math.min(100, Math.round(value)));
}

function formatJson(value: Record<string, unknown>) {
  return JSON.stringify(value, null, 2);
}

function formatDateTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value));
}
