import {useEffect, useState} from 'react';
import {CheckCircle2, RefreshCw, Save, TriangleAlert} from 'lucide-react';
import {getStoragePaths, updateStoragePaths, validateStoragePaths} from '@/services/settings-service';
import type {StoragePath, StoragePaths} from '@/types/settings';

interface StorageFormState {
  knowledgeBaseRoot: string;
}

export function StorageSettingsPanel() {
  const [paths, setPaths] = useState<StoragePaths | null>(null);
  const [formState, setFormState] = useState<StorageFormState>({knowledgeBaseRoot: ''});
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isValidating, setIsValidating] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void loadStoragePaths();
  }, []);

  async function loadStoragePaths() {
    setIsLoading(true);
    setError(null);
    try {
      const nextPaths = await getStoragePaths();
      setPaths(nextPaths);
      setFormState({
        knowledgeBaseRoot: nextPaths.knowledgeBaseRoot.value,
      });
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : '加载存储路径失败。');
    } finally {
      setIsLoading(false);
    }
  }

  async function savePaths() {
    setIsSaving(true);
    setError(null);
    setMessage(null);
    try {
      const nextPaths = await updateStoragePaths(formState);
      setPaths(nextPaths);
      setFormState({
        knowledgeBaseRoot: nextPaths.knowledgeBaseRoot.value,
      });
      setMessage('存储路径已保存。');
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : '保存存储路径失败。');
    } finally {
      setIsSaving(false);
    }
  }

  async function validatePaths() {
    setIsValidating(true);
    setError(null);
    setMessage(null);
    try {
      const nextPaths = await validateStoragePaths(formState);
      setPaths(nextPaths);
      setMessage('路径校验已完成。');
    } catch (validateError) {
      setError(validateError instanceof Error ? validateError.message : '校验存储路径失败。');
    } finally {
      setIsValidating(false);
    }
  }

  return (
    <section className="panel border-b border-[var(--border-subtle)] px-5 py-4">
      <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-[var(--text-main)]">存储路径</div>
          <div className="mt-1 text-sm text-[var(--text-muted)]">KnowledgeBase 用于本地归档镜像。</div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <button
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] px-3 py-2 text-sm text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isLoading || isValidating}
            onClick={() => void validatePaths()}
            type="button"
          >
            <RefreshCw size={15} />
            {isValidating ? '校验中...' : '校验'}
          </button>
          <button
            className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white shadow-sm disabled:cursor-not-allowed disabled:opacity-50"
            disabled={isLoading || isSaving}
            onClick={() => void savePaths()}
            type="button"
          >
            <Save size={15} />
            {isSaving ? '保存中...' : '保存路径'}
          </button>
        </div>
      </div>

      <div className="mt-4 grid gap-3">
        <PathInput
          label="KnowledgeBase Root"
          path={paths?.knowledgeBaseRoot ?? null}
          value={formState.knowledgeBaseRoot}
          onChange={(value) => setFormState((current) => ({...current, knowledgeBaseRoot: value}))}
        />
      </div>

      {message ? <div className="mt-3 text-sm text-emerald-600">{message}</div> : null}
      {error ? <div className="mt-3 text-sm text-red-600">{error}</div> : null}
    </section>
  );
}

function PathInput({
  label,
  path,
  value,
  onChange,
}: {
  label: string;
  path: StoragePath | null;
  value: string;
  onChange: (value: string) => void;
}) {
  const isReady = Boolean(path?.isWritable && (!path.exists || path.isDir));
  return (
    <label className="block rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-panel)] px-3 py-3">
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="text-xs font-semibold uppercase text-[var(--text-soft)]">{label}</span>
        {path ? (
          <span className="inline-flex items-center gap-1 text-xs text-[var(--text-muted)]">
            {isReady ? <CheckCircle2 size={13} className="text-emerald-600" /> : <TriangleAlert size={13} className="text-amber-600" />}
            {path.source}
          </span>
        ) : null}
      </div>
      <input
        className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)]"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      {path ? (
        <div className="mt-2 space-y-1 text-xs text-[var(--text-muted)]">
          <div className="break-all">Resolved: {path.resolvedPath}</div>
          <div>
            exists: {path.exists ? 'yes' : 'no'} · writable: {path.isWritable ? 'yes' : 'no'} · project:{' '}
            {path.isInsideProject ? 'inside' : 'outside'}
          </div>
          {path.error ? <div className="text-red-600">{path.error}</div> : null}
        </div>
      ) : null}
    </label>
  );
}
