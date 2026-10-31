import {useEffect, useState} from 'react';
import {Archive, ChevronDown, ChevronRight, Lock, Plus, RefreshCw, Save, SlidersHorizontal} from 'lucide-react';
import {
  createInsightChannel,
  getInsightChannelImpact,
  listInsightChannels,
  retireInsightChannel,
  updateInsightChannel,
} from '@/services/settings-service';
import type {
  InsightChannel,
  InsightChannelCreateInput,
  InsightChannelDefaultFlowPolicy,
  InsightChannelImpact,
  ReadingDepth,
} from '@/types/settings';
import type {AppOperationDto} from '@/types/source';

type ChannelDraft = Pick<
  InsightChannel,
  | 'label'
  | 'description'
  | 'focusPrompt'
  | 'boundaryPrompt'
  | 'readingDepth'
  | 'defaultFlowPolicy'
  | 'sortOrder'
  | 'enabled'
>;

type NewChannelDraft = ChannelDraft & Pick<InsightChannelCreateInput, 'id'>;

interface InsightChannelSettingsPanelProps {
  onChannelsChanged?: () => void | Promise<void>;
  onBackendOperation: (operation: AppOperationDto) => void;
}

const READING_DEPTH_LABELS: Record<ReadingDepth, string> = {
  light: '轻读',
  medium: '标准',
  deep: '深读',
};

const READING_DEPTH_OPTIONS: ReadingDepth[] = ['light', 'medium', 'deep'];

const FLOW_POLICY_LABELS: Record<InsightChannelDefaultFlowPolicy, string> = {
  normal: '普通',
  mixed_high_frequency: '混合高频',
  high_frequency_brief: '快讯优先',
};

const FLOW_POLICY_OPTIONS: InsightChannelDefaultFlowPolicy[] = [
  'normal',
  'mixed_high_frequency',
  'high_frequency_brief',
];

function buildDraft(channel: InsightChannel): ChannelDraft {
  return {
    label: channel.label,
    description: channel.description,
    focusPrompt: channel.focusPrompt,
    boundaryPrompt: channel.boundaryPrompt,
    readingDepth: channel.readingDepth,
    defaultFlowPolicy: channel.defaultFlowPolicy,
    sortOrder: channel.sortOrder,
    enabled: channel.enabled,
  };
}

function buildEmptyNewChannelDraft(nextSortOrder: number): NewChannelDraft {
  return {
    id: '',
    label: '',
    description: '',
    focusPrompt: '',
    boundaryPrompt: '',
    readingDepth: 'medium',
    defaultFlowPolicy: 'normal',
    sortOrder: nextSortOrder,
    enabled: true,
  };
}

function nextSortOrder(channels: InsightChannel[]): number {
  const editableSortOrders = channels
    .filter((channel) => !channel.isSystem && !channel.retiredAt)
    .map((channel) => channel.sortOrder)
    .filter((sortOrder) => Number.isFinite(sortOrder));
  if (editableSortOrders.length === 0) {
    return 100;
  }
  return Math.min(Math.max(...editableSortOrders) + 10, 9980);
}

function formatRetiredAt(retiredAt: string | null): string | null {
  if (!retiredAt) {
    return null;
  }
  return new Date(retiredAt).toLocaleString();
}

export function InsightChannelSettingsPanel({onBackendOperation, onChannelsChanged}: InsightChannelSettingsPanelProps) {
  const [channels, setChannels] = useState<InsightChannel[]>([]);
  const [drafts, setDrafts] = useState<Record<string, ChannelDraft>>({});
  const [newChannelDraft, setNewChannelDraft] = useState<NewChannelDraft>(() => buildEmptyNewChannelDraft(100));
  const [isCreating, setIsCreating] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [savingIds, setSavingIds] = useState<Record<string, boolean>>({});
  const [impactById, setImpactById] = useState<Record<string, InsightChannelImpact | null>>({});
  const [retiringIds, setRetiringIds] = useState<Record<string, boolean>>({});
  const [messages, setMessages] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [isExpanded, setIsExpanded] = useState(false);
  const catalogVersion = channels[0]?.channelCatalogVersion ?? 1;
  const activeChannelCount = channels.filter((channel) => !channel.retiredAt && channel.enabled).length;
  const retiredChannelCount = channels.filter((channel) => channel.retiredAt).length;

  useEffect(() => {
    void loadChannels();
  }, []);

  async function loadChannels(options?: {notifyChanged?: boolean}) {
    setIsLoading(true);
    setLoadError(null);
    try {
      const nextChannels = await listInsightChannels();
      setChannels(nextChannels);
      setDrafts(Object.fromEntries(nextChannels.map((channel) => [channel.id, buildDraft(channel)])));
      setNewChannelDraft(buildEmptyNewChannelDraft(nextSortOrder(nextChannels)));
      setImpactById({});
      setMessages({});
      setErrors({});
      if (options?.notifyChanged) {
        await onChannelsChanged?.();
      }
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : '加载 Insight Channels 失败。');
    } finally {
      setIsLoading(false);
    }
  }

  function updateDraft(channelId: string, patch: Partial<ChannelDraft>) {
    setDrafts((current) => {
      const draft = current[channelId];
      if (!draft) {
        return current;
      }
      return {
        ...current,
        [channelId]: {...draft, ...patch},
      };
    });
    setMessages((current) => ({...current, [channelId]: ''}));
    setErrors((current) => ({...current, [channelId]: ''}));
  }

  function updateNewChannelDraft(patch: Partial<NewChannelDraft>) {
    setNewChannelDraft((current) => ({...current, ...patch}));
    setErrors((current) => ({...current, __create__: ''}));
    setMessages((current) => ({...current, __create__: ''}));
  }

  async function createChannel() {
    if (isCreating) {
      return;
    }
    setIsCreating(true);
    setErrors((current) => ({...current, __create__: ''}));
    setMessages((current) => ({...current, __create__: ''}));
    try {
      await createInsightChannel({
        ...newChannelDraft,
        id: newChannelDraft.id.trim(),
        label: newChannelDraft.label.trim(),
        description: newChannelDraft.description.trim(),
        focusPrompt: newChannelDraft.focusPrompt.trim(),
        boundaryPrompt: newChannelDraft.boundaryPrompt.trim(),
        expectedChannelCatalogVersion: catalogVersion,
      });
      await loadChannels({notifyChanged: true});
      setMessages((current) => ({...current, __create__: '已创建。'}));
    } catch (error) {
      setErrors((current) => ({
        ...current,
        __create__: error instanceof Error ? error.message : '创建 Insight Channel 失败。',
      }));
    } finally {
      setIsCreating(false);
    }
  }

  async function saveChannel(channelId: string) {
    const draft = drafts[channelId];
    if (!draft || savingIds[channelId]) {
      return;
    }

    setSavingIds((current) => ({...current, [channelId]: true}));
    setMessages((current) => ({...current, [channelId]: ''}));
    setErrors((current) => ({...current, [channelId]: ''}));
    try {
      await updateInsightChannel(channelId, {
        label: draft.label,
        description: draft.description,
        focusPrompt: draft.focusPrompt,
        boundaryPrompt: draft.boundaryPrompt,
        readingDepth: draft.readingDepth,
        defaultFlowPolicy: draft.defaultFlowPolicy,
        sortOrder: draft.sortOrder,
        enabled: draft.enabled,
        expectedChannelCatalogVersion: channels.find((channel) => channel.id === channelId)?.channelCatalogVersion,
      });
      await loadChannels({notifyChanged: true});
      setMessages((current) => ({...current, [channelId]: '已保存。'}));
    } catch (error) {
      setErrors((current) => ({
        ...current,
        [channelId]: error instanceof Error ? error.message : '保存 Insight Channel 失败。',
      }));
    } finally {
      setSavingIds((current) => ({...current, [channelId]: false}));
    }
  }

  async function previewImpact(channelId: string) {
    if (retiringIds[channelId]) {
      return;
    }
    setErrors((current) => ({...current, [channelId]: ''}));
    try {
      const impact = await getInsightChannelImpact(channelId);
      setImpactById((current) => ({...current, [channelId]: impact}));
    } catch (error) {
      setErrors((current) => ({
        ...current,
        [channelId]: error instanceof Error ? error.message : '读取退休影响失败。',
      }));
    }
  }

  async function retireChannel(channel: InsightChannel) {
    if (retiringIds[channel.id]) {
      return;
    }
    setRetiringIds((current) => ({...current, [channel.id]: true}));
    setErrors((current) => ({...current, [channel.id]: ''}));
    setMessages((current) => ({...current, [channel.id]: ''}));
    try {
      const operation = await retireInsightChannel(channel.id, channel.channelCatalogVersion);
      onBackendOperation(operation);
      await loadChannels({notifyChanged: true});
      setMessages((current) => ({...current, [channel.id]: '已退休，reroute 任务已启动。'}));
    } catch (error) {
      setErrors((current) => ({
        ...current,
        [channel.id]: error instanceof Error ? error.message : '退休 Insight Channel 失败。',
      }));
    } finally {
      setRetiringIds((current) => ({...current, [channel.id]: false}));
    }
  }

  return (
    <section className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <button className="flex min-w-0 flex-1 items-start gap-2 text-left" type="button" onClick={() => setIsExpanded((current) => !current)}>
          <span className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-[var(--border-subtle)] text-[var(--text-muted)]">
            {isExpanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          </span>
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-sm font-semibold text-[var(--text-main)]">
              <SlidersHorizontal size={16} />
              Insight Channels
            </div>
            <p className="mt-1 text-sm text-[var(--text-muted)]">
              {isLoading
                ? '正在加载频道配置。'
                : `${channels.length} 个频道，${activeChannelCount} 个启用，${retiredChannelCount} 个已退休；默认收起。`}
            </p>
          </div>
        </button>
        <button
          className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
          disabled={isLoading}
          type="button"
          onClick={() => void loadChannels({notifyChanged: true})}
        >
          <RefreshCw size={15} className={isLoading ? 'animate-spin' : ''} />
          刷新
        </button>
      </div>

      {loadError ? (
        <div className="mt-4 rounded-md border border-red-200 bg-red-50 px-3 py-3 text-sm text-red-700">
          <div>{loadError}</div>
          <button
            className="mt-2 inline-flex items-center gap-1 rounded-md border border-red-200 px-2 py-1 text-xs font-medium"
            disabled={isLoading}
            type="button"
            onClick={() => void loadChannels({notifyChanged: true})}
          >
            <RefreshCw size={13} />
            重试
          </button>
        </div>
      ) : null}

      {!loadError && isExpanded ? (
        <div className="mt-4 grid gap-4">
          <div className="grid gap-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-[var(--text-main)]">
              <Plus size={15} />
              新建频道
            </div>
            <div className="grid gap-3 lg:grid-cols-[160px_minmax(160px,1fr)_150px_150px_120px]">
              <label className="grid gap-1.5 text-sm">
                <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">ID</span>
                <input
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                  disabled={isCreating}
                  placeholder="my_channel"
                  value={newChannelDraft.id}
                  onChange={(event) => updateNewChannelDraft({id: event.target.value})}
                />
              </label>
              <label className="grid gap-1.5 text-sm">
                <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">名称</span>
                <input
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                  disabled={isCreating}
                  value={newChannelDraft.label}
                  onChange={(event) => updateNewChannelDraft({label: event.target.value})}
                />
              </label>
              <label className="grid gap-1.5 text-sm">
                <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">阅读深度</span>
                <select
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                  disabled={isCreating}
                  value={newChannelDraft.readingDepth}
                  onChange={(event) => updateNewChannelDraft({readingDepth: event.target.value as ReadingDepth})}
                >
                  {READING_DEPTH_OPTIONS.map((depth) => (
                    <option key={depth} value={depth}>
                      {READING_DEPTH_LABELS[depth]}
                    </option>
                  ))}
                </select>
              </label>
              <label className="grid gap-1.5 text-sm">
                <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">默认流策略</span>
                <select
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                  disabled={isCreating}
                  value={newChannelDraft.defaultFlowPolicy}
                  onChange={(event) =>
                    updateNewChannelDraft({defaultFlowPolicy: event.target.value as InsightChannelDefaultFlowPolicy})
                  }
                >
                  {FLOW_POLICY_OPTIONS.map((policy) => (
                    <option key={policy} value={policy}>
                      {FLOW_POLICY_LABELS[policy]}
                    </option>
                  ))}
                </select>
              </label>
              <label className="grid gap-1.5 text-sm">
                <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">排序</span>
                <input
                  className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                  disabled={isCreating}
                  type="number"
                  value={newChannelDraft.sortOrder}
                  onChange={(event) => updateNewChannelDraft({sortOrder: Number(event.target.value)})}
                />
              </label>
            </div>
            <div className="grid gap-3 lg:grid-cols-3">
              <textarea
                className="min-h-20 resize-y rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                disabled={isCreating}
                placeholder="说明"
                value={newChannelDraft.description}
                onChange={(event) => updateNewChannelDraft({description: event.target.value})}
              />
              <textarea
                className="min-h-20 resize-y rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                disabled={isCreating}
                placeholder="关注提示词"
                value={newChannelDraft.focusPrompt}
                onChange={(event) => updateNewChannelDraft({focusPrompt: event.target.value})}
              />
              <textarea
                className="min-h-20 resize-y rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                disabled={isCreating}
                placeholder="边界提示词"
                value={newChannelDraft.boundaryPrompt}
                onChange={(event) => updateNewChannelDraft({boundaryPrompt: event.target.value})}
              />
            </div>
            <div className="flex items-center justify-between gap-3">
              <label className="flex items-center gap-2 text-sm text-[var(--text-main)]">
                <input
                  checked={newChannelDraft.enabled}
                  disabled={isCreating}
                  type="checkbox"
                  onChange={(event) => updateNewChannelDraft({enabled: event.target.checked})}
                />
                启用
              </label>
              <button
                className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50"
                disabled={isCreating || isLoading}
                type="button"
                onClick={() => void createChannel()}
              >
                <Plus size={15} />
                {isCreating ? '创建中' : '创建频道'}
              </button>
            </div>
            {messages.__create__ ? <div className="text-sm text-emerald-600">{messages.__create__}</div> : null}
            {errors.__create__ ? <div className="text-sm text-red-600">{errors.__create__}</div> : null}
          </div>
          <div className="divide-y divide-[var(--border-subtle)] overflow-hidden rounded-lg border border-[var(--border-subtle)]">
          {channels.map((channel) => {
            const draft = drafts[channel.id] ?? buildDraft(channel);
            const isSaving = Boolean(savingIds[channel.id]);
            const isRetired = Boolean(channel.retiredAt);
            const isLocked = channel.isSystem || isRetired;
            const controlsDisabled = isSaving || isLocked;
            const isRetiring = Boolean(retiringIds[channel.id]);
            const impact = impactById[channel.id];
            const retiredAtLabel = formatRetiredAt(channel.retiredAt);
            return (
              <div key={channel.id} className="grid gap-3 bg-[var(--bg-panel-strong)] px-3 py-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="grid gap-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <code className="rounded-md bg-[var(--bg-panel)] px-2 py-1 text-xs text-[var(--text-main)]">
                        {channel.id}
                      </code>
                      {channel.isSystem ? (
                        <span className="inline-flex items-center gap-1 rounded-full border border-[var(--border-subtle)] px-2 py-1 text-xs font-medium text-[var(--text-muted)]">
                          <Lock size={12} />
                          系统频道
                        </span>
                      ) : null}
                      <span
                        className={`rounded-full px-2 py-1 text-xs font-medium ${
                          isRetired
                            ? 'bg-slate-100 text-slate-600'
                            : draft.enabled
                              ? 'bg-emerald-50 text-emerald-700'
                              : 'bg-amber-50 text-amber-700'
                        }`}
                      >
                        {isRetired ? '已退休' : draft.enabled ? 'Active' : 'Disabled'}
                      </span>
                      <span className="text-xs text-[var(--text-soft)]">catalog v{channel.channelCatalogVersion}</span>
                    </div>
                    {retiredAtLabel ? (
                      <div className="text-xs text-[var(--text-muted)]">退休时间：{retiredAtLabel}</div>
                    ) : null}
                  </div>
                  <div className="flex flex-wrap justify-end gap-2">
                    {!channel.isSystem && !isRetired ? (
                      <button
                        className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                        disabled={isRetiring}
                        type="button"
                        onClick={() => void previewImpact(channel.id)}
                      >
                        <SlidersHorizontal size={15} />
                        影响预览
                      </button>
                    ) : null}
                    {!channel.isSystem && !isRetired && impact ? (
                      <button
                        className="inline-flex items-center gap-1.5 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm font-medium text-amber-800 disabled:cursor-not-allowed disabled:opacity-50"
                        disabled={isRetiring}
                        type="button"
                        onClick={() => void retireChannel(channel)}
                      >
                        <Archive size={15} />
                        {isRetiring ? '退休中' : '确认退休'}
                      </button>
                    ) : null}
                  </div>
                </div>
                {impact ? (
                  <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
                    影响预览：{impact.affectedFeedItemCount} 篇文章、{impact.sourceTendencyCount} 条来源倾向、
                    {impact.humanCorrectionCount} 条人工修正。当前 catalog v{impact.channelCatalogVersion}。
                  </div>
                ) : null}
                <div className="grid gap-3 lg:grid-cols-[minmax(160px,1fr)_160px_160px_120px_120px]">
                  <label className="grid gap-1.5 text-sm">
                    <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">名称</span>
                    <input
                      className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                      disabled={controlsDisabled}
                      value={draft.label}
                      onChange={(event) => updateDraft(channel.id, {label: event.target.value})}
                    />
                  </label>
                  <label className="grid gap-1.5 text-sm">
                    <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">阅读深度</span>
                    <select
                      className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                      disabled={controlsDisabled}
                      value={draft.readingDepth}
                      onChange={(event) => updateDraft(channel.id, {readingDepth: event.target.value as ReadingDepth})}
                    >
                      {READING_DEPTH_OPTIONS.map((depth) => (
                        <option key={depth} value={depth}>
                          {READING_DEPTH_LABELS[depth]}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="grid gap-1.5 text-sm">
                    <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">默认流策略</span>
                    <select
                      className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                      disabled={controlsDisabled}
                      value={draft.defaultFlowPolicy}
                      onChange={(event) =>
                        updateDraft(channel.id, {
                          defaultFlowPolicy: event.target.value as InsightChannelDefaultFlowPolicy,
                        })
                      }
                    >
                      {FLOW_POLICY_OPTIONS.map((policy) => (
                        <option key={policy} value={policy}>
                          {FLOW_POLICY_LABELS[policy]}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="grid gap-1.5 text-sm">
                    <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">排序</span>
                    <input
                      className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                      disabled={controlsDisabled}
                      type="number"
                      value={draft.sortOrder}
                      onChange={(event) => updateDraft(channel.id, {sortOrder: Number(event.target.value)})}
                    />
                  </label>
                  <div className="flex items-end justify-between gap-3">
                    <label className="flex items-center gap-2 pb-2 text-sm text-[var(--text-main)]">
                      <input
                        checked={draft.enabled}
                        disabled={controlsDisabled}
                        type="checkbox"
                        onChange={(event) => updateDraft(channel.id, {enabled: event.target.checked})}
                      />
                      启用
                    </label>
                    <button
                      className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50"
                      disabled={isSaving || isLocked}
                      type="button"
                      onClick={() => void saveChannel(channel.id)}
                    >
                      <Save size={15} />
                      {isSaving ? '保存中' : '保存'}
                    </button>
                  </div>
                </div>
                <label className="grid gap-1.5 text-sm">
                  <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">说明</span>
                  <textarea
                    className="min-h-20 resize-y rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                    disabled={controlsDisabled}
                    value={draft.description}
                    onChange={(event) => updateDraft(channel.id, {description: event.target.value})}
                  />
                </label>
                <div className="grid gap-3 lg:grid-cols-2">
                  <label className="grid gap-1.5 text-sm">
                    <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">关注提示词</span>
                    <textarea
                      className="min-h-24 resize-y rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                      disabled={controlsDisabled}
                      value={draft.focusPrompt}
                      onChange={(event) => updateDraft(channel.id, {focusPrompt: event.target.value})}
                    />
                  </label>
                  <label className="grid gap-1.5 text-sm">
                    <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">边界提示词</span>
                    <textarea
                      className="min-h-24 resize-y rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)]"
                      disabled={controlsDisabled}
                      value={draft.boundaryPrompt}
                      onChange={(event) => updateDraft(channel.id, {boundaryPrompt: event.target.value})}
                    />
                  </label>
                </div>
                {messages[channel.id] ? <div className="text-sm text-emerald-600">{messages[channel.id]}</div> : null}
                {errors[channel.id] ? <div className="text-sm text-red-600">{errors[channel.id]}</div> : null}
              </div>
            );
          })}
          {!isLoading && channels.length === 0 ? (
            <div className="bg-[var(--bg-panel-strong)] px-3 py-8 text-center text-sm text-[var(--text-muted)]">
              暂无 Insight Channels。
            </div>
          ) : null}
          {isLoading && channels.length === 0 ? (
            <div className="bg-[var(--bg-panel-strong)] px-3 py-8 text-center text-sm text-[var(--text-muted)]">
              正在加载 Insight Channels...
            </div>
          ) : null}
          </div>
        </div>
      ) : null}
    </section>
  );
}
