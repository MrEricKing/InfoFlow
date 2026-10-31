import {ChevronDown, MessageSquare, RefreshCw, Sparkles} from 'lucide-react';
import type {ReactNode} from 'react';
import type {
  FeedItem,
  SavedItemAIRoute,
  SavedItemInsightPayload,
} from '@/types/feed';
import type {AppOperationDto, AppOperationStatus} from '@/types/source';

export type SavedItemRebuildRouteOption = 'saved_item' | SavedItemAIRoute;

export interface SavedInsightDraft {
  summary: string;
  longTermValue: string;
  actionableTakeawaysText: string;
  keyConceptsText: string;
  suggestedTagsText: string;
  topicIdsText: string;
  contentType: string;
}

export const EMPTY_SAVED_INSIGHT_DRAFT: SavedInsightDraft = {
  summary: '',
  longTermValue: '',
  actionableTakeawaysText: '',
  keyConceptsText: '',
  suggestedTagsText: '',
  topicIdsText: '',
  contentType: '',
};

export function SavedItemInsightPanel({
  actionError,
  actionMessage,
  draft,
  editError,
  editNote,
  isEditing,
  isRebuilding,
  isSaving,
  item,
  operation,
  rebuildRoute,
  onCancelEdit,
  onClear,
  onDraftChange,
  onEdit,
  onEditNoteChange,
  onRebuild,
  onRebuildRouteChange,
  onSave,
}: {
  actionError: string | null;
  actionMessage: string | null;
  draft: SavedInsightDraft;
  editError: string | null;
  editNote: string;
  isEditing: boolean;
  isRebuilding: boolean;
  isSaving: boolean;
  item: FeedItem;
  operation: AppOperationDto | null;
  rebuildRoute: SavedItemRebuildRouteOption;
  onCancelEdit: () => void;
  onClear: () => void;
  onDraftChange: (draft: SavedInsightDraft) => void;
  onEdit: () => void;
  onEditNoteChange: (value: string) => void;
  onRebuild: () => void;
  onRebuildRouteChange: (value: SavedItemRebuildRouteOption) => void;
  onSave: () => void;
}) {
  const resolvedInsight = item.resolvedInsight;
  const payload = resolvedInsight?.payload ?? null;
  const isCurated = resolvedInsight?.source === 'curated';
  const operationError = operation && isFailedOperation(operation.status) ? operation.error_message : null;
  const operationMessage = operation ? buildOperationMessage(operation) : null;
  const effectiveActionError = actionError ?? operationError;
  const effectiveActionMessage = actionMessage ?? operationMessage;
  const showProgress = Boolean(operation && (isRebuilding || isOperationActive(operation.status) || isFailedOperation(operation.status)));

  return (
    <section className="mt-4 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Sparkles size={15} className="text-[var(--text-soft)]" />
            <span className="text-xs font-semibold uppercase tracking-[0.18em] text-[var(--text-soft)]">
              保存洞察
            </span>
            <span className="rounded-full border border-[var(--border-subtle)] px-2 py-0.5 text-[11px] text-[var(--text-soft)]">
              {isCurated ? '已人工修订' : payload ? 'AI 生成' : '未生成'}
            </span>
            {item.originType ? (
              <span className="rounded-full border border-[var(--border-subtle)] px-2 py-0.5 text-[11px] text-[var(--text-soft)]">
                {item.originType}
              </span>
            ) : null}
            {item.aiRoute ? (
              <span className="rounded-full border border-[var(--border-subtle)] px-2 py-0.5 text-[11px] text-[var(--text-soft)]">
                {describeAiRoute(item.aiRoute)}
              </span>
            ) : null}
          </div>
          <div className="mt-1 text-xs text-[var(--text-muted)]">
            {payload
              ? `输入：${payload.inputTextSource ?? '未知来源'} · ${payload.inputCharCount} 字`
              : '有可读归档内容后可生成保存洞察。'}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <select
            className="rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-2 py-1.5 text-xs text-[var(--text-muted)] outline-none transition-colors hover:bg-[var(--bg-hover)]"
            disabled={isRebuilding}
            title="保存洞察 AI 路由"
            value={rebuildRoute}
            onChange={(event) => onRebuildRouteChange(event.target.value as SavedItemRebuildRouteOption)}
          >
            <option value="saved_item">跟随条目（{describeAiRoute(item.aiRoute ?? 'sensitive')}）</option>
            <option value="sensitive">敏感</option>
            <option value="general">通用</option>
          </select>
          <InsightActionButton
            disabled={isRebuilding}
            icon={<Sparkles size={14} className={isRebuilding ? 'animate-pulse' : ''} />}
            label={isRebuilding ? '生成中' : payload ? '重建' : '生成'}
            onClick={onRebuild}
          />
          <InsightActionButton
            disabled={!payload || isEditing}
            icon={<MessageSquare size={14} />}
            label="编辑"
            onClick={onEdit}
          />
        </div>
      </div>

      {showProgress && operation ? <SavedInsightOperationProgress operation={operation} /> : null}

      {effectiveActionError || effectiveActionMessage ? (
        <div
          className={`mt-3 rounded-lg border px-3 py-2 text-xs ${
            effectiveActionError
              ? 'border-red-500/20 bg-red-500/5 text-red-500'
              : 'border-[var(--border-subtle)] bg-[var(--bg-panel)] text-[var(--text-muted)]'
          }`}
        >
          {effectiveActionError ?? effectiveActionMessage}
        </div>
      ) : null}

      {isEditing ? (
        <div className="mt-3 space-y-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-3">
          <SavedInsightTextField
            label="摘要"
            value={draft.summary}
            onChange={(value) => onDraftChange({...draft, summary: value})}
          />
          <SavedInsightTextField
            label="长期价值"
            value={draft.longTermValue}
            onChange={(value) => onDraftChange({...draft, longTermValue: value})}
          />
          <SavedInsightTextField
            label="行动要点"
            rows={4}
            value={draft.actionableTakeawaysText}
            onChange={(value) => onDraftChange({...draft, actionableTakeawaysText: value})}
          />
          <SavedInsightTextField
            label="关键概念"
            rows={3}
            value={draft.keyConceptsText}
            onChange={(value) => onDraftChange({...draft, keyConceptsText: value})}
          />
          <div className="grid gap-3 sm:grid-cols-2">
            <SavedInsightTextField
              label="建议标签"
              rows={3}
              value={draft.suggestedTagsText}
              onChange={(value) => onDraftChange({...draft, suggestedTagsText: value})}
            />
            <SavedInsightTextField
              label="主题 ID"
              rows={3}
              value={draft.topicIdsText}
              onChange={(value) => onDraftChange({...draft, topicIdsText: value})}
            />
          </div>
          <SavedInsightTextField
            label="内容类型"
            rows={1}
            value={draft.contentType}
            onChange={(value) => onDraftChange({...draft, contentType: value})}
          />
          <SavedInsightTextField label="编辑备注" rows={2} value={editNote} onChange={onEditNoteChange} />
          {editError ? <div className="text-xs text-red-500">{editError}</div> : null}
          <div className="flex flex-wrap justify-end gap-2">
            {item.curatedInsight ? (
              <InsightActionButton disabled={isSaving} icon={<RefreshCw size={14} />} label="清除" onClick={onClear} />
            ) : null}
            <InsightActionButton disabled={isSaving} icon={<ChevronDown size={14} />} label="取消" onClick={onCancelEdit} />
            <button
              className="rounded-md bg-[var(--accent)] px-3 py-1.5 text-xs font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-45"
              disabled={isSaving}
              type="button"
              onClick={onSave}
            >
              {isSaving ? '保存中...' : '保存修订洞察'}
            </button>
          </div>
        </div>
      ) : payload ? (
        <div className="mt-3 space-y-3 text-sm text-[var(--text-muted)]">
          {payload.summary ? <SavedInsightReadRow label="摘要" value={payload.summary} /> : null}
          {payload.longTermValue ? <SavedInsightReadRow label="长期价值" value={payload.longTermValue} /> : null}
          <SavedInsightReadList label="行动要点" values={payload.actionableTakeaways} />
          <SavedInsightReadList label="关键概念" values={payload.keyConcepts} />
          <div className="flex flex-wrap gap-2 text-xs">
            {payload.topicLabels.map((topic) => (
              <span key={topic} className="rounded-full border border-[var(--border-subtle)] px-2 py-0.5 text-[var(--text-soft)]">
                {topic}
              </span>
            ))}
            {payload.contentTypeLabel ? (
              <span className="rounded-full border border-[var(--border-subtle)] px-2 py-0.5 text-[var(--text-soft)]">
                {payload.contentTypeLabel}
              </span>
            ) : null}
            {payload.suggestedTags.map((tag) => (
              <span key={tag} className="rounded-full bg-[var(--bg-panel)] px-2 py-0.5 text-[var(--text-soft)]">
                {tag}
              </span>
            ))}
          </div>
        </div>
      ) : (
        <div className="mt-3 rounded-lg border border-dashed border-[var(--border-subtle)] px-3 py-4 text-sm text-[var(--text-muted)]">
          No saved item insight yet.
        </div>
      )}
    </section>
  );
}

function SavedInsightOperationProgress({operation}: {operation: AppOperationDto}) {
  const percent = getOperationProgressPercent(operation);
  const tone = isFailedOperation(operation.status) ? 'bg-red-500' : 'bg-[var(--accent)]';
  const statusLabel = describeOperationStatus(operation.status);
  const stageLabel = describeOperationStage(operation.progress?.stage ?? null);
  const countLabel = buildOperationCountLabel(operation);

  return (
    <div
      className="mt-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2"
      aria-live="polite"
    >
      <div className="flex items-center justify-between gap-3 text-xs">
        <div className="min-w-0 truncate text-[var(--text-muted)]">
          <span className="font-medium text-[var(--text-main)]">{statusLabel}</span>
          <span className="text-[var(--text-soft)]"> · {stageLabel}</span>
          {countLabel ? <span className="text-[var(--text-soft)]"> · {countLabel}</span> : null}
        </div>
        <div className="shrink-0 font-medium text-[var(--text-muted)]">{percent}%</div>
      </div>
      <div className="mt-2 h-2 overflow-hidden rounded-full bg-[var(--bg-app)]">
        <div className={`h-full rounded-full ${tone} transition-all`} style={{width: `${percent}%`}} />
      </div>
    </div>
  );
}

function getOperationProgressPercent(operation: AppOperationDto) {
  if (operation.status === 'completed') {
    return 100;
  }
  if (isFailedOperation(operation.status)) {
    return 100;
  }

  const progress = operation.progress;
  if (progress && progress.total > 0 && progress.completed > 0) {
    return clampPercent(Math.round((progress.completed / progress.total) * 100));
  }

  if (operation.status === 'queued') {
    return 10;
  }
  if (operation.status === 'stopping') {
    return 75;
  }
  if (operation.status === 'running') {
    return 45;
  }
  return 0;
}

function buildOperationCountLabel(operation: AppOperationDto) {
  const progress = operation.progress;
  if (!progress || progress.total <= 0) {
    return null;
  }
  return `${progress.completed}/${progress.total}`;
}

function buildOperationMessage(operation: AppOperationDto) {
  if (isOperationActive(operation.status)) {
    return operation.message ?? '保存洞察重建任务已在后端运行。';
  }
  if (operation.status === 'completed') {
    return operation.message ?? '保存洞察已生成。';
  }
  return null;
}

function describeOperationStatus(status: AppOperationStatus) {
  switch (status) {
    case 'queued':
      return '排队中';
    case 'running':
      return '生成中';
    case 'stopping':
      return '停止中';
    case 'completed':
      return '已完成';
    case 'failed':
      return '生成失败';
    case 'interrupted':
      return '已中断';
    case 'stopped':
      return '已停止';
    default:
      return status;
  }
}

function describeOperationStage(stage: string | null) {
  switch (stage) {
    case 'insight':
      return '请求 AI 生成洞察';
    case 'queued':
    case 'idle':
      return '等待后端执行';
    case 'completed':
      return '写入洞察结果';
    case 'failed':
      return '后端返回失败';
    default:
      return stage ?? '等待进度';
  }
}

function isOperationActive(status: AppOperationStatus) {
  return status === 'queued' || status === 'running' || status === 'stopping';
}

function isFailedOperation(status: AppOperationStatus) {
  return status === 'failed' || status === 'interrupted';
}

function clampPercent(value: number) {
  return Math.max(0, Math.min(100, value));
}

function SavedInsightTextField({
  label,
  rows = 2,
  value,
  onChange,
}: {
  label: string;
  rows?: number;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block text-xs font-medium text-[var(--text-soft)]">
      <span>{label}</span>
      <textarea
        className="mt-1 w-full resize-y rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm leading-relaxed text-[var(--text-main)] outline-none transition-colors focus:border-[var(--accent)]"
        rows={rows}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function SavedInsightReadRow({label, value}: {label: string; value: string}) {
  return (
    <div className="grid gap-1 sm:grid-cols-[128px_minmax(0,1fr)]">
      <span className="text-xs font-medium uppercase tracking-[0.14em] text-[var(--text-soft)]">{label}</span>
      <span className="leading-relaxed text-[var(--text-main)]">{value}</span>
    </div>
  );
}

function SavedInsightReadList({label, values}: {label: string; values: string[]}) {
  if (!values.length) {
    return null;
  }
  return (
    <div className="grid gap-1 sm:grid-cols-[128px_minmax(0,1fr)]">
      <span className="text-xs font-medium uppercase tracking-[0.14em] text-[var(--text-soft)]">{label}</span>
      <ul className="space-y-1 text-[var(--text-main)]">
        {values.map((value) => (
          <li key={value}>{value}</li>
        ))}
      </ul>
    </div>
  );
}

function InsightActionButton({
  disabled = false,
  icon,
  label,
  onClick,
}: {
  disabled?: boolean;
  icon: ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border-subtle)] px-2.5 py-1.5 text-xs font-medium text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-45"
      disabled={disabled}
      type="button"
      onClick={onClick}
    >
      {icon}
      {label}
    </button>
  );
}

function describeAiRoute(route: SavedItemAIRoute) {
  if (route === 'general') {
    return '通用 AI';
  }
  if (route === 'disabled') {
    return '停用 AI';
  }
  return '敏感 AI';
}

export function buildSavedInsightDraft(payload: SavedItemInsightPayload | null): SavedInsightDraft {
  if (!payload) {
    return {...EMPTY_SAVED_INSIGHT_DRAFT};
  }
  return {
    summary: payload.summary ?? '',
    longTermValue: payload.longTermValue ?? '',
    actionableTakeawaysText: payload.actionableTakeaways.join('\n'),
    keyConceptsText: payload.keyConcepts.join('\n'),
    suggestedTagsText: payload.suggestedTags.join('\n'),
    topicIdsText: payload.topicIds.join('\n'),
    contentType: payload.contentType ?? '',
  };
}

export function buildSavedInsightPayload(
  draft: SavedInsightDraft,
  basePayload: SavedItemInsightPayload | null,
  item: FeedItem,
): SavedItemInsightPayload {
  return {
    summary: normalizeDraftText(draft.summary),
    longTermValue: normalizeDraftText(draft.longTermValue),
    actionableTakeaways: splitDraftList(draft.actionableTakeawaysText),
    keyConcepts: splitDraftList(draft.keyConceptsText),
    suggestedTags: splitDraftList(draft.suggestedTagsText),
    topicIds: splitDraftList(draft.topicIdsText),
    topicLabels: basePayload?.topicLabels ?? [],
    contentType: normalizeDraftText(draft.contentType) ?? basePayload?.contentType ?? 'unknown',
    contentTypeLabel: basePayload?.contentTypeLabel ?? null,
    sourceKind: basePayload?.sourceKind ?? item.originType ?? null,
    aiRoute: basePayload?.aiRoute ?? item.aiRoute ?? null,
    inputSnapshotKind: basePayload?.inputSnapshotKind ?? null,
    inputTextSource: basePayload?.inputTextSource ?? 'curated_only',
    inputCharCount: basePayload?.inputCharCount ?? 0,
    inputTextWasTruncated: basePayload?.inputTextWasTruncated ?? false,
  };
}

function normalizeDraftText(value: string) {
  const normalized = value.trim();
  return normalized || null;
}

function splitDraftList(value: string) {
  return value
    .split(/[\n,，]/)
    .map((entry) => entry.trim())
    .filter(Boolean);
}
