import {type Dispatch, type SetStateAction, useMemo} from 'react';
import type {
  FeedItemChannelCorrection,
  FeedItemChannelCorrectionInput,
  FeedItemStructuredFeedback,
  HomepageFlowLane,
  ReadingDepth,
  StructuredFeedbackIssueMarker,
  StructuredFeedbackOverallValue,
  StructuredFeedbackPriorityCorrection,
} from '@/types/feed';
import type {InsightChannel} from '@/types/settings';

export const EMPTY_STRUCTURED_FEEDBACK: FeedItemStructuredFeedback = {
  overallValue: null,
  priorityCorrection: null,
  topicAddIds: [],
  topicRemoveIds: [],
  contentTypeCorrection: null,
  reasonCodeAddIds: [],
  reasonCodeRemoveIds: [],
  issueMarkers: [],
  primaryChannelCorrection: null,
  channelAddIds: [],
  channelRemoveIds: [],
  readingDepthCorrection: null,
  flowLaneCorrection: null,
  channelCorrectionNotes: '',
};

export const EMPTY_CHANNEL_CORRECTION: FeedItemChannelCorrectionInput = {
  primaryChannelId: null,
  addChannelIds: [],
  removeChannelIds: [],
  readingDepth: null,
  flowLane: null,
  notes: '',
};

const OVERALL_VALUE_OPTIONS: Array<{value: StructuredFeedbackOverallValue | null; label: string}> = [
  {value: null, label: '未指定'},
  {value: 'no_value', label: '无价值'},
  {value: 'neutral', label: '一般'},
  {value: 'useful', label: '有用'},
  {value: 'very_useful', label: '很有用'},
  {value: 'must_read', label: '必读'},
];

const PRIORITY_CORRECTION_OPTIONS: Array<{value: StructuredFeedbackPriorityCorrection | null; label: string}> = [
  {value: null, label: '未指定'},
  {value: 'too_low', label: '应提高'},
  {value: 'appropriate', label: '合适'},
  {value: 'too_high', label: '应降低'},
];

const READING_DEPTH_OPTIONS: Array<{value: ReadingDepth | null; label: string}> = [
  {value: null, label: '未指定'},
  {value: 'light', label: '轻读'},
  {value: 'medium', label: '标准'},
  {value: 'deep', label: '深读'},
];

const FLOW_LANE_OPTIONS: Array<{value: HomepageFlowLane | null; label: string}> = [
  {value: null, label: '未指定'},
  {value: 'main', label: '主列表'},
  {value: 'brief_pool', label: '快讯池'},
  {value: 'low_priority', label: '低优先级'},
  {value: 'needs_review', label: '待复核'},
];

const ISSUE_MARKER_OPTIONS: Array<{value: StructuredFeedbackIssueMarker; label: string}> = [
  {value: 'misleading_summary', label: '摘要偏差'},
  {value: 'wrong_topic', label: '主题不准'},
  {value: 'wrong_priority', label: '优先级不准'},
  {value: 'thin_content', label: '内容偏薄'},
  {value: 'duplicate', label: '重复内容'},
  {value: 'source_quality', label: '来源质量'},
  {value: 'extraction_problem', label: '正文提取'},
];

const CONTENT_TYPE_OPTIONS = [
  {value: '', label: '未指定'},
  {value: 'news_update', label: '新闻快讯'},
  {value: 'analysis', label: '分析评论'},
  {value: 'tutorial', label: '教程指南'},
  {value: 'case_study', label: '案例复盘'},
  {value: 'reference', label: '资料参考'},
  {value: 'release_note', label: '发布更新'},
  {value: 'opinion', label: '观点文章'},
  {value: 'list_digest', label: '清单汇总'},
  {value: 'research_paper', label: '研究论文'},
  {value: 'unknown', label: '未确定'},
];

interface InsightFeedbackPopoverProps {
  channelFeedbackDraft: FeedItemChannelCorrectionInput;
  hasAnyChannelFeedback: boolean;
  hasAnyFeedback: boolean;
  hasInsightFeedbackChanges: boolean;
  insightChannels: InsightChannel[];
  insightFeedbackDraft: string;
  insightFeedbackError: string | null;
  isAdvancedOpen: boolean;
  isSaving: boolean;
  normalizedChannelFeedbackDraft: FeedItemChannelCorrectionInput | null;
  normalizedStructuredFeedbackDraft: FeedItemStructuredFeedback | null;
  structuredFeedbackDraft: FeedItemStructuredFeedback;
  onCancel: () => void;
  onClearAll: () => void;
  onClearChannelCorrection: () => void;
  onSave: (
    nextFeedbackText: string | null,
    nextStructuredFeedback: FeedItemStructuredFeedback | null,
    nextChannelFeedback: FeedItemChannelCorrectionInput | null,
  ) => void;
  setChannelFeedbackDraft: Dispatch<SetStateAction<FeedItemChannelCorrectionInput>>;
  setInsightFeedbackDraft: (value: string) => void;
  setIsAdvancedOpen: Dispatch<SetStateAction<boolean>>;
  setStructuredFeedbackDraft: Dispatch<SetStateAction<FeedItemStructuredFeedback>>;
}

export function InsightFeedbackPopover({
  channelFeedbackDraft,
  hasAnyChannelFeedback,
  hasAnyFeedback,
  hasInsightFeedbackChanges,
  insightChannels,
  insightFeedbackDraft,
  insightFeedbackError,
  isAdvancedOpen,
  isSaving,
  normalizedChannelFeedbackDraft,
  normalizedStructuredFeedbackDraft,
  structuredFeedbackDraft,
  onCancel,
  onClearAll,
  onClearChannelCorrection,
  onSave,
  setChannelFeedbackDraft,
  setInsightFeedbackDraft,
  setIsAdvancedOpen,
  setStructuredFeedbackDraft,
}: InsightFeedbackPopoverProps) {
  const activeInsightChannels = useMemo(
    () => insightChannels.filter((channel) => channel.enabled && !channel.retiredAt),
    [insightChannels],
  );
  const activeInsightChannelIds = useMemo(
    () => new Set(activeInsightChannels.map((channel) => channel.id)),
    [activeInsightChannels],
  );
  const selectedInactiveChannelIds = useMemo(
    () =>
      [
        channelFeedbackDraft.primaryChannelId,
        ...channelFeedbackDraft.addChannelIds,
        ...channelFeedbackDraft.removeChannelIds,
      ].filter((channelId): channelId is string => Boolean(channelId && !activeInsightChannelIds.has(channelId))),
    [activeInsightChannelIds, channelFeedbackDraft],
  );

  return (
    <div className="absolute left-0 top-full z-50 w-[420px] max-w-[calc(100vw-2rem)] rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-4 shadow-xl shadow-black/10">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="text-sm font-semibold text-[var(--text-main)]">反馈</div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            className="rounded-md px-3 py-1.5 text-xs font-medium text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-45"
            disabled={isSaving}
            type="button"
            onClick={onCancel}
          >
            取消
          </button>
          <button
            className="rounded-md border border-[var(--border-subtle)] px-3 py-1.5 text-xs font-medium text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-45"
            disabled={isSaving || !hasAnyFeedback}
            type="button"
            onClick={onClearAll}
          >
            清空
          </button>
          <button
            className="rounded-md bg-[var(--accent)] px-3 py-1.5 text-xs font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-45"
            disabled={!hasInsightFeedbackChanges || isSaving}
            type="button"
            onClick={() => onSave(insightFeedbackDraft, normalizedStructuredFeedbackDraft, normalizedChannelFeedbackDraft)}
          >
            {isSaving ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
      <div className="grid gap-3">
        <FeedbackSegment
          label="价值"
          options={OVERALL_VALUE_OPTIONS}
          value={structuredFeedbackDraft.overallValue}
          onChange={(value) => setStructuredFeedbackDraft((current) => ({...current, overallValue: value}))}
        />
        <FeedbackSegment
          label="优先级"
          options={PRIORITY_CORRECTION_OPTIONS}
          value={structuredFeedbackDraft.priorityCorrection}
          onChange={(value) => setStructuredFeedbackDraft((current) => ({...current, priorityCorrection: value}))}
        />
        <div>
          <div className="mb-1.5 text-xs font-semibold uppercase text-[var(--text-soft)]">问题标记</div>
          <div className="flex flex-wrap gap-1.5">
            {ISSUE_MARKER_OPTIONS.map((option) => (
              <button
                key={option.value}
                className={`rounded-md border px-2 py-1 text-xs transition-colors ${
                  structuredFeedbackDraft.issueMarkers.includes(option.value)
                    ? 'border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]'
                    : 'border-[var(--border-subtle)] text-[var(--text-muted)] hover:bg-[var(--bg-hover)]'
                }`}
                type="button"
                onClick={() =>
                  setStructuredFeedbackDraft((current) => ({
                    ...current,
                    issueMarkers: toggleListValue(current.issueMarkers, option.value),
                  }))
                }
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>
        <button
          className="w-fit rounded-md px-2 py-1 text-xs font-medium text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]"
          type="button"
          onClick={() => setIsAdvancedOpen((current) => !current)}
        >
          {isAdvancedOpen ? '收起修正项' : '展开修正项'}
        </button>
        {isAdvancedOpen ? (
          <div className="grid gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-3">
            <FeedbackTextInput
              label="添加主题 ID"
              value={structuredFeedbackDraft.topicAddIds.join(', ')}
              onChange={(value) => setStructuredFeedbackDraft((current) => ({...current, topicAddIds: parseTokenList(value)}))}
            />
            <FeedbackTextInput
              label="移除主题 ID"
              value={structuredFeedbackDraft.topicRemoveIds.join(', ')}
              onChange={(value) => setStructuredFeedbackDraft((current) => ({...current, topicRemoveIds: parseTokenList(value)}))}
            />
            <label className="grid gap-1 text-xs">
              <span className="font-semibold uppercase text-[var(--text-soft)]">内容类型</span>
              <select
                className="w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-2 py-1.5 text-xs text-[var(--text-main)]"
                value={structuredFeedbackDraft.contentTypeCorrection ?? ''}
                onChange={(event) =>
                  setStructuredFeedbackDraft((current) => ({
                    ...current,
                    contentTypeCorrection: event.target.value || null,
                  }))
                }
              >
                {CONTENT_TYPE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <FeedbackTextInput
              label="添加 reason code"
              value={structuredFeedbackDraft.reasonCodeAddIds.join(', ')}
              onChange={(value) =>
                setStructuredFeedbackDraft((current) => ({...current, reasonCodeAddIds: parseTokenList(value)}))
              }
            />
            <FeedbackTextInput
              label="移除 reason code"
              value={structuredFeedbackDraft.reasonCodeRemoveIds.join(', ')}
              onChange={(value) =>
                setStructuredFeedbackDraft((current) => ({...current, reasonCodeRemoveIds: parseTokenList(value)}))
              }
            />
          </div>
        ) : null}
        <div className="grid gap-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-3">
          <div className="flex items-center justify-between gap-3">
            <div className="text-xs font-semibold uppercase text-[var(--text-soft)]">频道纠偏</div>
            {hasAnyChannelFeedback ? (
              <button
                className="rounded-md px-2 py-1 text-xs font-medium text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-45"
                disabled={isSaving}
                type="button"
                onClick={onClearChannelCorrection}
              >
                清除频道纠偏
              </button>
            ) : null}
          </div>
          <label className="grid gap-1 text-xs">
            <span className="font-semibold uppercase text-[var(--text-soft)]">Primary channel</span>
            <select
              className="w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-2 py-1.5 text-xs text-[var(--text-main)]"
              value={channelFeedbackDraft.primaryChannelId ?? ''}
              onChange={(event) =>
                setChannelFeedbackDraft((current) => ({
                  ...current,
                  primaryChannelId: event.target.value || null,
                  removeChannelIds: event.target.value
                    ? current.removeChannelIds.filter((id) => id !== event.target.value)
                    : current.removeChannelIds,
                }))
              }
            >
              <option value="">未指定</option>
              {activeInsightChannels.map((channel) => (
                <option key={channel.id} value={channel.id}>
                  {channel.label}
                </option>
              ))}
              {channelFeedbackDraft.primaryChannelId &&
              !activeInsightChannelIds.has(channelFeedbackDraft.primaryChannelId) ? (
                <option value={channelFeedbackDraft.primaryChannelId} disabled>
                  {channelFeedbackDraft.primaryChannelId}（历史）
                </option>
              ) : null}
            </select>
          </label>
          {selectedInactiveChannelIds.length ? (
            <div className="text-xs text-amber-600">
              当前纠偏包含已禁用或已退休频道；保存前请改为 active channel 或清除该纠偏。
            </div>
          ) : null}
          <div className="grid gap-3 md:grid-cols-2">
            <ChannelToggleGroup
              label="补充频道"
              channels={activeInsightChannels}
              values={channelFeedbackDraft.addChannelIds}
              disabledIds={new Set([
                ...(channelFeedbackDraft.primaryChannelId ? [channelFeedbackDraft.primaryChannelId] : []),
                ...channelFeedbackDraft.removeChannelIds,
              ])}
              onToggle={(channelId) =>
                setChannelFeedbackDraft((current) => ({
                  ...current,
                  addChannelIds: toggleListValue(current.addChannelIds, channelId),
                  removeChannelIds: current.removeChannelIds.filter((id) => id !== channelId),
                }))
              }
            />
            <ChannelToggleGroup
              label="移除频道"
              channels={activeInsightChannels}
              values={channelFeedbackDraft.removeChannelIds}
              disabledIds={new Set([
                ...(channelFeedbackDraft.primaryChannelId ? [channelFeedbackDraft.primaryChannelId] : []),
                ...channelFeedbackDraft.addChannelIds,
              ])}
              onToggle={(channelId) =>
                setChannelFeedbackDraft((current) => ({
                  ...current,
                  removeChannelIds: toggleListValue(current.removeChannelIds, channelId),
                  addChannelIds: current.addChannelIds.filter((id) => id !== channelId),
                }))
              }
            />
          </div>
          <FeedbackSegment
            label="阅读深度"
            options={READING_DEPTH_OPTIONS}
            value={channelFeedbackDraft.readingDepth}
            onChange={(value) => setChannelFeedbackDraft((current) => ({...current, readingDepth: value}))}
          />
          <FeedbackSegment
            label="阅读流"
            options={FLOW_LANE_OPTIONS}
            value={channelFeedbackDraft.flowLane}
            onChange={(value) => setChannelFeedbackDraft((current) => ({...current, flowLane: value}))}
          />
          <textarea
            className="min-h-16 w-full resize-y rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-2 py-1.5 text-xs leading-relaxed text-[var(--text-main)] outline-none transition-colors focus:border-[var(--accent)]"
            placeholder="记录这次频道纠偏的原因..."
            value={channelFeedbackDraft.notes}
            onChange={(event) => setChannelFeedbackDraft((current) => ({...current, notes: event.target.value}))}
          />
        </div>
      </div>
      <textarea
        className="mt-3 min-h-24 w-full resize-y rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm leading-relaxed text-[var(--text-main)] outline-none transition-colors focus:border-[var(--accent)]"
        placeholder="补充自由反馈..."
        value={insightFeedbackDraft}
        onChange={(event) => setInsightFeedbackDraft(event.target.value)}
      />
      {insightFeedbackError ? <div className="mt-2 text-xs text-red-500">{insightFeedbackError}</div> : null}
    </div>
  );
}

export function cloneStructuredFeedback(
  feedback: FeedItemStructuredFeedback | null | undefined,
): FeedItemStructuredFeedback {
  return {
    overallValue: feedback?.overallValue ?? null,
    priorityCorrection: feedback?.priorityCorrection ?? null,
    topicAddIds: feedback?.topicAddIds ? [...feedback.topicAddIds] : [],
    topicRemoveIds: feedback?.topicRemoveIds ? [...feedback.topicRemoveIds] : [],
    contentTypeCorrection: feedback?.contentTypeCorrection ?? null,
    reasonCodeAddIds: feedback?.reasonCodeAddIds ? [...feedback.reasonCodeAddIds] : [],
    reasonCodeRemoveIds: feedback?.reasonCodeRemoveIds ? [...feedback.reasonCodeRemoveIds] : [],
    issueMarkers: feedback?.issueMarkers ? [...feedback.issueMarkers] : [],
    primaryChannelCorrection: feedback?.primaryChannelCorrection ?? null,
    channelAddIds: feedback?.channelAddIds ? [...feedback.channelAddIds] : [],
    channelRemoveIds: feedback?.channelRemoveIds ? [...feedback.channelRemoveIds] : [],
    readingDepthCorrection: feedback?.readingDepthCorrection ?? null,
    flowLaneCorrection: feedback?.flowLaneCorrection ?? null,
    channelCorrectionNotes: feedback?.channelCorrectionNotes ?? '',
    createdAt: feedback?.createdAt ?? null,
    updatedAt: feedback?.updatedAt ?? null,
  };
}

export function normalizeStructuredFeedbackDraft(feedback: FeedItemStructuredFeedback): FeedItemStructuredFeedback | null {
  const normalized: FeedItemStructuredFeedback = {
    overallValue: feedback.overallValue,
    priorityCorrection: feedback.priorityCorrection,
    topicAddIds: uniqueTokens(feedback.topicAddIds),
    topicRemoveIds: uniqueTokens(feedback.topicRemoveIds),
    contentTypeCorrection: feedback.contentTypeCorrection?.trim() || null,
    reasonCodeAddIds: uniqueTokens(feedback.reasonCodeAddIds),
    reasonCodeRemoveIds: uniqueTokens(feedback.reasonCodeRemoveIds),
    issueMarkers: uniqueTokens(feedback.issueMarkers),
    primaryChannelCorrection: feedback.primaryChannelCorrection?.trim() || null,
    channelAddIds: uniqueTokens(feedback.channelAddIds),
    channelRemoveIds: uniqueTokens(feedback.channelRemoveIds),
    readingDepthCorrection: feedback.readingDepthCorrection,
    flowLaneCorrection: feedback.flowLaneCorrection,
    channelCorrectionNotes: feedback.channelCorrectionNotes.trim(),
  };
  return hasStructuredFeedback(normalized) ? normalized : null;
}

export function hasStructuredFeedback(feedback: FeedItemStructuredFeedback | null | undefined) {
  if (!feedback) {
    return false;
  }
  return Boolean(
    feedback.overallValue ||
      feedback.priorityCorrection ||
      feedback.topicAddIds.length ||
      feedback.topicRemoveIds.length ||
      feedback.contentTypeCorrection ||
      feedback.reasonCodeAddIds.length ||
      feedback.reasonCodeRemoveIds.length ||
      feedback.issueMarkers.length ||
      feedback.primaryChannelCorrection ||
      feedback.channelAddIds.length ||
      feedback.channelRemoveIds.length ||
      feedback.readingDepthCorrection ||
      feedback.flowLaneCorrection ||
      feedback.channelCorrectionNotes,
  );
}

export function structuredFeedbackEquals(
  left: FeedItemStructuredFeedback | null,
  right: FeedItemStructuredFeedback | null,
) {
  return JSON.stringify(toComparableStructuredFeedback(left)) === JSON.stringify(toComparableStructuredFeedback(right));
}

export function cloneChannelFeedback(
  feedback: FeedItemChannelCorrection | null | undefined,
): FeedItemChannelCorrectionInput {
  return {
    primaryChannelId: feedback?.primaryChannelId ?? null,
    addChannelIds: feedback?.addChannelIds ? [...feedback.addChannelIds] : [],
    removeChannelIds: feedback?.removeChannelIds ? [...feedback.removeChannelIds] : [],
    readingDepth: feedback?.readingDepth ?? null,
    flowLane: feedback?.flowLane ?? null,
    notes: feedback?.notes ?? '',
  };
}

export function normalizeChannelFeedbackDraft(
  feedback: FeedItemChannelCorrectionInput,
): FeedItemChannelCorrectionInput | null {
  const primaryChannelId = feedback.primaryChannelId?.trim() || null;
  const addChannelIds = uniqueTokens(feedback.addChannelIds).filter((id) => id !== primaryChannelId);
  const removeChannelIds = uniqueTokens(feedback.removeChannelIds).filter(
    (id) => id !== primaryChannelId && !addChannelIds.includes(id),
  );
  const normalized: FeedItemChannelCorrectionInput = {
    primaryChannelId,
    addChannelIds,
    removeChannelIds,
    readingDepth: feedback.readingDepth ?? null,
    flowLane: feedback.flowLane ?? null,
    notes: feedback.notes.trim(),
  };
  return hasChannelFeedback(normalized) ? normalized : null;
}

export function hasChannelFeedback(feedback: FeedItemChannelCorrectionInput | FeedItemChannelCorrection | null | undefined) {
  if (!feedback) {
    return false;
  }
  return Boolean(
    feedback.primaryChannelId ||
      feedback.addChannelIds.length ||
      feedback.removeChannelIds.length ||
      feedback.readingDepth ||
      feedback.flowLane ||
      feedback.notes.trim(),
  );
}

export function channelFeedbackEquals(
  left: FeedItemChannelCorrectionInput | null,
  right: FeedItemChannelCorrection | null,
) {
  return JSON.stringify(toComparableChannelFeedback(left)) === JSON.stringify(toComparableChannelFeedback(right));
}

function toComparableChannelFeedback(feedback: FeedItemChannelCorrectionInput | FeedItemChannelCorrection | null) {
  if (!feedback) {
    return null;
  }
  return {
    primaryChannelId: feedback.primaryChannelId,
    addChannelIds: uniqueTokens(feedback.addChannelIds),
    removeChannelIds: uniqueTokens(feedback.removeChannelIds),
    readingDepth: feedback.readingDepth,
    flowLane: feedback.flowLane,
    notes: feedback.notes.trim(),
  };
}

function toComparableStructuredFeedback(feedback: FeedItemStructuredFeedback | null) {
  if (!feedback) {
    return null;
  }
  return {
    overallValue: feedback.overallValue,
    priorityCorrection: feedback.priorityCorrection,
    topicAddIds: uniqueTokens(feedback.topicAddIds),
    topicRemoveIds: uniqueTokens(feedback.topicRemoveIds),
    contentTypeCorrection: feedback.contentTypeCorrection,
    reasonCodeAddIds: uniqueTokens(feedback.reasonCodeAddIds),
    reasonCodeRemoveIds: uniqueTokens(feedback.reasonCodeRemoveIds),
    issueMarkers: uniqueTokens(feedback.issueMarkers),
  };
}

function parseTokenList(value: string) {
  return uniqueTokens(value.split(/[\s,，]+/));
}

function uniqueTokens<T extends string>(values: T[]): T[] {
  const result: T[] = [];
  for (const value of values) {
    const normalized = value.trim() as T;
    if (normalized && !result.includes(normalized)) {
      result.push(normalized);
    }
  }
  return result;
}

function toggleListValue<T extends string>(values: T[], value: T) {
  return values.includes(value) ? values.filter((entry) => entry !== value) : [...values, value];
}

function ChannelToggleGroup({
  channels,
  disabledIds,
  label,
  values,
  onToggle,
}: {
  channels: InsightChannel[];
  disabledIds: Set<string>;
  label: string;
  values: string[];
  onToggle: (channelId: string) => void;
}) {
  return (
    <div>
      <div className="mb-1.5 text-xs font-semibold uppercase text-[var(--text-soft)]">{label}</div>
      <div className="flex flex-wrap gap-1.5">
        {channels.map((channel) => {
          const isSelected = values.includes(channel.id);
          const isDisabled = disabledIds.has(channel.id) && !isSelected;
          return (
            <button
              key={channel.id}
              className={`rounded-md border px-2 py-1 text-xs transition-colors ${
                isSelected
                  ? 'border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]'
                  : 'border-[var(--border-subtle)] text-[var(--text-muted)] hover:bg-[var(--bg-hover)]'
              } disabled:cursor-not-allowed disabled:opacity-35`}
              disabled={isDisabled}
              type="button"
              onClick={() => onToggle(channel.id)}
            >
              {channel.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function FeedbackSegment<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: Array<{value: T | null; label: string}>;
  value: T | null;
  onChange: (value: T | null) => void;
}) {
  return (
    <div>
      <div className="mb-1.5 text-xs font-semibold uppercase text-[var(--text-soft)]">{label}</div>
      <div className="flex flex-wrap gap-1.5">
        {options.map((option) => (
          <button
            key={option.value ?? 'null'}
            className={`rounded-md border px-2 py-1 text-xs transition-colors ${
              value === option.value
                ? 'border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]'
                : 'border-[var(--border-subtle)] text-[var(--text-muted)] hover:bg-[var(--bg-hover)]'
            }`}
            type="button"
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function FeedbackTextInput({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="grid gap-1 text-xs">
      <span className="font-semibold uppercase text-[var(--text-soft)]">{label}</span>
      <input
        className="w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-2 py-1.5 text-xs text-[var(--text-main)] outline-none transition-colors focus:border-[var(--accent)]"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}
