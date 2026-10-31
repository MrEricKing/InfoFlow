import {useEffect, useState} from 'react';
import {CheckCircle2, Download, FileJson, KeyRound, RefreshCw, RotateCcw, Save, Sparkles, TriangleAlert, Undo2} from 'lucide-react';
import {
  getAISettings,
  testAIConnectionForProfile,
  updateAISettings,
} from '@/services/settings-service';
import type {
  AIProfile,
  AIProfileSettingsInput,
  AIProvider,
  AISettings,
  DeepSeekThinking,
  InsightEvidencePackageArtifactMode,
  InsightEvidencePackagePreview,
  InsightEvidencePackageRange,
  InsightProfileCandidateInspectResponse,
  InsightProfileDraftInspectResponse,
  InsightProfileFieldDiff,
  InsightProfileSettings,
  InsightProfileSourceType,
  InsightProfileVersion,
  InsightRequestPayload,
  InsightRequestPreview,
} from '@/types/settings';

interface AIFormState {
  provider: AIProvider;
  enabled: boolean;
  baseUrl: string;
  model: string;
  deepSeekThinking: DeepSeekThinking;
  artifactConcurrency: number;
  apiKey: string;
}

interface ProfileTestResult {
  status: 'pending' | 'success' | 'error';
  message: string;
}

const PROFILE_ORDER: AIProfile[] = ['general', 'sensitive'];
const PROFILE_META: Record<AIProfile, {title: string; description: string}> = {
  general: {
    title: '通用内容',
    description: '普通订阅源默认走这里，建议配置 DeepSeek 或其他低成本模型。',
  },
  sensitive: {
    title: '敏感内容',
    description: '敏感订阅源走这里，请选择你信任的模型服务。',
  },
};

const PROVIDER_DEFAULTS: Record<AIProvider, {label: string; baseUrl: string; model: string; artifactConcurrency: number}> = {
  deepseek: {
    label: 'DeepSeek',
    baseUrl: 'https://api.deepseek.com',
    model: 'deepseek-v4-flash',
    artifactConcurrency: 8,
  },
  'openai-compatible': {
    label: 'OpenAI-compatible',
    baseUrl: 'https://api.openai.com',
    model: 'gpt-4.1-mini',
    artifactConcurrency: 1,
  },
  'lm-studio': {
    label: 'LM Studio',
    baseUrl: 'http://localhost:1234',
    model: 'qwen/qwen3.5-9b',
    artifactConcurrency: 1,
  },
};

const DEFAULT_FORM_STATE: Record<AIProfile, AIFormState> = {
  general: {
    provider: 'deepseek',
    enabled: false,
    baseUrl: 'https://api.deepseek.com',
    model: 'deepseek-v4-flash',
    deepSeekThinking: 'disabled',
    artifactConcurrency: 8,
    apiKey: '',
  },
  sensitive: {
    provider: 'openai-compatible',
    enabled: false,
    baseUrl: 'https://api.openai.com',
    model: 'gpt-4.1-mini',
    deepSeekThinking: 'disabled',
    artifactConcurrency: 1,
    apiKey: '',
  },
};

const ARTIFACT_CONCURRENCY_OPTIONS = [1, 2, 4, 8, 16, 32] as const;

const PROFILE_SOURCE_OPTIONS: Array<{value: InsightProfileSourceType; label: string}> = [
  {value: 'manual_edit', label: '手工编辑'},
  {value: 'codex_import', label: 'Codex 导入'},
  {value: 'local_user', label: '本地用户'},
];

const EVIDENCE_RANGE_OPTIONS: Array<{value: InsightEvidencePackageRange; label: string}> = [
  {value: '90d', label: '最近 90 天'},
  {value: '180d', label: '最近 180 天'},
  {value: '365d', label: '最近 365 天'},
  {value: '30d', label: '最近 30 天'},
  {value: '7d', label: '最近 7 天'},
  {value: 'all', label: '全量'},
  {value: 'custom', label: '自定义'},
];

const EVIDENCE_ARTIFACT_MODE_OPTIONS: Array<{value: InsightEvidencePackageArtifactMode; label: string}> = [
  {value: 'latest', label: 'Latest artifacts'},
  {value: 'all_history', label: 'All history'},
];

export function AISettingsPanel() {
  const [settings, setSettings] = useState<AISettings | null>(null);
  const [formState, setFormState] = useState<Record<AIProfile, AIFormState>>(DEFAULT_FORM_STATE);
  const [isLoading, setIsLoading] = useState(true);
  const [savingProfile, setSavingProfile] = useState<AIProfile | null>(null);
  const [testingProfile, setTestingProfile] = useState<AIProfile | null>(null);
  const [testResults, setTestResults] = useState<Partial<Record<AIProfile, ProfileTestResult>>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void loadSettings();
  }, []);

  async function loadSettings() {
    setIsLoading(true);
    setError(null);
    try {
      const nextSettings = await getAISettings();
      applySettings(nextSettings);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : '加载 AI 设置失败。');
    } finally {
      setIsLoading(false);
    }
  }

  function applySettings(nextSettings: AISettings) {
    setSettings(nextSettings);
    setFormState({
      general: buildFormState(nextSettings, 'general'),
      sensitive: buildFormState(nextSettings, 'sensitive'),
    });
  }

  async function saveProfile(profile: AIProfile) {
    setSavingProfile(profile);
    setError(null);
    setMessage(null);
    try {
      const profilePayload = buildProfilePayload(formState[profile]);
      const nextSettings = await updateAISettings({profiles: {[profile]: profilePayload}});
      applySettings(nextSettings);
      setMessage(`${PROFILE_META[profile].title} AI 设置已保存。`);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : '保存 AI 设置失败。');
    } finally {
      setSavingProfile(null);
    }
  }

  async function clearApiKey(profile: AIProfile) {
    setSavingProfile(profile);
    setError(null);
    setMessage(null);
    try {
      const nextSettings = await updateAISettings({profiles: {[profile]: {apiKey: null}}});
      applySettings(nextSettings);
      setMessage(`${PROFILE_META[profile].title} API key 已清除。`);
    } catch (clearError) {
      setError(clearError instanceof Error ? clearError.message : '清除 API key 失败。');
    } finally {
      setSavingProfile(null);
    }
  }

  async function runConnectionTest(profile: AIProfile) {
    setTestingProfile(profile);
    setError(null);
    setMessage(null);
    setProfileTestResult(profile, {status: 'pending', message: '正在测试连接...'});
    try {
      const result = await testAIConnectionForProfile(profile, buildConnectionTestPayload(formState[profile]));
      if (result.ok) {
        setProfileTestResult(profile, {
          status: 'success',
          message: `连接成功：${result.model}${result.latencyMs !== null ? `，${result.latencyMs}ms` : ''}`,
        });
      } else {
        setProfileTestResult(profile, {status: 'error', message: `连接失败：${result.message}`});
      }
    } catch (testError) {
      setProfileTestResult(profile, {
        status: 'error',
        message: testError instanceof Error ? testError.message : '连接测试失败。',
      });
    } finally {
      setTestingProfile(null);
    }
  }

  function updateProfileForm<K extends keyof AIFormState>(profile: AIProfile, key: K, value: AIFormState[K]) {
    clearProfileTestResult(profile);
    setFormState((current) => ({
      ...current,
      [profile]: {
        ...current[profile],
        [key]: value,
      },
    }));
  }

  function changeProfileProvider(profile: AIProfile, provider: AIProvider) {
    clearProfileTestResult(profile);
    setFormState((current) => {
      const previous = current[profile];
      const defaults = PROVIDER_DEFAULTS[provider];
      return {
        ...current,
        [profile]: {
          ...previous,
          provider,
          baseUrl: shouldReplaceProviderDefault(previous.baseUrl) ? defaults.baseUrl : previous.baseUrl,
          model: shouldReplaceProviderDefault(previous.model) ? defaults.model : previous.model,
          artifactConcurrency: shouldReplaceProviderDefaultConcurrency(previous.artifactConcurrency)
            ? defaults.artifactConcurrency
            : previous.artifactConcurrency,
        },
      };
    });
  }

  function setProfileTestResult(profile: AIProfile, result: ProfileTestResult) {
    setTestResults((current) => ({...current, [profile]: result}));
  }

  function clearProfileTestResult(profile: AIProfile) {
    setTestResults((current) => {
      if (current[profile] === undefined) {
        return current;
      }
      const next = {...current};
      delete next[profile];
      return next;
    });
  }

  return (
    <section className="panel border-b border-[var(--border-subtle)] px-5 py-4">
      <div className="mx-auto w-full max-w-[1440px]">
        <div className="grid gap-4 xl:grid-cols-2">
          {PROFILE_ORDER.map((profile) => (
            <ProfileSettingsCard
              key={profile}
              disabled={isLoading}
              formState={formState[profile]}
              isSaving={savingProfile === profile}
              isTesting={testingProfile === profile}
              profile={profile}
              settings={settings}
              testResult={testResults[profile]}
              onClearKey={() => void clearApiKey(profile)}
              onProviderChange={changeProfileProvider}
              onSave={() => void saveProfile(profile)}
              onTest={() => void runConnectionTest(profile)}
              onUpdate={updateProfileForm}
            />
          ))}
        </div>
        {message ? <div className="mt-3 text-sm text-emerald-600">{message}</div> : null}
        {error ? <div className="mt-3 text-sm text-red-600">{error}</div> : null}
      </div>
    </section>
  );
}

function ProfileSettingsCard({
  disabled,
  formState,
  isSaving,
  isTesting,
  profile,
  settings,
  onClearKey,
  onProviderChange,
  onSave,
  onTest,
  onUpdate,
  testResult,
}: {
  disabled: boolean;
  formState: AIFormState;
  isSaving: boolean;
  isTesting: boolean;
  profile: AIProfile;
  settings: AISettings | null;
  testResult?: ProfileTestResult;
  onClearKey: () => void;
  onProviderChange: (profile: AIProfile, provider: AIProvider) => void;
  onSave: () => void;
  onTest: () => void;
  onUpdate: <K extends keyof AIFormState>(profile: AIProfile, key: K, value: AIFormState[K]) => void;
}) {
  const meta = PROFILE_META[profile];
  const persisted = settings?.profiles[profile] ?? null;
  const apiKeyRequired = formState.provider === 'deepseek';
  const apiKeyStatusLabel = persisted?.apiKeyConfigured
    ? (persisted.apiKeyMasked ?? 'configured')
    : apiKeyRequired
      ? 'not configured'
      : 'optional';
  const apiKeyStatusIsWarning = apiKeyRequired && !persisted?.apiKeyConfigured;

  return (
    <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-4 py-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-[var(--text-main)]">{meta.title}</div>
          <p className="mt-1 text-sm text-[var(--text-muted)]">{meta.description}</p>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-[var(--text-soft)]">
            <span>{formState.enabled ? '已启用' : '未启用'}</span>
            <span>·</span>
            <span>
              {persisted?.apiKeyConfigured
                ? `Key ${persisted.apiKeyMasked ?? 'configured'}`
                : apiKeyRequired
                  ? 'Key 未配置'
                  : 'Key 可选'}
            </span>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap justify-end gap-2">
          <button
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
            disabled={disabled || isTesting}
            onClick={onTest}
            type="button"
          >
            <RefreshCw size={15} className={isTesting ? 'animate-spin' : ''} />
            {isTesting ? '测试中...' : '测试'}
          </button>
          <button
            className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
            disabled={disabled || isSaving}
            onClick={onSave}
            type="button"
          >
            <Save size={15} />
            {isSaving ? '保存中...' : '保存'}
          </button>
        </div>
      </div>

      <div className="mt-4 grid gap-3">
        <label className="grid gap-2 text-sm">
          <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">Provider</span>
          <select
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)]"
            disabled={disabled}
            value={formState.provider}
            onChange={(event) => onProviderChange(profile, event.target.value as AIProvider)}
          >
            <option value="deepseek">DeepSeek</option>
            <option value="openai-compatible">OpenAI-compatible</option>
            <option value="lm-studio">LM Studio</option>
          </select>
        </label>

        <label className="flex items-center gap-2 text-sm text-[var(--text-muted)]">
          <input
            checked={formState.enabled}
            disabled={disabled}
            type="checkbox"
            onChange={(event) => onUpdate(profile, 'enabled', event.target.checked)}
          />
          <span>允许这个 profile 生成洞察结果</span>
        </label>

        <SettingInput
          disabled={disabled}
          label="Base URL"
          placeholder={PROVIDER_DEFAULTS[formState.provider].baseUrl}
          value={formState.baseUrl}
          onChange={(value) => onUpdate(profile, 'baseUrl', value)}
        />
        <SettingInput
          disabled={disabled}
          label="Model"
          placeholder={PROVIDER_DEFAULTS[formState.provider].model}
          value={formState.model}
          onChange={(value) => onUpdate(profile, 'model', value)}
        />

        <label className="grid gap-2 text-sm">
          <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">Artifact concurrency</span>
          <select
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)]"
            disabled={disabled}
            value={formState.artifactConcurrency}
            onChange={(event) => onUpdate(profile, 'artifactConcurrency', Number(event.target.value))}
          >
            {ARTIFACT_CONCURRENCY_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>

        {formState.provider === 'deepseek' ? (
          <label className="grid gap-2 text-sm">
            <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">DeepSeek Thinking</span>
            <select
              className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)]"
              disabled={disabled}
              value={formState.deepSeekThinking}
              onChange={(event) => onUpdate(profile, 'deepSeekThinking', event.target.value as DeepSeekThinking)}
            >
              <option value="disabled">关闭</option>
              <option value="enabled">开启</option>
            </select>
          </label>
        ) : null}

        <label className="grid gap-2 text-sm">
          <div className="flex items-center justify-between gap-3">
            <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">API Key</span>
            <span className={`inline-flex items-center gap-1 text-xs ${apiKeyStatusIsWarning ? 'text-amber-600' : 'text-[var(--text-muted)]'}`}>
              {apiKeyStatusIsWarning ? (
                <TriangleAlert size={13} />
              ) : (
                <CheckCircle2 size={13} className="text-emerald-600" />
              )}
              {apiKeyStatusLabel}
            </span>
          </div>
          <div className="flex gap-2">
            <div className="relative min-w-0 flex-1">
              <KeyRound className="pointer-events-none absolute left-3 top-2.5 text-[var(--text-soft)]" size={15} />
              <input
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-9 py-2 text-sm text-[var(--text-main)]"
                disabled={disabled}
                placeholder={
                  persisted?.apiKeyConfigured
                    ? '留空则保留现有 key'
                    : apiKeyRequired
                      ? '粘贴 API key'
                      : '可留空，本地网关通常不需要'
                }
                type="password"
                value={formState.apiKey}
                onChange={(event) => onUpdate(profile, 'apiKey', event.target.value)}
              />
            </div>
            <button
              className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
              disabled={disabled || isSaving || !persisted?.apiKeyConfigured}
              onClick={onClearKey}
              type="button"
            >
              <RotateCcw size={14} />
              清除
            </button>
          </div>
        </label>

        {testResult ? <ProfileTestResultAlert result={testResult} /> : null}
      </div>
    </div>
  );
}

function ProfileTestResultAlert({result}: {result: ProfileTestResult}) {
  const isSuccess = result.status === 'success';
  const isPending = result.status === 'pending';
  const className = isPending
    ? 'border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] text-[var(--text-muted)]'
    : isSuccess
      ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
      : 'border-red-200 bg-red-50 text-red-700';
  const icon = isPending ? (
    <RefreshCw size={14} className="mt-0.5 shrink-0 animate-spin" />
  ) : isSuccess ? (
    <CheckCircle2 size={14} className="mt-0.5 shrink-0" />
  ) : (
    <TriangleAlert size={14} className="mt-0.5 shrink-0" />
  );

  return (
    <div className={`flex items-start gap-2 rounded-md border px-3 py-2 text-sm ${className}`}>
      {icon}
      <span>{result.message}</span>
    </div>
  );
}

export function InsightProfileManager({
  candidateError,
  candidateInspection,
  candidateMessage,
  candidateSelectedSourceIds,
  draftInspection,
  editorText,
  editNote,
  isLoading,
  isCandidateLoading,
  isSaving,
  message,
  profileError,
  settings,
  sourceType,
  onActivate,
  onApplyCandidateSourceDrafts,
  onCandidateSourceToggle,
  onEditNoteChange,
  onEditorTextChange,
  onImportDraft,
  onImportProfileCandidate,
  onLoadTemplate,
  onRefresh,
  onReset,
  onRollback,
  onSave,
  onSourceTypeChange,
  onValidate,
}: {
  candidateError?: string | null;
  candidateInspection?: InsightProfileCandidateInspectResponse | null;
  candidateMessage?: string | null;
  candidateSelectedSourceIds?: number[];
  draftInspection: InsightProfileDraftInspectResponse | null;
  editorText: string;
  editNote: string;
  isLoading: boolean;
  isCandidateLoading?: boolean;
  isSaving: boolean;
  message: string | null;
  profileError: string | null;
  settings: InsightProfileSettings | null;
  sourceType: InsightProfileSourceType;
  onActivate: (profileId: number) => void;
  onApplyCandidateSourceDrafts?: () => void;
  onCandidateSourceToggle?: (sourceId: number) => void;
  onEditNoteChange: (value: string) => void;
  onEditorTextChange: (value: string) => void;
  onImportDraft: (file: File | null) => void;
  onImportProfileCandidate?: (file: File | null) => void;
  onLoadTemplate: () => void;
  onRefresh: () => void;
  onReset: () => void;
  onRollback: (profileId: number) => void;
  onSave: (activate: boolean) => void;
  onSourceTypeChange: (value: InsightProfileSourceType) => void;
  onValidate: () => void;
}) {
  const activeProfile = settings?.activeProfile ?? null;
  const activeProfileId = activeProfile?.id ?? null;
  return (
    <div className="mt-4 border-t border-[var(--border-subtle)] pt-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-[var(--text-main)]">洞察画像</div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-[var(--text-soft)]">
            <span>{activeProfile?.profileName ?? 'Default Insight Profile'}</span>
            <span>·</span>
            <span>{activeProfile?.profileVersion ?? 'default'}</span>
            <span>·</span>
            <span>{activeProfileId === null ? '内置默认' : `版本 #${activeProfileId}`}</span>
          </div>
        </div>
        <button
          className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
          disabled={isLoading}
          onClick={onRefresh}
          type="button"
        >
          <RefreshCw size={15} className={isLoading ? 'animate-spin' : ''} />
          {isLoading ? '刷新中...' : '刷新'}
        </button>
      </div>

      <div className="mt-2 grid items-start gap-3 xl:grid-cols-[minmax(0,1.15fr)_minmax(320px,0.85fr)]">
        <div className="grid self-start content-start gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <label
              className={`inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-[var(--border-subtle)] px-3 text-sm text-[var(--text-main)] ${
                isLoading || isSaving ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'
              }`}
            >
              <FileJson size={15} />
              导入草稿
              <input
                className="hidden"
                disabled={isLoading || isSaving}
                type="file"
                accept="application/json,.json"
                onChange={(event) => {
                  const file = event.currentTarget.files?.[0] ?? null;
                  onImportDraft(file);
                  event.currentTarget.value = '';
                }}
              />
            </label>
            {onImportProfileCandidate ? (
              <label
                className={`inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-[var(--border-subtle)] px-3 text-sm text-[var(--text-main)] ${
                  isLoading || isSaving || isCandidateLoading ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'
                }`}
              >
                <Sparkles size={15} />
                导入 Candidate
                <input
                  className="hidden"
                  disabled={isLoading || isSaving || isCandidateLoading}
                  type="file"
                  accept="application/json,.json"
                  onChange={(event) => {
                    const file = event.currentTarget.files?.[0] ?? null;
                    onImportProfileCandidate(file);
                    event.currentTarget.value = '';
                  }}
                />
              </label>
            ) : null}
            <button
              className="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-[var(--border-subtle)] px-3 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
              disabled={isLoading || isSaving || !settings}
              onClick={onLoadTemplate}
              type="button"
            >
              <FileJson size={15} />
              模板
            </button>
            <button
              className="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-[var(--border-subtle)] px-3 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
              disabled={isLoading || isSaving || !settings}
              onClick={onReset}
              type="button"
            >
              <RotateCcw size={15} />
              当前
            </button>
            <button
              className="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-[var(--border-subtle)] px-3 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
              disabled={isLoading || isSaving || !editorText.trim()}
              onClick={onValidate}
              type="button"
            >
              <CheckCircle2 size={15} />
              校验/审阅
            </button>
            <button
              className="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-[var(--border-subtle)] px-3 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
              disabled={isLoading || isSaving || !editorText.trim()}
              onClick={() => onSave(false)}
              type="button"
            >
              <Save size={15} />
              保存
            </button>
            <button
              className="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg bg-[var(--accent)] px-3 text-sm font-medium text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
              disabled={isLoading || isSaving || !editorText.trim()}
              onClick={() => onSave(true)}
              type="button"
            >
              <Save size={15} />
              保存并启用
            </button>
          </div>

          <textarea
            className="h-[420px] w-full resize-none overflow-auto rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3 font-mono text-xs leading-relaxed text-[var(--text-main)]"
            disabled={isLoading || isSaving}
            spellCheck={false}
            value={editorText}
            onChange={(event) => onEditorTextChange(event.target.value)}
          />

          <div className="grid gap-3 md:grid-cols-[180px_1fr]">
            <label className="grid gap-2 text-sm">
              <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">来源</span>
              <select
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)]"
                disabled={isLoading || isSaving}
                value={sourceType}
                onChange={(event) => onSourceTypeChange(event.target.value as InsightProfileSourceType)}
              >
                {PROFILE_SOURCE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <SettingInput
              disabled={isLoading || isSaving}
              label="Edit Note"
              placeholder="Profile change note"
              value={editNote}
              onChange={onEditNoteChange}
            />
          </div>

          {draftInspection ? <ProfileDraftReview inspection={draftInspection} /> : null}
          {candidateInspection ? (
            <ProfileCandidateReview
              inspection={candidateInspection}
              selectedSourceIds={candidateSelectedSourceIds ?? []}
              isLoading={Boolean(isCandidateLoading)}
              onApplySourceDrafts={onApplyCandidateSourceDrafts}
              onSourceToggle={onCandidateSourceToggle}
            />
          ) : null}
          {candidateMessage ? <div className="text-sm text-emerald-600">{candidateMessage}</div> : null}
          {candidateError ? <div className="text-sm text-red-600">{candidateError}</div> : null}
          {message ? <div className="text-sm text-emerald-600">{message}</div> : null}
          {profileError ? <div className="text-sm text-red-600">{profileError}</div> : null}
        </div>

        <div className="grid content-start gap-2">
          <InfoBlock
            title="active profile"
            rows={[
              ['id', activeProfileId === null ? 'default' : String(activeProfileId)],
              ['schema', activeProfile?.schemaVersion ?? 'insight_profile_v1'],
              ['version', activeProfile?.profileVersion ?? 'default'],
              ['source', activeProfile?.sourceType ?? 'default_fallback'],
              ['created_at', activeProfile?.createdAt ?? ''],
            ]}
          />
          <ProfileHistory
            activeProfileId={activeProfileId}
            disabled={isLoading || isSaving}
            versions={settings?.versions ?? []}
            onActivate={onActivate}
            onRollback={onRollback}
          />
          <JsonBlock
            collapsible
            defaultOpen={false}
            maxHeightClassName="max-h-56"
            title="active prompt context"
            value={settings?.activePromptContext ?? {status: 'loading'}}
          />
          <ProfileEvents events={settings?.events ?? []} />
        </div>
      </div>
    </div>
  );
}

const PROFILE_DRAFT_CHANGE_LABELS: Record<InsightProfileFieldDiff['changeType'], string> = {
  added: '新增',
  removed: '移除',
  modified: '修改',
  unchanged: '未变',
};

function ProfileDraftReview({inspection}: {inspection: InsightProfileDraftInspectResponse}) {
  const changedDiffs = inspection.fieldDiffs.filter((diff) => diff.changeType !== 'unchanged');
  return (
    <div className="grid gap-3">
      <InfoBlock
        title="draft review"
        rows={[
          ['base', `${inspection.baseProfile.profileVersion} (${inspection.baseProfile.id ?? 'default'})`],
          ['diff', inspection.diffSummary],
          ['changed fields', String(changedDiffs.length)],
          ['evidence refs', String(inspection.evidenceReferences.length)],
        ]}
      />

      <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
        <div className="text-xs font-semibold uppercase text-[var(--text-soft)]">field diffs</div>
        <div className="mt-2 grid gap-2">
          {changedDiffs.length === 0 ? (
            <div className="text-sm text-[var(--text-muted)]">归一化后的 profile 字段没有变化。</div>
          ) : null}
          {changedDiffs.map((diff) => (
            <details key={diff.field} className="rounded-md border border-[var(--border-subtle)] p-2.5">
              <summary className="flex min-h-8 cursor-pointer select-none items-center justify-between gap-3 text-sm">
                <span className="font-medium text-[var(--text-main)]">{diff.field}</span>
                <span className="rounded-md bg-[var(--bg-panel)] px-2 py-1 text-xs text-[var(--text-soft)]">
                  {PROFILE_DRAFT_CHANGE_LABELS[diff.changeType]}
                </span>
              </summary>
              <div className="mt-2 grid gap-2 lg:grid-cols-2">
                <JsonBlock collapsible defaultOpen maxHeightClassName="max-h-56" title="before" value={diff.before} />
                <JsonBlock collapsible defaultOpen maxHeightClassName="max-h-56" title="after" value={diff.after} />
              </div>
            </details>
          ))}
        </div>
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        <JsonBlock
          collapsible
          defaultOpen={false}
          maxHeightClassName="max-h-56"
          title="candidate prompt context"
          value={inspection.promptContext}
        />
        <JsonBlock
          collapsible
          defaultOpen={false}
          maxHeightClassName="max-h-56"
          title="evidence references"
          value={inspection.evidenceReferences}
        />
      </div>
    </div>
  );
}

function ProfileCandidateReview({
  inspection,
  isLoading,
  selectedSourceIds,
  onApplySourceDrafts,
  onSourceToggle,
}: {
  inspection: InsightProfileCandidateInspectResponse;
  isLoading: boolean;
  selectedSourceIds: number[];
  onApplySourceDrafts?: () => void;
  onSourceToggle?: (sourceId: number) => void;
}) {
  return (
    <div className="grid gap-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-xs font-semibold uppercase text-[var(--text-soft)]">profile candidate review</div>
          <p className="mt-1 text-sm text-[var(--text-muted)]">
            外部建议只会保存为草稿；Channel prompt/boundary 只展示，不会自动修改。
          </p>
        </div>
        <button
          className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
          disabled={isLoading || selectedSourceIds.length === 0 || !onApplySourceDrafts}
          type="button"
          onClick={onApplySourceDrafts}
        >
          <Save size={15} />
          保存选中 Source 草稿
        </button>
      </div>
      <InfoBlock
        title="candidate"
        rows={[
          ['version', inspection.candidateVersion],
          ['summary', inspection.summary ?? ''],
          ['global profile', inspection.globalProfileInspection ? '已载入编辑器' : '无'],
          ['source drafts', String(inspection.sourceProfileChanges.length)],
          ['channel suggestions', String(inspection.channelPromptBoundarySuggestions.length)],
          ['warnings', String(inspection.warnings.length)],
        ]}
      />
      {inspection.warnings.length ? (
        <div className="rounded-md border border-amber-300/60 bg-amber-50/70 p-3 text-sm text-amber-950 dark:border-amber-400/30 dark:bg-amber-500/10 dark:text-amber-100">
          <div className="text-xs font-semibold uppercase">validation warnings</div>
          <ul className="mt-2 list-disc space-y-1 pl-5">
            {inspection.warnings.map((warning, index) => (
              <li key={`${warning}-${index}`}>{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {inspection.sourceProfileChanges.length ? (
        <div className="grid gap-2">
          {inspection.sourceProfileChanges.map((change) => (
            <CandidateSourceProfileChangeReview
              key={change.sourceId}
              change={change}
              selected={selectedSourceIds.includes(change.sourceId)}
              onToggle={() => onSourceToggle?.(change.sourceId)}
            />
          ))}
        </div>
      ) : null}
      {inspection.channelPromptBoundarySuggestions.length ? (
        <div className="rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-3">
          <div className="text-xs font-semibold uppercase text-[var(--text-soft)]">channel suggestions</div>
          <div className="mt-2 grid gap-2 text-sm">
            {inspection.channelPromptBoundarySuggestions.map((suggestion) => (
              <div key={suggestion.channelId} className="rounded-md border border-[var(--border-subtle)] p-2">
                <div className="font-medium text-[var(--text-main)]">{suggestion.channelLabel}</div>
                <div className="mt-1 text-xs text-[var(--text-soft)]">current focus: {suggestion.currentFocusPrompt}</div>
                {suggestion.suggestedFocusPrompt ? (
                  <div className="mt-1 text-xs text-[var(--text-muted)]">suggested focus: {suggestion.suggestedFocusPrompt}</div>
                ) : null}
                <div className="mt-1 text-xs text-[var(--text-soft)]">current boundary: {suggestion.currentBoundaryPrompt}</div>
                {suggestion.suggestedBoundaryPrompt ? (
                  <div className="mt-1 text-xs text-[var(--text-muted)]">suggested boundary: {suggestion.suggestedBoundaryPrompt}</div>
                ) : null}
                {suggestion.rationale ? (
                  <div className="mt-1 text-xs text-[var(--text-muted)]">rationale: {suggestion.rationale}</div>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function CandidateSourceProfileChangeReview({
  change,
  selected,
  onToggle,
}: {
  change: InsightProfileCandidateInspectResponse['sourceProfileChanges'][number];
  selected: boolean;
  onToggle: () => void;
}) {
  const changedDiffs = change.fieldDiffs.filter((diff) => diff.changeType !== 'unchanged');
  return (
    <div className="grid gap-3 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-3 text-sm">
      <label className="flex items-start gap-2">
        <input className="mt-1" type="checkbox" checked={selected} onChange={onToggle} />
        <span className="min-w-0">
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-[var(--text-main)]">{change.sourceName}</span>
            <span className="text-xs text-[var(--text-soft)]">#{change.sourceId}</span>
            <span className="rounded-md bg-[var(--bg-panel-strong)] px-2 py-0.5 text-xs text-[var(--text-soft)]">
              {changedDiffs.length} changed fields
            </span>
          </span>
          <span className="mt-1 block text-xs text-[var(--text-muted)]">{change.diffSummary}</span>
        </span>
      </label>

      <details open={selected} className="rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
        <summary className="flex min-h-8 cursor-pointer select-none items-center justify-between gap-3 text-xs font-semibold uppercase text-[var(--text-soft)]">
          <span>source profile diff / dry-run preview</span>
          <span>{selected ? 'selected' : 'not selected'}</span>
        </summary>

        <div className="mt-3 grid gap-3">
          <div className="grid gap-2">
            {changedDiffs.length === 0 ? (
              <div className="text-sm text-[var(--text-muted)]">归一化后的 Source Profile 字段没有变化。</div>
            ) : null}
            {changedDiffs.map((diff) => (
              <details key={diff.field} open className="rounded-md border border-[var(--border-subtle)] p-2.5">
                <summary className="flex min-h-8 cursor-pointer select-none items-center justify-between gap-3 text-sm">
                  <span className="font-medium text-[var(--text-main)]">{diff.field}</span>
                  <span className="rounded-md bg-[var(--bg-panel)] px-2 py-1 text-xs text-[var(--text-soft)]">
                    {PROFILE_DRAFT_CHANGE_LABELS[diff.changeType]}
                  </span>
                </summary>
                <div className="mt-2 grid gap-2 lg:grid-cols-2">
                  <JsonBlock collapsible defaultOpen maxHeightClassName="max-h-44" title="before" value={diff.before} />
                  <JsonBlock collapsible defaultOpen maxHeightClassName="max-h-44" title="after" value={diff.after} />
                </div>
              </details>
            ))}
          </div>

          <div className="grid gap-3 xl:grid-cols-3">
            <JsonBlock
              collapsible
              defaultOpen={false}
              maxHeightClassName="max-h-56"
              title="normalized source profile"
              value={change.normalizedProfile}
            />
            <JsonBlock
              collapsible
              defaultOpen={false}
              maxHeightClassName="max-h-56"
              title="candidate prompt context"
              value={change.promptContext}
            />
            <JsonBlock
              collapsible
              defaultOpen={false}
              maxHeightClassName="max-h-56"
              title="evidence references"
              value={change.evidenceReferences}
            />
          </div>
        </div>
      </details>
    </div>
  );
}

function ProfileHistory({
  activeProfileId,
  disabled,
  versions,
  onActivate,
  onRollback,
}: {
  activeProfileId: number | null;
  disabled: boolean;
  versions: InsightProfileVersion[];
  onActivate: (profileId: number) => void;
  onRollback: (profileId: number) => void;
}) {
  return (
    <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
      <div className="text-xs font-semibold uppercase text-[var(--text-soft)]">versions</div>
      <div className="mt-2 grid max-h-[260px] gap-2 overflow-auto pr-1">
        {versions.length === 0 ? (
          <div className="text-sm text-[var(--text-muted)]">No saved versions.</div>
        ) : null}
        {versions.map((version) => {
          const isActiveVersion = version.id !== null && version.id === activeProfileId;
          const shouldRollback = version.id !== null && activeProfileId !== null && version.id < activeProfileId;
          const ActionIcon = shouldRollback ? Undo2 : CheckCircle2;
          const actionLabel = shouldRollback ? '回滚' : '启用';

          return (
            <div key={version.id ?? version.profileVersion} className="rounded-md border border-[var(--border-subtle)] p-2.5">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium text-[var(--text-main)]">
                    #{version.id} · {version.profileVersion}
                  </div>
                  <div className="mt-1 truncate text-xs text-[var(--text-soft)]">{version.sourceType} · {version.createdAt ?? '-'}</div>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {isActiveVersion ? (
                    <span className="rounded-md bg-emerald-500/10 px-2 py-1 text-xs text-emerald-700 dark:text-emerald-300">
                      active
                    </span>
                  ) : null}
                  {version.id !== null && !isActiveVersion ? (
                    <button
                      className="inline-flex h-8 items-center gap-1.5 whitespace-nowrap rounded-md border border-[var(--border-subtle)] px-2.5 text-xs text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
                      disabled={disabled}
                      onClick={() => {
                        if (version.id === null) {
                          return;
                        }
                        if (shouldRollback) {
                          onRollback(version.id);
                          return;
                        }
                        onActivate(version.id);
                      }}
                      type="button"
                    >
                      <ActionIcon size={13} />
                      {actionLabel}
                    </button>
                  ) : null}
                </div>
              </div>
              {version.diffSummary ? <div className="mt-2 clamp-2 text-xs text-[var(--text-muted)]">{version.diffSummary}</div> : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ProfileEvents({events}: {events: InsightProfileSettings['events']}) {
  return (
    <details className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
      <summary className="flex min-h-8 cursor-pointer select-none items-center text-xs font-semibold uppercase text-[var(--text-soft)]">
        events
      </summary>
      <div className="mt-2 grid max-h-[180px] gap-2 overflow-auto pr-1 text-sm">
        {events.length === 0 ? <div className="text-[var(--text-muted)]">No events.</div> : null}
        {events.slice(0, 8).map((event) => (
          <div key={event.id ?? `${event.eventType}-${event.createdAt}`} className="grid gap-1 rounded-md border border-[var(--border-subtle)] p-2">
            <div className="flex items-center gap-2">
              <span className="shrink-0 font-medium text-[var(--text-main)]">{event.eventType}</span>
              <span className="truncate text-xs text-[var(--text-soft)]">profile #{event.profileId ?? '-'}</span>
            </div>
            {event.note ? <div className="clamp-2 text-xs text-[var(--text-muted)]">{event.note}</div> : null}
            <div className="truncate text-xs text-[var(--text-soft)]">{event.createdAt ?? '-'}</div>
          </div>
        ))}
      </div>
    </details>
  );
}

export function InsightEvidencePackagePanel({
  artifactMode,
  endDate,
  error,
  isExporting,
  isLoading,
  message,
  preview,
  range,
  startDate,
  onArtifactModeChange,
  onDownload,
  onEndDateChange,
  onPreview,
  onRangeChange,
  onStartDateChange,
}: {
  artifactMode: InsightEvidencePackageArtifactMode;
  endDate: string;
  error: string | null;
  isExporting: boolean;
  isLoading: boolean;
  message: string | null;
  preview: InsightEvidencePackagePreview | null;
  range: InsightEvidencePackageRange;
  startDate: string;
  onArtifactModeChange: (value: InsightEvidencePackageArtifactMode) => void;
  onDownload: () => void;
  onEndDateChange: (value: string) => void;
  onPreview: () => void;
  onRangeChange: (value: InsightEvidencePackageRange) => void;
  onStartDateChange: (value: string) => void;
}) {
  const customRangeMissing = range === 'custom' && (!startDate || !endDate);
  return (
    <div className="mt-5 border-t border-[var(--border-subtle)] pt-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-[var(--text-main)]">洞察证据包</div>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-[var(--text-soft)]">
            <span>{preview ? `${preview.itemCount} 篇` : '未预览'}</span>
            <span>·</span>
            <span>{preview ? `${preview.feedbackItemCount} 条反馈` : '0 条反馈'}</span>
            <span>·</span>
            <span>{preview ? `${preview.structuredFeedbackItemCount} 条结构化` : '0 条结构化'}</span>
            <span>·</span>
            <span>{preview ? `${preview.behaviorEventCount} 条行为事件` : '0 条行为事件'}</span>
            <span>·</span>
            <span>{preview ? `${preview.excludedUnreadCount} 篇未读已排除` : '未读默认排除'}</span>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap justify-end gap-2">
          <button
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isLoading || customRangeMissing}
            onClick={onPreview}
            type="button"
          >
            <RefreshCw size={15} className={isLoading ? 'animate-spin' : ''} />
            {isLoading ? '预览中...' : '预览'}
          </button>
          <button
            className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isLoading || isExporting || customRangeMissing}
            onClick={onDownload}
            type="button"
          >
            <Download size={15} />
            {isExporting ? '导出中...' : '下载 ZIP'}
          </button>
        </div>
      </div>

      <div className="mt-3 rounded-lg border border-amber-300/70 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200">
        证据包用于离线交给外部 Codex 分析。导出前会排除 API keys、cookies、auth headers、数据库连接串和本机部署绝对路径，但包内仍会包含文章内容、反馈、保存内容和 Source/Profile 信号。
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-[220px_220px_1fr]">
        <label className="grid gap-2 text-sm">
          <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">范围</span>
          <select
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)]"
            disabled={isLoading || isExporting}
            value={range}
            onChange={(event) => onRangeChange(event.target.value as InsightEvidencePackageRange)}
          >
            {EVIDENCE_RANGE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="grid gap-2 text-sm">
          <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">artifact mode</span>
          <select
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)]"
            disabled={isLoading || isExporting}
            value={artifactMode}
            onChange={(event) => onArtifactModeChange(event.target.value as InsightEvidencePackageArtifactMode)}
          >
            {EVIDENCE_ARTIFACT_MODE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        {range === 'custom' ? (
          <div className="grid gap-3 md:grid-cols-2">
            <label className="grid gap-2 text-sm">
              <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">Start date</span>
              <input
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)]"
                disabled={isLoading || isExporting}
                type="date"
                value={startDate}
                onChange={(event) => onStartDateChange(event.target.value)}
              />
            </label>
            <label className="grid gap-2 text-sm">
              <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">End date</span>
              <input
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)]"
                disabled={isLoading || isExporting}
                type="date"
                value={endDate}
                onChange={(event) => onEndDateChange(event.target.value)}
              />
            </label>
          </div>
        ) : (
          <InfoBlock
            title="当前范围"
            rows={[
              ['start_at', preview?.startAt ?? 'all'],
              ['end_at', preview?.endAt ?? 'all'],
              ['generated_at', preview?.generatedAt ?? ''],
            ]}
          />
        )}
      </div>

      {range === 'custom' && preview ? (
        <div className="mt-3">
          <InfoBlock
            title="当前范围"
            rows={[
              ['start_at', preview.startAt ?? 'all'],
              ['end_at', preview.endAt ?? 'all'],
              ['generated_at', preview.generatedAt],
            ]}
          />
        </div>
      ) : null}

      {preview ? (
        <div className="mt-3 grid gap-3 xl:grid-cols-3">
          <InfoBlock
            title="导出统计"
            rows={[
              ['package', preview.packageVersion],
              ['range', preview.range],
              ['artifact_mode', preview.artifactMode],
              ['items', String(preview.itemCount)],
              ['content_text files', String(preview.contentTextFileCount)],
              ['human feedback', String(preview.feedbackItemCount)],
              ['structured feedback', String(preview.structuredFeedbackItemCount)],
              ['behavior events', String(preview.behaviorEventCount)],
              ['feedback clear events', String(preview.feedbackClearEventCount)],
              ['not interested undo events', String(preview.notInterestedClearEventCount)],
              ['auto low-priority clear events', String(preview.autoLowPriorityClearEventCount)],
              ['saved items', String(preview.savedItemCount)],
              ['saved item text files', String(preview.savedItemContentTextFileCount)],
              ['saved item display.md files', String(preview.savedItemDisplayMarkdownFileCount)],
              ['saved item display assets', String(preview.savedItemDisplayAssetCount)],
              ['saved item AI insights', String(preview.savedItemAiInsightCount)],
              ['curated saved item insights', String(preview.savedItemCuratedInsightCount)],
              ['curated edit events', String(preview.savedItemCuratedEventCount)],
              ['external saved signals', String(preview.externalSavedItemCount)],
              ['failed attempts', String(preview.failedAttemptCount)],
              ['skipped attempts', String(preview.skippedAttemptCount)],
              ['unread excluded', String(preview.excludedUnreadCount)],
            ]}
          />
          <InfoBlock
            title="artifact coverage"
            rows={Object.entries(preview.artifactCoverage).map(
              ([artifactKind, count]) => [artifactKind, String(count)] as [string, string],
            )}
          />
          <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3 text-sm text-[var(--text-muted)]">
            <div className="text-xs font-semibold uppercase text-[var(--text-soft)]">zip contents</div>
            <div className="mt-2 grid gap-1">
              <span>manifest / README / v2 schema / candidate template</span>
              <span>profiles / sources / channels / evidence reports</span>
              <span>feed item JSON + content.txt files</span>
              <span>saved item JSON + content.txt + display.md</span>
              <span>display.md 引用的图片 assets</span>
              <span>latest artifacts + score/routing breakdowns</span>
              <span>failed/skipped attempts summary</span>
              {preview.artifactMode === 'all_history' ? <span>selected-range Insight artifact history</span> : null}
            </div>
          </div>
        </div>
      ) : null}

      {message ? <div className="mt-3 text-sm text-emerald-600">{message}</div> : null}
      {error ? <div className="mt-3 text-sm text-red-600">{error}</div> : null}
    </div>
  );
}

export function InsightRequestInspector({
  isLoading,
  preview,
  previewError,
  selectedItemId,
  onRefresh,
  onSelectSample,
}: {
  isLoading: boolean;
  preview: InsightRequestPreview | null;
  previewError: string | null;
  selectedItemId: number | null;
  onRefresh: () => void;
  onSelectSample: (itemId: number) => void;
}) {
  const payload = preview?.payload ?? null;
  const sample = preview?.sample ?? null;

  return (
    <div className="mt-5 border-t border-[var(--border-subtle)] pt-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-[var(--text-main)]">洞察请求检查器</div>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-[var(--text-soft)]">
            {preview ? (
              <span
                className={`rounded-md px-2 py-1 ${
                  preview.canSend
                    ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
                    : 'bg-amber-500/10 text-amber-700 dark:text-amber-300'
                }`}
              >
                {preview.canSend ? '可发送' : '仅预览'}
              </span>
            ) : null}
            {payload ? <span>{payload.provider} / {payload.profile} / {payload.model}</span> : null}
          </div>
        </div>
        <button
          className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
          disabled={isLoading}
          onClick={onRefresh}
          type="button"
        >
          <RefreshCw size={15} className={isLoading ? 'animate-spin' : ''} />
          {isLoading ? '刷新中...' : '刷新'}
        </button>
      </div>

      {previewError ? <div className="mt-3 text-sm text-red-600">{previewError}</div> : null}
      {!previewError && preview ? <p className="mt-3 text-sm text-[var(--text-muted)]">{preview.message}</p> : null}
      {preview && preview.candidates.length > 0 ? (
        <label className="mt-3 grid gap-2 text-sm">
          <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">样本 item</span>
          <select
            className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)]"
            disabled={isLoading}
            value={selectedItemId ?? preview.sample?.itemId ?? ''}
            onChange={(event) => onSelectSample(Number(event.target.value))}
          >
            {preview.candidates.map((candidate) => (
              <option key={candidate.itemId} value={candidate.itemId}>
                #{candidate.itemId} · {candidate.title} · {candidate.sourceName}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      {!isLoading && !previewError && preview && !preview.available ? (
        <div className="mt-3 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-3 text-sm text-[var(--text-muted)]">
          暂无可预览样本。
        </div>
      ) : null}

      {sample && payload ? (
        <div className="mt-4 grid gap-4">
          <div className="grid gap-3 xl:grid-cols-3">
            <InfoBlock
              title="样本"
              rows={[
                ['item_id', String(sample.itemId)],
                ['title', sample.title],
                ['source', sample.sourceName],
                ['source_folder', sample.sourceFolder || ''],
                ['ai_route', sample.aiRoute],
                ['content_text_source', sample.contentTextSource],
                ['content_extraction_status', sample.contentExtractionStatus],
                ['missing_artifacts', sample.missingArtifactKinds.join(', ') || 'none'],
              ]}
            />
            <SampleStatusBlock payload={payload} />
            <InfoBlock
              title="模型参数"
              rows={[
                ['provider', payload.provider],
                ['profile', payload.profile],
                ['model', payload.model],
                ['endpoint', payload.endpoint],
                ['temperature', String(payload.temperature)],
                ['max_tokens', String(payload.maxTokens)],
                ['prompt_version', payload.promptVersion],
              ]}
            />
          </div>

          <CollapsibleTextBlock title="截断后的 content_text" value={payload.contentText || '(empty)'} />

          <div className="grid gap-3 xl:grid-cols-2">
            <JsonBlock title="active profile context" value={payload.activeInsightProfileContext} />
            <JsonBlock title="source preference profile" value={payload.sourcePreferenceProfile} />
            <JsonBlock title="taxonomy" value={payload.taxonomy} />
            <JsonBlock title="content type options" value={payload.contentTypeOptions} />
            <JsonBlock title="reason codes" value={payload.reasonCodes} />
          </div>

          <InfoBlock
            title="Source Profile"
            rows={[
              ['source_profile_version', payload.sourceProfileVersion ?? ''],
              ['source_profile_is_empty', payload.sourceProfileIsEmpty ? 'true' : 'false'],
              ['modify_in', 'Source Profile 在 Source settings 中修改。'],
            ]}
          />

          <div className="grid gap-3 xl:grid-cols-2">
            <TextBlock title="System message" value={payload.systemMessage} />
            <TextBlock title="User message" value={buildUserMessagePreview(payload.userMessage)} />
          </div>

          <JsonBlock title="request_body" value={payload.requestBody} />

        </div>
      ) : null}
    </div>
  );
}

function InfoBlock({title, rows}: {title: string; rows: Array<[string, string]>}) {
  return (
    <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
      <div className="text-xs font-semibold uppercase text-[var(--text-soft)]">{title}</div>
      <dl className="mt-2 grid gap-2 text-sm">
        {rows.map(([label, value]) => (
          <div key={label} className="grid grid-cols-[150px_1fr] gap-3">
            <dt className="truncate text-[var(--text-soft)]">{label}</dt>
            <dd className="min-w-0 break-words text-[var(--text-main)]">{value || '-'}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function SampleStatusBlock({payload}: {payload: InsightRequestPayload}) {
  const rows: Array<{label: string; value: string; note: string}> = [
    {
      label: 'source chars',
      value: String(payload.stats.sourceContentCharCount),
      note: '截断前可用正文字符数',
    },
    {
      label: 'content chars',
      value: String(payload.stats.contentTextCharCount),
      note: '实际注入请求的正文字数',
    },
    {
      label: 'content tokens',
      value: String(payload.stats.contentTextEstimatedTokenCount),
      note: '粗略 token 估算',
    },
    {
      label: 'truncated',
      value: payload.stats.contentTextWasTruncated ? 'yes' : 'no',
      note: '是否已按上限截断',
    },
  ];

  return (
    <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
      <div className="text-xs font-semibold uppercase text-[var(--text-soft)]">样本情况</div>
      <div className="mt-2 grid gap-2 text-sm">
        {rows.map((row) => (
          <div key={row.label} className="grid grid-cols-[110px_70px_1fr] gap-2">
            <div className="truncate text-[var(--text-soft)]">{row.label}</div>
            <div className="font-semibold text-[var(--text-main)]">{row.value}</div>
            <div className="min-w-0 break-words text-xs text-[var(--text-muted)]">{row.note}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function buildUserMessagePreview(userMessage: string) {
  const lines = userMessage.split('\n');
  const articleContextIndex = lines.findIndex((line) => line.trim() === 'Article context:');
  if (articleContextIndex < 0) {
    return userMessage;
  }
  return [
    ...lines.slice(0, articleContextIndex + 1),
    'Title: {{title}}',
    'Source name: {{source_name}}',
    'Source folder: {{source_folder}}',
    'Content: {{content_text}}',
  ].join('\n');
}

function CollapsibleTextBlock({title, value}: {title: string; value: string}) {
  return (
    <details className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
      <summary className="cursor-pointer select-none text-xs font-semibold uppercase text-[var(--text-soft)]">
        {title}
      </summary>
      <pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md bg-[var(--bg-panel)] p-3 font-mono text-xs leading-relaxed text-[var(--text-main)]">
        {value}
      </pre>
    </details>
  );
}

function TextBlock({
  title,
  value,
  collapsible = false,
  defaultOpen = true,
  maxHeightClassName = 'max-h-80',
}: {
  title: string;
  value: string;
  collapsible?: boolean;
  defaultOpen?: boolean;
  maxHeightClassName?: string;
}) {
  const content = (
    <pre className={`mt-2 overflow-auto whitespace-pre-wrap break-words rounded-md bg-[var(--bg-panel)] p-3 font-mono text-xs leading-relaxed text-[var(--text-main)] ${maxHeightClassName}`}>
      {value}
    </pre>
  );

  if (collapsible) {
    return (
      <details open={defaultOpen} className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
        <summary className="flex min-h-8 cursor-pointer select-none items-center text-xs font-semibold uppercase text-[var(--text-soft)]">
          {title}
        </summary>
        {content}
      </details>
    );
  }

  return (
    <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
      <div className="text-xs font-semibold uppercase text-[var(--text-soft)]">{title}</div>
      {content}
    </div>
  );
}

function JsonBlock({
  title,
  value,
  collapsible = false,
  defaultOpen = true,
  maxHeightClassName = 'max-h-80',
}: {
  title: string;
  value: unknown;
  collapsible?: boolean;
  defaultOpen?: boolean;
  maxHeightClassName?: string;
}) {
  const content = (
    <pre className={`mt-2 overflow-auto whitespace-pre-wrap break-words rounded-md bg-[var(--bg-panel)] p-3 font-mono text-xs leading-relaxed text-[var(--text-main)] ${maxHeightClassName}`}>
      {JSON.stringify(value, null, 2)}
    </pre>
  );

  if (collapsible) {
    return (
      <details open={defaultOpen} className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
        <summary className="flex min-h-8 cursor-pointer select-none items-center text-xs font-semibold uppercase text-[var(--text-soft)]">
          {title}
        </summary>
        {content}
      </details>
    );
  }

  return (
    <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-3">
      <div className="text-xs font-semibold uppercase text-[var(--text-soft)]">{title}</div>
      {content}
    </div>
  );
}

function SettingInput({
  label,
  value,
  disabled,
  placeholder,
  onChange,
}: {
  label: string;
  value: string;
  disabled: boolean;
  placeholder: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="grid gap-2 text-sm">
      <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">{label}</span>
      <input
        className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)]"
        disabled={disabled}
        placeholder={placeholder}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function buildFormState(settings: AISettings, profile: AIProfile): AIFormState {
  const profileSettings = settings.profiles[profile];
  return {
    provider: profileSettings.provider,
    enabled: profileSettings.enabled,
    baseUrl: profileSettings.baseUrl,
    model: profileSettings.model,
    deepSeekThinking: profileSettings.deepSeekThinking,
    artifactConcurrency: profileSettings.artifactConcurrency,
    apiKey: '',
  };
}

function buildProfilePayload(current: AIFormState): AIProfileSettingsInput {
  return {
    provider: current.provider,
    enabled: current.enabled,
    baseUrl: current.baseUrl,
    model: current.model,
    deepSeekThinking: current.deepSeekThinking,
    artifactConcurrency: current.artifactConcurrency,
    apiKey: current.apiKey.trim() ? current.apiKey : undefined,
  };
}

function buildConnectionTestPayload(current: AIFormState): AIProfileSettingsInput {
  return {
    ...buildProfilePayload(current),
    enabled: true,
  };
}

function shouldReplaceProviderDefault(value: string) {
  const trimmedValue = value.trim();
  return !trimmedValue || Object.values(PROVIDER_DEFAULTS).some((providerDefault) => {
    return trimmedValue === providerDefault.baseUrl || trimmedValue === providerDefault.model;
  });
}

function shouldReplaceProviderDefaultConcurrency(value: number) {
  return Object.values(PROVIDER_DEFAULTS).some((providerDefault) => value === providerDefault.artifactConcurrency);
}
