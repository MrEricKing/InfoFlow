import {useEffect, useState} from 'react';
import {RefreshCw, RotateCcw, Save} from 'lucide-react';
import {getRsshubSettings, updateRsshubSettings} from '@/services/settings-service';
import type {RsshubSettings} from '@/types/settings';

export function RsshubSettingsPanel() {
  const [settings, setSettings] = useState<RsshubSettings | null>(null);
  const [rsshubBaseUrl, setRsshubBaseUrl] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void loadSettings();
  }, []);

  async function loadSettings() {
    setIsLoading(true);
    setError(null);
    try {
      const nextSettings = await getRsshubSettings();
      setSettings(nextSettings);
      setRsshubBaseUrl(nextSettings.value);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : '加载 RSSHub 设置失败。');
    } finally {
      setIsLoading(false);
    }
  }

  async function saveSettings() {
    setIsSaving(true);
    setError(null);
    setMessage(null);
    try {
      const nextSettings = await updateRsshubSettings({rsshubBaseUrl});
      setSettings(nextSettings);
      setRsshubBaseUrl(nextSettings.value);
      setMessage('RSSHub Base URL 已保存。');
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : '保存 RSSHub 设置失败。');
    } finally {
      setIsSaving(false);
    }
  }

  async function clearOverride() {
    setIsSaving(true);
    setError(null);
    setMessage(null);
    try {
      const nextSettings = await updateRsshubSettings({rsshubBaseUrl: null});
      setSettings(nextSettings);
      setRsshubBaseUrl(nextSettings.value);
      setMessage('RSSHub 数据库覆盖值已清除。');
    } catch (clearError) {
      setError(clearError instanceof Error ? clearError.message : '清除 RSSHub 设置失败。');
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <section className="panel border-b border-[var(--border-subtle)] px-5 py-4">
      <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-[var(--text-main)]">RSSHub Base URL</div>
          <div className="mt-1 text-sm text-[var(--text-muted)]">
            当前来源：{settings?.source ?? (isLoading ? '加载中' : '未知')}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <button
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isLoading}
            onClick={() => void loadSettings()}
            type="button"
          >
            <RefreshCw size={15} />
            重新加载
          </button>
          <button
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isLoading || isSaving}
            onClick={() => void clearOverride()}
            type="button"
          >
            <RotateCcw size={15} />
            清除覆盖值
          </button>
          <button
            className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isLoading || isSaving}
            onClick={() => void saveSettings()}
            type="button"
          >
            <Save size={15} />
            {isSaving ? '保存中...' : '保存'}
          </button>
        </div>
      </div>

      <label className="mt-4 block rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-3">
        <div className="mb-2 flex items-center justify-between gap-3">
          <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">Base URL</span>
          {settings ? <span className="text-xs text-[var(--text-muted)]">{settings.key}</span> : null}
        </div>
        <input
          className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)]"
          disabled={isLoading}
          placeholder="http://localhost:1200"
          value={rsshubBaseUrl}
          onChange={(event) => setRsshubBaseUrl(event.target.value)}
        />
        {settings ? (
          <div className="mt-2 break-all text-xs text-[var(--text-muted)]">
            生效值：{settings.value}（{settings.source}）
          </div>
        ) : null}
      </label>

      {message ? <div className="mt-3 text-sm text-emerald-600">{message}</div> : null}
      {error ? <div className="mt-3 text-sm text-red-600">{error}</div> : null}
    </section>
  );
}
