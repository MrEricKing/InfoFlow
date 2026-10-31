import {useEffect, useMemo, useState, type ReactNode} from 'react';
import {ChevronDown, ExternalLink, Save, Trash2, TriangleAlert, X} from 'lucide-react';
import {SourceIcon, formatSourceIconSource} from '@/components/source-icon';
import {SourceRoutingEditor} from '@/components/source-routing-editor';
import type {InsightChannel} from '@/types/settings';
import type {
  SourceAIRoute,
  SourceDto,
  SourceInsightRouting,
  SourceInsightRoutingInput,
  SourceKind,
  SourcePreferenceProfileFieldError,
  SourcePreferenceProfileInput,
  SourcePreferenceProfileState,
  SourcePreviewResponseDto,
} from '@/types/source';

export interface SourceEditorFormValue {
  name: string;
  kind: SourceKind;
  url: string;
  site_url: string | null;
  folder_path: string | null;
  is_active: boolean;
  ai_route: SourceAIRoute;
  insightRouting: SourceInsightRoutingInput;
  isInsightRoutingDirty: boolean;
  sourcePreferenceProfile: SourcePreferenceProfileInput;
  isSourcePreferenceProfileDirty: boolean;
  icon_url?: string | null;
  icon_source?: SourceDto['icon_source'];
}

interface SourceEditorDialogProps {
  categorySuggestions: string[];
  isSaving: boolean;
  mode: 'create' | 'edit';
  preview: SourcePreviewResponseDto | null;
  insightChannels: InsightChannel[];
  routing: SourceInsightRouting | null;
  routingError: string | null;
  isRoutingLoading: boolean;
  sourcePreferenceProfile: SourcePreferenceProfileState | null;
  sourceProfileError: string | null;
  isSourceProfileLoading: boolean;
  source: SourceDto | null;
  onClose: () => void;
  onSave: (value: SourceEditorFormValue) => Promise<void>;
  onDeleteSourceProfileDraft: () => Promise<void>;
  onSaveSourceProfileDraft: (value: SourcePreferenceProfileInput) => Promise<void>;
}

export function SourceEditorDialog({
  categorySuggestions,
  isSaving,
  mode,
  preview,
  insightChannels,
  routing,
  routingError,
  isRoutingLoading,
  sourcePreferenceProfile,
  sourceProfileError,
  isSourceProfileLoading,
  source,
  onClose,
  onDeleteSourceProfileDraft,
  onSave,
  onSaveSourceProfileDraft,
}: SourceEditorDialogProps) {
  const [formValue, setFormValue] = useState<SourceEditorFormValue>(() =>
    buildInitialValue(mode, source, preview, routing, sourcePreferenceProfile),
  );
  const [error, setError] = useState<string | null>(null);
  const [sourceProfileFieldErrors, setSourceProfileFieldErrors] = useState<SourcePreferenceProfileFieldError[]>([]);
  const [sourceProfileMessage, setSourceProfileMessage] = useState<string | null>(null);
  const [isDraftActionSaving, setIsDraftActionSaving] = useState(false);

  useEffect(() => {
    setFormValue(buildInitialValue(mode, source, preview, routing, sourcePreferenceProfile));
    setError(null);
    setSourceProfileFieldErrors([]);
    setSourceProfileMessage(null);
  }, [mode, preview, source]);

  useEffect(() => {
    if (formValue.isInsightRoutingDirty || !routing) {
      return;
    }
    setFormValue((current) => ({
      ...current,
      insightRouting: toInsightRoutingInput(routing),
    }));
  }, [formValue.isInsightRoutingDirty, routing]);

  useEffect(() => {
    if (formValue.isSourcePreferenceProfileDirty || !sourcePreferenceProfile) {
      return;
    }
    setFormValue((current) => ({
      ...current,
      sourcePreferenceProfile: sourcePreferenceProfileInputFromState(sourcePreferenceProfile),
    }));
  }, [formValue.isSourcePreferenceProfileDirty, sourcePreferenceProfile]);

  async function submit() {
    const normalized = {
      ...formValue,
      name: formValue.name.trim(),
      url: formValue.url.trim(),
      site_url: normalizeOptional(formValue.site_url),
      folder_path: normalizeOptional(formValue.folder_path),
      ai_route: formValue.ai_route,
      sourcePreferenceProfile: normalizeSourcePreferenceProfileInput(formValue.sourcePreferenceProfile),
    };

    if (!normalized.name) {
      setError('请输入订阅标题。');
      return;
    }
    if (!normalized.url) {
      setError('订阅 URL 不能为空。');
      return;
    }

    setError(null);
    setSourceProfileFieldErrors([]);
    setSourceProfileMessage(null);
    try {
      await onSave(normalized);
    } catch (saveError) {
      setSourceProfileFieldErrors(extractSourceProfileFieldErrors(saveError));
      setError(saveError instanceof Error ? saveError.message : '保存订阅失败。');
    }
  }

  async function saveSourceProfileDraft() {
    setIsDraftActionSaving(true);
    setError(null);
    setSourceProfileMessage(null);
    setSourceProfileFieldErrors([]);
    try {
      await onSaveSourceProfileDraft(normalizeSourcePreferenceProfileInput(formValue.sourcePreferenceProfile));
      setFormValue((current) => ({...current, isSourcePreferenceProfileDirty: false}));
      setSourceProfileMessage('Source Profile 草稿已保存。');
    } catch (saveError) {
      setSourceProfileFieldErrors(extractSourceProfileFieldErrors(saveError));
      setError(saveError instanceof Error ? saveError.message : '保存 Source Profile 草稿失败。');
    } finally {
      setIsDraftActionSaving(false);
    }
  }

  async function deleteSourceProfileDraft() {
    setIsDraftActionSaving(true);
    setError(null);
    setSourceProfileMessage(null);
    setSourceProfileFieldErrors([]);
    try {
      await onDeleteSourceProfileDraft();
      setFormValue((current) => ({
        ...current,
        sourcePreferenceProfile: sourcePreferenceProfileInputFromState(sourcePreferenceProfile),
        isSourcePreferenceProfileDirty: false,
      }));
      setSourceProfileMessage('Source Profile 草稿已删除。');
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : '删除 Source Profile 草稿失败。');
    } finally {
      setIsDraftActionSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/35 px-4 py-6">
      <div className="panel flex max-h-full w-full max-w-2xl flex-col overflow-hidden rounded-lg border border-[var(--border-subtle)] shadow-[0_24px_80px_rgba(15,23,42,0.28)]">
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-[var(--border-subtle)] px-5 py-4">
          <div>
            <div className="text-sm font-semibold text-[var(--text-main)]">
              {mode === 'create' ? '添加订阅' : '编辑订阅'}
            </div>
            <div className="mt-1 text-xs text-[var(--text-muted)]">
              {preview ? preview.source.resolved_url : source?.url}
            </div>
          </div>
          <button
            className="rounded-md p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]"
            title="关闭"
            type="button"
            onClick={onClose}
          >
            <X size={18} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {preview ? <PreviewSummary preview={preview} /> : null}
          {!preview && source ? <SourceIconSummary source={source} /> : null}

          <div className="mt-4 grid gap-4">
            <LabeledInput label="订阅标题">
              <input
                className="w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)] outline-none focus:border-[var(--accent)]"
                value={formValue.name}
                onChange={(event) => setFormValue((current) => ({...current, name: event.target.value}))}
              />
            </LabeledInput>

            <div className="grid gap-4 md:grid-cols-[160px_1fr]">
              <LabeledInput label="类型">
                <select
                  className="w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)] outline-none focus:border-[var(--accent)]"
                  value={formValue.kind}
                  disabled={mode === 'edit'}
                  onChange={(event) =>
                    setFormValue((current) => ({...current, kind: event.target.value as SourceKind}))
                  }
                >
                  <option value="rss">RSS</option>
                  <option value="rsshub">RSSHub</option>
                </select>
              </LabeledInput>
              <LabeledInput label="订阅 URL">
                <input
                  className="w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)] outline-none focus:border-[var(--accent)]"
                  value={formValue.url}
                  readOnly={mode === 'create'}
                  onChange={(event) => setFormValue((current) => ({...current, url: event.target.value}))}
                />
              </LabeledInput>
            </div>

            <LabeledInput label="站点 URL">
              <input
                className="w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)] outline-none focus:border-[var(--accent)]"
                value={formValue.site_url ?? ''}
                onChange={(event) => setFormValue((current) => ({...current, site_url: event.target.value}))}
              />
            </LabeledInput>

            <LabeledInput label="分类">
              <CategoryPicker
                suggestions={categorySuggestions}
                value={formValue.folder_path ?? ''}
                onChange={(value) => setFormValue((current) => ({...current, folder_path: value}))}
              />
            </LabeledInput>

            <LabeledInput label="AI 路由">
              <select
                className="w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)] outline-none focus:border-[var(--accent)]"
                value={formValue.ai_route}
                onChange={(event) => setFormValue((current) => ({...current, ai_route: event.target.value as SourceAIRoute}))}
              >
                <option value="general">通用内容 · general</option>
                <option value="sensitive">敏感内容 · sensitive</option>
                <option value="disabled">不生成 AI · disabled</option>
              </select>
            </LabeledInput>

            <SourceRoutingEditor
              channels={insightChannels}
              disabled={isSaving}
              error={routingError}
              isLoading={isRoutingLoading}
              routing={routing}
              value={formValue.insightRouting}
              onChange={(insightRouting) =>
                setFormValue((current) => ({...current, insightRouting, isInsightRoutingDirty: true}))
              }
            />

            <SourceProfileEditor
              disabled={isSaving || isDraftActionSaving}
              errors={sourceProfileFieldErrors}
              isCreateMode={mode === 'create'}
              isLoading={isSourceProfileLoading}
              message={sourceProfileMessage}
              profileError={sourceProfileError}
              state={sourcePreferenceProfile}
              value={formValue.sourcePreferenceProfile}
              onChange={(sourceProfile) =>
                setFormValue((current) => ({
                  ...current,
                  sourcePreferenceProfile: sourceProfile,
                  isSourcePreferenceProfileDirty: true,
                }))
              }
              onDeleteDraft={() => void deleteSourceProfileDraft()}
              onSaveDraft={() => void saveSourceProfileDraft()}
            />

            <label className="flex items-center gap-3 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-3 text-sm text-[var(--text-main)]">
              <input
                checked={formValue.is_active}
                type="checkbox"
                onChange={(event) => setFormValue((current) => ({...current, is_active: event.target.checked}))}
              />
              启用订阅
            </label>
          </div>

          {error ? <div className="mt-3 text-sm text-red-600">{error}</div> : null}
        </div>

        <div className="flex shrink-0 items-center justify-end gap-2 border-t border-[var(--border-subtle)] px-5 py-4">
          <button
            className="rounded-md border border-[var(--border-subtle)] px-4 py-2 text-sm text-[var(--text-main)] transition-colors hover:bg-[var(--bg-hover)]"
            type="button"
            onClick={onClose}
          >
            取消
          </button>
          <button
            className="inline-flex items-center gap-2 rounded-md bg-[var(--accent)] px-4 py-2 text-sm font-medium text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isSaving}
            type="button"
            onClick={() => void submit()}
          >
            <Save size={15} />
            {isSaving ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
    </div>
  );
}

function buildInitialValue(
  mode: SourceEditorDialogProps['mode'],
  source: SourceDto | null,
  preview: SourcePreviewResponseDto | null,
  routing: SourceInsightRouting | null,
  sourcePreferenceProfile: SourcePreferenceProfileState | null,
): SourceEditorFormValue {
  if (mode === 'edit' && source) {
    return {
      name: source.name,
      kind: source.kind,
      url: source.url,
      site_url: source.site_url,
      folder_path: source.folder_path,
      is_active: source.is_active,
      ai_route: source.ai_route,
      insightRouting: routing ? toInsightRoutingInput(routing) : defaultInsightRouting(),
      isInsightRoutingDirty: false,
      sourcePreferenceProfile: sourcePreferenceProfileInputFromState(sourcePreferenceProfile),
      isSourcePreferenceProfileDirty: false,
      icon_url: source.icon_url,
      icon_source: source.icon_source,
    };
  }

  return {
    name: preview?.source.title ?? '',
    kind: preview?.source.detected_kind ?? 'rss',
    url: preview?.source.canonical_url ?? '',
    site_url: preview?.source.site_url ?? null,
    folder_path: null,
    is_active: true,
    ai_route: 'general',
    insightRouting: defaultInsightRouting(),
    isInsightRoutingDirty: false,
    sourcePreferenceProfile: emptySourcePreferenceProfileInput(),
    isSourcePreferenceProfileDirty: false,
    icon_url: preview?.source.icon_url ?? null,
    icon_source: preview?.source.icon_source ?? null,
  };
}

function defaultInsightRouting(): SourceInsightRoutingInput {
  return {
    flowPolicy: 'normal',
    channelTendencies: [],
  };
}

function toInsightRoutingInput(routing: SourceInsightRouting): SourceInsightRoutingInput {
  return {
    flowPolicy: routing.flowPolicy,
    channelTendencies: routing.channelTendencies.map((tendency) => ({...tendency})),
  };
}

function normalizeOptional(value: string | null) {
  const trimmed = value?.trim() ?? '';
  return trimmed ? trimmed : null;
}

function emptySourcePreferenceProfileInput(): SourcePreferenceProfileInput {
  return {
    subscriptionReason: '',
    goodArticleCriteria: [],
    badArticlePatterns: [],
    preferredTopics: [],
    ignoredTopics: [],
    preferredContentTypes: [],
    positiveExampleSummaries: [],
    negativeExampleSummaries: [],
    qualityNotes: '',
    sourceType: 'user_edit',
    editNote: '',
  };
}

function sourcePreferenceProfileInputFromState(state: SourcePreferenceProfileState | null): SourcePreferenceProfileInput {
  const profile = state?.draft ?? state?.active;
  if (!profile) {
    return emptySourcePreferenceProfileInput();
  }
  return {
    subscriptionReason: profile.subscriptionReason ?? '',
    goodArticleCriteria: profile.goodArticleCriteria,
    badArticlePatterns: profile.badArticlePatterns,
    preferredTopics: profile.preferredTopics,
    ignoredTopics: profile.ignoredTopics,
    preferredContentTypes: profile.preferredContentTypes,
    positiveExampleItemIds: profile.positiveExampleItemIds,
    negativeExampleItemIds: profile.negativeExampleItemIds,
    positiveExampleSummaries: profile.positiveExampleSummaries,
    negativeExampleSummaries: profile.negativeExampleSummaries,
    qualityNotes: profile.qualityNotes ?? '',
    sourceType: 'user_edit',
    editNote: profile.editNote ?? '',
  };
}

function normalizeSourcePreferenceProfileInput(value: SourcePreferenceProfileInput): SourcePreferenceProfileInput {
  return {
    subscriptionReason: normalizeOptional(value.subscriptionReason ?? null),
    goodArticleCriteria: normalizeStringList(value.goodArticleCriteria),
    badArticlePatterns: normalizeStringList(value.badArticlePatterns),
    preferredTopics: normalizeStringList(value.preferredTopics),
    ignoredTopics: normalizeStringList(value.ignoredTopics),
    preferredContentTypes: normalizeStringList(value.preferredContentTypes),
    positiveExampleItemIds: value.positiveExampleItemIds ?? [],
    negativeExampleItemIds: value.negativeExampleItemIds ?? [],
    positiveExampleSummaries: normalizeStringList(value.positiveExampleSummaries),
    negativeExampleSummaries: normalizeStringList(value.negativeExampleSummaries),
    qualityNotes: normalizeOptional(value.qualityNotes ?? null),
    sourceType: value.sourceType ?? 'user_edit',
    editNote: normalizeOptional(value.editNote ?? null),
  };
}

function normalizeStringList(value: string[] | undefined) {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const item of value ?? []) {
    const normalized = item.trim();
    if (!normalized || seen.has(normalized)) {
      continue;
    }
    result.push(normalized);
    seen.add(normalized);
  }
  return result;
}

function splitLines(value: string) {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function utf8ByteLength(value: string) {
  return new TextEncoder().encode(value).length;
}

function errorsForField(errors: SourcePreferenceProfileFieldError[], fieldName: string) {
  return errors.filter((error) => error.field === fieldName || error.field.startsWith(`${fieldName}[`));
}

function extractSourceProfileFieldErrors(error: unknown): SourcePreferenceProfileFieldError[] {
  if (!error || typeof error !== 'object' || !('detail' in error)) {
    return [];
  }
  const detail = (error as {detail?: unknown}).detail;
  const nested = detail && typeof detail === 'object' && 'detail' in detail ? (detail as {detail?: unknown}).detail : null;
  if (!nested || typeof nested !== 'object' || !('errors' in nested)) {
    return [];
  }
  const errors = (nested as {errors?: unknown}).errors;
  if (!Array.isArray(errors)) {
    return [];
  }
  return errors.flatMap((item) => {
    if (!item || typeof item !== 'object') {
      return [];
    }
    const payload = item as Record<string, unknown>;
    return [{
      field: String(payload.field ?? ''),
      code: String(payload.code ?? ''),
      message: String(payload.message ?? ''),
      actual: typeof payload.actual === 'number' ? payload.actual : null,
      limit: typeof payload.limit === 'number' ? payload.limit : null,
      unit: typeof payload.unit === 'string' ? payload.unit : null,
      patternClass: typeof payload.pattern_class === 'string' ? payload.pattern_class : null,
    }];
  });
}

function LabeledInput({children, label}: {children: ReactNode; label: string}) {
  return (
    <label className="grid gap-2">
      <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">{label}</span>
      {children}
    </label>
  );
}

function CategoryPicker({
  suggestions,
  value,
  onChange,
}: {
  suggestions: string[];
  value: string;
  onChange: (value: string) => void;
}) {
  const normalizedValue = value.trim().toLowerCase();
  const filteredSuggestions = useMemo(
    () =>
      suggestions
        .filter((suggestion) => !normalizedValue || suggestion.toLowerCase().includes(normalizedValue))
        .slice(0, 8),
    [normalizedValue, suggestions],
  );

  return (
    <div className="grid gap-2">
      <input
        className="w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)] outline-none focus:border-[var(--accent)]"
        placeholder="未分类"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      {filteredSuggestions.length ? (
        <div className="flex flex-wrap gap-2">
          <button
            className="rounded-full border border-[var(--border-subtle)] px-2.5 py-1 text-xs text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)]"
            type="button"
            onClick={() => onChange('')}
          >
            未分类
          </button>
          {filteredSuggestions.map((suggestion) => (
            <button
              key={suggestion}
              className="rounded-full border border-[var(--border-subtle)] px-2.5 py-1 text-xs text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)]"
              type="button"
              onClick={() => onChange(suggestion)}
            >
              {suggestion}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function SourceProfileEditor({
  disabled,
  errors,
  isCreateMode,
  isLoading,
  message,
  profileError,
  state,
  value,
  onChange,
  onDeleteDraft,
  onSaveDraft,
}: {
  disabled: boolean;
  errors: SourcePreferenceProfileFieldError[];
  isCreateMode: boolean;
  isLoading: boolean;
  message: string | null;
  profileError: string | null;
  state: SourcePreferenceProfileState | null;
  value: SourcePreferenceProfileInput;
  onChange: (value: SourcePreferenceProfileInput) => void;
  onDeleteDraft: () => void;
  onSaveDraft: () => void;
}) {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const isEmptyProfile = state?.renderedPromptContext?.is_empty === true;
  const activeVersion = isEmptyProfile ? null : state?.active?.profileVersion ?? null;
  const hasDraft = Boolean(state?.draft);
  const hasExternalCandidateDraft = state?.draft?.sourceType === 'codex_import';

  function patch(patchValue: Partial<SourcePreferenceProfileInput>) {
    onChange({...value, ...patchValue});
  }

  return (
    <section className="grid gap-3 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-[var(--text-main)]">Source Profile</div>
          <p className="mt-1 text-xs leading-5 text-[var(--text-muted)]">
            默认只需要写订阅理由；这段内容会进入未来 Insight 请求。
          </p>
        </div>
        <span className="shrink-0 rounded-md border border-[var(--border-subtle)] px-2 py-1 text-xs text-[var(--text-soft)]">
          {activeVersion ?? 'empty'}
        </span>
      </div>

      {isLoading ? <div className="text-xs text-[var(--text-muted)]">正在加载 Source Profile...</div> : null}
      {profileError ? <div className="text-xs text-red-600">Source Profile 加载失败：{profileError}</div> : null}
      {hasExternalCandidateDraft ? (
        <div className="flex items-start gap-2 rounded-md border border-amber-300/70 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200">
          <TriangleAlert size={15} className="mt-0.5 shrink-0" />
          <span>
            这是外部分析导入的 Source Profile 建议。保存为草稿期间不会影响 AI 请求；只有你显式保存并激活后，未来 Insight jobs 才会使用它。
          </span>
        </div>
      ) : null}

      <TextAreaField
        disabled={disabled}
        errors={errorsForField(errors, 'subscription_reason')}
        label="为什么订阅这个源？"
        limit={1024}
        placeholder="我订阅这个源是为了看深入工程实践和调试经验，不太关心产品公告。"
        rows={3}
        value={value.subscriptionReason ?? ''}
        onChange={(nextValue) => patch({subscriptionReason: nextValue})}
      />

      <button
        className="inline-flex w-fit items-center gap-1.5 rounded-md border border-[var(--border-subtle)] px-3 py-1.5 text-xs text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)]"
        type="button"
        onClick={() => setAdvancedOpen((current) => !current)}
      >
        <ChevronDown size={14} className={advancedOpen ? 'rotate-180 transition-transform' : 'transition-transform'} />
        高级字段
      </button>

      {advancedOpen ? (
        <div className="grid gap-3">
          <ListTextAreaField
            disabled={disabled}
            errors={errorsForField(errors, 'good_article_criteria')}
            label="好文章标准"
            limit={400}
            value={value.goodArticleCriteria ?? []}
            onChange={(items) => patch({goodArticleCriteria: items})}
          />
          <ListTextAreaField
            disabled={disabled}
            errors={errorsForField(errors, 'bad_article_patterns')}
            label="坏文章模式"
            limit={400}
            value={value.badArticlePatterns ?? []}
            onChange={(items) => patch({badArticlePatterns: items})}
          />
          <ListTextAreaField
            disabled={disabled}
            errors={errorsForField(errors, 'preferred_topics')}
            label="偏好 topic id"
            value={value.preferredTopics ?? []}
            onChange={(items) => patch({preferredTopics: items})}
          />
          <ListTextAreaField
            disabled={disabled}
            errors={errorsForField(errors, 'ignored_topics')}
            label="忽略 topic id"
            value={value.ignoredTopics ?? []}
            onChange={(items) => patch({ignoredTopics: items})}
          />
          <ListTextAreaField
            disabled={disabled}
            errors={errorsForField(errors, 'preferred_content_types')}
            label="偏好 content type id"
            value={value.preferredContentTypes ?? []}
            onChange={(items) => patch({preferredContentTypes: items})}
          />
          <ListTextAreaField
            disabled={disabled}
            errors={errorsForField(errors, 'positive_example_summaries')}
            label="正向样本摘要"
            limit={500}
            value={value.positiveExampleSummaries ?? []}
            onChange={(items) => patch({positiveExampleSummaries: items})}
          />
          <ListTextAreaField
            disabled={disabled}
            errors={errorsForField(errors, 'negative_example_summaries')}
            label="负向样本摘要"
            limit={500}
            value={value.negativeExampleSummaries ?? []}
            onChange={(items) => patch({negativeExampleSummaries: items})}
          />
          <TextAreaField
            disabled={disabled}
            errors={errorsForField(errors, 'quality_notes')}
            label="质量备注"
            limit={2048}
            rows={3}
            value={value.qualityNotes ?? ''}
            onChange={(nextValue) => patch({qualityNotes: nextValue})}
          />
        </div>
      ) : null}

      {message ? <div className="text-xs text-emerald-600">{message}</div> : null}
      {errors.length ? (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          Source Profile 未保存：请根据字段提示改写后再试。
        </div>
      ) : null}

      {!isCreateMode ? (
        <div className="flex flex-wrap gap-2">
          <button
            className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border-subtle)] px-3 py-2 text-xs text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
            disabled={disabled || isLoading}
            type="button"
            onClick={onSaveDraft}
          >
            <Save size={14} />
            仅保存草稿
          </button>
          <button
            className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border-subtle)] px-3 py-2 text-xs text-[var(--text-muted)] disabled:cursor-not-allowed disabled:opacity-50"
            disabled={disabled || isLoading || !hasDraft}
            type="button"
            onClick={onDeleteDraft}
          >
            <Trash2 size={14} />
            删除草稿
          </button>
        </div>
      ) : null}
    </section>
  );
}

function TextAreaField({
  disabled,
  errors,
  label,
  limit,
  placeholder,
  rows = 2,
  value,
  onChange,
}: {
  disabled: boolean;
  errors: SourcePreferenceProfileFieldError[];
  label: string;
  limit?: number;
  placeholder?: string;
  rows?: number;
  value: string;
  onChange: (value: string) => void;
}) {
  const count = utf8ByteLength(value);
  return (
    <label className="grid gap-2 text-sm">
      <span className="flex items-center justify-between gap-3">
        <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">{label}</span>
        {limit ? (
          <span className={count > limit ? 'text-xs text-red-600' : 'text-xs text-[var(--text-soft)]'}>
            {count} / {limit} bytes
          </span>
        ) : (
          <span className="text-xs text-[var(--text-soft)]">{count} bytes</span>
        )}
      </span>
      <textarea
        className="min-h-0 w-full resize-y rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-2 text-sm text-[var(--text-main)] outline-none placeholder:text-[var(--text-soft)] placeholder:opacity-55 focus:border-[var(--accent)]"
        disabled={disabled}
        placeholder={placeholder}
        rows={rows}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      <FieldErrors errors={errors} />
    </label>
  );
}

function ListTextAreaField({
  disabled,
  errors,
  label,
  limit,
  value,
  onChange,
}: {
  disabled: boolean;
  errors: SourcePreferenceProfileFieldError[];
  label: string;
  limit?: number;
  value: string[];
  onChange: (value: string[]) => void;
}) {
  const joined = value.join('\n');
  return (
    <TextAreaField
      disabled={disabled}
      errors={errors}
      label={limit ? `${label} · 每行上限 ${limit} bytes` : label}
      rows={3}
      value={joined}
      onChange={(nextValue) => onChange(splitLines(nextValue))}
    />
  );
}

function FieldErrors({errors}: {errors: SourcePreferenceProfileFieldError[]}) {
  if (!errors.length) {
    return null;
  }
  return (
    <div className="grid gap-1 text-xs text-red-600">
      {errors.map((error) => (
        <div key={`${error.field}:${error.code}:${error.message}`}>{error.message}</div>
      ))}
    </div>
  );
}

function PreviewSummary({preview}: {preview: SourcePreviewResponseDto}) {
  return (
    <div className="rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <SourceIcon
            source={{
              name: preview.source.title ?? preview.source.canonical_url,
              icon_url: preview.source.icon_url,
              icon_source: preview.source.icon_source,
            }}
            size={28}
          />
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-[var(--text-main)]">
              {preview.source.title ?? '未提供标题'}
            </div>
            <div className="mt-1 break-all text-xs text-[var(--text-muted)]">
              canonical: {preview.source.canonical_url}
            </div>
            <div className="mt-1 break-all text-xs text-[var(--text-muted)]">
              读取地址：{preview.source.resolved_url}
            </div>
          </div>
        </div>
        {preview.source.site_url ? (
          <button
            className="shrink-0 rounded-md p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]"
            title="打开站点"
            type="button"
            onClick={() => window.open(preview.source.site_url ?? preview.source.resolved_url, '_blank', 'noopener,noreferrer')}
          >
            <ExternalLink size={16} />
          </button>
        ) : null}
      </div>
      {preview.source.description ? (
        <div className="mt-2 line-clamp-2 text-xs text-[var(--text-muted)]">{preview.source.description}</div>
      ) : null}
      <div className="mt-2 text-xs text-[var(--text-soft)]">
        {preview.source.detected_kind.toUpperCase()} · HTTP {preview.diagnostics.http_status} · {preview.entries.length}{' '}
        entries · {formatSourceIconSource(preview.source.icon_source)}
      </div>
    </div>
  );
}

function SourceIconSummary({source}: {source: SourceDto}) {
  return (
    <div className="rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-3">
      <div className="flex items-center gap-3">
        <SourceIcon source={source} size={28} />
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-[var(--text-main)]">{source.name}</div>
          <div className="mt-1 text-xs text-[var(--text-soft)]">{formatSourceIconSource(source.icon_source)}</div>
        </div>
      </div>
    </div>
  );
}
