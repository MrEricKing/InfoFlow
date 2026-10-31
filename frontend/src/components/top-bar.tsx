import {useState, type ReactNode} from 'react';
import {LibraryBig, Loader2, Moon, PanelsTopLeft, Plus, Rss, Search, Settings, Sun} from 'lucide-react';
import type {ThemeMode, ViewMode} from '@/types/feed';

interface TopBarProps {
  searchQuery: string;
  searchPlaceholder: string;
  theme: ThemeMode;
  view: ViewMode;
  isPreviewingSource: boolean;
  onSearchQueryChange: (nextValue: string) => void;
  onSettingsOpen: () => void;
  onSourceQuickAdd: (url: string) => Promise<void>;
  onThemeToggle: () => void;
  onViewChange: (nextView: ViewMode) => void;
}

export function TopBar({
  searchQuery,
  searchPlaceholder,
  theme,
  view,
  isPreviewingSource,
  onSearchQueryChange,
  onSettingsOpen,
  onSourceQuickAdd,
  onThemeToggle,
  onViewChange,
}: TopBarProps) {
  const [isQuickAddOpen, setIsQuickAddOpen] = useState(false);
  const [quickAddUrl, setQuickAddUrl] = useState('');
  const [quickAddError, setQuickAddError] = useState<string | null>(null);

  async function submitQuickAdd() {
    const url = quickAddUrl.trim();
    if (!url) {
      setQuickAddError('请输入订阅 URL。');
      return;
    }

    setQuickAddError(null);
    try {
      await onSourceQuickAdd(url);
      setQuickAddUrl('');
      setIsQuickAddOpen(false);
    } catch (error) {
      setQuickAddError(error instanceof Error ? error.message : '订阅预览失败。');
    }
  }

  return (
    <header className="panel flex h-14 shrink-0 items-center justify-between border-b border-[var(--border-subtle)] px-4">
      <div className="flex items-center gap-6">
        <div className="flex items-center gap-2 text-lg font-semibold">
          <img className="h-7 w-7 rounded-md shadow-sm" src="/infoflow-icon.svg" alt="" />
          <div className="flex items-baseline gap-2">
            <span>InfoFlow</span>
            <span className="text-base font-normal text-[var(--text-muted)]">聚合器</span>
          </div>
        </div>

        <nav className="hidden items-center gap-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] p-1 md:flex">
          <NavButton active={view === 'ai'} icon={<PanelsTopLeft size={16} />} label="AI 首页" onClick={() => onViewChange('ai')} />
          <NavButton active={view === 'reader'} icon={<Rss size={16} />} label="阅读器" onClick={() => onViewChange('reader')} />
          <NavButton active={view === 'kb'} icon={<LibraryBig size={16} />} label="知识库" onClick={() => onViewChange('kb')} />
        </nav>
        <div className="relative hidden md:block">
          <button
            className="flex h-9 w-9 items-center justify-center rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)] disabled:cursor-not-allowed disabled:opacity-50"
            title="快速添加订阅"
            type="button"
            disabled={isPreviewingSource}
            onClick={() => {
              setIsQuickAddOpen((current) => !current);
              setQuickAddError(null);
            }}
          >
            {isPreviewingSource ? <Loader2 className="animate-spin" size={16} /> : <Plus size={17} />}
          </button>
          {isQuickAddOpen ? (
            <form
              className="absolute left-0 top-11 z-30 w-96 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-panel)] p-3 shadow-[0_20px_60px_rgba(15,23,42,0.18)]"
              onSubmit={(event) => {
                event.preventDefault();
                void submitQuickAdd();
              }}
            >
              <div className="flex gap-2">
                <input
                  className="min-w-0 flex-1 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] px-3 py-2 text-sm text-[var(--text-main)] outline-none placeholder:text-[var(--text-soft)] focus:border-[var(--accent)]"
                  placeholder="https://example.com/feed.xml 或 rsshub://..."
                  value={quickAddUrl}
                  onChange={(event) => setQuickAddUrl(event.target.value)}
                />
                <button
                  className="rounded-md bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50"
                  disabled={isPreviewingSource}
                  type="submit"
                >
                  预览
                </button>
              </div>
              {quickAddError ? <div className="mt-2 text-xs text-red-600">{quickAddError}</div> : null}
            </form>
          ) : null}
        </div>
      </div>

      <div className="flex items-center gap-3">
        <label className="relative hidden sm:block">
          <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[var(--text-soft)]" size={16} />
          <input
            className="h-9 w-72 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-panel-strong)] pl-9 pr-3 text-sm outline-none transition-colors placeholder:text-[var(--text-soft)] focus:border-[var(--accent)]"
            placeholder={searchPlaceholder}
            value={searchQuery}
            onChange={(event) => onSearchQueryChange(event.target.value)}
          />
        </label>

        <div className="flex items-center gap-1 border-l border-[var(--border-subtle)] pl-3">
          <button
            className="rounded-md border border-[var(--border-subtle)] p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]"
            title="切换主题"
            type="button"
            onClick={onThemeToggle}
          >
            {theme === 'light' ? <Moon size={18} /> : <Sun size={18} />}
          </button>
          <button
            className={`rounded-md p-2 transition-colors ${
              view === 'settings'
                ? 'bg-[var(--bg-panel-strong)] text-[var(--text-main)]'
                : 'text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]'
            }`}
            title="设置与 Sources 工作台"
            type="button"
            onClick={onSettingsOpen}
          >
            <Settings size={18} />
          </button>
        </div>
      </div>
    </header>
  );
}

function NavButton({
  active,
  icon,
  label,
  onClick,
}: {
  active: boolean;
  icon: ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      className={`flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
        active
          ? 'border border-[var(--border-subtle)] bg-[var(--bg-panel)] text-[var(--text-main)] shadow-sm'
          : 'text-[var(--text-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-main)]'
      }`}
      onClick={onClick}
      type="button"
    >
      {icon}
      <span>{label}</span>
    </button>
  );
}
