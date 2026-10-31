import {RefreshCw} from 'lucide-react';
import type {SourceSyncProgressState} from '@/types/source';

interface SourceSyncProgressPanelProps {
  progress: SourceSyncProgressState;
  className?: string;
  variant?: 'source' | 'insight';
}

export function SourceSyncProgressPanel({progress, className = '', variant = 'source'}: SourceSyncProgressPanelProps) {
  const isInsightOnly = variant === 'insight' || isInsightOnlyProgress(progress);
  const isQueued = progress.stage === 'queued' && progress.jobPendingCount > 0 && progress.jobRunningCount === 0;
  const isActiveStage = !['completed', 'stopped', 'failed', 'interrupted'].includes(progress.stage) && !progress.stopped;
  const percent = getProgressPercent(progress, isInsightOnly);
  const activeLabel = buildActiveLabel(progress, isInsightOnly);
  const latestLabel =
    progress.latestSourceName && progress.latestStatus
      ? `最近完成：${progress.latestSourceName} · ${formatStatus(progress.latestStatus)}`
      : null;
  const showCurrentFetch = !isQueued && !isInsightOnly && progress.currentSourceName && progress.currentFetchStatus;
  const showSourceStats = !isInsightOnly;
  const showQueueStats =
    progress.jobTotalCount > 0 && (progress.stage === 'queued' || progress.jobPendingCount > 0 || progress.jobRunningCount > 0);
  const showInsight =
    progress.currentInsightTotal > 0 ||
    progress.insightTotalCount > 0 ||
    progress.insightArtifactRowsCreated > 0 ||
    progress.insightFailedCount > 0;

  return (
    <div
      className={`rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3 ${className}`}
      aria-live="polite"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2">
          {isActiveStage && progress.currentSourceName && !isQueued ? (
            <RefreshCw className="mt-0.5 shrink-0 animate-spin text-[var(--accent)]" size={15} />
          ) : null}
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-[var(--text-main)]">{activeLabel}</div>
            {showSourceStats ? (
              <div className="mt-1 text-xs text-[var(--text-muted)]">
                source 成功 {progress.successCount} · 失败 {progress.errorCount} · RSS 已见 {progress.itemsSeenCount} · 新增{' '}
                {progress.itemsCreatedCount} · 匹配 {progress.itemsMatchedCount} · 无效 {progress.itemsInvalidCount}
              </div>
            ) : null}
            {showCurrentFetch ? (
              <div className="mt-1 text-xs text-[var(--text-soft)]">
                当前刷新：{formatStatus(progress.currentFetchStatus)} · 已见 {progress.currentItemsSeenCount} · 新增{' '}
                {progress.currentItemsCreatedCount} · 匹配 {progress.currentItemsMatchedCount} · 无效{' '}
                {progress.currentItemsInvalidCount}
              </div>
            ) : null}
            {showQueueStats ? (
              <div className="mt-1 text-xs text-[var(--text-soft)]">
                队列：等待 {progress.jobPendingCount} · 运行 {progress.jobRunningCount} · 已完成{' '}
                {progress.jobSucceededCount + progress.jobFailedCount + progress.jobCancelledCount}/{progress.jobTotalCount} ·
                失败 job {progress.jobFailedCount}
              </div>
            ) : null}
            {showInsight ? (
              <div className="mt-1 text-xs text-[var(--text-soft)]">
                当前洞察 {progress.currentInsightCompleted}/{progress.currentInsightTotal} · 当前失败{' '}
                {progress.currentInsightFailed} · 累计洞察 {progress.insightCompletedCount}/{progress.insightTotalCount} ·
                生成 {progress.insightArtifactRowsCreated} 条结果 · 失败 {progress.insightFailedCount}
              </div>
            ) : null}
            {progress.stopRequested && !progress.stopped ? (
              <div className="mt-1 text-xs text-[var(--warning)]">已请求停止，当前来源完成后不会继续下一个 source。</div>
            ) : null}
            {latestLabel ? <div className="mt-1 truncate text-xs text-[var(--text-soft)]">{latestLabel}</div> : null}
          </div>
        </div>
        <div className="shrink-0 text-xs font-medium text-[var(--text-muted)]">{percent}%</div>
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-[var(--bg-panel)]">
        <div className="h-full rounded-full bg-[var(--accent)] transition-all" style={{width: `${percent}%`}} />
      </div>
      {progress.latestErrorMessage ? (
        <div className="mt-2 break-words text-xs text-[var(--warning)]">{progress.latestErrorMessage}</div>
      ) : null}
      {progress.failedSources.length > 0 ? (
        <div className="mt-3 rounded-md border border-[var(--warning)]/30 bg-[var(--bg-panel)] p-2">
          <div className="text-xs font-medium text-[var(--warning)]">失败 source</div>
          <div className="mt-2 space-y-1">
            {progress.failedSources.slice(0, 6).map((source) => (
              <div key={`${source.source_id}-${source.source_name}`} className="min-w-0 text-xs text-[var(--text-muted)]">
                <span className="font-medium text-[var(--text-main)]">{source.source_name}</span>
                <span className="text-[var(--text-soft)]"> · {formatFailedSourceError(source)}</span>
              </div>
            ))}
          </div>
          {progress.failedSources.length > 6 ? (
            <div className="mt-1 text-xs text-[var(--text-soft)]">还有 {progress.failedSources.length - 6} 个失败 source</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function getProgressPercent(progress: SourceSyncProgressState, isInsightOnly: boolean) {
  if (isInsightOnly && progress.insightTotalCount > 0) {
    return clampPercent(Math.round((progress.insightCompletedCount / progress.insightTotalCount) * 100));
  }

  if (!isInsightOnly && progress.stage === 'insight' && progress.currentInsightTotal > 0 && progress.total > 0) {
    const currentSourceRatio = progress.currentInsightCompleted / progress.currentInsightTotal;
    return clampPercent(Math.round(((progress.completed + currentSourceRatio) / progress.total) * 100));
  }

  return progress.total > 0 ? clampPercent(Math.round((progress.completed / progress.total) * 100)) : 0;
}

function isInsightOnlyProgress(progress: SourceSyncProgressState) {
  return (
    progress.stage === 'insight' &&
    progress.currentFetchStatus === null &&
    progress.itemsSeenCount === 0 &&
    progress.itemsCreatedCount === 0 &&
    progress.itemsMatchedCount === 0 &&
    progress.itemsInvalidCount === 0 &&
    progress.insightTotalCount > 0
  );
}

function clampPercent(value: number) {
  return Math.max(0, Math.min(100, value));
}

function buildActiveLabel(progress: SourceSyncProgressState, isInsightOnly: boolean) {
  if (progress.stopped || progress.stage === 'stopped') {
    return isInsightOnly
      ? `洞察已停止：已完成 ${progress.insightCompletedCount}/${progress.insightTotalCount}`
      : `刷新已停止：已完成 ${progress.completed}/${progress.total}`;
  }
  if (progress.stopRequested) {
    if (isInsightOnly) {
      return `洞察停止中 ${progress.insightCompletedCount}/${progress.insightTotalCount}`;
    }
    return progress.currentSourceName && progress.currentSourceIndex !== null
      ? `刷新停止中 ${progress.currentSourceIndex}/${progress.total}：${progress.currentSourceName}`
      : `刷新停止中 ${progress.completed}/${progress.total}`;
  }
  if (progress.stage === 'queued' && progress.jobPendingCount > 0 && progress.jobRunningCount === 0) {
    if (isInsightOnly) {
      return `洞察等待队列 ${progress.insightCompletedCount}/${progress.insightTotalCount}`;
    }
    return progress.currentSourceName
      ? `等待队列/重试 ${progress.currentSourceIndex ?? progress.completed + 1}/${progress.total}：${progress.currentSourceName}`
      : `等待队列/重试 ${progress.completed}/${progress.total}`;
  }
  if (isInsightOnly) {
    if (progress.stage === 'waiting_source_sync') {
      return `洞察等待刷新完成 ${progress.insightCompletedCount}/${progress.insightTotalCount}`;
    }
    return `洞察进度 ${progress.insightCompletedCount}/${progress.insightTotalCount}`;
  }
  if (progress.currentSourceName && progress.currentSourceIndex !== null) {
    if (progress.stage === 'insight') {
      return `Insight Engine ${progress.currentSourceIndex}/${progress.total}：${progress.currentSourceName}`;
    }
    return `刷新/入库 ${progress.currentSourceIndex}/${progress.total}：${progress.currentSourceName}`;
  }
  if (progress.total > 0) {
    return `刷新进度 ${progress.completed}/${progress.total}`;
  }
  return '没有启用的 source 可刷新';
}

function formatStatus(status: SourceSyncProgressState['latestStatus']) {
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

function formatFailedSourceError(source: SourceSyncProgressState['failedSources'][number]) {
  const httpStatus = source.http_status ? `HTTP ${source.http_status}` : 'request failed';
  return source.error_message ? `${httpStatus}: ${source.error_message}` : httpStatus;
}
