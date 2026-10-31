import {requestJson} from '@/api/client';

export interface RuntimeInfoDto {
  status: string;
  service: string;
  environment: string;
  version?: string;
  git_commit?: string | null;
  git_commit_short?: string | null;
  git_commit_display?: string | null;
  git_branch?: string | null;
  git_dirty?: boolean | null;
  git_dirty_file_count?: number | null;
  version_source?: string | null;
}

export interface RuntimeInfo {
  appVersion: string | null;
  gitCommit: string | null;
  gitCommitShort: string | null;
  gitCommitDisplay: string | null;
  gitBranch: string | null;
  gitDirty: boolean | null;
  gitDirtyFileCount: number | null;
  versionSource: string | null;
}

export async function getRuntimeInfo(): Promise<RuntimeInfo> {
  const payload = await requestJson<RuntimeInfoDto>('/health');
  return {
    appVersion: payload.version ?? null,
    gitCommit: payload.git_commit ?? null,
    gitCommitShort: payload.git_commit_short ?? null,
    gitCommitDisplay: payload.git_commit_display ?? payload.git_commit_short ?? null,
    gitBranch: payload.git_branch ?? null,
    gitDirty: payload.git_dirty ?? null,
    gitDirtyFileCount: payload.git_dirty_file_count ?? null,
    versionSource: payload.version_source ?? null,
  };
}
