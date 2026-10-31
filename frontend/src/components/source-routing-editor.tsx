import type {InsightChannel} from '@/types/settings';
import type {SourceChannelTendency, SourceFlowPolicy, SourceInsightRouting, SourceInsightRoutingInput} from '@/types/source';

const FLOW_POLICY_LABELS: Record<SourceFlowPolicy, string> = {
  normal: '普通',
  mixed_high_frequency: '混合高频',
  high_frequency_brief: '快讯优先',
};

interface SourceRoutingEditorProps {
  channels: InsightChannel[];
  routing: SourceInsightRouting | null;
  value: SourceInsightRoutingInput;
  error: string | null;
  isLoading: boolean;
  disabled: boolean;
  onChange: (value: SourceInsightRoutingInput) => void;
}

export function SourceRoutingEditor({
  channels,
  disabled,
  error,
  isLoading,
  routing,
  value,
  onChange,
}: SourceRoutingEditorProps) {
  const hasChannelCatalog = channels.length > 0;
  const enabledChannels = channels.filter((channel) => channel.enabled && !channel.retiredAt);
  const enabledChannelIds = hasChannelCatalog ? new Set(enabledChannels.map((channel) => channel.id)) : null;
  const enabledChannelTendencies = value.channelTendencies.filter((tendency) =>
    enabledChannelIds ? enabledChannelIds.has(tendency.channelId) : true,
  );
  const hiddenChannelTendencies = enabledChannelIds
    ? value.channelTendencies.filter((tendency) => !enabledChannelIds.has(tendency.channelId))
    : [];
  const isBlockingRoutingError = Boolean(error && !routing);
  const controlsDisabled = disabled || isLoading || isBlockingRoutingError;

  function emit(nextValue: SourceInsightRoutingInput) {
    if (isBlockingRoutingError) {
      return;
    }
    onChange(normalizeVisibleRouting(nextValue));
  }

  function mergeChannelTendencies(visibleTendencies: SourceChannelTendency[]) {
    return [...visibleTendencies, ...hiddenChannelTendencies];
  }

  function setFlowPolicy(flowPolicy: SourceFlowPolicy) {
    emit({...value, flowPolicy});
  }

  function toggleChannel(channelId: string, enabled: boolean) {
    const existing = enabledChannelTendencies.find((tendency) => tendency.channelId === channelId);
    if (!enabled) {
      emit({
        ...value,
        channelTendencies: mergeChannelTendencies(
          enabledChannelTendencies.filter((tendency) => tendency.channelId !== channelId),
        ),
      });
      return;
    }

    if (!existing) {
      emit({
        ...value,
        channelTendencies: mergeChannelTendencies([
          ...enabledChannelTendencies,
          {channelId, tendency: 0.7, isPrimary: value.channelTendencies.length === 0, notes: ''},
        ]),
      });
    }
  }

  function updateTendency(channelId: string, patch: Partial<SourceChannelTendency>) {
    emit({
      ...value,
      channelTendencies: mergeChannelTendencies(
        enabledChannelTendencies.map((tendency) =>
          tendency.channelId === channelId ? {...tendency, ...patch} : tendency,
        ),
      ),
    });
  }

  function setPrimary(channelId: string) {
    emit({
      ...value,
      channelTendencies: [
        ...enabledChannelTendencies.map((tendency) => ({
          ...tendency,
          isPrimary: tendency.channelId === channelId,
        })),
        ...hiddenChannelTendencies.map((tendency) => ({...tendency, isPrimary: false})),
      ],
    });
  }

  function removeHiddenChannel(channelId: string) {
    onChange({
      ...value,
      channelTendencies: value.channelTendencies.filter((tendency) => tendency.channelId !== channelId),
    });
  }

  return (
    <section className="grid gap-3 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-[var(--text-main)]">Insight routing</div>
          <p className="mt-1 text-xs leading-5 text-[var(--text-muted)]">
            一个源可以关联多个频道；Primary 最多只能选择一个。
          </p>
        </div>
        {routing ? (
          <span className="shrink-0 rounded-md border border-[var(--border-subtle)] px-2 py-1 text-xs text-[var(--text-soft)]">
            #{routing.sourceId}
          </span>
        ) : null}
      </div>

      <label className="grid gap-2 text-sm">
        <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">Flow policy</span>
        <select
          className="rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)] outline-none focus:border-[var(--accent)]"
          disabled={controlsDisabled}
          value={value.flowPolicy}
          onChange={(event) => setFlowPolicy(event.target.value as SourceFlowPolicy)}
        >
          {Object.entries(FLOW_POLICY_LABELS).map(([policy, label]) => (
            <option key={policy} value={policy}>
              {label}
            </option>
          ))}
        </select>
      </label>

      {isLoading ? <div className="text-xs text-[var(--text-muted)]">正在加载 source routing...</div> : null}
      {error ? <div className="text-xs text-red-600">Routing 加载失败：{error}</div> : null}
      {isBlockingRoutingError ? (
        <div className="text-xs text-red-600">Routing 未成功加载，不能从默认空 routing 修改后保存。</div>
      ) : null}
      {!hasChannelCatalog ? (
        <div className="text-xs text-[var(--text-muted)]">Insight Channel 列表尚未加载，暂时只可修改 Flow policy。</div>
      ) : null}
      {hasChannelCatalog && !enabledChannels.length ? (
        <div className="text-xs text-[var(--text-muted)]">当前没有启用的 Insight Channel。</div>
      ) : null}
      {hiddenChannelTendencies.length ? (
        <div className="grid gap-2 rounded-md border border-amber-200 bg-amber-50 px-2 py-2 text-xs text-amber-800">
          <div>当前 routing 含有已禁用或已退休频道，保存前请移除或改用 active channel。</div>
          <div className="flex flex-wrap gap-2">
            {hiddenChannelTendencies.map((tendency) => (
              <button
                key={tendency.channelId}
                className="rounded-md border border-amber-300 bg-white px-2 py-1 text-amber-800"
                disabled={controlsDisabled}
                type="button"
                onClick={() => removeHiddenChannel(tendency.channelId)}
              >
                移除 {tendency.channelId}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      <div className="grid gap-2">
        {enabledChannels.map((channel) => {
          const tendency = enabledChannelTendencies.find((entry) => entry.channelId === channel.id);
          const tendencyValue = tendency ? clampTendency(tendency.tendency) : 0;
          return (
            <div
              key={channel.id}
              className="grid gap-2 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2"
            >
              <label className="flex items-center justify-between gap-3 text-sm text-[var(--text-main)]">
                <span className="min-w-0 truncate">{channel.label}</span>
                <input
                  checked={Boolean(tendency)}
                  disabled={controlsDisabled}
                  type="checkbox"
                  onChange={(event) => toggleChannel(channel.id, event.target.checked)}
                />
              </label>

              {tendency ? (
                <div className="grid gap-2 md:grid-cols-[1fr_96px]">
                  <label className="grid gap-1 text-xs text-[var(--text-muted)]">
                    <span>倾向 {tendencyValue.toFixed(2)}</span>
                    <div className="grid grid-cols-[1fr_70px] items-center gap-2">
                      <input
                        disabled={controlsDisabled}
                        max={1}
                        min={0}
                        step={0.05}
                        type="range"
                        value={tendencyValue}
                        onChange={(event) =>
                          updateTendency(channel.id, {tendency: clampTendency(Number(event.target.value))})
                        }
                      />
                      <input
                        className="rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-2 py-1 text-xs text-[var(--text-main)] outline-none focus:border-[var(--accent)]"
                        disabled={controlsDisabled}
                        max={1}
                        min={0}
                        step={0.05}
                        type="number"
                        value={tendencyValue}
                        onChange={(event) =>
                          updateTendency(channel.id, {tendency: clampTendency(Number(event.target.value))})
                        }
                      />
                    </div>
                  </label>
                  <button
                    className={`rounded-md border px-2 py-1.5 text-xs ${
                      tendency.isPrimary
                        ? 'border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]'
                        : 'border-[var(--border-subtle)] text-[var(--text-muted)] hover:bg-[var(--bg-hover)]'
                    }`}
                    disabled={controlsDisabled}
                    type="button"
                    onClick={() => setPrimary(channel.id)}
                  >
                    Primary
                  </button>
                  <input
                    className="rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-2 py-1.5 text-xs text-[var(--text-main)] outline-none focus:border-[var(--accent)] md:col-span-2"
                    disabled={controlsDisabled}
                    placeholder="备注"
                    value={tendency.notes}
                    onChange={(event) => updateTendency(channel.id, {notes: event.target.value})}
                  />
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function normalizeVisibleRouting(value: SourceInsightRoutingInput): SourceInsightRoutingInput {
  let primarySeen = false;
  return {
    ...value,
    channelTendencies: value.channelTendencies
      .map((tendency) => {
        const isPrimary = tendency.isPrimary && !primarySeen;
        if (isPrimary) {
          primarySeen = true;
        }
        return {
          ...tendency,
          tendency: clampTendency(tendency.tendency),
          isPrimary,
        };
      }),
  };
}

function clampTendency(value: number) {
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.min(Math.max(value, 0), 1);
}
